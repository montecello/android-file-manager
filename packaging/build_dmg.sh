#!/bin/bash
#
# Build a distributable "Android File Manager.dmg" that anyone can download,
# drag to Applications, and run — with no Python, no Homebrew, no terminal.
#
#   ./packaging/build_dmg.sh            # version from the VERSION file
#   ./packaging/build_dmg.sh 1.2.0      # or state it explicitly
#
# Output: dist/AndroidFileManager-<version>-<arch>.dmg
#
# The DMG is architecture-specific, because it carries a real Python
# interpreter and a real libusb. Build it on an Apple Silicon Mac for Apple
# Silicon users, on an Intel Mac for Intel users — or let the GitHub Actions
# workflow (.github/workflows/release.yml) build both for you.

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT"

VERSION="${1:-$(cat VERSION 2>/dev/null || echo 1.0.0)}"
ARCH="$(uname -m)"
APP="dist/Android File Manager.app"
DMG="dist/AndroidFileManager-${VERSION}-${ARCH}.dmg"
VENV="packaging/.buildvenv"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

[ "$(uname -s)" = "Darwin" ] || { echo "error: this builds a Mac app; run it on a Mac"; exit 1; }

# ---------------------------------------------------------------------------
# 1. libusb — the one piece pip cannot provide
# ---------------------------------------------------------------------------
if [ -z "${AFM_LIBUSB:-}" ]; then
    if command -v brew >/dev/null && brew --prefix libusb >/dev/null 2>&1; then
        AFM_LIBUSB="$(brew --prefix libusb)/lib/libusb-1.0.0.dylib"
    elif [ -f /opt/homebrew/lib/libusb-1.0.0.dylib ]; then
        AFM_LIBUSB=/opt/homebrew/lib/libusb-1.0.0.dylib
    elif [ -f /usr/local/lib/libusb-1.0.0.dylib ]; then
        AFM_LIBUSB=/usr/local/lib/libusb-1.0.0.dylib
    else
        echo "error: libusb not installed. Run:  brew install libusb"; exit 1
    fi
fi
export AFM_LIBUSB
say "libusb: $AFM_LIBUSB"
file "$AFM_LIBUSB" | grep -q "$ARCH" || echo "warning: libusb is not $ARCH — the built app may not run natively"

# ---------------------------------------------------------------------------
# 2. Clean build environment (kept out of the dev .venv on purpose)
# ---------------------------------------------------------------------------
say "Preparing build environment"
# Prefer the interpreter this project already runs on, so the packaged app
# matches what you have been testing. Falls back to whatever python3 is.
if [ -n "${AFM_PYTHON:-}" ]; then PYBIN="$AFM_PYTHON"
elif [ -x ".venv/bin/python" ]; then PYBIN=".venv/bin/python"
else PYBIN="python3"; fi
[ -x "$VENV/bin/python" ] || "$PYBIN" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade --quiet pip
"$VENV/bin/pip" install --quiet -r requirements.txt pyinstaller
"$VENV/bin/python" -c "import platform; print('build python:', platform.python_version(), platform.machine())"

# ---------------------------------------------------------------------------
# 3. Icon
# ---------------------------------------------------------------------------
if [ ! -f assets/icon.icns ]; then
    say "Drawing the icon"
    "$VENV/bin/python" make_icon.py
fi

# ---------------------------------------------------------------------------
# 4. Freeze
# ---------------------------------------------------------------------------
say "Building the app bundle (this takes a minute)"
rm -rf build dist
AFM_VERSION="$VERSION" "$VENV/bin/pyinstaller" --noconfirm --clean \
    --distpath dist --workpath build \
    packaging/AndroidFileManager.spec

[ -d "$APP" ] || { echo "error: PyInstaller did not produce $APP"; exit 1; }

# Sanity check: the bundled libusb has to actually be inside the app, or the
# phone will never appear on anyone else's machine.
if ! find "$APP" -name 'libusb-1.0*.dylib' | grep -q .; then
    echo "error: libusb is missing from the bundle"; exit 1
fi

# ---------------------------------------------------------------------------
# 5. Sign
# ---------------------------------------------------------------------------
# With a Developer ID in the keychain, set AFM_SIGN_ID to it and the app opens
# with no warning at all. Without one, an ad-hoc signature is still required:
# macOS refuses to launch an unsigned arm64 bundle outright.
SIGN_ID="${AFM_SIGN_ID:--}"
say "Signing ($([ "$SIGN_ID" = "-" ] && echo "ad-hoc" || echo "$SIGN_ID"))"
codesign --force --deep --timestamp=none --sign "$SIGN_ID" "$APP"
codesign --verify --deep --strict "$APP" && echo "signature verified"

# ---------------------------------------------------------------------------
# 6. DMG — app, an Applications alias to drag it onto, and the first-run note
# ---------------------------------------------------------------------------
say "Building $DMG"
STAGE="build/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp packaging/READ_ME_FIRST.txt "$STAGE/READ ME FIRST.txt"

rm -f "$DMG"
hdiutil create -quiet -volname "Android File Manager" \
    -srcfolder "$STAGE" -ov -format UDZO "$DMG"

SIZE="$(du -h "$DMG" | cut -f1)"
say "Done"
echo "  $PROJECT/$DMG  ($SIZE)"
echo
echo "Test it yourself before publishing:"
echo "  open \"$DMG\"     # drag the app across, then launch it from Applications"
echo
echo "Publish it:"
echo "  git tag v$VERSION && git push origin v$VERSION"
echo "  (GitHub Actions then builds both architectures and creates the release)"
