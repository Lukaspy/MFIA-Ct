"""Multi-channel LED source interface (NI PXI-7853R FPGA driver).

The optical source is the 8-channel LED AWG driven by an NI PXI-7853R
FPGA card — see the PXI-AWG repo's ``led_driver`` package. Its scripting
entry point, ``led_driver.LEDController``, addresses channels **by
wavelength** and drives them by **intensity percent** (0–100%), with
per-channel current limits and an optional power calibration applied
internally. The channel→wavelength wiring is Ch0=850 nm … Ch7=385 nm.

This module exposes:

- ``LedSource`` Protocol: the wavelength/percent interface the C-f
  experiment codes to.
- ``PxiLedSource``: production adapter wrapping ``LEDController``. The
  ``led_driver`` import is lazy (deferred to ``connect``) so this package
  doesn't hard-depend on it; with ``bitfile=None`` it runs the driver's
  own mock backend (no FPGA / no ``nifpga`` needed).
- ``MockLedSource``: lightweight in-memory fake that records the call
  sequence. Used by tests and the GUI's --mock path so they work even
  when ``led_driver`` isn't installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

# Canonical physical wiring of the LED source (Ch0=850 nm … Ch7=385 nm),
# mirroring led_driver.config.DEFAULT_WAVELENGTHS_NM. Used by the mock and
# as the GUI's channel list default.
DEFAULT_WAVELENGTHS_NM = [850.0, 740.0, 625.0, 590.0, 530.0, 505.0, 470.0, 385.0]


@runtime_checkable
class LedSource(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def idn(self) -> str: ...
    def wavelengths(self) -> list[float]:
        """Wavelengths (nm) of all addressable channels."""
        ...
    def channel_of(self, nm: float) -> int:
        """Channel index (0-based) mapped to a wavelength."""
        ...
    def set_intensity(self, nm: float, pct: float) -> None:
        """Set CW intensity (0–100 %) on the channel at ``nm`` and enable it."""
        ...
    def all_off(self) -> None:
        """Zero and disable every channel."""
        ...
    def current_ma_for(self, nm: float, pct: float) -> Optional[float]:
        """Best-effort actual drive current (mA) for a wavelength+percent,
        or None if the full-scale isn't known. Informational only.
        """
        ...
    def predicted_power_mw(self, nm: float, pct: float) -> Optional[float]:
        """Calibration-predicted delivered optical power (mW) for a
        wavelength+percent, or None if that channel isn't calibrated.
        """
        ...


class PxiLedSource:
    """Adapter over ``led_driver.LEDController`` (NI PXI-7853R).

    Parameters
    ----------
    bitfile : str or None
        Compiled ``.lvbitx`` FPGA bitfile. ``None`` runs the led_driver
        package's own MockBackend — useful to exercise this adapter
        without hardware.
    resource : str
        NI-RIO resource name (default ``"RIO0"``).
    use_cal : bool
        Apply the led_driver power calibration so equal ``pct`` across
        wavelengths yields equal optical power. Requires a recorded
        calibration; uncalibrated channels pass through linearly.
    """

    def __init__(
        self,
        bitfile: Optional[str] = None,
        resource: str = "RIO0",
        use_cal: bool = False,
    ) -> None:
        self.bitfile = bitfile
        self.resource = resource
        self.use_cal = use_cal
        self._ctl = None
        # Effective full-scale current per wavelength (full_scale × safety
        # current_scale), cached on connect for current_ma_for.
        self._effective_fs_ma: dict[float, float] = {}

    def connect(self) -> None:
        try:
            from led_driver import LEDController
        except ImportError as e:
            raise RuntimeError(
                "led_driver is not importable. Clone the PXI-AWG repo and "
                "put its directory on PYTHONPATH (or drop a .pth pointing at "
                "it into this venv's site-packages). Original error: " + str(e)
            ) from e

        self._ctl = LEDController(
            self.bitfile,
            self.resource,
            use_cal=self.use_cal,
            auto_connect=False,
        )
        if not self._ctl.connect():
            self._ctl = None
            raise RuntimeError(
                f"LEDController.connect() failed for resource {self.resource!r} "
                f"(bitfile={self.bitfile!r})."
            )
        # Cache effective full-scale (full_scale × current_scale) so the
        # reported current reflects the per-channel safety limit, not the
        # raw driver full-scale.
        self._effective_fs_ma = {}
        for ch in self._ctl.channels():
            wl = ch.get("wavelength_nm")
            if wl is not None:
                fs = float(ch.get("full_scale_ma", 0.0))
                scale = float(ch.get("current_scale", 1.0))
                self._effective_fs_ma[float(wl)] = fs * scale

    def disconnect(self) -> None:
        if self._ctl is not None:
            try:
                self._ctl.disconnect()
            finally:
                self._ctl = None

    def idn(self) -> str:
        mode = "mock" if self.bitfile is None else f"FPGA {self.resource}"
        return f"PXI-7853R 8-ch LED driver ({mode})"

    def wavelengths(self) -> list[float]:
        if self._ctl is None:
            return list(DEFAULT_WAVELENGTHS_NM)
        return [float(w) for w in self._ctl.wavelengths()]

    def channel_of(self, nm: float) -> int:
        if self._ctl is None:
            raise RuntimeError("PxiLedSource.connect() must be called first")
        return int(self._ctl.channel_of(nm))

    def set_intensity(self, nm: float, pct: float) -> None:
        if self._ctl is None:
            raise RuntimeError("PxiLedSource.connect() must be called first")
        self._ctl.set_intensity(nm=nm, pct=pct)

    def all_off(self) -> None:
        if self._ctl is not None:
            self._ctl.all_off()

    def current_ma_for(self, nm: float, pct: float) -> Optional[float]:
        fs = self._effective_fs_ma.get(float(nm))
        if fs is None:
            return None
        return (pct / 100.0) * fs

    def predicted_power_mw(self, nm: float, pct: float) -> Optional[float]:
        """Delivered optical power from the led_driver calibration.

        With ``use_cal`` on, the command percent is a target fraction of the
        equalized maximum, so delivered power = pct/100 × equalized_max.
        With it off, percent is the raw drive and delivered power is the
        channel's drive→power curve evaluated at it. None if uncalibrated.
        """
        if self._ctl is None:
            return None
        try:
            ch = self._ctl.channel_of(nm)
            cal = self._ctl.cal.channels[ch]
            if not cal.is_calibrated:
                return None
            if self._ctl.use_cal and self._ctl.cal.equalize:
                eq = self._ctl.cal.equalized_max_power
                if eq != float("inf"):
                    return (pct / 100.0) * eq
                return float(cal.drive_to_power(pct))
            if self._ctl.use_cal:
                return (pct / 100.0) * float(cal.max_power)
            return float(cal.drive_to_power(pct))
        except Exception:
            return None


@dataclass
class _Call:
    name: str
    kwargs: dict = field(default_factory=dict)


class MockLedSource:
    """Records the call sequence + tracks state, in the wavelength/percent
    model. No ``led_driver`` dependency, so tests run anywhere.

    ``active`` is ``{nm: pct}`` of the single channel last set (the driver's
    set_intensity enables one channel; dark/``all_off`` clears it).
    """

    def __init__(
        self,
        wavelengths: Optional[list[float]] = None,
        power_model_mw: Optional[dict[float, float]] = None,
    ) -> None:
        self._wavelengths = list(wavelengths or DEFAULT_WAVELENGTHS_NM)
        self.calls: list[_Call] = []
        self.active: dict[float, float] = {}
        self.connected = False
        # Optional {wavelength: max_power_mw}; predicted_power_mw scales it
        # linearly by drive %. None → uncalibrated (returns None), matching a
        # real driver with no calibration loaded.
        self._power_model_mw = power_model_mw
        # Mirror the led_driver defaults: 1000 mA full-scale on every channel
        # except 590 nm, whose driver full-scale is 750 mA but is software
        # current-limited to 700 mA (effective full-scale = 700 mA).
        self._effective_fs_ma = {wl: (700.0 if wl == 590.0 else 1000.0)
                                 for wl in self._wavelengths}

    def connect(self) -> None:
        self.calls.append(_Call("connect"))
        self.connected = True

    def disconnect(self) -> None:
        self.calls.append(_Call("disconnect"))
        self.connected = False

    def idn(self) -> str:
        return "MOCK,PXI-LED,0,0"

    def wavelengths(self) -> list[float]:
        return list(self._wavelengths)

    def channel_of(self, nm: float) -> int:
        try:
            return self._wavelengths.index(float(nm))
        except ValueError:
            raise KeyError(f"No channel at {nm} nm. Available: {self._wavelengths}")

    def set_intensity(self, nm: float, pct: float) -> None:
        if float(nm) not in self._wavelengths:
            raise KeyError(
                f"No channel at {nm} nm. Available: {self._wavelengths}"
            )
        self.calls.append(_Call("set_intensity", {"nm": float(nm), "pct": pct}))
        self.active = {float(nm): pct}

    def all_off(self) -> None:
        self.calls.append(_Call("all_off"))
        self.active = {}

    def current_ma_for(self, nm: float, pct: float) -> Optional[float]:
        fs = self._effective_fs_ma.get(float(nm))
        if fs is None:
            return None
        return (pct / 100.0) * fs

    def predicted_power_mw(self, nm: float, pct: float) -> Optional[float]:
        if self._power_model_mw is None:
            return None
        max_mw = self._power_model_mw.get(float(nm))
        if max_mw is None:
            return None
        return (pct / 100.0) * max_mw
