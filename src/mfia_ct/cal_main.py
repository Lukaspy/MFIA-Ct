"""CLI entry point for the automated LED power-calibration GUI.

Drives the LED source + reads the Thorlabs PM16 to build per-wavelength
power-calibration curves, persisted in the led_driver calibration format.

Usage:
    mfia-ledcal           # real LED (PXI) + real PM16
    mfia-ledcal --mock    # mock LED + synthetic meter (no hardware)
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mfia-ledcal", description=__doc__)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the mock LED + synthetic power meter (no hardware).",
    )
    args = parser.parse_args(argv)

    from .gui.cal_app import run

    return run(preselect_mock=args.mock)


if __name__ == "__main__":
    sys.exit(main())
