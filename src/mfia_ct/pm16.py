"""Thorlabs PM16-121 power-meter abstraction for automated LED calibration.

A thin, reusable wrapper around the same USBTMC/ThorlabsPM100 stack used by
the standalone ``pm16_read.py`` / ``pm16_gui.py`` scripts, plus a mock that
synthesizes power from a connected MockLedSource so the calibration routine
is testable without hardware.

The meter is the optical-power sensor for the LED power calibration
(``led_calibration``): set the wavelength correction to each channel's λ,
then read averaged power at each drive level.
"""

from __future__ import annotations

import time
from typing import Optional, Protocol

THORLABS_VID = 0x1313


class PowerMeter(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def idn(self) -> str: ...
    def set_wavelength(self, nm: float) -> None: ...
    def read_power_w(self) -> float:
        """Single instantaneous power reading, in watts."""
        ...

    def read_power_mw(self, averages: int = 1, settle_s: float = 0.0) -> float:
        """Averaged power in milliwatts (default impl averages read_power_w)."""
        ...


def _find_thorlabs(rm) -> Optional[str]:
    """First Thorlabs USB VISA resource (hex or decimal VID forms)."""
    for r in rm.list_resources():
        parts = r.split("::")
        if len(parts) < 3:
            continue
        try:
            if int(parts[1], 0) == THORLABS_VID:
                return r
        except ValueError:
            continue
    return None


class ThorlabsPM16:
    """Real PM16-121 over USBTMC via pyvisa-py + ThorlabsPM100."""

    def __init__(self, resource: Optional[str] = None) -> None:
        self.resource = resource  # None → auto-discover on connect
        self._inst = None
        self._rm = None
        self._pm = None

    def connect(self) -> None:
        import pyvisa
        from ThorlabsPM100 import ThorlabsPM100

        self._rm = pyvisa.ResourceManager("@py")
        resource = self.resource or _find_thorlabs(self._rm)
        if not resource:
            raise RuntimeError(
                "No Thorlabs PM-series USB device found. Plug it in or pass an "
                "explicit VISA resource string."
            )
        self.resource = resource
        self._inst = self._rm.open_resource(resource)
        self._inst.timeout = 3000
        self._pm = ThorlabsPM100(inst=self._inst)

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
        self._inst = self._rm = self._pm = None

    def idn(self) -> str:
        if self._inst is None:
            return "Thorlabs PM16 (disconnected)"
        return str(self._inst.query("*IDN?")).strip()

    def set_wavelength(self, nm: float) -> None:
        if self._pm is None:
            raise RuntimeError("ThorlabsPM16.connect() must be called first")
        self._pm.sense.correction.wavelength = float(nm)

    def read_power_w(self) -> float:
        if self._pm is None:
            raise RuntimeError("ThorlabsPM16.connect() must be called first")
        return float(self._pm.read)

    def read_power_mw(self, averages: int = 1, settle_s: float = 0.0) -> float:
        if settle_s > 0:
            time.sleep(settle_s)
        n = max(1, int(averages))
        total = 0.0
        for _ in range(n):
            total += self.read_power_w()
        return (total / n) * 1e3


class MockPowerMeter:
    """Synthesizes power from a connected MockLedSource's active channel.

    The model is a mild saturating nonlinearity per wavelength with a
    wavelength-dependent maximum power, so a calibration sweep produces a
    monotonic, slightly-curved response (enough to exercise the inverse
    interpolation and the cross-channel equalization).
    """

    # Per-wavelength max optical power at 100% drive (mW), arbitrary but
    # distinct so equalization has something to equalize.
    _MAX_MW = {
        385.0: 6.0,
        470.0: 9.0,
        505.0: 8.0,
        530.0: 7.5,
        590.0: 5.0,
        625.0: 10.0,
        740.0: 12.0,
        850.0: 11.0,
    }

    def __init__(self, led=None, gamma: float = 1.15) -> None:
        # ``led`` is a MockLedSource; the meter "sees" its active channel.
        self.led = led
        self.gamma = gamma
        self._wavelength = 532.0
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def idn(self) -> str:
        return "MOCK,PM16,0,0"

    def set_wavelength(self, nm: float) -> None:
        self._wavelength = float(nm)

    def read_power_w(self) -> float:
        return self.read_power_mw(averages=1) * 1e-3

    def read_power_mw(self, averages: int = 1, settle_s: float = 0.0) -> float:
        # Sum the contribution of whatever the mock LED currently has on.
        if self.led is None:
            return 0.0
        total = 0.0
        for nm, pct in getattr(self.led, "active", {}).items():
            max_mw = self._MAX_MW.get(float(nm), 8.0)
            total += max_mw * (max(0.0, pct) / 100.0) ** self.gamma
        return total
