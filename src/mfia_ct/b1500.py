"""Keysight B1500A backend over VISA (GPIB), FLEX/GPIB command set.

Implements the same ``_Backend`` surface the MFIA does — ``configure_impedance_for_cf``,
``set_dc_bias``, ``set_amplitude``, ``run_sweep`` (C-f), ``run_bias_sweep`` (C-V) —
plus ``run_iv_sweep`` (SMU I-V, which the MFIA cannot do). So the same
``CfExperiment`` bias × illumination × LED loop drives the B1500 unchanged, and
its C-f / C-V land in the identical ``SweepResult`` → CSV format as the MFIA
(free MFIA/B1500 merge).

Transport: the B1500A exposes the FLEX command set over **GPIB** (default
address 17) and USB — NOT a native LAN socket. So this driver takes a plain
VISA resource string (``GPIB0::17::INSTR`` by default); LAN only works through a
GPIB↔LAN gateway or an IO-Libraries remote-GPIB proxy, which is still just a
different resource string. Requires ``pyvisa`` + a VISA backend (NI-VISA or
``pyvisa-py``) — the ``hardware`` optional-dependency set.

Command reference (opcodes, parameter orders, MM/IMP/FMT codes) is transcribed
from the B1500 Series Programming Guide; see the cookbook in the repo history.
Two design choices keep the ASCII data-parsing robust despite hardware I can't
unit-test the wire format against:

  1. **Regenerate the swept axis** (frequency / bias / voltage) from the
     commanded grid rather than parse the source values back — the source and
     the DC monitor share the ``V`` data-type letter, so parsing is ambiguous;
     the commanded grid is not.
  2. **Select measured values by data-type letter** (``C`` = capacitance,
     ``Y`` = conductance, ``I`` = current) rather than by fixed field position,
     so the parser tolerates whichever monitor/time fields are enabled.

``FMT 1`` (ASCII, with the 3-char status/channel/type header, comma-separated,
CR/LF-terminated block) is used throughout so one read returns the whole block
and ``str.split(',')`` parses it.

NOTE (bench bring-up): the exact XE→read→OPC handshake and the module slot
numbers depend on the instrument; both are isolated (``_measure``,
``_discover_slots``) and overridable via the constructor so they can be pinned
at the bench without touching the sweep logic.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Optional

import numpy as np

from .cf_config import BiasSweepSettings, CfConfig, IvSweepSettings, SweeperSettings, SweepType

# MFCMU hard limits (Programming Guide): AC OSC level 0–250 mV in 1 mV steps;
# frequency 1 kHz – 5 MHz. We clamp to these so an out-of-range plan value can't
# silently error the instrument mid-campaign.
_ACV_MAX_V = 0.25
_CMU_FREQ_MIN_HZ = 1_000.0
_CMU_FREQ_MAX_HZ = 5_000_000.0

# IMP measurement-parameter codes (Table 4-16): Cp-G is what the analysis wants
# (parallel capacitance + conductance), which maps cleanly to Y = G + jωCp.
_IMP_CP_G = 100

# MM measurement-mode codes: 17 spot C, 18 CV (DC-bias) sweep, 22 C-f sweep,
# 2 SMU staircase sweep.
_MM_CV_SWEEP = 18
_MM_CF_SWEEP = 22
_MM_IV_SWEEP = 2


class B1500Error(RuntimeError):
    """A non-zero entry drained from the B1500 error queue (``ERRX?``)."""


class B1500:
    # Analyzer identity stamped into every sweep's metadata / filename.
    instrument = "B1500"
    # ``device`` mirrors the MFIA attribute the experiment logs; not a real id.
    device = "b1500"

    def __init__(
        self,
        resource: str = "GPIB0::18::INSTR",
        *,
        timeout_ms: int = 120_000,
        cmu_channel: Optional[int] = None,
        smu_channel: Optional[int] = None,
        inst: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        resource : VISA resource string (default the B1500's GPIB address 17).
        timeout_ms : VISA timeout; sweeps can take tens of seconds, so it's
            large by default.
        cmu_channel / smu_channel : FLEX channel (slot) numbers for the MFCMU
            and the SMU. ``None`` → auto-discover from ``UNT?`` on connect;
            pass explicitly to override if discovery can't identify them.
        inst : a pre-opened VISA instrument (duck-typed: ``write``/``query``/
            ``read`` + ``timeout``/terminator attrs). Lets tests inject a fake
            without pyvisa; production leaves it ``None`` and ``connect`` opens
            the real session.
        """
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._cmu = cmu_channel
        self._smu = smu_channel
        self._inst = inst
        self._rm: Any = None
        # Operating point held between calls (the experiment sets bias/amplitude
        # via set_dc_bias/set_amplitude, then calls a run_* method).
        self._bias_v: float = 0.0
        self._amp_vrms: float = 0.03
        self._cfg: Optional[CfConfig] = None

    # ---- Session lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Open the VISA session (unless one was injected) and initialize."""
        if self._inst is None:
            import pyvisa

            self._rm = pyvisa.ResourceManager()
            self._inst = self._rm.open_resource(self.resource)
        self._inst.timeout = self.timeout_ms
        # FMT 1 → CR/LF-terminated block; one read + split(',') parses it.
        self._inst.write_termination = "\n"
        self._inst.read_termination = "\n"

        # Safe known state. NOTE: the B1500 FLEX set does NOT implement the
        # 488.2 common commands *RST / *CLS (they return "+100 Undefined GPIB
        # command." — verified on B1500A firmware A.06.02) even though *IDN? /
        # *OPC? / ERRX? do work. `CL` (disable all channel outputs) is the FLEX
        # equivalent for getting to a safe, outputs-off starting point.
        self._write("CL")
        self.idn()
        self._discover_slots()
        # ASCII, with header, source value appended — set once up front.
        self._write("FMT 1,1")
        self._check_err()

    def disconnect(self) -> None:
        # Safe the instrument (all channel outputs off) before closing.
        if self._inst is not None:
            try:
                self._inst.write("CL")  # disable all channels
            except Exception:
                pass
            try:
                self._inst.close()
            except Exception:
                pass
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
        self._inst = None
        self._rm = None

    def __enter__(self) -> "B1500":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.disconnect()

    def idn(self) -> str:
        return str(self._inst.query("*IDN?")).strip()

    # ---- Low-level VISA I/O ------------------------------------------------

    def _write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def _query(self, cmd: str) -> str:
        return str(self._inst.query(cmd)).strip()

    def _check_err(self) -> None:
        """Drain the richer error queue (``ERRX?``); raise on a non-zero code.

        ``ERRX?`` returns ``code,"message; SLOTn"`` (``0,"No Error."`` clean).
        """
        try:
            resp = self._query("ERRX?")
        except Exception:
            return  # never let the error check itself kill a run
        if resp and not resp.startswith("0,") and not resp.startswith("+0,"):
            raise B1500Error(f"B1500: {resp}")

    def _discover_slots(self) -> None:
        """Fill in CMU / SMU channel numbers from ``UNT?`` if not given.

        ``UNT?`` lists installed modules as ``model,rev;model,rev;...`` in slot
        order (slot = FLEX channel). We pick the first CMU-like and SMU-like
        module. Any explicit channel passed to the constructor wins. If nothing
        matches, the channels stay ``None`` and a run raises a clear error
        rather than driving the wrong slot.
        """
        if self._cmu is not None and self._smu is not None:
            return
        try:
            resp = self._query("UNT? 0")  # ";"-joined "model,rev" per slot
        except Exception:
            return
        entries = [e for e in resp.replace("\n", ";").split(";") if e.strip()]
        for slot, entry in enumerate(entries, start=1):
            model = entry.split(",")[0].upper()
            is_cmu = "CMU" in model or "B1520" in model
            # SMU modules are the B1510/B1511/B1517 (HR/MP/HP-SMU) family.
            is_smu = "SMU" in model or model.startswith(("B151", "B152"))
            if self._cmu is None and is_cmu:
                self._cmu = slot
            elif self._smu is None and is_smu:
                self._smu = slot

    def _require_cmu(self) -> int:
        if self._cmu is None:
            raise B1500Error(
                "No MFCMU channel — pass cmu_channel=... (auto-discovery from "
                "UNT? found none)."
            )
        return self._cmu

    def _require_smu(self) -> int:
        if self._smu is None:
            raise B1500Error(
                "No SMU channel — pass smu_channel=... (auto-discovery from "
                "UNT? found none)."
            )
        return self._smu

    # ---- Operating-point setters (called by the experiment loop) -----------

    def configure_impedance_for_cf(self, cfg: CfConfig) -> None:
        """Record the config and set the CMU baseline for a C-f / C-V block.

        The per-sweep methods issue the full command sequence themselves
        (CN/MM/IMP/…); this just captures the operating point (initial AC level
        and the model) so a first sweep has sane state. For an I_V block it is
        essentially a no-op — the SMU sequence is fully self-contained.
        """
        self._cfg = cfg
        self._bias_v = float(cfg.ia.dc_bias_v)
        self._amp_vrms = min(_ACV_MAX_V, float(cfg.ia.ac_amplitude_v))
        # Disable all channel outputs at each block's start. On a mainframe with
        # an SCUU (the SMU-CMU switcher), this lets the switch route cleanly when
        # a plan mixes CMU blocks (C-f/C-V) and SMU blocks (I-V): otherwise the
        # prior block's channel stays enabled and both contend for the switch.
        self._write("CL")
        self._check_err()

    def set_dc_bias(self, bias_v: float) -> None:
        """Hold the DC bias for the next C-f sweep (applied as ``DCV`` there).

        The MFCMU sweep sequence re-issues ``DCV`` with this value, so storing
        it here is enough; we don't drive a bias between sweeps.
        """
        self._bias_v = float(bias_v)

    def set_amplitude(self, v_rms: float) -> None:
        """Set the AC OSC level for subsequent sweeps (V RMS, clamped ≤250 mV).

        The MFCMU ``ACV`` node is already in V RMS, so — unlike the MFIA — there
        is no √2 conversion. Values above the 250 mV hardware ceiling are
        clamped (the plan's lit-step drop-down never approaches it, but a
        fat-fingered dark amplitude would otherwise error the instrument).
        """
        self._amp_vrms = min(_ACV_MAX_V, max(0.0, float(v_rms)))

    # ---- Sweeps ------------------------------------------------------------

    def run_sweep(
        self,
        cfg: SweeperSettings,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """C-f: MFCMU frequency staircase at the held DC bias. Returns
        ``(frequency_hz, z_real, z_imag)`` so the ω-math in ``SweepResult``
        recovers Cp/Rp exactly as for the MFIA."""
        cmu = self._require_cmu()
        if stop_check is not None and stop_check():
            return np.array([]), np.array([]), np.array([])

        start = max(_CMU_FREQ_MIN_HZ, min(_CMU_FREQ_MAX_HZ, float(cfg.start_hz)))
        stop = max(_CMU_FREQ_MIN_HZ, min(_CMU_FREQ_MAX_HZ, float(cfg.stop_hz)))
        n = cfg.n_points
        log = bool(cfg.log_spacing)
        freq = (
            np.logspace(math.log10(start), math.log10(stop), n)
            if log else np.linspace(start, stop, n)
        )
        mode = 2 if log else 1  # WFC: 2 = log single, 1 = linear single

        if progress_cb is not None:
            progress_cb(0.0)
        self._write("CN {}".format(cmu))
        self._write("MM {},{}".format(_MM_CF_SWEEP, cmu))
        self._write("IMP {}".format(_IMP_CP_G))
        self._write("ACV {},{:.6g}".format(cmu, self._amp_vrms))
        self._write("DCV {},{:.6g}".format(cmu, self._bias_v))
        self._write("WMFC 2,1")  # auto-abort on, return to start after
        self._write("WFC {},{},{:.9g},{:.9g},{}".format(cmu, mode, start, stop, n))
        self._write("RC {},0".format(cmu))  # auto range
        cp, g = self._measure_cp_g(n)
        if progress_cb is not None:
            progress_cb(1.0)

        z_real, z_imag = _cp_g_to_z(freq, cp, g)
        return freq, z_real, z_imag

    def run_bias_sweep(
        self,
        cv: BiasSweepSettings,
        fixed_freq_hz: float,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """C-V: MFCMU DC-bias staircase at a fixed test frequency. Returns
        ``(bias_v, z_real, z_imag)``."""
        cmu = self._require_cmu()
        if stop_check is not None and stop_check():
            return np.array([]), np.array([]), np.array([])

        n = max(2, int(cv.n_points))
        bias = np.linspace(cv.start_v, cv.stop_v, n)
        freq = max(_CMU_FREQ_MIN_HZ, min(_CMU_FREQ_MAX_HZ, float(fixed_freq_hz)))

        if progress_cb is not None:
            progress_cb(0.0)
        self._write("CN {}".format(cmu))
        self._write("MM {},{}".format(_MM_CV_SWEEP, cmu))
        self._write("IMP {}".format(_IMP_CP_G))
        self._write("FC {},{:.9g}".format(cmu, freq))
        self._write("ACV {},{:.6g}".format(cmu, self._amp_vrms))
        self._write("WMDCV 2,1")  # auto-abort on, return to start
        self._write("WDCV {},1,{:.6g},{:.6g},{}".format(cmu, cv.start_v, cv.stop_v, n))
        self._write("RC {},0".format(cmu))
        cp, g = self._measure_cp_g(n)
        if progress_cb is not None:
            progress_cb(1.0)

        z_real, z_imag = _cp_g_to_z(np.full(n, freq), cp, g)
        return bias, z_real, z_imag

    def run_iv_sweep(
        self,
        iv: IvSweepSettings,
        *,
        progress_cb: Optional[Callable[[float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        light_active: bool = False,
    ) -> tuple[np.ndarray, np.ndarray]:
        """I-V: SMU voltage staircase, measure current. Returns ``(V, I)``.

        ``double_sweep`` runs the leg forward-then-reverse (WV mode 3), so the
        instrument returns ``2 × n_points`` measurements; the voltage axis is
        regenerated to match from ``iv.values_v``.
        """
        smu = self._require_smu()
        if stop_check is not None and stop_check():
            return np.array([]), np.array([])

        v = np.asarray(iv.values_v, dtype=float)
        leg_n = max(2, int(iv.n_points))
        if iv.double_sweep:
            mode = 4 if iv.log_spacing else 3
        else:
            mode = 2 if iv.log_spacing else 1
        icomp = abs(float(iv.i_compliance_a))

        if progress_cb is not None:
            progress_cb(0.0)
        self._write("CN {}".format(smu))
        self._write("MM {},{}".format(_MM_IV_SWEEP, smu))
        self._write("CMM {},1".format(smu))  # measure current
        if iv.measure_range_a is None:
            self._write("RI {},0".format(smu))  # auto current-measure range
        # WT hold,delay
        self._write("WT {:.6g},{:.6g}".format(iv.hold_s, iv.delay_s))
        self._write("WM 2,1")  # auto-abort on, post = start value
        self._write(
            "WV {},{},0,{:.6g},{:.6g},{},{:.6g}".format(
                smu, mode, iv.start_v, iv.stop_v, leg_n, icomp
            )
        )
        current = self._measure_current(len(v))
        if progress_cb is not None:
            progress_cb(1.0)
        return v, current

    # ---- Measurement execution + ASCII parsing -----------------------------

    def _measure(self) -> str:
        """Trigger a configured measurement and return the raw ASCII block.

        ``XE`` starts it; a bare ``read`` then blocks until the instrument
        finishes and sends the CR/LF-terminated, comma-separated data block
        (VISA timeout must exceed the sweep duration). ``ERRX?`` verifies after.
        Verified on B1500A fw A.06.02: ``XE`` → ``read`` is the working pattern;
        an intervening ``*OPC?`` is NOT needed (and the B1500 has no ``*RST`` —
        see ``connect``). Isolated here so this handshake can be adjusted at
        bring-up without touching sweep logic.
        """
        self._write("XE")
        data = str(self._inst.read())  # blocks until the measurement data arrives
        self._check_err()
        return data

    def _measure_cp_g(self, n_expected: int) -> tuple[np.ndarray, np.ndarray]:
        """Run a CMU sweep and return (Cp, G) arrays of length ``n_expected``."""
        raw = self._measure()
        tuples = _parse_ascii_block(raw)
        cp = np.array([v for (_s, _c, d, v) in tuples if d == "C"], dtype=float)
        g = np.array([v for (_s, _c, d, v) in tuples if d in ("Y", "G")], dtype=float)
        _check_len("CMU sweep", cp, g, n_expected)
        return cp, g

    def _measure_current(self, n_expected: int) -> np.ndarray:
        """Run an SMU sweep and return the measured current array."""
        raw = self._measure()
        tuples = _parse_ascii_block(raw)
        cur = np.array([v for (_s, _c, d, v) in tuples if d == "I"], dtype=float)
        if cur.size != n_expected:
            raise B1500Error(
                f"SMU sweep returned {cur.size} current points, expected "
                f"{n_expected}. Raw head: {raw[:120]!r}"
            )
        return cur


# --- Free helpers (unit-tested independently of any instrument) -------------


def _cp_g_to_z(
    freq_hz: np.ndarray, cp_f: np.ndarray, g_s: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Convert parallel Cp + G to complex impedance real/imag parts.

    Y = G + jωCp ; Z = 1/Y. Returning Z real/imag keeps the B1500 data flowing
    through the exact same ``SweepResult`` ω-math (Cp = B/ω, Rp = 1/G) the MFIA
    path uses, so both instruments produce byte-identical CSV columns.
    """
    omega = 2.0 * math.pi * np.asarray(freq_hz, dtype=float)
    y = np.asarray(g_s, dtype=float) + 1j * omega * np.asarray(cp_f, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(y != 0, 1.0 / y, np.nan + 0j)
    return z.real, z.imag


def _parse_ascii_block(raw: str) -> list[tuple[str, str, str, float]]:
    """Parse a FLEX ``FMT 1`` ASCII block into (status, channel, dtype, value).

    Each comma-separated field is ``<status><channel><dtype><±d.ddddddE±dd>``
    with a 3-letter header when the header is enabled. We split off the leading
    run of letters as the header (status, channel, data-type) and parse the
    remainder as the float. Over-range points carry the dummy 199.999E+99 and a
    ``V`` status — those are mapped to NaN so they can't masquerade as data.
    """
    out: list[tuple[str, str, str, float]] = []
    for tok in raw.replace("\r", "").replace("\n", "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        j = 0
        while j < len(tok) and tok[j].isalpha():
            j += 1
        head, num = tok[:j], tok[j:]
        status = head[0] if len(head) >= 1 else ""
        channel = head[1] if len(head) >= 2 else ""
        dtype = head[2] if len(head) >= 3 else ""
        try:
            val = float(num)
        except ValueError:
            continue
        # Over-range / aborted dummy value -> NaN.
        if abs(val) >= 1e99:
            val = float("nan")
        out.append((status, channel, dtype, val))
    return out


def _check_len(what: str, a: np.ndarray, b: np.ndarray, n: int) -> None:
    if a.size != n or b.size != n:
        raise B1500Error(
            f"{what} returned mismatched data (got {a.size}/{b.size}, "
            f"expected {n}); check the FMT/IMP/LMN state."
        )
