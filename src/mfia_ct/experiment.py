"""Orchestrates a continuous photo-C-t run.

The MFIA streams Cp(t) and Gp(t) at the demod rate. A background ``Pulser``
thread software-paces Aux Out as the optical pulse train and records the
moment each pulse fires. ``CtExperiment.run()`` is a generator that yields
``StreamChunk``s as they arrive from ``poll_continuous``; pulse timings are
queryable via ``pulse_times`` at any point.
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


class _Pulser:
    """Background thread that software-paces Aux Out and timestamps each edge."""

    def __init__(self, backend: _Backend, cfg: CtConfig) -> None:
        self.backend = backend
        self.cfg = cfg
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._times: list[float] = []
        self._t_start: float = 0.0
        self._thread: threading.Thread | None = None
        self._done = threading.Event()

    @property
    def pulse_times(self) -> list[float]:
        with self._lock:
            return list(self._times)

    def is_done(self) -> bool:
        return self._done.is_set()

    def start(self, t_start: float) -> None:
        self._t_start = t_start
        self._thread = threading.Thread(target=self._run, name="ct-pulser", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        ch = self.cfg.pulse.aux_out_channel
        period = self.cfg.pulse.period_s
        width = self.cfg.pulse.pulse_width_s
        n = self.cfg.pulse.n_pulses

        try:
            self.backend.set_aux_out(ch, self.cfg.pulse.low_v)
            for i in range(n):
                if self._stop.is_set():
                    return
                target = i * period
                wait = target - (time.monotonic() - self._t_start)
                if wait > 0 and self._stop.wait(wait):
                    return

                self.backend.set_aux_out(ch, self.cfg.pulse.high_v)
                t_pulse = time.monotonic() - self._t_start
                with self._lock:
                    self._times.append(t_pulse)
                if hasattr(self.backend, "record_pulse"):
                    self.backend.record_pulse(t_pulse)  # for mock synthesis

                if self._stop.wait(width):
                    return
                self.backend.set_aux_out(ch, self.cfg.pulse.low_v)
        finally:
            try:
                self.backend.set_aux_out(ch, self.cfg.pulse.low_v)
            except Exception:
                pass
            self._done.set()


class CtExperiment:
    """Drive a continuous photo-C-t run.

    Usage::

        exp = CtExperiment(backend, cfg)
        for chunk in exp.run():
            # plot chunk; query exp.pulse_times when convenient
            if user_wants_stop:
                exp.stop()
    """

    def __init__(self, backend: _Backend, cfg: CtConfig) -> None:
        self.backend = backend
        self.cfg = cfg
        self._stop = threading.Event()
        self._pulser: _Pulser | None = None
        self._t_start: float = 0.0

    @property
    def pulse_times(self) -> list[float]:
        return self._pulser.pulse_times if self._pulser else []

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> Iterator[StreamChunk]:
        self.backend.configure_impedance(self.cfg)
        self.backend.start_continuous()
        self._t_start = time.monotonic()

        self._pulser = _Pulser(self.backend, self.cfg)
        self._pulser.start(self._t_start)

        # Once the pulser finishes its train, drain this many extra seconds
        # of trailing data so the last off-transient is captured before we
        # stop on our own.
        drain_after_done = max(self.cfg.pulse.period_s, 1.0)
        done_at: float | None = None

        try:
            while not self._stop.is_set():
                chunk = self.backend.poll_continuous(self.cfg.acq.poll_interval_s)
                if chunk is not None:
                    yield chunk

                if done_at is None and self._pulser.is_done():
                    done_at = time.monotonic()
                if done_at is not None and time.monotonic() - done_at >= drain_after_done:
                    break
        finally:
            self._pulser.stop()
            self.backend.stop_continuous()
            try:
                self.backend.set_aux_out(
                    self.cfg.pulse.aux_out_channel, self.cfg.pulse.low_v
                )
            except Exception:
                pass
