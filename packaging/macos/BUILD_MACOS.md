# Building the macOS `.dmg`

Produces `dist/Friday-<version>.dmg` containing `Friday.app` and an
`Applications` symlink to drag it onto.

```bash
python3 -m pip install pyinstaller Pillow
packaging/macos/build.sh
```

That is the whole build. It compiles the voice launcher, generates the icon,
runs PyInstaller, signs, and assembles the disk image.

---

## What ends up in the app

| Path | What |
|---|---|
| `Friday.app/Contents/MacOS/Friday` | `mac_app.py` — menu bar + core supervisor |
| `Friday.app/Contents/Resources/FridayVoice.app` | the voice satellite's TCC wrapper |
| `Friday.app/Contents/Resources/` | `AGENTS.md`, `quips.yaml`, `dashboard/static` |

Three invocations of the same binary:

```
Friday.app             menu bar, supervises the core, runs the wizard on first launch
Friday.app --core      the agent (friday.main()); spawned by the supervisor
Friday.app --setup     the setup wizard alone; the menu's "Run Setup Wizard" re-execs this
```

Mutable state never lives inside the bundle — `paths.data_dir()` puts it in
`~/.friday` for any frozen build, so `/Applications` stays read-only and the
app survives being replaced by a new `.dmg`.

---

## Architectures

`build.sh` builds for whichever architecture the building Mac runs, because
PyInstaller can only emit a universal2 app when *every* wheel in the
environment is universal2 — and several of Friday's dependencies ship
arch-specific wheels only. An Apple Silicon build runs on Intel Macs only
under Rosetta, which is not installed by default on Sequoia.

If you need one `.dmg` for both, build on Apple Silicon in an x86_64
environment (`arch -x86_64 python3 -m pip install …` into a separate venv, then
`arch -x86_64 packaging/macos/build.sh`) and ship two images.

`build.sh` compiles the voice launcher universal (it is plain C with no
dependencies), but PyInstaller thins the copy it bundles down to the app's
own architecture. The standalone `FridayVoice.app` at the repo root stays
universal for source checkouts.

---

## Signing and Gatekeeper on Sequoia

**This is the part that decides whether your users can open the app at all.**

### Ad-hoc (default, free)

With no `SIGN_ID`, `build.sh` ad-hoc signs. The app runs fine when built
locally, but once the `.dmg` has been *downloaded* it carries the
`com.apple.quarantine` attribute, and Gatekeeper refuses it with "Friday is
damaged and can't be opened" or "cannot be opened because Apple cannot check
it for malicious software".

On macOS 15 (Sequoia) Apple removed the old Control-click → Open escape hatch.
The only route is:

> System Settings → Privacy & Security → scroll to the bottom → **Open Anyway**
> next to the message about Friday → confirm.

`build.sh` drops a `READ ME FIRST.txt` into unsigned images spelling this out,
because the Gatekeeper dialog itself does not.

Someone comfortable in a terminal can instead strip the attribute:

```bash
xattr -dr com.apple.quarantine /Applications/Friday.app
```

### Developer ID + notarization (what you want for distribution)

Requires a paid Apple Developer account ($99/yr). With a Developer ID
Application certificate in your keychain:

```bash
SIGN_ID="Developer ID Application: Your Name (TEAMID)" packaging/macos/build.sh

xcrun notarytool submit dist/Friday-1.0.0.dmg \
    --apple-id you@example.com --team-id TEAMID \
    --password "app-specific-password" --wait

xcrun stapler staple dist/Friday-1.0.0.dmg
```

A stapled `.dmg` opens on a first-time click with no warning and no System
Settings detour. Notarization takes a few minutes and the submission has to
succeed *before* stapling; `notarytool log <submission-id>` explains rejections.

`build.sh` already signs with `--options runtime` (the hardened runtime), which
notarization requires. The entitlements the hardened runtime needs are none
beyond the defaults — Friday does not JIT, load unsigned plugins, or disable
library validation.

---

## Permissions the app will ask for

None of these can be granted ahead of time by the installer; macOS prompts on
first use and the user must accept.

| Prompt | Triggered by | Declared in |
|---|---|---|
| Calendar access | first Apple Calendar read/write | `NSCalendarsUsageDescription` + `NSCalendarsFullAccessUsageDescription` |
| Automation / Apple Events | the JXA calendar bridge | `NSAppleEventsUsageDescription` |
| Microphone | voice, if enabled | `NSMicrophoneUsageDescription` |
| Location | first "where am I" question | `NSLocationWhenInUseUsageDescription` |

**Grant Calendar access in full, not write-only.** Briefings read the calendar
through EventKit, which is effectively instant. Denying it — or granting only
the write-only variant macOS 14 offers when
`NSCalendarsFullAccessUsageDescription` is missing — silently demotes reads to
the JXA fallback, which costs roughly 35 ms per event *in the calendar being
scanned*. On a shared calendar with a few thousand events one briefing takes
minutes and times out, and the user sees "nothing scheduled" on a day that is
full. `friday.log` says which reader is in use on the first read.

**Microphone is granted per executable binary, not per app.** `Friday.app` and
the nested `FridayVoice.app` are separate binaries and need separate grants —
that separation is the entire reason `FridayVoice.app` exists. See the TCC
section in the root `README.md`.

Re-signing an app resets its TCC grants, so a user updating to a new build will
be prompted again. That is expected and unavoidable.

---

## Start at login

The `.dmg` app supervises its own core process, so it does not need launchd to
stay up — but it does need something to start it after a reboot. Either add
Friday under System Settings → General → Login Items, or install the
LaunchAgents:

```bash
python3 -c "import macos_setup; macos_setup.install_agents(voice=True)"
```

`macos_setup` generates the plists against the paths on the machine running it
and validates each one before installing. Nothing is hand-edited, and nothing
in the repo hardcodes a home directory or a Python version.

---

## Troubleshooting the build

| Symptom | Cause |
|---|---|
| `PyInstaller did not produce dist/Friday.app` | Check `build/Friday/warn-Friday.txt` for the import that failed |
| App launches, no menu bar icon | `rumps`/`AppKit` missing from the build — check they are installed and not in the spec's `excludes` |
| `ModuleNotFoundError` at runtime | Add the module to `hiddenimports` in `friday.spec`; PyInstaller misses dynamic imports |
| Voice never starts | `~/.friday/voice_launcher.conf` missing or stale — `mac_app` rewrites it on every launch; check `~/.friday/logs/` |
| Calendar operations silently return nothing | Automation permission was denied; System Settings → Privacy & Security → Automation |
| Briefings say "nothing scheduled" on a busy day, `friday.log` shows `Apple Calendar read timed out` | EventKit access is missing or write-only, so reads fell back to JXA and ran out of time. System Settings → Privacy & Security → Calendars → give Friday **Full Access** |
