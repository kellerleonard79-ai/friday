"""
dashboard/auth.py
One user, one token, one cookie.

WHY NOW, WHEN THE SERVER IS STILL ON LOOPBACK. Right now "only this machine
can reach it" IS the security model, and it is a good one. The moment the bind
address changes, that assumption is void everywhere at once — every route, the
stream, the card endpoints, the config endpoint that hands back the Telegram
bot token and the Gemini key in plaintext.

So auth lands FIRST, against loopback, where getting it wrong costs nothing.
The reverse order — open the port, then add auth — has a window in it, and the
window is exactly as long as it takes to notice.

WHAT IT IS NOT. No user table, no sessions, no login form, no password
hashing, no expiry. There is one user and there is one device at a time. A
shared secret in a cookie is the correct size of mechanism for that, and
everything else on the list would be machinery guarding a machine the user
already owns.

THREE WAYS TO PRESENT IT, one door:

    cookie   friday_auth   — the browser, after the first visit
    header   X-Friday-Token — the menubar, the tray, the .app supervisor
    query    ?token=…      — the first visit, which sets the cookie and
                             redirects to the bare path

The query form is what makes this usable from a phone: one link, opened once,
and the cookie carries every visit after. It redirects rather than rendering,
so the token does not stay in the address bar, in the history, or in whatever
the browser syncs.

THE TOKEN IS GENERATED, NEVER DEFAULTED. There is no fallback value and no
"auth disabled" switch. A dashboard with a blank token would be a dashboard
that is open the moment the bind changes, and a config key someone forgot is
the likeliest way for that to happen.

COMPARISON IS CONSTANT-TIME. secrets.compare_digest, because == on strings
returns early and the difference is measurable over a network. The bind is
loopback today; the code is not written for today.
"""

from __future__ import annotations

import logging
import secrets

logger = logging.getLogger("friday.dashboard.auth")

COOKIE = "friday_auth"
HEADER = "X-Friday-Token"
QUERY = "token"

# A year. The cookie is the login: a user who has to re-enter a token every
# week will paste it into something searchable instead.
COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# Paths reachable without a token. Deliberately almost empty — the favicon,
# because a browser fetches it before anything else and a 401 there makes the
# tab look broken while the user is trying to read the 401 page.
# The PWA icons are here for the same reason plus a sharper one: an install
# prompt fetches them from the OS's own installer context, which carries no
# cookie at all. A 401 there means no home-screen icon and no error message
# anywhere. None of these files contain anything.
#
# /sw.js joined this list after Safari was observed — in a real browser, not
# a claim from the spec — never sending the session cookie on the service
# worker's own registration fetch. A 401 there means no worker installs at
# all: no offline cache, ever, on that browser, with nothing in the console
# to explain why. Exempting the SCRIPT is safe because it exempts nothing
# else: the script contains no secret and no user data, and every route it
# fetches from inside its own cache logic (/api/schedule, /api/after-school,
# the shell files) still goes through this same middleware on ITS OWN
# request — an unauthenticated browser gets 401s from those exactly as
# before, so nothing protected becomes reachable that wasn't already.
_OPEN_PATHS = frozenset({
    "/static/favicon.png",
    "/static/manifest.webmanifest",
    "/static/icon-192.png",
    "/static/icon-512.png",
    "/static/icon-maskable-192.png",
    "/static/icon-maskable-512.png",
    "/sw.js",
})


def generate() -> str:
    """A new token. 32 bytes of urandom, url-safe so it survives being a
    query parameter and a hand-typed line."""
    return secrets.token_urlsafe(32)


def resolve(config: dict) -> tuple[str, bool]:
    """The configured token, generating one if there is none.

    Returns (token, was_generated). The caller persists and announces it — this
    module does not write files, because the atomic-save logic already exists
    in the server and two implementations of it is one too many.
    """
    token = str(((config or {}).get("dashboard") or {}).get("auth_token") or "").strip()
    if token:
        return token, False
    return generate(), True


def authorized(request, token: str) -> bool:
    """Cookie or header. NOT the query parameter — that is handled separately,
    because presenting a token in a URL has to result in a redirect, and a
    predicate cannot redirect."""
    if not token:
        # Refuse rather than fall open. A server with no token is misconfigured,
        # and the safe reading of "I do not know the password" is "no".
        return False
    for candidate in (request.cookies.get(COOKIE),
                      request.headers.get(HEADER)):
        if candidate and secrets.compare_digest(str(candidate), token):
            return True
    return False


def is_open_path(path: str) -> bool:
    return path in _OPEN_PATHS


def local_headers() -> dict:
    """The auth header for a client on this machine — the menubar, the tray,
    the .app supervisor.

    They read the same config file the server does, which is the whole reason
    a shared token works here: there is no credential to distribute, only a
    file both sides already have. Returns an empty dict if the config cannot
    be read, so a caller degrades to a 401 it can report rather than a crash
    at import.
    """
    try:
        import yaml
        import paths
        with open(paths.config_path(), "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        token = str((cfg.get("dashboard") or {}).get("auth_token") or "").strip()
        return {HEADER: token} if token else {}
    except Exception as e:
        logger.debug(f"could not read the local auth token: {e}")
        return {}


def local_url(base: str) -> str:
    """A browser-openable dashboard URL carrying the token, for the menubar's
    "Open Dashboard". One click has to work on a machine that has never had
    the cookie set."""
    headers = local_headers()
    token = headers.get(HEADER)
    return f"{base}/?{QUERY}={token}" if token else base


def announce(token: str, generated: bool, port: int) -> None:
    """Put the token where the user will find it exactly once.

    Logged at WARNING on generation so it survives the log level the daemon
    actually runs at. A token the user cannot find is a dashboard they cannot
    open.
    """
    if generated:
        logger.warning(
            "\n"
            "  ────────────────────────────────────────────────────────────\n"
            "  Dashboard access token generated. Open the dashboard once with:\n"
            f"    http://127.0.0.1:{port}/?token={token}\n"
            "  The cookie is set from that link and lasts a year.\n"
            "  Stored in friday_config.yaml under dashboard.auth_token.\n"
            "  ────────────────────────────────────────────────────────────"
        )
    else:
        logger.info("Dashboard auth token loaded from config.")
