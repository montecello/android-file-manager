# Android File Manager for macOS

A Python/PyQt6 dual-pane file manager for transferring files between Mac and MTP phones (Android, TCL flip phones, etc.) over USB.

> **No ADB, no developer mode, no USB debugging required.** Uses raw USB/MTP directly.

---

## Download (for everyone else)

Grab the latest DMG from the [Releases page](../../releases/latest):

| Your Mac | File |
|----------|------|
| Apple Silicon (M1–M4) | `AndroidFileManager-<version>-arm64.dmg` |
| Intel | `AndroidFileManager-<version>-x86_64.dmg` |

Open it, drag the app onto **Applications**, and launch it. Nothing else to
install — Python, PyQt6 and libusb all travel inside the app.

**The first launch is blocked.** This app is not signed with a paid Apple
developer certificate, so macOS asks for confirmation once: open **System
Settings → Privacy & Security**, scroll to the message naming Android File
Manager, and click **Open Anyway**. Every launch after that is a plain
double-click. (On macOS 14 and earlier, right-click → Open is enough.)

Then: plug in the phone, choose **File Transfer / MTP** on the phone, and
click **🔌 Connect Phone**.

### Is it safe?

The app has no network code at all — it only speaks USB to the phone in front
of it, and reads and writes files you pick. Nothing is uploaded anywhere,
there is no telemetry, and no account. Every line of it is in this repository,
and the DMGs on the Releases page are built from this source by GitHub
Actions, in public, on GitHub's own machines — the build log for each release
is there to read.

It is MIT licensed: use it, change it, ship your own version.

---

# Working on the code

Everything below is for developing this app, not for using it.

## Run

During development the app runs from this folder. `build_app.sh` makes a
thin `.app` launcher that points back here, so edits take effect on the next
launch with nothing to rebuild — it is *not* what other people download.
(That is `packaging/build_dmg.sh`, further down.)

Double-click **Android File Manager** in the Applications folder.

If it isn't there yet, build the launcher once:

```bash
cd /Users/powerfan/Desktop/Android
./build_app.sh                 # installs into /Applications
./build_app.sh ~/Applications  # or somewhere else
```

The bundle is a thin launcher — it runs this project in place, from
`.venv/bin/python`. Edits to the Python here take effect on the next launch,
so there is nothing to rebuild. Re-run `build_app.sh` only if you **move the
project folder** or change the icon.

### From the terminal

Still works, and is the way to pass flags such as `--verbose`:

```bash
cd /Users/powerfan/Desktop/Android
.venv/bin/python android_file_manager.py
```

> **Do not use `python3`** — it points to the system Python which lacks the required packages. Always use `.venv/bin/python`.

## Features

- Dual-pane browser — Mac filesystem on the left, phone on the right
- Navigate both MTP storages (internal + SD card)
- Upload and download files (streaming, handles large files)
- Rename, delete, new folder, cut/paste move on the phone
- Preview gallery for photos & videos on both panes (🖼 button, or double-click a media file) — thumbnails, arrow-key browsing, in-app video playback
- Create M3U playlists from a phone folder
- External USB drive shortcuts (💽 Volumes)
- Auto disconnect/reconnect detection (no restart needed)

## Tech Stack

- **Python 3** + **PyQt6** — GUI
- **pyusb** + **libusb** (Homebrew) — raw USB/MTP transport
- Pure-Python MTP protocol implementation in `mtp_client.py`

## Phone Setup

1. Connect phone via USB
2. On the phone, choose **"File Transfer" / MTP** mode when prompted
3. Click **Connect Phone** in the app

## Setup from scratch

```bash
cd /Users/powerfan/Desktop/Android
brew install libusb
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python android_file_manager.py
```

## Dependencies

| Package | How to install |
|---------|---------------|
| `PyQt6` | `pip install PyQt6` (included in `requirements.txt`) |
| `pyusb` | `pip install pyusb` (included in `requirements.txt`) |
| libusb  | `brew install libusb` (system library, not a Python package) |

## Files

| File | Purpose |
|------|---------|
| `android_file_manager.py` | Main UI (PyQt6) |
| `mtp_client.py` | MTP protocol over USB |
| `usb_doctor.py` | Connection diagnostic — run it when the phone won't connect |
| `build_app.sh` | Builds `Android File Manager.app` for the Applications folder |
| `make_icon.py` | Draws the app icon → `assets/icon.png` + `assets/icon.icns` |
| `requirements.txt` | Python dependencies |
| `.venv/` | Python virtual environment (not committed) |

