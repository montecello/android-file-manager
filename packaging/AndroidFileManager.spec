# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the distributable "Android File Manager.app".

Unlike build_app.sh (a thin launcher that runs this project in place, for
the developer's own machine), this produces a self-contained bundle: the
Python interpreter, PyQt6, pyusb and libusb all live inside the .app, so a
user who downloads it needs nothing preinstalled.

Driven by packaging/build_dmg.sh — run that, not this.
"""
import os
import shutil
import subprocess
from pathlib import Path

PROJECT = Path(SPECPATH).resolve().parent
VERSION = os.environ.get("AFM_VERSION", "1.0.0")


def locate_libusb() -> Path:
    """The libusb dylib to copy into the bundle."""
    override = os.environ.get("AFM_LIBUSB", "")
    if override and Path(override).is_file():
        return Path(override)

    candidates = [
        Path("/opt/homebrew/lib/libusb-1.0.0.dylib"),   # Apple Silicon
        Path("/usr/local/lib/libusb-1.0.0.dylib"),      # Intel
    ]
    brew = shutil.which("brew")
    if brew:
        try:
            prefix = subprocess.check_output(
                [brew, "--prefix", "libusb"], text=True, stderr=subprocess.DEVNULL
            ).strip()
            if prefix:
                candidates.insert(0, Path(prefix) / "lib" / "libusb-1.0.0.dylib")
        except subprocess.CalledProcessError:
            pass

    for c in candidates:
        if c.is_file():
            return c
    raise SystemExit(
        "libusb not found. Install it first:  brew install libusb\n"
        "(or set AFM_LIBUSB to the dylib's full path)"
    )


LIBUSB = locate_libusb()
print(f"[spec] bundling libusb from {LIBUSB}")

a = Analysis(
    [str(PROJECT / "android_file_manager.py")],
    pathex=[str(PROJECT)],
    binaries=[(str(LIBUSB), ".")],
    datas=[
        (str(PROJECT / "assets" / "icon.png"), "assets"),
        (str(PROJECT / "usb_doctor.py"), "."),
    ],
    hiddenimports=[
        "usb.backend.libusb1",
        "bundle_support",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebEngineQuick",
        "PyQt6.QtQuick",
        "PyQt6.QtQuick3D",
        "PyQt6.QtQml",
        "PyQt6.QtBluetooth",
        "PyQt6.QtNfc",
        "PyQt6.QtPositioning",
        "PyQt6.QtSensors",
        "PyQt6.QtSerialPort",
        "PyQt6.QtDesigner",
        "PyQt6.QtTest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AndroidFileManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,         # the app takes no file arguments; leaving this on
                                  # can stall a windowed bundle at launch
    target_arch=None,             # native arch of the build machine
    codesign_identity=None,       # ad-hoc signing happens in build_dmg.sh
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AndroidFileManager",
)

app = BUNDLE(
    coll,
    name="Android File Manager.app",
    icon=str(PROJECT / "assets" / "icon.icns"),
    bundle_identifier="io.github.androidfilemanager",
    version=VERSION,
    info_plist={
        "CFBundleName": "Android File Manager",
        "CFBundleDisplayName": "Android File Manager",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHumanReadableCopyright": "MIT licensed. Open source.",
        # The app talks to the phone over raw USB and, on macOS, temporarily
        # stops the system's own PTP/camera helpers so they stop fighting it
        # for the interface. Both are user-visible reasons worth declaring.
        "NSAppleEventsUsageDescription":
            "Used to bring an already-running copy of the app to the front.",
    },
)
