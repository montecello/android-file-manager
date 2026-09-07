#!/usr/bin/env python3
"""
usb_doctor.py — figure out why the phone will not connect.

    cd ~/Desktop/Android
    .venv/bin/python usb_doctor.py

Prints what is on the USB bus, who else on the Mac is holding the phone's
MTP interface, and whether we can claim it. Read-only apart from optionally
quitting macOS's PTP helper daemons (which macOS restarts on demand).
"""
import os
import sys
import subprocess

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pyusb is missing. Run:  .venv/bin/pip install -r requirements.txt\n"
             "(and make sure you are using .venv/bin/python, not python3)")

import mtp_client
from mtp_client import (
    _mtp_interfaces,
    IS_MACOS, is_access_error, _process_is_running, _comm_name,
    macos_ptp_holders_running, free_macos_ptp_holders,
    macos_usb_interface_clients, launchd_labels_for_ptp, launchd_agent_loaded,
    LAUNCHD_PTP_AGENTS, bootout_macos_ptp_agents, restore_macos_ptp_agents,
    MTPDevice,
)

OK, BAD, WARN = "  ✅", "  ❌", "  ⚠️ "


def hr(title):
    print(f"\n{title}\n" + "─" * 58)


def libusb_version():
    try:
        import usb.backend.libusb1 as l1
        b = l1.get_backend()
        if b is None:
            return "libusb backend NOT FOUND — run: brew install libusb"
        path = getattr(getattr(b, 'lib', None), '_name', None)
        return f"libusb backend: {path or 'loaded'}"
    except Exception as e:
        return f"libusb backend unknown ({e})"


def other_apps_running():
    """MTP apps that will fight us for the interface."""
    names = ["Android File Transfer", "OpenMTP", "Image Capture", "Photos",
             "MacDroid", "Commander One"]
    return [n for n in names if _process_is_running(n)]