## Usage

1. **Connect your phone** via USB
2. **On the phone** — choose **"File Transfer"** or **"MTP"** mode (not "Charging only")
3. **Click "🔌 Connect Phone"** in the app
4. **Navigate** by double-clicking folders
5. **Select files** with click (⌘-click for multi-select, ⇧-click for range)
6. **Transfer files** using the → Phone / ← Mac buttons, or drag files from Finder onto the phone pane
7. **Preview media** — click 🖼 (or double-click an image/video) to open the gallery; ← → keys navigate, Space plays/pauses video, Esc returns to the grid. Phone files download to a temporary cache (cleared when the app closes); videos are only fetched when you open them.
8. **Create folders** via right-click → New Folder
9. **Rename / delete** via right-click context menu

## Project Structure

```
Android/
├── android_file_manager.py   # PyQt6 GUI — dual-pane file manager
├── mtp_client.py             # Pure-Python MTP-over-USB protocol layer
├── usb_doctor.py             # Diagnostic: why won't the phone connect?
├── build_app.sh              # Builds the .app bundle for /Applications
├── make_icon.py              # Draws the app icon
├── assets/                   # icon.png, icon.icns
├── requirements.txt          # Python dependencies
└── .venv/                    # Virtual environment (not committed)
```

## Notes

- Tested with TCL flip phone (VID=0x1bbb, PID=0x0168)
- **App bundle** — launched from the bundle, output goes to
  `~/Library/Logs/AndroidFileManager.log` (there is no terminal to print to);
  check it there if a launch misbehaves. The launcher focuses an already-running
  copy instead of starting a second one, since two copies fight over the phone's
  USB interface — macOS asks for Automation permission the first time it does
  this; denying it just means you get a "already running" notice instead of the
  window coming forward. The menu bar still reads "Python" while the app runs: the venv's
  interpreter is a symlink into python.org's framework `Python.app`, so macOS
  binds the menu-bar name to *that* bundle. The Dock icon and the Applications
  folder icon are unaffected.
- Per the PTP/MTP spec, `GetObjectHandles` uses `parent=0xFFFFFFFF` for "root level" and `parent=0` for "all objects on the device". The UI's storage-root sentinel (`0`) is mapped to `0xFFFFFFFF` in `mtp_client.py`, so listings work on spec-compliant devices generally.

## Troubleshooting

### Phone not detected

1. Check USB cable — use a **data cable**, not a charge-only cable
2. On the phone, confirm mode is **"File Transfer"** (not "Charging only")
3. Unplug, wait 5 seconds, replug — then click **Connect Phone**
4. If the phone shows **"Trust This Computer?"**, tap **Yes**
5. Close **Image Capture** or **Photos** if open — they can block USB access
6. Run `.venv/bin/python usb_doctor.py` for a report on what's holding the device

### Transfer failures

1. Check available storage on the phone
2. For large files, ensure a stable USB connection
3. Disconnect and reconnect, then retry

### Interface claim error — "Cannot claim USB interface: [Errno 13] Access denied"

Despite the wording this is almost never a permissions problem, and `sudo` is
not the fix. An MTP phone presents a **class 6 (Still Image / PTP)** interface,
so macOS automatically launches `ptpcamerad` (and sometimes
`Image Capture Extension`) as soon as you plug the phone in. Those helpers open
the interface *exclusively*, libusb then can't have it, and reports
`LIBUSB_ERROR_ACCESS` → errno 13.

Quitting the helper is not enough on its own: **launchd relaunches
`ptpcamerad` within a fraction of a second**, and the replacement re-opens the
interface before we can take it. So the app escalates, gentlest first:

1. claim the interface normally
2. `pkill` the helpers, then retry the claim in a tight loop (~1.5 s) to win the
   gap before launchd's replacement opens the device
3. as root, capture the device away from the kernel driver (deterministic, so
   it is tried before the races)
4. run a **kill-storm**: a background thread kills the helper every 30 ms while
   the main thread hammers `claim_interface`, so one of many attempts lands in
   a gap. This is the only thing that works without root when SIP is engaged
5. boot the helper's launchd job out so nothing respawns — the label and domain
   are discovered by matching the running helper's PID against `launchctl list`,
   since the label differs between macOS versions
6. `libusb_reset_device` to force a re-enumeration, invalidating the other
   process's handle, then claim through the re-attach with the storm running

**On a Mac with SIP engaged (the default), step 5 is refused**
(`Boot-out failed: 150: Operation not permitted while System Integrity
Protection is engaged`). The helper then cannot be stopped at all, only
out-raced — so if the storm doesn't win, run the app as root:

