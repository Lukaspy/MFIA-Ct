"""Single-threaded continuous photo-C-t orchestrator.

The zhinst ziDAQServer Python client is not safe under concurrent access
from multiple OS threads — calling ``daq.set`` from one thread while a
``daq.poll`` is in flight on another reliably segfaults. So pulse pacing
and IA-sample polling share one loop here: each iteration checks the
pulse schedule, toggles Aux Out at the right moment, then polls for new
samples. Poll length is shortened on the fly when the next pulse edge is
closer than the configured ``poll_interval_s`` — that bounds pulse jitter
to ~1 ms regardless of how lazily the user wants the GUI to update.
"""

from __future__ import annotations

import threading
import time
from typing import Iterator, Protocol

from .acquisition import StreamChunk
from .config import CtConfig, PulseSource


class _Backend(Protocol):
    device: str

    def configure_impedance(self, cfg: CtConfig) -> None: ...
    def set_aux_out(self, channel: int, value_v: float) -> None: ...
    def start_continuous(
        self,
        *,
        external_sync: bool = False,
        sync_aux_in_channel: int = 0,
        sync_threshold_v: float = 1.0,
    ) -> None: ...
    def stop_continuous(self) -> None: ...
    def poll_continuous(self, length_s: float) -> StreamChunk | None: ...


