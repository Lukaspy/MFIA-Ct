"""CLI entry point for the C-f (impedance spectroscopy) campaign GUI.

Instrument selection is done inside the GUI; all flags here are just
pre-fills for the "Instrument" panel — identical convention to ``mfia-ct``.

Usage:
    mfia-cf                              # blank GUI; pick instrument inside
    mfia-cf --mock                       # pre-select mock backend
    mfia-cf --device dev32369            # pre-fill MFIA device id
    mfia-cf --device dev32369 --host 1.2.3.4 --port 8004
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mfia-cf", description=__doc__)
    parser.add_argument("--mock", action="store_true", help="Pre-select mock backend.")
    parser.add_argument("--device", help="Pre-fill MFIA device id, e.g. dev32369.")
    parser.add_argument("--host", default=None, help="Pre-fill LabOne data server host.")
    parser.add_argument("--port", type=int, default=None, help="Pre-fill LabOne port.")
    args = parser.parse_args(argv)

    from .gui.cf_app import run

    return run(
        preselect_mock=args.mock,
        preselect_device=args.device,
        preselect_host=args.host,
        preselect_port=args.port,
    )


if __name__ == "__main__":
    sys.exit(main())
