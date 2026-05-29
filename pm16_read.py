#!/usr/bin/env python3
"""Thorlabs PM16-121 power-meter readout.

Auto-discovers the meter over USBTMC (any device with Thorlabs VID 0x1313),
sets the wavelength correction, and streams power readings to stdout and
optionally to a CSV file.

Dependencies (one-time):
    pip install pyvisa pyvisa-py pyusb ThorlabsPM100

Linux udev (one-time, so you don't need sudo to talk to the meter):
    echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="1313", MODE="0666"' \\
        | sudo tee /etc/udev/rules.d/99-thorlabs.rules
    sudo udevadm control --reload && sudo udevadm trigger

Usage:
    ./pm16_read.py --list                       # show all VISA resources
    ./pm16_read.py                              # stream at default 532 nm
    ./pm16_read.py --wavelength 405 --interval 0.05
    ./pm16_read.py --wavelength 660 --csv run.csv
"""

from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from typing import Optional


THORLABS_VID = 0x1313


def find_pm(rm) -> Optional[str]:
    """Return the VISA resource string for the first Thorlabs USB device.

    USB resource strings come in two flavors depending on the backend:
        NI-VISA:    USB0::0x1313::0x807B::M00XXXXX::INSTR  (hex)
        pyvisa-py:  USB0::4883::32891::18061430::0::INSTR  (decimal)
    ``int(..., 0)`` accepts both, so we just compare integers.
    """
    for r in rm.list_resources():
        parts = r.split("::")
        if len(parts) < 3:
            continue
        try:
            vid = int(parts[1], 0)
        except ValueError:
            continue
        if vid == THORLABS_VID:
            return r
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="List all VISA resources and exit.")
    parser.add_argument("--resource", help="VISA resource string (auto-discover if omitted).")
    parser.add_argument(
        "--wavelength",
        type=float,
        default=532.0,
        help="Wavelength correction in nm (default: 532).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.1,
        help="Seconds between readings (default: 0.1).",
    )
    parser.add_argument("--csv", help="Append readings to this CSV file.")
    parser.add_argument(
        "--avg",
        type=int,
        default=1,
        help="Hardware average count (PM16 internal averaging, default: 1).",
    )
    args = parser.parse_args(argv)

    try:
        import pyvisa
    except ImportError:
        print("pyvisa not installed. Run: pip install pyvisa pyvisa-py pyusb ThorlabsPM100", file=sys.stderr)
        return 1

    rm = pyvisa.ResourceManager("@py")

    if args.list:
        resources = rm.list_resources()
        if not resources:
            print("No VISA resources found.")
        else:
            for r in resources:
                print(r)
        return 0

    resource = args.resource or find_pm(rm)
    if resource is None:
        print(
            "No Thorlabs PM-series device found.\n"
            "Try ./pm16_read.py --list to see all detected VISA resources,\n"
            "or check that the udev rule is in place (see header comment).",
            file=sys.stderr,
        )
        return 1

    try:
        from ThorlabsPM100 import ThorlabsPM100
    except ImportError:
        print("ThorlabsPM100 not installed. Run: pip install ThorlabsPM100", file=sys.stderr)
        return 1

    inst = rm.open_resource(resource)
    inst.timeout = 3000
    pm = ThorlabsPM100(inst=inst)

    idn = inst.query("*IDN?").strip()
    print(f"# Connected: {idn}")
    print(f"# Resource: {resource}")

    pm.sense.correction.wavelength = args.wavelength
    print(f"# Wavelength: {pm.sense.correction.wavelength:.1f} nm")

    # Average count and sensor IDN aren't implemented on every PM16 firmware
    # rev (some silently drop the query and the read times out). Both are
    # informational — skip with a note if unsupported.
    def _safe(label: str, fn):
        """Try an optional query; on timeout, clear the USBTMC buffer so the
        next query doesn't inherit the stuck state."""
        try:
            return fn()
        except Exception:
            try:
                inst.clear()
            except Exception:
                pass
            print(f"# {label}: (not reported by this firmware)")
            return None

    if args.avg != 1:
        _safe(
            "set average count",
            lambda: setattr(pm.sense.average, "count", args.avg),
        )
    avg = _safe("Average count", lambda: pm.sense.average.count)
    if avg is not None:
        print(f"# Average count: {avg}")
    sensor_idn = _safe("Sensor IDN", lambda: pm.system.sensor.idn)
    if sensor_idn is not None:
        print(f"# Sensor IDN: {sensor_idn}")
    print("# t_s\tpower_W")

    csv_file = None
    csv_writer = None
    if args.csv:
        csv_file = open(args.csv, "a", newline="")
        csv_writer = csv.writer(csv_file)
        # Header on empty file
        if csv_file.tell() == 0:
            csv_writer.writerow(["t_s", "power_W", "wavelength_nm"])

    # Make Ctrl-C exit the loop cleanly even if a query is in flight.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    t0 = time.monotonic()
    try:
        while True:
            t = time.monotonic() - t0
            p = pm.read
            print(f"{t:8.3f}\t{p: .6e}")
            if csv_writer:
                csv_writer.writerow([f"{t:.6f}", f"{p:.9e}", args.wavelength])
                csv_file.flush()
            time.sleep(max(0.0, args.interval))
    except KeyboardInterrupt:
        print("\n# Stopped.", file=sys.stderr)
    finally:
        if csv_file is not None:
            csv_file.close()
        try:
            inst.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
