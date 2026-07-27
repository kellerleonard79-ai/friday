#!/bin/bash
# Build Friday.app and wrap it in a distributable .dmg.
#
#   packaging/macos/build.sh                 # ad-hoc signed (free, no account)
#   SIGN_ID="Developer ID Application: …" packaging/macos/build.sh
#
# With SIGN_ID set the app is Developer ID signed and ready to notarize; see
# BUILD_MACOS.md for the notarytool step and for what Sequoia does to a build
# that skips it.
#
# Output: dist/Friday-<version>.dmg
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$REPO_ROOT"

# Exported, not just set: friday.spec reads it out of the environment so the
# bundle's CFBundleVersion matches the .dmg filename.
export VERSION="${VERSION:-1.0.0}"
APP="dist/Friday.app"
DMG="dist/Friday-${VERSION}.dmg"
STAGE="dist/dmg-stage"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This build only runs on macOS." >&2
    exit 1
fi

PY="${PYTHON:-python3}"

echo "==> Checking build dependencies"
"$PY" -c "import PyInstaller" 2>/dev/null || {
    echo "PyInstaller is missing. Install it with: $PY -m pip install pyinstaller" >&2
    exit 1
}
"$PY" -c "import PIL" 2>/dev/null || {
    echo "Pillow is missing (needed for the icon). Install: $PY -m pip install Pillow" >&2
    exit 1
}

echo "==> Building the voice launcher (universal)"
# Rebuilt every time so the shipped binary always matches the .c source. It
# resolves its paths at runtime — nothing machine-specific is compiled in.
clang -std=c11 -Wall -Wextra -O2 -arch arm64 -arch x86_64 \
    -mmacosx-version-min=11.0 \
    -o FridayVoice.app/Contents/MacOS/FridayVoice FridayVoice_launcher.c
codesign --force --sign - --identifier com.friday.voice FridayVoice.app

echo "==> Generating friday.icns"
"$PY" "$HERE/make_icon.py"

echo "==> Running PyInstaller"
rm -rf build "$APP" "$STAGE" "$DMG"
"$PY" -m PyInstaller "$HERE/friday.spec" --noconfirm --distpath dist --workpath build

if [[ ! -d "$APP" ]]; then
    echo "PyInstaller did not produce $APP" >&2
    exit 1
fi

echo "==> Signing"
if [[ -n "${SIGN_ID:-}" ]]; then
    # --deep is deprecated but still the only way to reach the nested
    # FridayVoice.app in one pass; the inner bundle is signed first so the
    # outer seal covers it.
    codesign --force --sign "$SIGN_ID" --timestamp --options runtime \
        --identifier com.friday.voice \
        "$APP/Contents/Resources/FridayVoice.app" 2>/dev/null || true
    codesign --force --deep --sign "$SIGN_ID" --timestamp --options runtime \
        --identifier com.friday.app "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
else
    echo "    No SIGN_ID — ad-hoc signing. The .dmg will be quarantined on"
    echo "    download; see BUILD_MACOS.md for what users have to do on Sequoia."
    codesign --force --deep --sign - --identifier com.friday.app "$APP"
fi

echo "==> Staging the .dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

# A plain-text note next to the app, because a quarantined ad-hoc build fails
# with a dialog that says nothing useful about how to proceed.
if [[ -z "${SIGN_ID:-}" ]]; then
    cat > "$STAGE/READ ME FIRST.txt" <<'NOTE'
Installing Friday
=================

1. Drag Friday.app onto the Applications folder in this window.
2. Open Friday from Applications. macOS will refuse the first time and say the
   app "is damaged" or "cannot be opened because Apple cannot check it".
   That is expected: this build is not notarized.
3. Open System Settings > Privacy & Security, scroll to the bottom, and click
   "Open Anyway" next to the message about Friday. Confirm.

   On macOS 15 (Sequoia) and later, Control-clicking the app and choosing Open
   no longer works — System Settings is the only route.

4. Friday opens its setup wizard and walks you through creating each account
   and key it needs. Nothing has to be prepared in advance.

Friday lives in your menu bar (the orange F). There is no Dock icon and no
window until you open the dashboard from that menu.
NOTE
fi

echo "==> Creating $DMG"
hdiutil create -volname "Friday" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

if [[ -n "${SIGN_ID:-}" ]]; then
    codesign --force --sign "$SIGN_ID" "$DMG"
fi

echo
echo "Built $DMG"
if [[ -z "${SIGN_ID:-}" ]]; then
    echo "Unnotarized: users must approve it in System Settings > Privacy & Security."
else
    echo "Next: notarize and staple — see BUILD_MACOS.md."
fi
