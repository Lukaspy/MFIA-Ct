"""Real MFIA backend via the zhinst LabOne Python API.

Mirrors the patterns in the official examples (mf/python/example_poll_impedance.py
and hf2-mf-uhf/python/example_data_acquisition_edge.py): an API session is
created against the data server, then nodes are configured via `daq.set(...)`
and the DAQ module is obtained via `daq.dataAcquisitionModule()`.
"""

from __future__ import annotations

from typing import Any

from .config import CtConfig, EquivCircuit

# Equivalent-circuit selectors as exposed by /dev/imps/N/model.
# 0 = R+C series, 1 = R||C parallel. Match the LabOne IA tab.
_EQUIV_CIRCUIT_NODE_VALUE = {
    EquivCircuit.CP_RP: 1,
    EquivCircuit.CS_RS: 0,
}


class MFIA:
    """Thin wrapper around a zhinst data-server session.

    The connection is established by `connect()`; the underlying `daq` handle
    and `device` id are exposed as attributes so that acquisition.py can build
    a DAQ module against the same session.
    """

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
        self._props: Any = None

    def connect(self) -> None:
        import zhinst.utils

        self.daq, self.device, self._props = zhinst.utils.create_api_session(
            self.device_id,
            self.api_level,
            server_host=self.server_host,
            server_port=self.server_port,
        )
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
        """Configure the IA module per the experiment config."""
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        dev = self.device
        ia = cfg.ia
        model = _EQUIV_CIRCUIT_NODE_VALUE[ia.equiv_circuit]

        settings = [
            (f"/{dev}/imps/{ia.imp_index}/enable", 1),
            (f"/{dev}/imps/{ia.imp_index}/mode", 0),  # 4-terminal / standard
            (f"/{dev}/imps/{ia.imp_index}/auto/output", 1),
            (f"/{dev}/imps/{ia.imp_index}/auto/bw", 0),  # manual BW so TC sticks
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
        """Set Aux Out to a static voltage (used to drive the LED in INTERNAL mode)."""
        if self.daq is None:
            raise RuntimeError("MFIA.connect() must be called first")
        self.daq.set(
            [
                (f"/{self.device}/auxouts/{channel}/outputselect", -1),  # manual
                (f"/{self.device}/auxouts/{channel}/offset", value_v),
            ]
        )

    def clockbase(self) -> float:
        return float(self.daq.getInt(f"/{self.device}/clockbase"))

    @property
    def imps_sample_path(self) -> str:
        return f"/{self.device}/imps/0/sample"
