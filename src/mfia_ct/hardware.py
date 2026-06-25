"""Real MFIA backend via the zhinst LabOne Python API.

Mirrors the patterns in the official examples (mf/python/example_poll_impedance.py
and hf2-mf-uhf/python/example_data_acquisition_edge.py): an API session is
created against the data server, then nodes are configured via `daq.set(...)`.
Continuous acquisition uses `daq.subscribe` + `daq.poll` on the IA sample node.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

import numpy as np

from .acquisition import StreamChunk
from .cf_config import BiasSweepSettings, CfConfig, SweeperSettings
from .config import CtConfig, EquivCircuit, TerminalMode

# Equivalent-circuit selectors as exposed by /dev/imps/N/model.
# 0 = R+C series, 1 = R||C parallel. Match the LabOne IA tab.
_EQUIV_CIRCUIT_NODE_VALUE = {
    EquivCircuit.CP_RP: 1,
    EquivCircuit.CS_RS: 0,
}

# Terminal-mode selector as exposed by /dev/imps/N/mode.
# 0 = 4 Terminal, 1 = 2 Terminal (per the MFIA user manual node reference).
_TERMINAL_MODE_NODE_VALUE = {
    TerminalMode.FOUR_TERMINAL: 0,
    TerminalMode.TWO_TERMINAL: 1,
}


class MFIA:
    def __init__(
        self,
        device_id: str,
        server_host: str = "localhost",
        server_port: int = 8004,
        api_level: int = 6,
    ) -> None:
        self.device_id = device_id
        self.server_host = server_host
        self.server_port = server_port
        self.api_level = api_level
        self.daq: Any = None
        self.device: str = ""
        self._t_zero_ticks: Optional[int] = None
        self._clockbase_cached: float = 0.0
        self._subscribed: bool = False
        self._demod_sample_path: str = ""
        self._auxin_field: str = ""
        self._auxin_threshold: float = 0.0
        self._auxin_last_state: bool = False

    def connect(self) -> None:
        """Open a session against the LabOne data server and attach the device.

        Bypasses ``zhinst.utils.create_api_session`` because its multicast
        discovery step fails on networks where UDP multicast is blocked or
        routed away.
        """
        import zhinst.core
        import zhinst.utils

        self.daq = zhinst.core.ziDAQServer(
            self.server_host, self.server_port, self.api_level
        )
        devid = self.device_id.lower()

        last_err: Exception | None = None
        for iface in ("1GbE", "USB", "PCIe"):
            try:
                self.daq.connectDevice(devid, iface)
                break
            except RuntimeError as e:
                last_err = e
        else:
            try:
                visible = self.daq.getString("/zi/devices/visible")
            except Exception:
                visible = "(could not query)"
            raise RuntimeError(
                f"Could not attach {devid} to data server at "
                f"{self.server_host}:{self.server_port}. "
                f"Visible devices: {visible}. Last error: {last_err}"
            )

        self.device = devid
        zhinst.utils.disable_everything(self.daq, self.device)

    def disconnect(self) -> None:
        if self.daq is not None:
            try:
                self.daq.disconnect()
            except Exception:
                pass
        self.daq = None

    def __enter__(self) -> "MFIA":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.disconnect()

    def configure_impedance(self, cfg: CtConfig) -> None:
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        dev = self.device
        ia = cfg.ia
        model = _EQUIV_CIRCUIT_NODE_VALUE[ia.equiv_circuit]

        # auto/output and auto/bw left OFF: both recalibrate periodically in
        # the background and cause demod-stream gaps. The user's amplitude and
        # demod time-constant settings are used as-is.
        settings = [
            (f"/{dev}/imps/{ia.imp_index}/enable", 1),
            (f"/{dev}/imps/{ia.imp_index}/mode", _TERMINAL_MODE_NODE_VALUE[ia.terminal_mode]),
            (f"/{dev}/imps/{ia.imp_index}/auto/output", 0),
            (f"/{dev}/imps/{ia.imp_index}/auto/bw", 0),
            (f"/{dev}/imps/{ia.imp_index}/auto/inputrange", 0),
            (f"/{dev}/imps/{ia.imp_index}/freq", ia.frequency_hz),
            (f"/{dev}/imps/{ia.imp_index}/output/amplitude", ia.ac_amplitude_v),
            (f"/{dev}/imps/{ia.imp_index}/bias/value", ia.dc_bias_v),
            (f"/{dev}/imps/{ia.imp_index}/bias/enable", 1 if ia.dc_bias_v != 0 else 0),
            (f"/{dev}/imps/{ia.imp_index}/model", model),
            (f"/{dev}/demods/{ia.imp_index}/rate", cfg.demod.sample_rate_hz),
            (f"/{dev}/demods/{ia.imp_index}/order", cfg.demod.filter_order),
            (f"/{dev}/demods/{ia.imp_index}/timeconstant", cfg.demod.time_constant_s),
        ]
        self.daq.set(settings)
        self.daq.sync()

    def configure_impedance_for_cf(self, cfg: CfConfig) -> None:
        """Configure the IA for frequency sweeping under the C-f campaign.

        Differences from ``configure_impedance``:
        - No demod TC/rate is written; the Sweeper module manages per-point
          settling via auto-bandwidth + settling/tcs.
        - The ``ac_amplitude_v`` in CfConfig is in **V RMS** (B1500 convention).
          The MFIA ``/imps/N/output/amplitude`` node takes V peak. The √2
          conversion happens here, exactly once, so the rest of the stack
          can stay in RMS without per-call worrying about it.
        """
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        dev = self.device
        ia = cfg.ia
        model = _EQUIV_CIRCUIT_NODE_VALUE[ia.equiv_circuit]
        amp_pk = ia.ac_amplitude_v * math.sqrt(2.0)

        # OUTPUT RANGE must cover the largest DC bias this block visits + the AC
        # peak, else the bias over-ranges the output stage (OVO) and the sweep
        # reads garbage. Auto-output sizes only to the AC amplitude and ignores
        # the bias, so we set the range explicitly — the LabOne webUI's
        # manual-range approach. configure runs once per block and set_dc_bias
        # only steps the value, so size to the block's worst-case |bias| here.
        # The MFIA rounds up to the next available output range.
        if cfg.sweep_type.name == "C_V":
            max_bias = max(abs(cfg.cv_bias.start_v), abs(cfg.cv_bias.stop_v))
        else:
            biases = cfg.bias.values_v or [ia.dc_bias_v]
            max_bias = max(abs(float(b)) for b in biases)
        output_range = (max_bias + amp_pk) * 1.05

        settings = [
            (f"/{dev}/imps/{ia.imp_index}/enable", 1),
            (f"/{dev}/imps/{ia.imp_index}/mode", _TERMINAL_MODE_NODE_VALUE[ia.terminal_mode]),
            # Manual output range sized to bias + amp (see above); auto-output OFF
            # so it can't shrink the range back down to the AC amplitude.
            (f"/{dev}/imps/{ia.imp_index}/auto/output", 0),
            (f"/{dev}/sigouts/{ia.imp_index}/range", output_range),
            (f"/{dev}/imps/{ia.imp_index}/auto/bw", 1),  # sweeper expects auto-BW on
            (f"/{dev}/imps/{ia.imp_index}/output/amplitude", amp_pk),
            (f"/{dev}/imps/{ia.imp_index}/bias/value", ia.dc_bias_v),
            (f"/{dev}/imps/{ia.imp_index}/bias/enable", 1),
            (f"/{dev}/imps/{ia.imp_index}/model", model),
        ]
        # Current-input range: AUTO by default — the webUI runs auto and reads
        # the high-Z low-f tail cleanly once wired right, so auto is the norm.
        # Pinning a fixed range remains available (current_range_a) but is rarely
        # needed; a wide C-f can't fit one fixed range anyway (it overloads at
        # high f where the cap draws µA). Voltage input range stays auto.
        if ia.current_range_a is None:
            settings.append((f"/{dev}/imps/{ia.imp_index}/auto/inputrange", 1))
        else:
            settings.append((f"/{dev}/imps/{ia.imp_index}/auto/inputrange", 0))
            settings.append(
                (f"/{dev}/imps/{ia.imp_index}/current/range", float(ia.current_range_a))
            )
        self.daq.set(settings)
        self.daq.sync()

    def set_dc_bias(self, bias_v: float) -> None:
        """Update the IA DC bias mid-campaign; bias output stays enabled."""
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        self.daq.set(
            [
                (f"/{self.device}/imps/0/bias/value", bias_v),
                (f"/{self.device}/imps/0/bias/enable", 1),
            ]
        )
        self.daq.sync()

    def run_sweep(
        self,
        cfg: SweeperSettings,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Run one frequency sweep via the LabOne Sweeper module.

        Returns ``(frequency_hz, z_real, z_imag)`` arrays. Optional
        ``progress_cb`` is called with a 0..1 fraction every ~0.5 s while
        the sweep runs; ``stop_check`` is polled to abort cleanly.
        ``light_active`` is accepted for signature parity with the mock
        backend (real hardware's optical state is physical, not commanded).
        """
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        dev = self.device
        n_pts = cfg.n_points

        sweeper = self.daq.sweep()
        sweeper.set("device", dev)
        sweeper.set("gridnode", f"/{dev}/imps/0/freq")
        sweeper.set("start", cfg.start_hz)
        sweeper.set("stop", cfg.stop_hz)
        sweeper.set("samplecount", n_pts)
        sweeper.set("xmapping", 1 if cfg.log_spacing else 0)
        # Auto-BW: 0=manual, 1=fixed, 2=auto (recommended for IS).
        sweeper.set("bandwidthcontrol", 2 if cfg.auto_bandwidth else 0)
        sweeper.set("settling/tc", float(cfg.settling_tcs))
        sweeper.set("settling/inaccuracy", float(cfg.settling_inaccuracy))
        sweeper.set("averaging/sample", int(cfg.averaging_samples))
        sweeper.set("averaging/time", float(cfg.averaging_time_s))
        sweeper.set("order", int(cfg.filter_order))

        return self._execute_sweeper(
            sweeper, progress_cb=progress_cb, stop_check=stop_check
        )

    def run_bias_sweep(
        self,
        cv: BiasSweepSettings,
        fixed_freq_hz: float,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Run one C-V sweep: step DC bias at a fixed test frequency.

        Holds ``/imps/0/freq`` at ``fixed_freq_hz`` and points the Sweeper's
        ``gridnode`` at ``/imps/0/bias/value`` so it steps the DC bias from
        ``cv.start_v`` to ``cv.stop_v``. Returns ``(bias_v, z_real, z_imag)``.
        Identical settling/averaging machinery to ``run_sweep`` — only the
        swept node and the held frequency differ. ``light_active`` is accepted
        for signature parity with the mock (real optical state is physical).
        """
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        dev = self.device

        # Hold the test frequency fixed and make sure the bias output is on, so
        # the Sweeper's grid writes actually move the DC bias.
        self.daq.set(
            [
                (f"/{dev}/imps/0/freq", float(fixed_freq_hz)),
                (f"/{dev}/imps/0/bias/enable", 1),
            ]
        )
        self.daq.sync()

        sweeper = self.daq.sweep()
        sweeper.set("device", dev)
        sweeper.set("gridnode", f"/{dev}/imps/0/bias/value")
        sweeper.set("start", cv.start_v)
        sweeper.set("stop", cv.stop_v)
        sweeper.set("samplecount", max(2, int(cv.n_points)))
        sweeper.set("xmapping", 1 if cv.log_spacing else 0)
        sweeper.set("bandwidthcontrol", 2 if cv.auto_bandwidth else 0)
        sweeper.set("settling/tc", float(cv.settling_tcs))
        sweeper.set("settling/inaccuracy", float(cv.settling_inaccuracy))
        sweeper.set("averaging/sample", int(cv.averaging_samples))
        sweeper.set("averaging/time", float(cv.averaging_time_s))
        sweeper.set("order", int(cv.filter_order))

        return self._execute_sweeper(
            sweeper, progress_cb=progress_cb, stop_check=stop_check
        )

    def _execute_sweeper(
        self,
        sweeper: Any,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Run a configured Sweeper module to completion and unpack its data.

        Shared by ``run_sweep`` (gridnode = freq) and ``run_bias_sweep``
        (gridnode = bias): subscribes to the IA sample, polls ``finished()``
        while reporting progress and honouring ``stop_check``, then returns
        ``(grid, z_real, z_imag)`` where ``grid`` is whichever axis was swept.
        """
        sample_path = f"/{self.device}/imps/0/sample"
        sweeper.subscribe(sample_path)
        try:
            sweeper.execute()
            while True:
                if sweeper.finished():
                    break
                if stop_check is not None and stop_check():
                    try:
                        sweeper.finish()
                    except Exception:
                        pass
                    break
                if progress_cb is not None:
                    try:
                        progress_cb(float(sweeper.progress()))
                    except Exception:
                        pass
                time.sleep(0.5)

            raw = sweeper.read(True)
        finally:
            try:
                sweeper.unsubscribe("*")
            except Exception:
                pass
            try:
                sweeper.clear()
            except Exception:
                pass

        # The sweeper packs the sample path into a nested list.
        if sample_path not in raw:
            raise RuntimeError("Sweeper returned no data for the IA sample")
        sweeps = raw[sample_path]
        if not sweeps or not sweeps[0]:
            raise RuntimeError("Sweeper returned empty sweep block")
        block = sweeps[0][0]

        grid = np.asarray(block.get("grid", block.get("frequency", [])), dtype=float)
        z_real = np.asarray(block.get("realz", []), dtype=float)
        z_imag = np.asarray(block.get("imagz", []), dtype=float)
        if grid.size == 0 or z_real.size != grid.size or z_imag.size != grid.size:
            raise RuntimeError(
                "Sweeper data malformed — expected matching grid/realz/imagz arrays"
            )
        return grid, z_real, z_imag

    def set_aux_out(self, channel: int, value_v: float) -> None:
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        self.daq.set(
            [
                (f"/{self.device}/auxouts/{channel}/outputselect", -1),  # manual
                (f"/{self.device}/auxouts/{channel}/offset", value_v),
            ]
        )

    def clockbase(self) -> float:
        if not self._clockbase_cached:
            self._clockbase_cached = float(self.daq.getInt(f"/{self.device}/clockbase"))
        return self._clockbase_cached

    @property
    def imps_sample_path(self) -> str:
        return f"/{self.device}/imps/0/sample"

    # --- Continuous acquisition ----------------------------------------------

    def start_continuous(
        self,
        *,
        external_sync: bool = False,
        sync_aux_in_channel: int = 0,
        sync_threshold_v: float = 1.0,
    ) -> None:
        """Subscribe to the IA sample node so ``poll_continuous`` can stream.

        When ``external_sync`` is true also subscribe to the demod sample to
        read the Aux In channel; pulse edges are detected in the poll loop.
        """
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        self._t_zero_ticks = None
        self.clockbase()  # warm cache
        # Brief settle so the demod filter's initial transient (after the
        # configure_impedance writes) doesn't end up in the recorded stream.
        import time as _time
        _time.sleep(0.1)
        self.daq.subscribe(self.imps_sample_path)
        if external_sync:
            self._demod_sample_path = f"/{self.device}/demods/0/sample"
            self._auxin_field = f"auxin{sync_aux_in_channel}"
            self._auxin_threshold = sync_threshold_v
            self._auxin_last_state = False
            self.daq.subscribe(self._demod_sample_path)
        else:
            self._demod_sample_path = ""
            self._auxin_field = ""
        self._subscribed = True

    def stop_continuous(self) -> None:
        if self._subscribed and self.daq is not None:
            try:
                self.daq.unsubscribe(self.imps_sample_path)
            except Exception:
                pass
            if self._demod_sample_path:
                try:
                    self.daq.unsubscribe(self._demod_sample_path)
                except Exception:
                    pass
        self._subscribed = False
        self._demod_sample_path = ""

    def poll_continuous(self, length_s: float, timeout_ms: int = 500) -> Optional[StreamChunk]:
        """Pull whatever IA samples have accumulated since the last poll."""
        if not self._subscribed:
            return None
        data = self.daq.poll(length_s, timeout_ms, 0, True)
        if self.imps_sample_path not in data:
            return None
        sample = data[self.imps_sample_path]
        ts = np.asarray(sample["timestamp"])
        if ts.size == 0:
            return None
        if self._t_zero_ticks is None:
            self._t_zero_ticks = int(ts[0])
        t = (ts - self._t_zero_ticks) / self._clockbase_cached
        cp = np.asarray(sample["param1"], dtype=float)
        gp = np.asarray(sample["param0"], dtype=float)

        edges = self._detect_aux_in_edges(data)
        return StreamChunk(t=t, cp=cp, gp=gp, pulse_edges_s=edges)

    def _detect_aux_in_edges(self, data: dict) -> list[float]:
        """Find low→high crossings of ``auxin_threshold`` in the demod stream."""
        if not self._demod_sample_path or self._demod_sample_path not in data:
            return []
        sample = data[self._demod_sample_path]
        if self._auxin_field not in sample:
            return []
        values = np.asarray(sample[self._auxin_field], dtype=float)
        ts = np.asarray(sample["timestamp"])
        if values.size == 0:
            return []

        above = values > self._auxin_threshold
        edges_ticks: list[int] = []
        if not self._auxin_last_state and above[0]:
            edges_ticks.append(int(ts[0]))
        if above.size > 1:
            rising = (~above[:-1]) & above[1:]
            for idx in np.where(rising)[0]:
                edges_ticks.append(int(ts[idx + 1]))
        self._auxin_last_state = bool(above[-1])

        if self._t_zero_ticks is None:
            return []
        return [(tk - self._t_zero_ticks) / self._clockbase_cached for tk in edges_ticks]
