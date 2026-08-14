"""
dashboard/tls_certs.py
Tailscale-issued HTTPS certs for the dashboard's tailnet bind.

The loopback bind stays plain HTTP always — sitting at the Mac must never be
forced onto TLS. Only the tailnet socket (the phone's access path) needs a
real cert, because service workers refuse to register over anything but
localhost or HTTPS; network-level privacy from Tailscale was never enough
for that specific requirement, only for keeping the port off school Wi-Fi.

`tailscale cert` is idempotent: called against a domain with a still-valid
cert it returns that cert unchanged, and only actually renews once the
~90-day cert is close to expiry. That is what lets renew_if_needed() run on
a plain daily schedule with no expiry math on this side — see its docstring.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import paths

logger = logging.getLogger(__name__)

# Same TCC/PATH-attribution trap as dashboard/server.py's tailnet-address
# lookup: under the LaunchAgent, a bare "tailscale" on PATH can resolve
# differently than it does in a Terminal probe. Try PATH first, then the
# two places the CLI actually lives on macOS.
_TAILSCALE_CANDIDATES = (
    "tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def run(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess | None:
    """Run a tailscale subcommand, trying each known binary location in
    turn. Returns None if no candidate works (not installed, not running,
    not logged in, or the subcommand itself failed)."""
    for candidate in _TAILSCALE_CANDIDATES:
        try:
            return subprocess.run(
                [candidate, *args], capture_output=True, text=True,
                timeout=timeout, check=True,
            )
        except Exception:
            continue
    return None


def domain() -> str | None:
    """This machine's MagicDNS tailnet name (e.g. host.tailXXXX.ts.net), or
    None if Tailscale isn't installed, isn't running, or MagicDNS is off."""
    result = run(["status", "--json"])
    if result is None:
        return None
    try:
        parsed = json.loads(result.stdout)
        name = ((parsed.get("Self") or {}).get("DNSName") or "").rstrip(".")
        return name or None
    except Exception as e:
        logger.info(f"Could not parse tailscale status --json: {e}")
        return None


def cert_paths(dom: str) -> tuple[Path, Path]:
    tls_dir = paths.tls_dir()
    return tls_dir / f"{dom}.crt", tls_dir / f"{dom}.key"


def _fetch(dom: str, cert_path: Path, key_path: Path) -> bool:
    result = run(
        ["cert", f"--cert-file={cert_path}", f"--key-file={key_path}", dom],
        timeout=30,
    )
    if result is None or not cert_path.exists() or not key_path.exists():
        return False
    # tailscale writes the key however the umask lands; a private key must
    # never be group/world-readable regardless of what created it.
    key_path.chmod(0o600)
    return True


def _cleanup_stray_home_copies(dom: str) -> None:
    """One-time cleanup for a cert/key pair fetched by hand into $HOME
    before this module existed (`tailscale cert` with no --cert-file/
    --key-file writes into the current directory). Canonical copies now
    live under paths.tls_dir(); a private key has no business sitting
    somewhere world-readable in the home directory."""
    for suffix in (".crt", ".key"):
        stray = Path.home() / f"{dom}{suffix}"
        if not stray.exists():
            continue
        try:
            stray.unlink()
            logger.info(f"Removed stray {stray} (superseded by {paths.tls_dir()})")
        except OSError as e:
            logger.warning(f"Could not remove stray {stray}: {e}")


def ensure_cert(dom: str) -> tuple[Path, Path] | None:
    """Cert files for `dom`, fetching them the first time. Returns None if
    Tailscale HTTPS certs aren't available (the account's HTTPS
    Certificates feature is off, Tailscale unreachable, etc) — the tailnet
    bind then stays HTTP-only, same as before this existed."""
    cert_path, key_path = cert_paths(dom)
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    if _fetch(dom, cert_path, key_path):
        logger.info(f"Fetched Tailscale HTTPS cert for {dom}")
        _cleanup_stray_home_copies(dom)
        return cert_path, key_path
    logger.info(
        f"No Tailscale HTTPS cert for {dom} — tailnet bind stays HTTP. "
        "Enable 'HTTPS Certificates' in the Tailscale admin console to fix."
    )
    return None


def renew_if_needed(dom: str) -> bool:
    """Re-run `tailscale cert`, which no-ops while the current cert still
    has plenty of validity left and actually renews once it's close to its
    ~90-day expiry. Returns True when the cert file's content changed —
    the daemon's only cue that the live SSL context is now stale, since a
    running asyncio SSL context has no reload path short of a restart."""
    cert_path, key_path = cert_paths(dom)
    before = cert_path.read_bytes() if cert_path.exists() else None
    if not _fetch(dom, cert_path, key_path):
        return False
    after = cert_path.read_bytes() if cert_path.exists() else None
    return before != after
