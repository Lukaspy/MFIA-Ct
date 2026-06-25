"""Tests for the YAML measurement-plan loader (mfia_ct.cf_plan)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mfia_ct.cf_config import SweepType
from mfia_ct.cf_plan import PlanError, load_plan, parse_plan
from mfia_ct.config import EquivCircuit, TerminalMode

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "plan_blocks_A-E.yaml"


def _lit(cfg):
    return [s for s in cfg.illumination.steps if not s.is_dark]


def _dark(cfg):
    return [s for s in cfg.illumination.steps if s.is_dark]


# --- The worked Block A–E example ------------------------------------------


def test_example_plan_loads_into_six_blocks() -> None:
    configs = load_plan(EXAMPLE)
    assert len(configs) == 6
    # Device metadata + shared defaults propagate to every block.
    assert all(c.run.device_id == "2013-4" for c in configs)
    assert all(c.run.output_dir == "/data/mfia/2013-4" for c in configs)
    assert all(c.ia.terminal_mode == TerminalMode.TWO_TERMINAL for c in configs)
    assert all(c.ia.equiv_circuit == EquivCircuit.CP_RP for c in configs)
    assert all(abs(c.ia.ac_amplitude_v - 0.030) < 1e-9 for c in configs)


def test_example_block_b_matrix_with_per_wavelength_bracket() -> None:
    b = load_plan(EXAMPLE)[2]  # "B: spectral grid @ anchor"
    assert b.sweep_type == SweepType.C_F
    assert b.bias.values_v == [0.0]
    assert b.sweep.start_hz == 1.0  # 1 Hz floor from defaults
    # 8 wavelengths × 5 intensities lit; dark pre+post per wavelength = 16 dark.
    assert len(_lit(b)) == 8 * 5
    assert len(_dark(b)) == 8 * 2
    # Per wavelength the 5 intensities are contiguous and increasing.
    first_wl = _lit(b)[0].wavelength_nm
    first_group = [s.intensity_pct for s in _lit(b) if s.wavelength_nm == first_wl][:5]
    assert first_group == [1.0, 3.0, 10.0, 30.0, 100.0]


def test_example_block_b_randomized_order_is_seeded_reproducible() -> None:
    order1 = [s.wavelength_nm for s in _lit(load_plan(EXAMPLE)[2]) if s.intensity_pct == 1.0]
    order2 = [s.wavelength_nm for s in _lit(load_plan(EXAMPLE)[2]) if s.intensity_pct == 1.0]
    assert order1 == order2  # same seed → same realized order
    assert sorted(order1) == [385, 470, 505, 530, 590, 625, 740, 850]


def test_example_block_c_fixed_illumination_bias_ladder() -> None:
    c = load_plan(EXAMPLE)[3]  # "C: bias ladder @ 625nm"
    assert c.sweep_type == SweepType.C_F
    assert c.bias.values_v == [-4.0, -2.0, 0.0, 1.0, 2.0, 4.0]
    labels = [s.label for s in c.illumination.steps]
    assert labels == ["dark", "625nm_100pct"]


def test_example_block_d_overrides_floor_and_settle() -> None:
    d = load_plan(EXAMPLE)[4]  # "D: deep anchor @ 10 mHz"
    assert d.sweep.start_hz == 0.01           # 10 mHz floor override
    assert d.bias.bias_settle_s == 25.0       # device_settle_s override


def test_example_block_e_cv_dispersion_with_anchor_overrides() -> None:
    e = load_plan(EXAMPLE)[5]  # "E: C-V dispersion"
    assert e.sweep_type == SweepType.C_V
    assert e.cv_frequencies_hz == [1000.0, 100.0, 10.0, 1.0]  # high→low
    assert e.cv_bias.start_v == -4.0 and e.cv_bias.stop_v == 4.0
    assert e.cv_bias.n_points == 101
    # 4 anchor wavelengths × 5 intensities + 4 others × 1 = 24 lit steps.
    assert len(_lit(e)) == 4 * 5 + 4 * 1


# --- Validation / error messages -------------------------------------------


def _minimal(**over):
    plan = {
        "device": {"id": "D1"},
        "output_dir": "/tmp/x",
        "blocks": [
            {"name": "b", "type": "c-f", "bias": [0],
             "illumination": {"dark_only": True}},
        ],
    }
    plan.update(over)
    return plan


def test_requires_device_id_and_output_dir() -> None:
    with pytest.raises(PlanError, match="device.id"):
        parse_plan(_minimal(device={}))
    with pytest.raises(PlanError, match="output_dir"):
        parse_plan(_minimal(output_dir=""))


def test_block_error_names_the_block() -> None:
    with pytest.raises(PlanError, match="block 1 .b."):
        parse_plan(_minimal(blocks=[{"name": "b", "type": "c-f",
                                     "illumination": {"dark_only": True}}]))  # no bias


def test_cv_block_needs_bias_mapping_and_frequencies() -> None:
    with pytest.raises(PlanError, match="frequencies"):
        parse_plan(_minimal(blocks=[{"name": "e", "type": "c-v",
                                     "bias": {"start_v": -1, "stop_v": 1, "points": 11},
                                     "illumination": {"dark_only": True}}]))
    with pytest.raises(PlanError, match="bias"):
        parse_plan(_minimal(blocks=[{"name": "e", "type": "c-v", "frequencies": [100],
                                     "illumination": {"dark_only": True}}]))


def test_unknown_block_type_rejected() -> None:
    with pytest.raises(PlanError, match="c-f.*c-v|type"):
        parse_plan(_minimal(blocks=[{"name": "x", "type": "c-t",
                                     "illumination": {"dark_only": True}}]))


def test_matrix_requires_intensities_somewhere() -> None:
    with pytest.raises(PlanError, match="intensit"):
        parse_plan(_minimal(blocks=[{"name": "b", "type": "c-f", "bias": [0],
                                     "illumination": {"mode": "matrix",
                                                      "wavelengths": [625]}}]))


def test_reversed_and_aslisted_order() -> None:
    base = {"name": "b", "type": "c-f", "bias": [0]}
    listed = parse_plan(_minimal(blocks=[{**base, "illumination": {
        "mode": "matrix", "wavelengths": [385, 625, 850], "intensities": [100],
        "order": "as-listed", "dark_bracket": "none"}}]))[0]
    assert [s.wavelength_nm for s in _lit(listed)] == [385, 625, 850]
    rev = parse_plan(_minimal(blocks=[{**base, "illumination": {
        "mode": "matrix", "wavelengths": [385, 625, 850], "intensities": [100],
        "order": "reversed", "dark_bracket": "none"}}]))[0]
    assert [s.wavelength_nm for s in _lit(rev)] == [850, 625, 385]


def test_gui_load_plan_stages_into_queue(qtbot, monkeypatch) -> None:
    """The 'Load plan…' button reads a YAML plan and appends its campaigns to
    the overnight queue, ready to run without touching the controls."""
    from mfia_ct.gui import cf_app

    w = cf_app.CfMainWindow(preselect_mock=True)
    qtbot.addWidget(w)
    monkeypatch.setattr(
        cf_app.QFileDialog, "getOpenFileName",
        lambda *a, **k: (str(EXAMPLE), ""),
    )
    w.load_plan_into_queue()

    assert len(w._queue) == 6
    assert w.queue_list.count() == 6
    assert "C-f" in w.queue_list.item(0).text()
    assert any("C-V" in w.queue_list.item(i).text() for i in range(6))


def test_current_range_default_auto_and_override() -> None:
    # Default (no current_range_a) -> auto (None on IASettings).
    base = parse_plan(_minimal())[0]
    assert base.ia.current_range_a is None
    # Plan-level default + per-block override, accepting number or 'auto'.
    plan = _minimal()
    plan["defaults"] = {"current_range_a": 1.0e-6}
    plan["blocks"] = [
        {"name": "a", "type": "c-f", "bias": [0], "illumination": {"dark_only": True}},
        {"name": "b", "type": "c-f", "bias": [0], "current_range_a": 1.0e-7,
         "illumination": {"dark_only": True}},
        {"name": "c", "type": "c-f", "bias": [0], "current_range_a": "auto",
         "illumination": {"dark_only": True}},
    ]
    a, b, c = parse_plan(plan)
    assert a.ia.current_range_a == 1.0e-6   # plan default
    assert b.ia.current_range_a == 1.0e-7   # block override
    assert c.ia.current_range_a is None     # 'auto' -> None


def test_example_plans_load() -> None:
    for name in ("plan_2013-3_n-Si.yaml", "plan_2013-3_test.yaml"):
        p = EXAMPLE.parent / name
        if p.exists():
            configs = load_plan(p)
            assert configs, f"{name} produced no campaigns"


def test_oversampling_default_and_override() -> None:
    base = parse_plan(_minimal())[0]
    assert base.sweep.averaging_samples == 1  # default = none
    plan = _minimal()
    plan["defaults"] = {"oversampling": 50}
    plan["blocks"] = [
        {"name": "a", "type": "c-f", "bias": [0], "illumination": {"dark_only": True}},
        {"name": "b", "type": "c-f", "bias": [0], "oversampling": 200,
         "illumination": {"dark_only": True}},
        {"name": "e", "type": "c-v", "frequencies": [100], "oversampling": 300,
         "bias": {"start_v": -1, "stop_v": 1, "points": 11},
         "illumination": {"dark_only": True}},
    ]
    a, b, e = parse_plan(plan)
    assert a.sweep.averaging_samples == 50     # plan default
    assert b.sweep.averaging_samples == 200    # block override (c-f)
    assert e.cv_bias.averaging_samples == 300  # block override (c-v)
