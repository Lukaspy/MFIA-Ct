"""Orchestrates a photo-C-t run.

For the real backend, drives the IA + DAQ module and software-toggles Aux Out
to produce optical pulses. For the mock backend, synthesizes segments directly.
"""

from __future__ import annotations

import time
from typing import Iterator, Protocol

from .acquisition import CtAcquisition, CtSegment
from .config import CtConfig, TriggerSource
from .mock_hardware import MockMFIA


class _Backend(Protocol):
    device: str

    def configure_impedance(self, cfg: CtConfig) -> None: ...
    def set_aux_out(self, channel: int, value_v: float) -> None: ...


class CtExperiment:
    def __init__(self, backend: _Backend, cfg: CtConfig) -> None:
        self.backend = backend
        self.cfg = cfg

    def run(self) -> Iterator[CtSegment]:
        """Yield one CtSegment per pulse.

        For real hardware, this configures the DAQ module then loops over pulses
        emitting Aux Out high/low edges and polling the module for new data.
        For the mock backend it yields synthetic traces directly.
        """
        self.backend.configure_impedance(self.cfg)

        if isinstance(self.backend, MockMFIA):
            yield from self._run_mock()
        else:
            yield from self._run_hardware()

    def _run_mock(self) -> Iterator[CtSegment]:
        from .mock_hardware import MockMFIA

        backend: MockMFIA = self.backend  # type: ignore[assignment]
        for _ in range(self.cfg.pulse.n_pulses):
            t, cp, gp = backend.generate_trace()
            yield CtSegment(t=t, cp=cp, gp=gp)
            time.sleep(self.cfg.pulse.period_s * 0.02)  # keep GUI responsive

    def _run_hardware(self) -> Iterator[CtSegment]:
        pulse = self.cfg.pulse
        acq = CtAcquisition(self.backend)
        acq.configure(self.cfg)
        acq.start()

        use_force = self.cfg.acq.trigger_source == TriggerSource.SOFTWARE

        try:
            self.backend.set_aux_out(pulse.aux_out_channel, pulse.low_v)
            time.sleep(0.05)

            emitted = 0
            for _ in range(pulse.n_pulses):
                if acq.is_finished():
                    break
                # Drive light pulse. In SOFTWARE mode we force the DAQ trigger
                # immediately after raising Aux Out so the segment is aligned
                # to the light-on edge.
                self.backend.set_aux_out(pulse.aux_out_channel, pulse.high_v)
                if use_force:
                    acq.force_trigger()
                time.sleep(pulse.pulse_width_s)
                self.backend.set_aux_out(pulse.aux_out_channel, pulse.low_v)
                time.sleep(max(0.0, pulse.period_s - pulse.pulse_width_s))

                segments = acq.read()
                for seg in segments[emitted:]:
                    yield seg
                emitted = len(segments)

            # Drain anything still buffered.
            time.sleep(self.cfg.acq.duration_s)
            segments = acq.read()
            for seg in segments[emitted:]:
                yield seg
        finally:
            acq.stop()
            self.backend.set_aux_out(pulse.aux_out_channel, pulse.low_v)
