"""
Runtime helpers that let this project work both from a source checkout and
from inside the frozen ``Android File Manager.app`` that
``packaging/build_dmg.sh`` produces.

There is exactly one thing a frozen bundle cannot work out for itself:
pyusb locates libusb through ``ctypes.util.find_library('usb-1.0')``, which
only ever looks at system library paths. A downloaded .app carries its own
copy of libusb inside the bundle, where that search will never reach — so
this module points ``find_library`` at it.

IMPORT THIS BEFORE ``usb.core``. Importing it later has no effect, because
pyusb resolves and caches its backend on first use.
"""
from __future__ import annotations

import ctypes.util
import sys
from pathlib import Path

# Names pyusb may ask for when hunting the libusb-1.0 shared library.
_LIBUSB_ALIASES = ("usb-1.0", "libusb-1.0", "usb")

_DYLIB_NAMES = ("libusb-1.0.0.dylib", "libusb-1.0.dylib")


def is_frozen() -> bool:
    """True when running from a PyInstaller-built .app bundle."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """
    Directory holding bundled data files (the icon, and anything added
    to ``datas`` in the .spec). Works in both frozen and source layouts.
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _search_roots() -> list[Path]:
    roots: list[Path] = []
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        exe_dir = Path(sys.executable).resolve().parent
        if meipass:
            roots += [Path(meipass), Path(meipass) / "Frameworks"]
        # PyInstaller 6 puts binaries in Contents/Frameworks; older ones
        # next to the executable in Contents/MacOS.
        roots += [exe_dir, exe_dir.parent / "Frameworks", exe_dir.parent / "Resources"]
    # Source checkout: an optional vendored copy, then the usual Homebrew
    # prefixes (Apple Silicon, then Intel) as a last resort.
    roots += [
        Path(__file__).resolve().parent / "vendor",
        Path("/opt/homebrew/lib"),
        Path("/usr/local/lib"),
    ]
    return roots


def find_bundled_libusb() -> str | None:
    """Absolute path to a usable libusb dylib, or None if none is bundled."""
    for root in _search_roots():
        for name in _DYLIB_NAMES:
            candidate = root / name
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


_installed = False


def install_libusb_finder() -> str | None:
    """
    Teach ``ctypes.util.find_library`` about the bundled libusb.

    Idempotent, and a no-op when nothing is bundled — in a normal checkout
    the system search already works, so we leave it alone.
    """
    global _installed
    if _installed:
        return find_bundled_libusb()

    lib = find_bundled_libusb()
    if not lib:
        return None

    original = ctypes.util.find_library

    def find_library(name):
        if name in _LIBUSB_ALIASES:
            return lib
        return original(name)

    ctypes.util.find_library = find_library
    _installed = True
    return lib


# Applied at import time, which is why the import must come first.
BUNDLED_LIBUSB = install_libusb_finder()
