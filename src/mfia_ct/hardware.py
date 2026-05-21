"""Real MFIA backend via the zhinst LabOne Python API.

Mirrors the patterns in the official examples (mf/python/example_poll_impedance.py
and hf2-mf-uhf/python/example_data_acquisition_edge.py): an API session is
created against the data server, then nodes are configured via `daq.set(...)`.
Continuous acquisition uses `daq.subscribe` + `daq.poll` on the IA sample node.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from .acquisition import StreamChunk
from .config import CtConfig, EquivCircuit

# Equivalent-circuit selectors as exposed by /dev/imps/N/model.
# 0 = R+C series, 1 = R||C parallel. Match the LabOne IA tab.
_EQUIV_CIRCUIT_NODE_VALUE = {
    EquivCircuit.CP_RP: 1,
    EquivCircuit.CS_RS: 0,
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
            (f"/{dev}/imps/{ia.imp_index}/mode", 0),
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

    def start_continuous(self) -> None:
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        self._t_zero_ticks = None
        self.clockbase()  # warm cache
        # Brief settle so the demod filter's initial transient (after the
        # configure_impedance writes) doesn't end up in the recorded stream.
        import time as _time
        _time.sleep(0.1)
        self.daq.subscribe(self.imps_sample_path)
        self._subscribed = True

    def stop_continuous(self) -> None:
        if self._subscribed and self.daq is not None:
            try:
                self.daq.unsubscribe(self.imps_sample_path)
            except Exception:
                pass
        self._subscribed = False

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
        return StreamChunk(t=t, cp=cp, gp=gp)