def our_other_instances():
    """PIDs of other running copies of the app (a python process running it)."""
    mine = {os.getpid(), os.getppid()}
    found = []
    try:
        r = subprocess.run(["ps", "-axo", "pid=,command="],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return found
    for line in r.stdout.splitlines():
        line = line.strip()
        pid, _, cmd = line.partition(' ')
        if not pid.isdigit() or int(pid) in mine:
            continue
        if 'android_file_manager.py' in cmd and 'python' in cmd.split()[0].lower():
            found.append(pid)
    return found


def pids_of(name):
    try:
        r = subprocess.run(["pgrep", "-x", _comm_name(name)],
                           capture_output=True, text=True, timeout=3)
        return sorted(p for p in r.stdout.split() if p.isdigit())
    except Exception:
        return []


def respawn_test():
    """
    Kill ptpcamerad and watch whether launchd brings it straight back.

    This is the difference between "quitting the helper fixes it" and "we have
    to take launchd out of the loop", so it is worth measuring rather than
    guessing.
    """
    import time
    before = pids_of("ptpcamerad")
    if not before:
        print(OK, "ptpcamerad is not running right now")
        return
    print(f"   ptpcamerad pid(s) before: {', '.join(before)}")
    free_macos_ptp_holders()
    for delay in (0.1, 0.5, 1.5, 3.0):
        time.sleep(delay if delay == 0.1 else delay - 0.1)
        now = pids_of("ptpcamerad")
        state = "gone" if not now else ("SAME pid" if now == before
                                        else f"respawned as {', '.join(now)}")
        print(f"   after {delay:>4.1f}s: {state}")
        if now and now != before:
            print(WARN, "launchd respawns it immediately — the app boots the")
            print("       launchd agent out to get a stable window.")
            return
    if not pids_of("ptpcamerad"):
        print(OK, "ptpcamerad stayed down after being quit")


def main():
    if "--restore" in sys.argv:
        restored = restore_macos_ptp_agents(list(LAUNCHD_PTP_AGENTS))
        print("Restored: " + (", ".join(restored) if restored
                              else "nothing was booted out"))
        return 0

    print("USB / MTP diagnostic")
    hr("Environment")
    print(f"   python     : {sys.executable}")
    print(f"   platform   : {sys.platform}")
    print(f"   uid        : {os.getuid()}" + ("  (root)" if os.getuid() == 0 else ""))
    print(f"   {libusb_version()}")
    if ".venv" not in sys.executable:
        print(WARN, "Not running from .venv — use .venv/bin/python")

    hr("Other apps that grab MTP phones")
    dupes = our_other_instances()
    if dupes:
        print(BAD, f"another copy of this app is running (pid {', '.join(dupes)})")
        print("       Quit it — two instances cannot share the phone.")
    else:
        print(OK, "no second copy of this app")
    others = other_apps_running()
    if others:
        print(BAD, "running: " + ", ".join(others))
        print("       Quit these, they hold the phone's USB interface.")
    else:
        print(OK, "no competing MTP apps")

    if IS_MACOS:
        # Note: per-phone IOKit holders are printed with each device below.
        # A machine-wide list is useless — every keyboard, trackpad, webcam
        # and hub has an open interface too.
        hr("macOS PTP helpers")
        holders = macos_ptp_holders_running()
        if holders:
            print(WARN, "running: " + ", ".join(holders))
            print("       These are the usual cause of 'Errno 13 Access denied'.")
        else:
            print(OK, "none running")

        print("   launchd jobs that own them:")
        for label in launchd_labels_for_ptp():
            state = "loaded" if launchd_agent_loaded(label) else "not loaded / not visible"
            print(f"     {label}: {state}")
        try:
            sip = subprocess.run(["csrutil", "status"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            print(f"   {sip or 'SIP status unknown'}")
        except Exception:
            pass
        booted = bootout_macos_ptp_agents()
        if booted:
            print(OK, "booted out: " + ", ".join(booted))
        elif mtp_client.sip_blocks_bootout:
            print(WARN, "System Integrity Protection forbids disabling these jobs.")
            print("       They cannot be stopped — the app out-races them instead,")
            print("       and root is the only guaranteed way in.")
        else:
            print(BAD, "could not boot any of them out"
                  + (f" — {mtp_client.last_bootout_error}"
                     if mtp_client.last_bootout_error else ""))
        respawn_test()

    hr("MTP devices on the bus")
    devs = []
    for d in usb.core.find(find_all=True):
        ifaces = _mtp_interfaces(d)
        if ifaces:
            devs.append((d, ifaces))

    if not devs:
        print(BAD, "no MTP interface found on any USB device.")
        print("       • Use a data cable, not charge-only")
        print("       • On the phone choose 'File Transfer' / 'MTP', not 'Charging'")
        print("       • Try another port, then replug")
        return 1

    rc = 0
    for d, ifaces in devs:
        vid, pid = d.idVendor, d.idProduct
        try:
            name = usb.util.get_string(d, d.iProduct) or ""
        except Exception:
            name = ""
        print(f"\n   {name or 'device'}  {vid:04x}:{pid:04x}")
        for f in ifaces:
            print(f"     interface {f['intf_num']} (config {f['config']}, {f['kind']}) "
                  f"in=0x{f['ep_in']:02x} out=0x{f['ep_out']:02x}")

        if IS_MACOS:
            holders = [c for c in macos_usb_interface_clients(vid, pid)
                       if f"(pid {os.getpid()})" not in c]
            print("     holders: " + (", ".join(holders) if holders else "none"))

        # Bare claim of each candidate first, so the raw error is visible
        # before anything is killed or booted out.
        free = False
        for f in ifaces:
            try:
                usb.util.claim_interface(d, f['intf_num'])
                print(OK, f"interface {f['intf_num']} claimed straight away — "
                          "nothing is holding it")
                usb.util.release_interface(d, f['intf_num'])
                free = True
                break
            except Exception as e:
                print(WARN, f"interface {f['intf_num']} plain claim failed: {e}")
                if not is_access_error(e):
                    print("       (not a contention error — check cable and USB mode)")
        try:
            usb.util.dispose_resources(d)
        except Exception:
            pass
        if free:
            continue

        # Now run the app's real escalation ladder and report what it did.
        dev = MTPDevice()
        ok, msg = dev.connect(vendor_id=vid, product_id=pid)
        if dev.last_holders_freed:
            print(f"       quit: {', '.join(dev.last_holders_freed)}")
        if dev.last_agents_booted_out:
            print(f"       booted out: {', '.join(dev.last_agents_booted_out)}")
        if ok:
            print(OK, "connected with the app's normal recovery path")
            dev.disconnect()
        else:
            print(BAD, msg)
            rc = 1
    restore_macos_ptp_agents()

    hr("Result")
    print("   All good — start the app: .venv/bin/python android_file_manager.py"
          if rc == 0 else
          "   Fix the ❌ items above, then re-run this script.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
