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
from .config import CtConfig


class _Backend(Protocol):
    device: str

    def configure_impedance(self, cfg: CtConfig) -> None: ...
    def set_aux_out(self, channel: int, value_v: float) -> None: ...
    def start_continuous(self) -> None: ...
    def stop_continuous(self) -> None: ...
    def poll_continuous(self, length_s: float) -> StreamChunk | None: ...


_MIN_POLL_S = 0.001


class CtExperiment:
    def __init__(self, backend: _Backend, cfg: CtConfig) -> None:
        self.backend = backend
        self.cfg = cfg
        self._stop = threading.Event()
        self._pulse_times: list[float] = []

    @property
    def pulse_times(self) -> list[float]:
        return list(self._pulse_times)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> Iterator[StreamChunk]:
        self.backend.configure_impedance(self.cfg)
        self.backend.start_continuous()
        t_start = time.monotonic()

        ch = self.cfg.pulse.aux_out_channel
        high_v = self.cfg.pulse.high_v
        low_v = self.cfg.pulse.low_v
        period = self.cfg.pulse.period_s
        width = self.cfg.pulse.pulse_width_s
        n = self.cfg.pulse.n_pulses
        poll_interval = self.cfg.acq.poll_interval_s

        next_pulse_i = 0
        pulse_low_at: float | None = None
        drain_until: float | None = None
        drain_seconds = max(period, 1.0)

        try:
            self.backend.set_aux_out(ch, low_v)

            while not self._stop.is_set():
                now = time.monotonic() - t_start

                # Rising edge of pulse N when due.
                if next_pulse_i < n:
                    rise_at = next_pulse_i * period
                    if now >= rise_at:
                        self.backend.set_aux_out(ch, high_v)
                        t_pulse = time.monotonic() - t_start
                        self._pulse_times.append(t_pulse)
                        if hasattr(self.backend, "record_pulse"):
                            self.backend.record_pulse(t_pulse)
                        pulse_low_at = t_pulse + width
                        next_pulse_i += 1

                # Falling edge when the high period ends.
                if pulse_low_at is not None:
                    if (time.monotonic() - t_start) >= pulse_low_at:
                        self.backend.set_aux_out(ch, low_v)
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
            try:
                self.backend.set_aux_out(ch, low_v)
            except Exception:
                pass
