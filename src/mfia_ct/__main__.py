"""CLI entry point.

Usage:
    mfia-ct --mock                       # synthetic backend, no MFIA needed
    mfia-ct --device DEV3000             # real MFIA via local LabOne data server
    mfia-ct --device DEV3000 --host 1.2.3.4 --port 8004
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mfia-ct", description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--mock", action="store_true", help="Run with synthetic backend.")
    src.add_argument("--device", help="MFIA device id, e.g. DEV3000.")
    parser.add_argument("--host", default="localhost", help="LabOne data server host.")
    parser.add_argument("--port", type=int, default=8004, help="LabOne data server port.")
    args = parser.parse_args(argv)

    if args.mock:
        from .mock_hardware import MockMFIA

        backend = MockMFIA()
    else:
        from .hardware import MFIA

        backend = MFIA(args.device, server_host=args.host, server_port=args.port)
        backend.connect()

    try:
        from .gui.app import run

        return run(backend)
    finally:
        if hasattr(backend, "disconnect"):
            backend.disconnect()


if __name__ == "__main__":
    sys.exit(main())
