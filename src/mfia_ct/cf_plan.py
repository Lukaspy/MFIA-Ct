"""Declarative YAML measurement-plan loader for mfia-cf.

A *plan* is a single YAML file that defines a whole multi-block measurement
campaign — device, shared instrument defaults, and an ordered list of blocks
(each a C-f or C-V campaign with its own illumination, bias/frequency axis,
and timing). :func:`load_plan` expands it into a list of :class:`CfConfig`,
one per block, ready to stage into the overnight queue.

The plan format is documented in ``docs/measurement-plan-format.md`` and a
worked example lives in ``examples/plan_blocks_A-E.yaml``. The short version::

    device:   {id, substrate, notes}
    output_dir: <folder>
    defaults: {amplitude_mv_rms, terminal_mode, equiv_circuit,
               device_settle_s, dark_settle_s, settling_tcs, auto_bandwidth,
               freq: {start_hz, stop_hz, points_per_decade, log_spacing}}
    blocks:
      - {name, type: c-f|c-v, <axis>, illumination: {...}, <overrides>}

This module only *builds configs*; it never talks to hardware. It is pure and
import-light (PyYAML + the existing config models), so it is fully unit-testable.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from .cf_config import (
    AmplitudeUnit,
    BiasSequence,
    BiasSweepSettings,
    CfConfig,
    IlluminationSequence,
    IlluminationStep,
    RunMetadata,
    SweeperSettings,
    SweepType,
)
from .config import EquivCircuit, IASettings, TerminalMode


class PlanError(ValueError):
    """Raised for any structural or semantic problem in a plan file."""


# --- Shared defaults --------------------------------------------------------


@dataclass
class _Defaults:
    """Plan-wide settings, overridable per block.

    ``device_settle_s`` is the short DC settle after a light or bias step (the
    operating point; fast for OGT). ``dark_settle_s`` is the dark-bracket dwell.
    ``settling_tcs`` is the per-point AC settling in demod time constants
    (instrument-set, unavoidable at low frequency). See the settle-timing notes
    in the format doc.
    """

    amplitude_mv_rms: float = 30.0
    # AC amplitude for LIT steps only (mV RMS); None = use amplitude_mv_rms.
    # Keep the high dark drive for the high-Z tail, drop it under bright light.
    light_amplitude_mv_rms: float | None = None
    terminal_mode: TerminalMode = TerminalMode.TWO_TERMINAL
    equiv_circuit: EquivCircuit = EquivCircuit.CP_RP
    device_settle_s: float = 10.0
    dark_settle_s: float = 10.0
    settling_tcs: float = 7.0
    auto_bandwidth: bool = True
    # Demod samples averaged per point ("oversampling"). 1 = none. ZI's high-Z
    # recipe uses ~800-2000 to pull a few-pA signal out of the noise at low f.
    oversampling: int = 1
    # Fixed current-input range in amps (e.g. 1.0e-7 for 100 nA); None / "auto"
    # = auto-range. Pin a sensitive range for high-Z / low-current sweeps.
    current_range_a: float | None = None
    # C-f frequency sweep floor/ceiling.
    start_hz: float = 1.0
    stop_hz: float = 300_000.0
    points_per_decade: int = 10
    log_spacing: bool = True


def _parse_current_range(value: Any, default: float | None) -> float | None:
    """YAML current-range value -> amps or None. Accepts a number (amps),
    or 'auto' / null for auto-range."""
    if value is None:
        return default
    if isinstance(value, str):
        if value.strip().lower() in ("auto", "none", ""):
            return None
        return float(value)
    return float(value)


def _enum_from(value: Any, enum_cls: type, field: str) -> Any:
    """Map a YAML string onto an enum by value or name, with a clear error."""
    if isinstance(value, enum_cls):
        return value
    for member in enum_cls:
        if value == member.value or str(value).lower() == member.name.lower():
            return member
    allowed = ", ".join(repr(m.value) for m in enum_cls)
    raise PlanError(f"{field}: {value!r} is not one of {allowed}")


def _merge_defaults(base: _Defaults, raw: dict[str, Any]) -> _Defaults:
    """Override ``base`` with a (possibly partial) ``defaults`` mapping."""
    if not isinstance(raw, dict):
        raise PlanError("'defaults' must be a mapping.")
    out = base
    freq = raw.get("freq", {}) or {}
    if not isinstance(freq, dict):
        raise PlanError("'defaults.freq' must be a mapping.")
    out = replace(
        out,
        amplitude_mv_rms=float(raw.get("amplitude_mv_rms", out.amplitude_mv_rms)),
        light_amplitude_mv_rms=(
            float(raw["light_amplitude_mv_rms"])
            if raw.get("light_amplitude_mv_rms") is not None
            else out.light_amplitude_mv_rms
        ),
        terminal_mode=_enum_from(
            raw.get("terminal_mode", out.terminal_mode), TerminalMode, "terminal_mode"
        ),
        equiv_circuit=_enum_from(
            raw.get("equiv_circuit", out.equiv_circuit), EquivCircuit, "equiv_circuit"
        ),
        device_settle_s=float(raw.get("device_settle_s", out.device_settle_s)),
        dark_settle_s=float(raw.get("dark_settle_s", out.dark_settle_s)),
        settling_tcs=float(raw.get("settling_tcs", out.settling_tcs)),
        auto_bandwidth=bool(raw.get("auto_bandwidth", out.auto_bandwidth)),
        oversampling=max(1, int(raw.get("oversampling", out.oversampling))),
        current_range_a=_parse_current_range(
            raw.get("current_range_a", out.current_range_a), out.current_range_a
        ),
        start_hz=float(freq.get("start_hz", out.start_hz)),
        stop_hz=float(freq.get("stop_hz", out.stop_hz)),
        points_per_decade=int(freq.get("points_per_decade", out.points_per_decade)),
        log_spacing=bool(freq.get("log_spacing", out.log_spacing)),
    )
    return out


def _block_defaults(base: _Defaults, block: dict[str, Any]) -> _Defaults:
    """A block may override any default inline (e.g. a longer settle for the
    deep-anchor block). ``freq`` overrides nest under ``freq:`` as in defaults."""
    keys = {
        "amplitude_mv_rms", "light_amplitude_mv_rms", "terminal_mode",
        "equiv_circuit", "device_settle_s", "dark_settle_s", "settling_tcs",
        "auto_bandwidth", "oversampling", "current_range_a", "freq",
    }
    inline = {k: block[k] for k in keys if k in block}
    return _merge_defaults(base, inline) if inline else base


# --- Illumination expansion -------------------------------------------------


def _dark_step(label: str, settle_s: float) -> IlluminationStep:
    return IlluminationStep(label, None, intensity_pct=0.0, settle_s=settle_s)


def _lit_step(wl: float, pct: float, settle_s: float) -> IlluminationStep:
    return IlluminationStep(
        f"{int(wl)}nm_{pct:g}pct", float(wl), intensity_pct=float(pct), settle_s=settle_s
    )


def _normalize_wavelengths(
    raw_wavelengths: Any, default_intensities: list[float] | None
) -> list[tuple[float, list[float]]]:
    """Return ``[(wl, [intensities...]), ...]``.

    Each wavelength is either a bare number (uses the block's ``intensities``)
    or a mapping ``{nm, intensities}`` for a per-wavelength override (e.g.
    anchor wavelengths get the full set, others get only 100 %).
    """
    if not isinstance(raw_wavelengths, list) or not raw_wavelengths:
        raise PlanError("illumination.wavelengths must be a non-empty list.")
    out: list[tuple[float, list[float]]] = []
    for w in raw_wavelengths:
        if isinstance(w, dict):
            if "nm" not in w:
                raise PlanError("each wavelength mapping needs an 'nm' key.")
            intensities = w.get("intensities", default_intensities)
        else:
            intensities = default_intensities
        if not intensities:
            raise PlanError(
                f"wavelength {w!r} has no intensities (set illumination.intensities "
                f"or a per-wavelength 'intensities')."
            )
        nm = float(w["nm"]) if isinstance(w, dict) else float(w)
        out.append((nm, [float(i) for i in intensities]))
    return out


def _order_wavelengths(
    items: list[tuple[float, list[float]]], order: str, seed: int
) -> list[tuple[float, list[float]]]:
    if order in ("as-listed", "listed", None):
        return items
    if order == "reversed":
        return list(reversed(items))
    if order in ("randomized", "random", "shuffled"):
        # Seeded so a plan is reproducible — the realized order is fixed once
        # the plan is loaded, and the same seed always gives the same order.
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        return shuffled
    raise PlanError(
        f"illumination.order {order!r} must be 'as-listed', 'reversed', or 'randomized'."
    )


def _expand_illumination(spec: Any, d: _Defaults) -> IlluminationSequence:
    """Build an :class:`IlluminationSequence` from an ``illumination`` mapping."""
    if not isinstance(spec, dict):
        raise PlanError("'illumination' must be a mapping.")

    if spec.get("dark_only"):
        return IlluminationSequence(steps=[_dark_step("dark", d.dark_settle_s)])

    mode = spec.get("mode")
    light_settle = float(spec.get("light_settle_s", d.device_settle_s))
    dark_settle = float(spec.get("dark_settle_s", d.dark_settle_s))

    if mode == "fixed":
        raw_steps = spec.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanError("illumination.mode 'fixed' needs a non-empty 'steps' list.")
        steps: list[IlluminationStep] = []
        for s in raw_steps:
            if s == "dark" or (isinstance(s, dict) and s.get("dark")):
                steps.append(_dark_step("dark", float(
                    s.get("settle_s", dark_settle) if isinstance(s, dict) else dark_settle
                )))
            elif isinstance(s, dict) and "wl" in s:
                steps.append(_lit_step(
                    s["wl"], s.get("intensity", 100.0),
                    float(s.get("settle_s", light_settle)),
                ))
            else:
                raise PlanError(
                    f"fixed step {s!r} must be 'dark' or a mapping with 'wl' (+ optional "
                    f"'intensity', 'settle_s')."
                )
        return IlluminationSequence(steps=steps)

    if mode == "matrix":
        items = _normalize_wavelengths(spec.get("wavelengths"), spec.get("intensities"))
        items = _order_wavelengths(
            items, spec.get("order", "as-listed"), int(spec.get("seed", 0))
        )
        bracket = spec.get("dark_bracket", "per-wavelength")
        if bracket is True:
            bracket = "per-wavelength"
        elif bracket is False or bracket is None:
            bracket = "none"
        if bracket not in ("per-wavelength", "interleaved", "ends", "none"):
            raise PlanError(
                f"illumination.dark_bracket {bracket!r} must be one of "
                f"'per-wavelength', 'interleaved', 'ends', 'none'."
            )
        steps = []
        if bracket == "ends":
            steps.append(_dark_step("dark_pre", dark_settle))
        for wl, intensities in items:
            if bracket == "per-wavelength":
                steps.append(_dark_step(f"dark_pre_{int(wl)}", dark_settle))
            for pct in intensities:
                steps.append(_lit_step(wl, pct, light_settle))
                if bracket == "interleaved":
                    steps.append(_dark_step(f"dark_post_{int(wl)}_{pct:g}pct", dark_settle))
            if bracket == "per-wavelength":
                steps.append(_dark_step(f"dark_post_{int(wl)}", dark_settle))
        if bracket == "ends":
            steps.append(_dark_step("dark_post", dark_settle))
        return IlluminationSequence(steps=steps)

    raise PlanError(
        f"illumination.mode {mode!r} must be 'matrix' or 'fixed' "
        f"(or set illumination.dark_only: true)."
    )


# --- Block → CfConfig -------------------------------------------------------


def _build_ia(d: _Defaults, init_freq_hz: float) -> IASettings:
    return IASettings(
        frequency_hz=init_freq_hz,
        ac_amplitude_v=d.amplitude_mv_rms / 1000.0,  # mV RMS → V RMS
        light_ac_amplitude_v=(
            None if d.light_amplitude_mv_rms is None
            else d.light_amplitude_mv_rms / 1000.0
        ),
        dc_bias_v=0.0,  # set per bias by the experiment loop
        equiv_circuit=d.equiv_circuit,
        terminal_mode=d.terminal_mode,
        imp_index=0,
        current_range_a=d.current_range_a,
    )


def _block_to_config(
    block: dict[str, Any], device: dict[str, Any], output_dir: str, base: _Defaults
) -> CfConfig:
    if not isinstance(block, dict):
        raise PlanError("each block must be a mapping.")
    d = _block_defaults(base, block)
    typ = str(block.get("type", "")).lower()
    illum = _expand_illumination(block.get("illumination", {}), d)
    run = RunMetadata(
        device_id=str(device.get("id", "")),
        substrate_type=str(device.get("substrate", "")),
        notes=str(device.get("notes", "")),
        output_dir=output_dir,
    )

    if typ in ("c-f", "cf"):
        bias_vals = block.get("bias")
        if not isinstance(bias_vals, list) or not bias_vals:
            raise PlanError("a c-f block needs a non-empty 'bias' list (volts).")
        ia = _build_ia(d, d.start_hz)
        sweep = SweeperSettings(
            start_hz=d.start_hz,
            stop_hz=d.stop_hz,
            points_per_decade=d.points_per_decade,
            log_spacing=d.log_spacing,
            settling_tcs=d.settling_tcs,
            auto_bandwidth=d.auto_bandwidth,
            averaging_samples=d.oversampling,
        )
        bias = BiasSequence(
            values_v=[float(b) for b in bias_vals], bias_settle_s=d.device_settle_s
        )
        return CfConfig(
            ia=ia,
            amplitude_unit=AmplitudeUnit.VRMS,
            sweep_type=SweepType.C_F,
            sweep=sweep,
            bias=bias,
            illumination=illum,
            run=run,
        )

    if typ in ("c-v", "cv"):
        freqs = block.get("frequencies")
        if not isinstance(freqs, list) or not freqs:
            raise PlanError("a c-v block needs a non-empty 'frequencies' list (Hz).")
        bias = block.get("bias")
        if not isinstance(bias, dict):
            raise PlanError(
                "a c-v block needs 'bias: {start_v, stop_v, points}' (the swept axis)."
            )
        for key in ("start_v", "stop_v", "points"):
            if key not in bias:
                raise PlanError(f"c-v bias is missing '{key}'.")
        cv_freqs = [float(f) for f in freqs]
        cv_bias = BiasSweepSettings(
            start_v=float(bias["start_v"]),
            stop_v=float(bias["stop_v"]),
            n_points=int(bias["points"]),
            settling_tcs=d.settling_tcs,
            auto_bandwidth=d.auto_bandwidth,
            averaging_samples=d.oversampling,
        )
        ia = _build_ia(d, cv_freqs[0])
        return CfConfig(
            ia=ia,
            amplitude_unit=AmplitudeUnit.VRMS,
            sweep_type=SweepType.C_V,
            cv_frequencies_hz=cv_freqs,
            cv_bias=cv_bias,
            illumination=illum,
            run=run,
        )

    raise PlanError(f"block 'type' must be 'c-f' or 'c-v', got {block.get('type')!r}.")


# --- Public API -------------------------------------------------------------


def parse_plan(raw: dict[str, Any]) -> list[CfConfig]:
    """Expand an already-parsed plan mapping into a list of CfConfig."""
    if not isinstance(raw, dict):
        raise PlanError("plan must be a mapping at the top level.")
    device = raw.get("device", {}) or {}
    if not isinstance(device, dict):
        raise PlanError("'device' must be a mapping.")
    if not device.get("id"):
        raise PlanError("'device.id' is required (used in every output filename).")
    output_dir = str(raw.get("output_dir", "") or "")
    if not output_dir:
        raise PlanError("'output_dir' is required (destination folder for CSVs).")
    defaults = _merge_defaults(_Defaults(), raw.get("defaults", {}) or {})
    blocks = raw.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise PlanError("'blocks' must be a non-empty list.")

    configs: list[CfConfig] = []
    for i, block in enumerate(blocks):
        name = block.get("name", "?") if isinstance(block, dict) else "?"
        try:
            configs.append(_block_to_config(block, device, output_dir, defaults))
        except PlanError as e:
            raise PlanError(f"block {i + 1} ({name}): {e}") from e
    return configs


def load_plan(path: str | Path) -> list[CfConfig]:
    """Read and expand a YAML plan file into a list of CfConfig (one per block).

    Raises:
        PlanError: for a missing file, malformed YAML, or any schema problem
            (the message names the offending block and field).
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise PlanError(f"cannot read plan file {p}: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PlanError(f"invalid YAML in {p}: {e}") from e
    return parse_plan(raw)


__all__ = ["PlanError", "load_plan", "parse_plan"]
