"""Output-range sizing in MFIA.configure_impedance_for_cf.

Regression guard for the OVO-under-bias bug: auto-output sized the output range
to the AC amplitude and ignored the DC bias, so any bias over-ranged the output
stage (OVO) and the sweep read garbage. The fix sets an explicit output range
covering the block's worst-case |bias| + AC peak, with auto-output off.
"""

import math
from pathlib import Path

from mfia_ct.cf_plan import load_plan
from mfia_ct.hardware import MFIA

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
DEV = "dev9999"


class _FakeDaq:
    """Records the (node, value) pairs written via set()."""

    def __init__(self) -> None:
        self.settings: dict[str, object] = {}

    def set(self, items) -> None:
        for node, val in items:
            self.settings[node] = val

    def sync(self) -> None:
        pass


def _configure(cfg) -> dict:
    m = MFIA(DEV)
    m.daq = _FakeDaq()
    m.device = DEV
    m.configure_impedance_for_cf(cfg)
    return m.daq.settings


def test_cf_output_range_covers_bias_and_amp() -> None:
    """A -2 V C-f block must size the output range to >= |bias| + AC peak."""
    cfgs = load_plan(EXAMPLES / "plan_2013-3_test.yaml")
    biased = next(c for c in cfgs if -2.0 in c.bias.values_v)
    s = _configure(biased)

    amp_pk = biased.ia.ac_amplitude_v * math.sqrt(2.0)
    assert s[f"/{DEV}/imps/0/auto/output"] == 0           # not auto
    assert s[f"/{DEV}/sigouts/0/range"] >= 2.0 + amp_pk    # fits bias + signal
    assert s[f"/{DEV}/imps/0/auto/inputrange"] == 1        # input range stays auto


def test_cf_output_range_uses_block_worst_case_bias() -> None:
    """A bias-ladder block sizes to the largest |bias| it will visit (±4 V)."""
    cfgs = load_plan(EXAMPLES / "plan_2013-3_n-Si.yaml")
    ladder = next(c for c in cfgs if c.sweep_type.name == "C_F" and 4.0 in c.bias.values_v)
    s = _configure(ladder)
    amp_pk = ladder.ia.ac_amplitude_v * math.sqrt(2.0)
    assert s[f"/{DEV}/sigouts/0/range"] >= 4.0 + amp_pk


def test_cv_output_range_covers_swept_bias_span() -> None:
    """A C-V block sizes to the extreme of its swept bias range."""
    cfgs = load_plan(EXAMPLES / "plan_2013-3_n-Si.yaml")
    cv = next(c for c in cfgs if c.sweep_type.name == "C_V")
    s = _configure(cv)
    span = max(abs(cv.cv_bias.start_v), abs(cv.cv_bias.stop_v))
    assert s[f"/{DEV}/sigouts/0/range"] >= span