```bash
sudo .venv/bin/python android_file_manager.py
```

Root is the only guaranteed route, because only root can take the device from
macOS's camera stack. Also check the phone's USB mode: an interface macOS reads
as a camera (`PTP` / "Transfer photos") is fought over far harder than
`File Transfer` / MTP. The error message names the interface string the phone
is advertising, so you can tell which you have.

If it still fails, the app asks IOKit who actually holds the interface
(`ioreg -r -l -c IOUSBHostDevice` → `IOUserClientCreator`, filtered to the
phone's own VID:PID) and names that process in the error, so you are never
guessing. The filter matters — without it the list includes every keyboard,
trackpad, webcam and hub on the machine.

Phones can also expose **more than one** MTP-capable interface (extra
configurations, or a vendor-specific interface whose `iInterface` string is
`MTP` alongside the class-6 one). The app enumerates all of them and tries each
in turn, so a phone whose first interface is held can still connect on another.

Step 3 is per-login and reversible: the app restores the agent when you quit,
and logging out and back in restores it regardless. To restore by hand:

```bash
.venv/bin/python usb_doctor.py --restore
```

While the agent is booted out, Image Capture and Photos won't auto-detect
cameras — that's the trade, and it lasts only while the app is running.

If it still fails, something else owns the interface:

```bash
cd ~/Desktop/Android
.venv/bin/python usb_doctor.py     # says exactly what is holding the phone
```

Usual culprits, in order:

1. A **second copy of this app** still running (each instance claims the phone)
2. **Android File Transfer**, **OpenMTP**, **MacDroid**, **Image Capture**, **Photos**
3. A stale claim after a crash — unplug, wait 5 seconds, replug, reselect **File Transfer**
4. Nothing else has it, but the claim still fails — then root really is needed:
   `sudo .venv/bin/python android_file_manager.py`

### Verbose protocol logging

Run with `--verbose` (or `-v`) to log every MTP operation and response code to the terminal — useful when a new phone misbehaves:

```bash
.venv/bin/python android_file_manager.py --verbose
```

## Putting it on GitHub (first time only)

This repo has commits but no remote yet. Create an empty public repository on
GitHub — no README, no license, no .gitignore, since this folder has them —
then, from this folder:

```bash
git add -A
git commit -m "Add self-contained Mac build and release pipeline"
git remote add origin https://github.com/<your-username>/android-file-manager.git
git push -u origin main
```

Nothing else needs configuring. `GITHUB_TOKEN` is provided to the workflow
automatically, so the release pipeline works on the first tag.

## Releasing a new version

The whole release is one tag push. GitHub Actions builds on a real Apple
Silicon Mac and a real Intel Mac, and attaches both DMGs to the release.

```bash
# 1. bump the version
echo 1.1.0 > VERSION
git add -A && git commit -m "Release 1.1.0"

# 2. tag and push
git tag v1.1.0
git push origin main --tags
```

Watch it build in the repo's **Actions** tab; the release appears under
**Releases** a few minutes later, DMGs attached.

### Building a DMG locally

Useful for testing the packaged app before tagging:

```bash
brew install libusb          # once
./packaging/build_dmg.sh     # -> dist/AndroidFileManager-<version>-<arch>.dmg
```

This builds only for the Mac you run it on. `packaging/build_dmg.sh` uses its
own throwaway virtualenv (`packaging/.buildvenv`) and never touches `.venv`.

| Packaging file | Purpose |
|----------------|---------|
| `VERSION` | Single source of the version number |
| `packaging/build_dmg.sh` | One command: freeze, sign, and package the DMG |
| `packaging/AndroidFileManager.spec` | PyInstaller recipe — what goes in the bundle |
| `packaging/READ_ME_FIRST.txt` | Install instructions shown inside the DMG |
| `bundle_support.py` | Points the frozen app at its own bundled libusb |
| `.github/workflows/release.yml` | Builds both architectures and publishes the release |

### Signing, if you ever get a Developer ID

An Apple Developer account ($99/yr) removes the "Open Anyway" step entirely.
With the certificate in your keychain:

```bash
AFM_SIGN_ID="Developer ID Application: Your Name (TEAMID)" ./packaging/build_dmg.sh
xcrun notarytool submit dist/*.dmg --apple-id you@example.com \
      --team-id TEAMID --password APP_SPECIFIC_PASSWORD --wait
xcrun stapler staple dist/*.dmg
```

## License

MIT
