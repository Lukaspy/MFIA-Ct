"""Agilent 33250A function generator driver over GPIB (NI-VISA).

Used in external-sync mode to pace the optical pulse train: the 33250A
runs a triggered pulse burst, its Output BNC drives the LED driver, and
its Sync BNC goes to the MFIA's Aux In so each rising edge is timestamped
at demod-rate precision.

The 33250A's SCPI dialect is the same as most Agilent/Keysight benchtop
function generators; ``OUTP:LOAD`` and burst-mode quirks are the only
spots where this code is model-specific.
"""

from __future__ import annotations

from typing import Any


class Agilent33250A:
    def __init__(self, resource: str, timeout_ms: int = 5000) -> None:
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._inst: Any = None
        self._rm: Any = None

    def connect(self) -> None:
        """Open the VISA session. NI-VISA must be installed on the system."""
        import pyvisa

        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self.resource)
        self._inst.timeout = self.timeout_ms
        self._inst.write_termination = "\n"
        self._inst.read_termination = "\n"

    def disconnect(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
        self._inst = None
        self._rm = None

    def __enter__(self) -> "Agilent33250A":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.disconnect()

    def idn(self) -> str:
        return str(self._inst.query("*IDN?")).strip()

    def reset(self) -> None:
        self._inst.write("*RST")
        self._inst.write("*CLS")
        self._inst.query("*OPC?")  # block until reset completes

    def configure_pulse_burst(
        self,
        *,
        freq_hz: float,
        width_s: float,
        n_cycles: int,
        high_v: float,
        low_v: float,
        load_ohms: str = "INF",
    ) -> None:
        """Set up a triggered pulse burst with N cycles and software trigger.

        After this call the output is enabled but idle; call ``trigger()`` to
        fire the burst.
        """
        if load_ohms not in ("INF", "50"):
            raise ValueError(f"load_ohms must be 'INF' or '50', got {load_ohms!r}")
        if width_s >= 1.0 / freq_hz:
            raise ValueError(
                f"pulse width ({width_s} s) must be less than period "
                f"({1.0 / freq_hz} s)"
            )

        cmds = [
            "FUNC PULS",
            f"FREQ {freq_hz:.9g}",
            f"PULS:WIDT {width_s:.9g}",
            # Set high/low after function so the 33250A re-coerces amplitude
            # against the high-Z / 50 Ω load it thinks it's driving.
            f"OUTP:LOAD {load_ohms}",
            f"VOLT:HIGH {high_v:.6g}",
            f"VOLT:LOW {low_v:.6g}",
            "BURS:MODE TRIG",
            f"BURS:NCYC {int(n_cycles)}",
            "TRIG:SOUR BUS",
            "BURS:STAT ON",
            "OUTP ON",
        ]
        for c in cmds:
            self._inst.write(c)
        self._inst.query("*OPC?")
        self._raise_on_error()

    def trigger(self) -> None:
        """Fire the burst (equivalent to pressing Trigger on the front panel)."""
        self._inst.write("*TRG")

    def disarm(self) -> None:
        """Turn the output off so the LED can't be re-fired by stray triggers."""
        if self._inst is None:
            return
        try:
            self._inst.write("OUTP OFF")
            self._inst.write("BURS:STAT OFF")
        except Exception:
            pass

    def _raise_on_error(self) -> None:
        """Drain the SCPI error queue; raise on the first non-zero entry."""
        for _ in range(16):
            resp = str(self._inst.query("SYST:ERR?")).strip()
            if resp.startswith("+0") or resp.startswith("0,"):
                return
            raise RuntimeError(f"33250A error: {resp}")
