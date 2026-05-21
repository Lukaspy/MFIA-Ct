"""DAQ module wrapper for triggered C-t acquisition.

Subscribes to the IA `sample` node's `param0` (R or 1/G) and `param1` (C)
fields and records aligned segments around each trigger edge on Aux In.
Mirrors hf2-mf-uhf/python/example_data_acquisition_edge.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import CtConfig, TriggerSource


@dataclass
class CtSegment:
    """One trigger's worth of data, time-aligned to the trigger edge at t=0."""

    t: np.ndarray
    cp: np.ndarray
    gp: np.ndarray


class CtAcquisition:
    """Configure and drive the LabOne Data Acquisition module."""

    def __init__(self, mfia) -> None:
        self.mfia = mfia
        self.module: Any = None
        self._cp_path: str = ""
        self._gp_path: str = ""

    def configure(self, cfg: CtConfig) -> None:
        if self.mfia.daq is None:
            raise RuntimeError("MFIA not connected")
        dev = self.mfia.device
        acq = cfg.acq
        imp = cfg.ia.imp_index

        self.module = self.mfia.daq.dataAcquisitionModule()
        m = self.module
        m.set("device", dev)
        m.set("type", 1)  # edge trigger
        m.set("edge", 1)  # rising
        m.set("level", acq.trigger_level_v)
        m.set("hysteresis", acq.trigger_hysteresis_v)
        m.set("count", cfg.pulse.n_pulses)
        m.set("delay", -acq.pre_trigger_s)
        m.set("duration", acq.duration_s)
        m.set("grid/mode", 4)  # exact: defined by highest demod rate
        m.set("grid/repetitions", acq.repetitions)
        m.set("holdoff/time", cfg.pulse.period_s * 0.5)
        m.set("holdoff/count", 0)

        sample_path = f"/{dev}/imps/{imp}/sample"
        # imps sample param0 = R (or Rs/Rp depending on model), param1 = C
        self._cp_path = f"{sample_path}.param1"
        self._gp_path = f"{sample_path}.param0"

        if acq.trigger_source == TriggerSource.INTERNAL:
            # Self-trigger off the same Aux Out that drives the LED. The Aux
            # output value is mirrored on an internal node that can be used as
            # a trigger source via the auxins path on instruments that route
            # it; for compatibility we trigger off the demod sample bias node
            # toggling instead.
            trignode = f"/{dev}/auxouts/{cfg.pulse.aux_out_channel}/value"
        else:
            trignode = f"/{dev}/auxins/{acq.trigger_aux_in_channel}/sample.value"
        m.set("triggernode", trignode)

        m.subscribe(self._cp_path)
        m.subscribe(self._gp_path)

    def start(self) -> None:
        if self.module is None:
            raise RuntimeError("configure() must be called first")
        self.module.execute()

    def stop(self) -> None:
        if self.module is not None:
            self.module.finish()

    def progress(self) -> float:
        return float(self.module.progress()[0]) if self.module else 0.0

    def is_finished(self) -> bool:
        return bool(self.module.finished()) if self.module else True

    def read(self) -> list[CtSegment]:
        if self.module is None:
            return []
        data = self.module.read(True)
        return self._parse(data)

    def _parse(self, data: dict) -> list[CtSegment]:
        if self._cp_path not in data or self._gp_path not in data:
            return []
        cp_chunks = data[self._cp_path]
        gp_chunks = data[self._gp_path]
        clockbase = self.mfia.clockbase()
        out: list[CtSegment] = []
        for cp_chunk, gp_chunk in zip(cp_chunks, gp_chunks):
            ts = cp_chunk["timestamp"][0]
            trig_ts = int(cp_chunk["header"]["changedtimestamp"][0]) - int(
                cp_chunk["header"]["gridcoloffset"][0] * clockbase
            )
            t = (ts - float(trig_ts)) / clockbase
            out.append(
                CtSegment(t=t, cp=cp_chunk["value"][0], gp=gp_chunk["value"][0])
            )
        return out
