# Android File Manager for macOS

A Python/PyQt6 dual-pane file manager for transferring files between Mac and MTP phones (Android, TCL flip phones, etc.) over USB.

> **No ADB, no developer mode, no USB debugging required.** Uses raw USB/MTP directly.

## Run

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
├── requirements.txt          # Python dependencies
└── .venv/                    # Virtual environment (not committed)
```

## Notes

- Tested with TCL flip phone (VID=0x1bbb, PID=0x0168)
- Per the PTP/MTP spec, `GetObjectHandles` uses `parent=0xFFFFFFFF` for "root level" and `parent=0` for "all objects on the device". The UI's storage-root sentinel (`0`) is mapped to `0xFFFFFFFF` in `mtp_client.py`, so listings work on spec-compliant devices generally.

## Troubleshooting

### Phone not detected

1. Check USB cable — use a **data cable**, not a charge-only cable
2. On the phone, confirm mode is **"File Transfer"** (not "Charging only")
3. Unplug, wait 5 seconds, replug — then click **Connect Phone**
4. If the phone shows **"Trust This Computer?"**, tap **Yes**
5. Close **Image Capture** or **Photos** if open — they can block USB access

### Transfer failures

1. Check available storage on the phone
2. For large files, ensure a stable USB connection
3. Disconnect and reconnect, then retry

### Interface claim error ("Cannot claim USB interface")

Run the app with `sudo` once to reset the USB state, then relaunch normally:

```bash
sudo .venv/bin/python android_file_manager.py
```

### Verbose protocol logging

Run with `--verbose` (or `-v`) to log every MTP operation and response code to the terminal — useful when a new phone misbehaves:

```bash
.venv/bin/python android_file_manager.py --verbose
```

## License

MIT
