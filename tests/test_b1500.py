"""Tests for the B1500 backend (mfia_ct.b1500) and the I-V path.

No hardware: the VISA session is a small in-memory fake, so these exercise the
FLEX command sequences, the ASCII data parser, the Cp-G→Z conversion, and the
full MockB1500 → CfExperiment → I-V CSV round-trip.
"""
from __future__ import annotations

import numpy as np

from mfia_ct.b1500 import B1500, _cp_g_to_z, _parse_ascii_block
from mfia_ct.cf_config import (
    BiasSweepSettings,
    CfConfig,
    IlluminationSequence,
    IlluminationStep,
    IvSweepSettings,
    RunMetadata,
    SweeperSettings,
    SweepType,
)
from mfia_ct.cf_experiment import CfExperiment
from mfia_ct.cf_storage import IvResult, SweepResult, make_filename, write_iv_csv
from mfia_ct.led_source import MockLedSource


# --- Fake VISA instrument ---------------------------------------------------


class FakeInst:
    """Duck-typed VISA instrument: records writes, canned query answers, and a
    settable ``read_response`` for the measurement data block."""

    def __init__(self, unt: str = "B1520A,0;B1517A,0") -> None:
        self.writes: list[str] = []
        self.read_response = ""
        self.timeout = 0
        self.write_termination = None
        self.read_termination = None
        self._unt = unt

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)

    def query(self, cmd: str) -> str:
        c = cmd.strip()
        if c.startswith("UNT?"):  # UNT? or UNT? 0
            return self._unt
        return {
            "*IDN?": "Keysight Technologies,B1500A,0,A.01",
            "*OPC?": "1",
            "ERRX?": '0,"No Error."',
            "NUB?": "0",
        }.get(c, "0")

    def read(self) -> str:
        return self.read_response

    def close(self) -> None:
        pass


def _connect(unt: str = "B1520A,0;B1517A,0") -> tuple[B1500, FakeInst]:
    fake = FakeInst(unt)
    b = B1500(inst=fake)
    b.connect()
    return b, fake


def _cmu_block(freqs, cps, gs) -> str:
    """FMT-1 CMU block: Cp (dtype C), G (dtype Y), source freq (dtype F)."""
    toks = []
    for f, cp, g in zip(freqs, cps, gs):
        toks += [f"NAC{cp:+.6E}", f"NAY{g:+.6E}", f"NAF{f:+.6E}"]
    return ",".join(toks) + "\n"


def _iv_block(vs, currents) -> str:
    """FMT-1 SMU block: current (dtype I), source voltage (dtype V, status W)."""
    toks = []
    for v, i in zip(vs, currents):
        toks += [f"NAI{i:+.6E}", f"WAV{v:+.6E}"]
    return ",".join(toks) + "\n"


# --- Parser + conversion (pure) ---------------------------------------------


def test_parse_ascii_block_strips_header_and_reads_values() -> None:
    raw = "NAC+1.234560E-09,NAY+2.000000E-06,VAC+1.999990E+99\n"
    out = _parse_ascii_block(raw)
    assert out[0] == ("N", "A", "C", 1.23456e-9)
    assert out[1][2] == "Y" and abs(out[1][3] - 2e-6) < 1e-12
    # The over-range dummy (199.999E+99) is mapped to NaN, not a giant number.
    assert np.isnan(out[2][3])


def test_cp_g_to_z_roundtrips_through_sweepresult() -> None:
    freq = np.array([1e3, 1e4, 1e5])
    cp = np.full(3, 1.0e-9)
    g = np.full(3, 1.0e-6)
    zr, zi = _cp_g_to_z(freq, cp, g)
    r = SweepResult(frequency_hz=freq, z_real=zr, z_imag=zi, metadata=None)
    cp_back, rp_back = r.cp_rp
    assert np.allclose(cp_back, cp)
    assert np.allclose(rp_back, 1.0 / g)


# --- connect / slot discovery ------------------------------------------------


def test_connect_discovers_cmu_and_smu_slots() -> None:
    b, fake = _connect("B1520A,0;B1517A,0;B1517A,0")
    assert b._cmu == 1  # first CMU-like module
    assert b._smu == 2  # first SMU-like module
    assert "FMT 1,1" in fake.writes
    assert b.instrument == "B1500"


# --- C-f sweep ---------------------------------------------------------------


def test_run_sweep_cf_sequence_and_data() -> None:
    b, fake = _connect()
    cfg = SweeperSettings(start_hz=1e3, stop_hz=1e6, points_per_decade=3)
    n = cfg.n_points
    freq = np.logspace(3, 6, n)
    fake.read_response = _cmu_block(freq, np.full(n, 1e-9), np.full(n, 1e-6))

    b.set_dc_bias(-2.0)
    b.set_amplitude(0.05)
    f, zr, zi = b.run_sweep(cfg)

    assert len(f) == n and len(zr) == n and len(zi) == n
    w = "\n".join(fake.writes)
    assert "MM 22,1" in w  # C-f sweep mode on the CMU channel
    assert "IMP 100" in w  # Cp-G
    assert "ACV 1,0.05" in w  # V RMS directly, no sqrt(2)
    assert "DCV 1,-2" in w
    assert any(cmd.startswith("WFC 1,2,") for cmd in fake.writes)  # log sweep
    # Cp recovered from the returned Z matches what the instrument "reported".
    r = SweepResult(frequency_hz=f, z_real=zr, z_imag=zi, metadata=None)
    assert np.allclose(r.cp_rp[0], 1e-9)


