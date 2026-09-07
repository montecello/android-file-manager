#!/bin/bash
#
# Build "Android File Manager.app" so the app can be launched from the
# Applications folder instead of the terminal.
#
#   ./build_app.sh                 -> installs into /Applications
#   ./build_app.sh ~/Applications  -> installs somewhere else
#
# The bundle is a thin launcher: it runs this project in place, from
# .venv/bin/python. Editing the Python here takes effect on the next launch —
# no rebuild. Re-run this script only if you move the project folder or
# change the icon.

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-/Applications}"
APP="$DEST/Android File Manager.app"

PYTHON="$PROJECT/.venv/bin/python"
ENTRY="$PROJECT/android_file_manager.py"
ICON="$PROJECT/assets/icon.icns"

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
[ -x "$PYTHON" ] || { echo "error: no venv at $PYTHON — see the README's 'Setup from scratch'"; exit 1; }
[ -f "$ENTRY" ]  || { echo "error: $ENTRY not found"; exit 1; }
[ -d "$DEST" ]   || { echo "error: $DEST does not exist"; exit 1; }
[ -w "$DEST" ]   || { echo "error: $DEST is not writable — try: ./build_app.sh ~/Applications"; exit 1; }

if [ ! -f "$ICON" ]; then
    echo "icon missing, drawing it…"
    "$PYTHON" "$PROJECT/make_icon.py"
fi

# ---------------------------------------------------------------------------
# Bundle skeleton
# ---------------------------------------------------------------------------
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$ICON" "$APP/Contents/Resources/icon.icns"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# A bundle whose executable is a shell script carries no architecture of its
# own, and macOS then launches it under Rosetta. The universal python.org
# interpreter would follow suit and pick its x86_64 slice — which cannot load
# Homebrew's arm64-only libusb, so the phone never appears. Naming the
# architecture here keeps the whole chain native.
ARCH="$(uname -m)"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>                  <string>Android File Manager</string>
    <key>CFBundleDisplayName</key>           <string>Android File Manager</string>
    <key>CFBundleExecutable</key>            <string>AndroidFileManager</string>
    <key>CFBundleIdentifier</key>            <string>local.androidfilemanager</string>
    <key>CFBundleIconFile</key>              <string>icon.icns</string>
    <key>CFBundlePackageType</key>           <string>APPL</string>
    <key>CFBundleShortVersionString</key>    <string>1.0</string>
    <key>CFBundleVersion</key>               <string>1.0</string>
    <key>LSMinimumSystemVersion</key>        <string>11.0</string>
    <key>LSArchitecturePriority</key>        <array><string>$ARCH</string></array>
    <key>LSRequiresNativeExecution</key>     <true/>
    <key>NSHighResolutionCapable</key>       <true/>
    <key>NSPrincipalClass</key>              <string>NSApplication</string>
</dict>
</plist>
PLIST

# ---------------------------------------------------------------------------
# Launcher — project path is baked in at build time, since the bundle lives
# in /Applications and cannot find the project on its own.
# ---------------------------------------------------------------------------
cat > "$APP/Contents/MacOS/AndroidFileManager" <<LAUNCHER
#!/bin/bash
PROJECT="$PROJECT"
LAUNCHER

cat >> "$APP/Contents/MacOS/AndroidFileManager" <<'LAUNCHER'
PYTHON="$PROJECT/.venv/bin/python"
ENTRY="$PROJECT/android_file_manager.py"
LOG="$HOME/Library/Logs/AndroidFileManager.log"

mkdir -p "$(dirname "$LOG")"

alert() {   # alert <message>
    /usr/bin/osascript -e "display alert \"Android File Manager\" message \"$1\" as critical" >/dev/null 2>&1
}

# Two copies fight over the phone's USB interface, so focus the running one
# instead of starting a second. (README: "Usual culprits, in order" #1.)
# -i matters: the framework interpreter re-execs as ".../MacOS/Python", capital P.
RUNNING="$(/usr/bin/pgrep -if -U "$(id -u)" "python.*android_file_manager\.py" | head -1 || true)"
if [ -n "$RUNNING" ]; then
    /usr/bin/osascript -e "tell application \"System Events\" to set frontmost of (first process whose unix id is $RUNNING) to true" >/dev/null 2>&1 \
        || alert "Android File Manager is already running."
    exit 0
fi

if [ ! -x "$PYTHON" ] || [ ! -f "$ENTRY" ]; then
    alert "The project folder moved or was deleted.\n\nExpected it at:\n$PROJECT\n\nPut it back, or re-run build_app.sh from its new location."
    exit 1
fi

# Catch a broken environment here, where a dialog can still be shown — once we
# exec, failures only reach the log. Reports the interpreter's architecture on
# success, which is what the log header records.
if ! INFO="$("$PYTHON" -c '
import platform, PyQt6, usb.core, usb.backend.libusb1 as b
assert b.get_backend(), "libusb backend unavailable"
print(platform.machine())' 2>&1)"; then
    DETAIL="$(printf '%s' "$INFO" | tail -3 | tr '"' "'")"
    alert "The Python environment is incomplete.\n\n$DETAIL\n\nSee 'Setup from scratch' in the README. If libusb is the problem: brew install libusb"
    exit 1
fi

cd "$PROJECT"
{ echo; echo "=== $(date) — $INFO ==="; } >> "$LOG"
# exec keeps the PID macOS launched, so the Dock stays bound to this bundle.
exec "$PYTHON" "$ENTRY" >> "$LOG" 2>&1
LAUNCHER

chmod +x "$APP/Contents/MacOS/AndroidFileManager"

# Ad-hoc signature: without one, macOS 15+ re-verifies the bundle on every
# launch and can refuse it outright after the contents change.
/usr/bin/codesign --force --deep --sign - "$APP" >/dev/null 2>&1 \
    || echo "note: codesign unavailable — the bundle still runs"

touch "$APP"   # nudge Finder to pick up the new icon

echo "built $APP"
echo "project stays at $PROJECT — edits there take effect on the next launch"