class _FunctionGenerator(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def reset(self) -> None: ...
    def configure_pulse_burst(
        self,
        *,
        freq_hz: float,
        width_s: float,
        n_cycles: int,
        high_v: float,
        low_v: float,
        load_ohms: str,
    ) -> None: ...
    def trigger(self) -> None: ...
    def disarm(self) -> None: ...


_MIN_POLL_S = 0.001


class _Actuator:
    """Turns the optical pulse on/off for the software-paced internal loop.

    Abstracts what "light on/off" means so the same pacing/timestamping/
    drain logic drives either an MFIA Aux Out square wave or an 8-channel
    LED-driver channel.
    """

    def setup(self) -> None: ...
    def light_on(self) -> None: ...
    def light_off(self) -> None: ...
    def teardown(self) -> None: ...


class _AuxOutActuator(_Actuator):
    def __init__(self, backend, channel: int, high_v: float, low_v: float) -> None:
        self._backend = backend
        self._ch = channel
        self._high = high_v
        self._low = low_v

    def setup(self) -> None:
        self._backend.set_aux_out(self._ch, self._low)

    def light_on(self) -> None:
        self._backend.set_aux_out(self._ch, self._high)

    def light_off(self) -> None:
        self._backend.set_aux_out(self._ch, self._low)

    def teardown(self) -> None:
        try:
            self._backend.set_aux_out(self._ch, self._low)
        except Exception:
            pass


class _LedActuator(_Actuator):
    def __init__(self, led, wavelength_nm: float, intensity_pct: float) -> None:
        self._led = led
        self._nm = wavelength_nm
        self._pct = intensity_pct
        self._connected = False

    def setup(self) -> None:
        self._led.connect()
        self._connected = True
        self._led.all_off()

    def light_on(self) -> None:
        self._led.set_intensity(self._nm, self._pct)

    def light_off(self) -> None:
        self._led.all_off()

    def teardown(self) -> None:
        try:
            self._led.all_off()
        except Exception:
            pass
        if self._connected:
            try:
                self._led.disconnect()
            except Exception:
                pass


class CtExperiment:
    def __init__(
        self,
        backend: _Backend,
        cfg: CtConfig,
        fg: _FunctionGenerator | None = None,
        led=None,
    ) -> None:
        self.backend = backend
        self.cfg = cfg
        self.fg = fg
        self.led = led
        self._stop = threading.Event()
        self._pulse_times: list[float] = []

    @property
    def pulse_times(self) -> list[float]:
        return list(self._pulse_times)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> Iterator[StreamChunk]:
        self.backend.configure_impedance(self.cfg)
        source = self.cfg.pulse.source
        external = source == PulseSource.EXTERNAL
        self.backend.start_continuous(
            external_sync=external,
            sync_aux_in_channel=self.cfg.pulse.sync_aux_in_channel,
            sync_threshold_v=self.cfg.pulse.sync_threshold_v,
        )
        if external:
            yield from self._run_external()
        elif source == PulseSource.LED_8CH:
            if self.led is None:
                raise RuntimeError(
                    "LED-driver pulse source selected but no LED source was "
                    "attached."
                )
            actuator = _LedActuator(
                self.led,
                self.cfg.pulse.led_wavelength_nm,
                self.cfg.pulse.led_intensity_pct,
            )
            yield from self._run_internal(actuator)
        else:
            actuator = _AuxOutActuator(
                self.backend,
                self.cfg.pulse.aux_out_channel,
                self.cfg.pulse.high_v,
                self.cfg.pulse.low_v,
            )
            yield from self._run_internal(actuator)

    def _run_internal(self, actuator: _Actuator) -> Iterator[StreamChunk]:
        t_start = time.monotonic()

        period = self.cfg.pulse.period_s
        width = self.cfg.pulse.pulse_width_s
        n = self.cfg.pulse.n_pulses
        poll_interval = self.cfg.acq.poll_interval_s

        next_pulse_i = 0
        pulse_low_at: float | None = None
        drain_until: float | None = None
        drain_seconds = max(period, 1.0)

        try:
            actuator.setup()

            while not self._stop.is_set():
                now = time.monotonic() - t_start

                # Rising edge of pulse N when due.
                if next_pulse_i < n:
                    rise_at = next_pulse_i * period
                    if now >= rise_at:
                        actuator.light_on()
                        t_pulse = time.monotonic() - t_start
                        self._pulse_times.append(t_pulse)
                        if hasattr(self.backend, "record_pulse"):
                            self.backend.record_pulse(t_pulse)
                        pulse_low_at = t_pulse + width
                        next_pulse_i += 1

                # Falling edge when the high period ends.
                if pulse_low_at is not None:
                    if (time.monotonic() - t_start) >= pulse_low_at:
                        actuator.light_off()
                        pulse_low_at = None

                # Shorten the poll if the next scheduled edge lands sooner
                # than the configured poll interval — bounds pulse jitter.
                now = time.monotonic() - t_start
                next_edge = float("inf")
                if next_pulse_i < n:
                    next_edge = min(next_edge, next_pulse_i * period)
                if pulse_low_at is not None:
                    next_edge = min(next_edge, pulse_low_at)
                poll_for = poll_interval
                if next_edge != float("inf"):
                    poll_for = min(poll_for, max(_MIN_POLL_S, next_edge - now))

                chunk = self.backend.poll_continuous(poll_for)
                if chunk is not None:
                    yield chunk

                # After all pulses have fired and dropped low, run a tail to
                # capture the last off-transient, then stop.
                if next_pulse_i >= n and pulse_low_at is None:
                    if drain_until is None:
                        drain_until = time.monotonic() + drain_seconds
                    elif time.monotonic() >= drain_until:
                        break
        finally:
            self.backend.stop_continuous()
            actuator.teardown()

    def _run_external(self) -> Iterator[StreamChunk]:
        """Polling-only loop. Pulse edges come from the backend's Aux In
        threshold detector in each StreamChunk; we just collect them.

        If a function generator is attached and ``cfg.fg.enabled`` is true,
        configure and trigger it here — by this point the MFIA is already
        subscribed (``start_continuous`` was called in ``run``), so the first
        rising edge won't be missed.
        """
        n = self.cfg.pulse.n_pulses
        poll_interval = self.cfg.acq.poll_interval_s
        drain_seconds = max(self.cfg.pulse.period_s, 1.0)
        drain_until: float | None = None

        fg_active = self.fg is not None and self.cfg.fg.enabled
        fg_connected = False
        fg_armed = False
        try:
            if fg_active:
                self.fg.connect()
                fg_connected = True
                self.fg.reset()
                self.fg.configure_pulse_burst(
                    freq_hz=1.0 / self.cfg.pulse.period_s,
                    width_s=self.cfg.pulse.pulse_width_s,
                    n_cycles=self.cfg.pulse.n_pulses,
                    high_v=self.cfg.fg.high_v,
                    low_v=self.cfg.fg.low_v,
                    load_ohms=self.cfg.fg.load_ohms,
                )
                fg_armed = True
                self.fg.trigger()

            while not self._stop.is_set():
                chunk = self.backend.poll_continuous(poll_interval)
                if chunk is not None:
                    if chunk.pulse_edges_s:
                        self._pulse_times.extend(chunk.pulse_edges_s)
                    yield chunk

                if len(self._pulse_times) >= n:
                    if drain_until is None:
                        drain_until = time.monotonic() + drain_seconds
                    elif time.monotonic() >= drain_until:
                        break
        finally:
            self.backend.stop_continuous()
            if fg_armed and self.fg is not None:
                try:
                    self.fg.disarm()
                except Exception:
                    pass
            if fg_connected and self.fg is not None:
                try:
                    self.fg.disconnect()
                except Exception:
                    pass