def test_cf_sweep_clamps_frequency_and_amplitude() -> None:
    b, fake = _connect()
    cfg = SweeperSettings(start_hz=1.0, stop_hz=5e6, points_per_decade=2)
    n = cfg.n_points
    freq = np.logspace(0, np.log10(5e6), n)
    fake.read_response = _cmu_block(freq, np.full(n, 1e-9), np.full(n, 1e-6))
    b.set_amplitude(0.5)  # above the 250 mV ceiling
    b.run_sweep(cfg)
    wfc = [c for c in fake.writes if c.startswith("WFC ")][0]
    # start clamped up to the 1 kHz MFCMU floor.
    assert ",1000," in wfc
    assert "ACV 1,0.25" in fake.writes  # clamped to the OSC ceiling


# --- C-V sweep ---------------------------------------------------------------


def test_run_bias_sweep_cv() -> None:
    b, fake = _connect()
    cv = BiasSweepSettings(start_v=-3, stop_v=3, n_points=7)
    fake.read_response = _cmu_block(
        np.full(7, 1e5), np.full(7, 2e-9), np.full(7, 5e-7)
    )
    bias, zr, zi = b.run_bias_sweep(cv, 100_000.0)
    assert np.allclose(bias, np.linspace(-3, 3, 7))
    w = "\n".join(fake.writes)
    assert "MM 18,1" in w  # CV sweep mode
    assert "FC 1,100000" in w
    assert any(c.startswith("WDCV 1,1,-3,3,7") for c in fake.writes)


# --- I-V sweep ---------------------------------------------------------------


def test_run_iv_sweep_single_and_double() -> None:
    b, fake = _connect()
    iv = IvSweepSettings(start_v=-1, stop_v=1, n_points=5, i_compliance_a=0.01)
    fake.read_response = _iv_block(iv.values_v, np.linspace(-1e-6, 1e-6, 5))
    v, cur = b.run_iv_sweep(iv)
    assert len(v) == 5 and len(cur) == 5
    w = "\n".join(fake.writes)
    assert "MM 2,2" in w  # SMU staircase on the SMU channel
    assert "CMM 2,1" in w  # measure current
    assert any(c.startswith("WV 2,1,0,-1,1,5,0.01") for c in fake.writes)

    # Double sweep: WV mode 3, and 2*n data expected.
    b2, fake2 = _connect()
    ivd = IvSweepSettings(start_v=-1, stop_v=1, n_points=5, double_sweep=True)
    fake2.read_response = _iv_block(ivd.values_v, np.zeros(10))
    v2, cur2 = b2.run_iv_sweep(ivd)
    assert len(v2) == 10
    assert any(c.startswith("WV 2,3,0,") for c in fake2.writes)


def test_iv_needs_smu_channel() -> None:
    # A mainframe with only a CMU (no SMU) can't do I-V.
    b, _ = _connect("B1520A,0")
    import pytest

    with pytest.raises(Exception):
        b.run_iv_sweep(IvSweepSettings())


# --- MockB1500 through the full experiment ----------------------------------


def _iv_config(tmp_path) -> CfConfig:
    return CfConfig(
        sweep_type=SweepType.I_V,
        iv=IvSweepSettings(start_v=-1, stop_v=1, n_points=11),
        illumination=IlluminationSequence(
            steps=[
                IlluminationStep("dark", None, 0.0, 0.0),
                IlluminationStep("470nm", 470.0, 100.0, 0.0),
            ]
        ),
        run=RunMetadata(
            device_id="D1", substrate_type="n-Si", output_dir=str(tmp_path)
        ),
    )


def test_mock_b1500_iv_through_experiment_and_csv(tmp_path) -> None:
    from mfia_ct.mock_hardware import MockB1500

    cfg = _iv_config(tmp_path)
    exp = CfExperiment(
        MockB1500(), cfg, led=MockLedSource(), sleeper=lambda s, st: None
    )
    results = list(exp.run())

    assert len(results) == 2
    assert all(isinstance(r, IvResult) for r in results)
    dark, lit = results
    assert dark.metadata.instrument == "B1500"
    assert dark.metadata.sweep_type == "I-V"
    assert len(dark.voltage_v) == 11
    # Illumination adds a photocurrent, so the lit curve differs from dark.
    assert not np.allclose(dark.current_a, lit.current_a)

    fname = make_filename(dark.metadata)
    assert fname.startswith("B1500_IV_D1_")
    path = write_iv_csv(dark, tmp_path / fname)
    text = path.read_text()
    assert "# instrument: B1500" in text
    assert "voltage_v,current_a" in text


def test_mock_b1500_cf_files_are_prefixed_b1500(tmp_path) -> None:
    """B1500 C-f data flows through the same SweepResult/CSV as the MFIA but is
    filed with a B1500_ prefix so the two instruments' data don't collide."""
    from mfia_ct.mock_hardware import MockB1500

    cfg = CfConfig(
        sweep_type=SweepType.C_F,
        sweep=SweeperSettings(start_hz=1e3, stop_hz=1e5, points_per_decade=3),
        illumination=IlluminationSequence(
            steps=[IlluminationStep("dark", None, 0.0, 0.0)]
        ),
        run=RunMetadata(device_id="D1", output_dir=str(tmp_path)),
    )
    cfg.bias.values_v = [0.0]
    exp = CfExperiment(MockB1500(), cfg, sleeper=lambda s, st: None)
    results = list(exp.run())
    assert results and isinstance(results[0], SweepResult)
    assert results[0].metadata.instrument == "B1500"
    assert make_filename(results[0].metadata).startswith("B1500_Cf_D1_")
