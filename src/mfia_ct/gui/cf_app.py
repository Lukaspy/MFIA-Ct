"""PyQt6 GUI for the automated C-f / IS campaign tool.

Layout:

    +-----------------+--------------------------------------+
    | Instrument      |   Live Bode plot                     |
    | IA settings     |   |Z| (top)  /  phase (bottom)       |
    | Sweeper         |                                      |
    | Bias list       |                                      |
    | Illumination    |                                      |
    |  - Channels tab |                                      |
    |  - Sequence tab |                                      |
    | Output / Notes  +--------------------------------------+
    | Run / Stop      |   Progress: bias k/N  illum j/M  ... |
    +-----------------+--------------------------------------+

Acquisition runs in a QThread that yields ``SweepResult`` per finished
sweep. The main thread writes each result to CSV, drops the trace on
the plot, and updates the progress bar.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..cf_config import (
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
from ..cf_experiment import CfExperiment
from ..cf_storage import SweepResult, make_filename, write_sweep_csv
from ..config import (
    TERMINAL_BIAS_LIMIT_V,
    EquivCircuit,
    IASettings,
    TerminalMode,
)
from .instrument import BackendKind, InstrumentPanel

LedFactory = Callable[[], object]


# Wavelengths offered by the multi-select / sequence builder, in
# measurement order (UV→IR). The LED driver addresses by wavelength, so
# physical channel index doesn't matter here. 740 & 850 nm are below the
# Ge2Se3 film gap but above Si's — the substrate-only mechanism probe.
DEFAULT_CHANNEL_WAVELENGTHS = [385.0, 470.0, 505.0, 530.0, 590.0, 625.0, 740.0, 850.0]


# Minimum width for numeric entry boxes. Without it, form layouts under some
# Qt styles shrink spin boxes to a cramped size-hint that clips the value and
# its suffix against the stepper arrows (e.g. "30.0 s").
_SPIN_MIN_WIDTH = 120


def _spin(value: float, lo: float, hi: float, step: float, decimals: int = 6) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setMinimumWidth(_SPIN_MIN_WIDTH)
    return s


def _int_spin(value: int, lo: int, hi: int) -> QSpinBox:
    s = QSpinBox()
    s.setMinimumWidth(_SPIN_MIN_WIDTH)
    s.setRange(lo, hi)
    s.setValue(value)
    return s


def _fmt_si_amps(a: float) -> str:
    """Compact current label for the range dropdown, e.g. 1e-8 -> '10 nA'."""
    for scale, unit in ((1e-3, "mA"), (1e-6, "µA"), (1e-9, "nA")):
        if a >= scale:
            return f"{a / scale:g} {unit}"
    return f"{a:g} A"


# --- Worker ------------------------------------------------------------------


class CfWorker(QObject):
    sweep_done = pyqtSignal(object)  # SweepResult
    progress = pyqtSignal(int, int, float)  # bias_i, step_i, frac
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, backend, cfg: CfConfig, led=None) -> None:
        super().__init__()
        self.backend = backend
        self.cfg = cfg
        self.led = led
        self._exp: Optional[CfExperiment] = None

    def stop(self) -> None:
        if self._exp is not None:
            self._exp.stop()

    def run(self) -> None:
        try:
            self._exp = CfExperiment(
                self.backend,
                self.cfg,
                led=self.led,
                progress_cb=lambda b, s, f: self.progress.emit(b, s, f),
            )
            for result in self._exp.run():
                self.sweep_done.emit(result)
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class CfQueueWorker(QObject):
    """Runs a list of campaigns back-to-back on one thread for unattended
    (overnight) operation.

    Each queued ``CfConfig`` is run by its own ``CfExperiment`` in sequence,
    reusing the already-connected MFIA backend and a fresh LED source per
    campaign. One campaign failing does **not** abort the queue — the error is
    reported via ``campaign_error`` and the worker moves on, so a single bad
    config (or a transient LED hiccup) can't waste the whole night. ``stop()``
    halts the current campaign and the rest of the queue.
    """

    sweep_done = pyqtSignal(object, int)      # SweepResult, campaign index
    progress = pyqtSignal(int, int, float)    # bias_i, step_i, frac (current campaign)
    campaign_started = pyqtSignal(int, int)   # index, total
    campaign_done = pyqtSignal(int)           # index (completed without error)
    campaign_error = pyqtSignal(int, str)     # index, message (campaign skipped)
    finished = pyqtSignal()

    def __init__(self, backend, configs: list[CfConfig], led_factory=None) -> None:
        super().__init__()
        self.backend = backend
        self.configs = configs
        self.led_factory = led_factory
        self._exp: Optional[CfExperiment] = None
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True
        if self._exp is not None:
            self._exp.stop()

    def run(self) -> None:
        try:
            total = len(self.configs)
            for i, cfg in enumerate(self.configs):
                if self._stopped:
                    break
                self.campaign_started.emit(i, total)
                led = self.led_factory() if self.led_factory else None
                try:
                    self._exp = CfExperiment(
                        self.backend,
                        cfg,
                        led=led,
                        progress_cb=lambda b, s, f: self.progress.emit(b, s, f),
                    )
                    for result in self._exp.run():
                        self.sweep_done.emit(result, i)
                except Exception as e:
                    self.campaign_error.emit(i, f"{type(e).__name__}: {e}")
                else:
                    if not self._stopped:
                        self.campaign_done.emit(i)
                finally:
                    self._exp = None
        finally:
            self.finished.emit()


# --- Illumination editor ----------------------------------------------------


class IlluminationEditor(QWidget):
    """Two views of the same illumination sequence:

    - **Channels tab**: pick wavelengths via checkbox; per-channel intensity
      (%) and a single global settle. "Interleave dark" auto-builds the
      dark-pre and dark-post-N pattern. This is the common case.
    - **Sequence tab**: explicit table — one row per step, edit label /
      wavelength / intensity / settle. Lets you randomize order, repeat
      channels, or assign per-step settle times.

    The Channels tab is authoritative until the user edits the Sequence
    table; once they do, the table view wins (a "Regenerate from channels"
    button rebuilds from the channel state if they want to start over).
    """

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        # Keep the tabbed editor tall enough that the channel checkboxes and
        # the sequence table stay usable inside the scrolling control column.
        self.setMinimumHeight(300)
        tabs = QTabWidget()

        # ---- Channels tab ----
        chan = QWidget()
        chan_layout = QVBoxLayout(chan)

        self.dark_pre = QCheckBox("Dark pre-baseline")
        self.dark_pre.setChecked(True)
        self.dark_post = QCheckBox("Interleave dark between channels")
        self.dark_post.setChecked(True)
        chan_layout.addWidget(self.dark_pre)
        chan_layout.addWidget(self.dark_post)

        ch_box = QGroupBox("LED channels (by wavelength)")
        ch_form = QFormLayout(ch_box)
        self.channel_rows: list[tuple[float, QCheckBox, QDoubleSpinBox]] = []
        for wl in DEFAULT_CHANNEL_WAVELENGTHS:
            cb = QCheckBox(f"{int(wl)} nm")
            cb.setChecked(True)
            pct = _spin(100.0, 0.0, 100.0, 1.0, decimals=1)
            pct.setSuffix(" %")
            row = QHBoxLayout()
            row.addWidget(cb, stretch=1)
            row.addWidget(pct)
            holder = QWidget()
            holder.setLayout(row)
            ch_form.addRow(holder)
            self.channel_rows.append((wl, cb, pct))
            cb.toggled.connect(self._emit_changed)
            pct.valueChanged.connect(self._emit_changed)
        chan_layout.addWidget(ch_box)

        settle_box = QGroupBox("Settle times")
        settle_form = QFormLayout(settle_box)
        self.light_settle = _spin(30.0, 0.0, 3600.0, 1.0, decimals=1)
        self.light_settle.setSuffix(" s")
        self.dark_settle = _spin(60.0, 0.0, 3600.0, 1.0, decimals=1)
        self.dark_settle.setSuffix(" s")
        settle_form.addRow("Lit step settle", self.light_settle)
        settle_form.addRow("Dark step settle", self.dark_settle)
        chan_layout.addWidget(settle_box)
        self.light_settle.valueChanged.connect(self._emit_changed)
        self.dark_settle.valueChanged.connect(self._emit_changed)

        regen = QPushButton("Regenerate sequence from channels")
        regen.clicked.connect(self._regenerate_table)
        chan_layout.addWidget(regen)
        chan_layout.addStretch()

        # ---- Sequence tab (free-form table) ----
        seq = QWidget()
        seq_layout = QVBoxLayout(seq)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Label", "Wavelength (nm, blank=dark)", "Intensity (%)", "Settle (s)"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(180)
        seq_layout.addWidget(self.table)
        btns = QHBoxLayout()
        add_btn = QPushButton("+ row")
        del_btn = QPushButton("− row")
        add_btn.clicked.connect(lambda: self._add_row("step", None, 0.0, 30.0))
        del_btn.clicked.connect(self._delete_selected_rows)
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addStretch()
        seq_layout.addLayout(btns)

        # ---- Intensity-series tab (linearity check + full λ × I matrix) ----
        isr = QWidget()
        isr_form = QFormLayout(isr)
        self.isr_all_channels = QCheckBox(
            "All checked channels (λ × intensity matrix)"
        )
        self.isr_all_channels.setToolTip(
            "Step every wavelength ticked on the Channels tab through the drive\n"
            "points below — the complete wavelength × intensity campaign. When\n"
            "off, only the single wavelength selected here is stepped."
        )
        self.isr_wavelength = QComboBox()
        for wl in DEFAULT_CHANNEL_WAVELENGTHS:
            self.isr_wavelength.addItem(f"{int(wl)} nm", userData=wl)
        self.isr_drive_points = QLineEdit("10,20,40,70,100")
        self.isr_light_settle = _spin(30.0, 0.0, 3600.0, 1.0, decimals=1)
        self.isr_light_settle.setSuffix(" s")
        self.isr_dark_settle = _spin(60.0, 0.0, 3600.0, 1.0, decimals=1)
        self.isr_dark_settle.setSuffix(" s")
        self.isr_interleave_dark = QCheckBox("Interleave dark between levels")
        self.isr_interleave_dark.setChecked(True)
        isr_build = QPushButton("Build → Sequence")
        isr_build.clicked.connect(self._build_intensity_series)
        isr_form.addRow(
            QLabel(
                "Step drive levels to test photo-response linearity in flux.\n"
                "One wavelength, or tick 'All checked channels' for the full\n"
                "wavelength × intensity matrix. Builds into the Sequence tab;\n"
                "runs at each bias / test frequency under the C-f / C-V loop."
            )
        )
        isr_form.addRow(self.isr_all_channels)
        isr_form.addRow("Wavelength", self.isr_wavelength)
        isr_form.addRow("Drive points (%)", self.isr_drive_points)
        isr_form.addRow("Lit settle", self.isr_light_settle)
        isr_form.addRow("Dark settle", self.isr_dark_settle)
        isr_form.addRow(self.isr_interleave_dark)
        isr_form.addRow(isr_build)
        # The single-wavelength picker is irrelevant in matrix mode.
        self.isr_all_channels.toggled.connect(
            lambda on: self.isr_wavelength.setEnabled(not on)
        )

        self._tabs = tabs
        self._seq_tab_index = 1
        tabs.addTab(chan, "Channels")
        tabs.addTab(seq, "Sequence")
        tabs.addTab(isr, "Intensity series")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(tabs)

        self._regenerate_table()

    def _build_intensity_series(self) -> None:
        from PyQt6.QtWidgets import QMessageBox

        try:
            pts = [float(p) for p in self.isr_drive_points.text().split(",") if p.strip()]
        except ValueError as e:
            QMessageBox.critical(self, "Intensity series", f"Bad drive points:\n{e}")
            return
        if len(pts) < 2:
            QMessageBox.warning(
                self, "Intensity series", "Need at least 2 drive points (e.g. 10,100)."
            )
            return

        if self.isr_all_channels.isChecked():
            wavelengths = [wl for wl, cb, _pct in self.channel_rows if cb.isChecked()]
            if not wavelengths:
                QMessageBox.warning(
                    self,
                    "Intensity series",
                    "Tick at least one wavelength on the Channels tab for the matrix.",
                )
                return
            seq = IlluminationSequence.wavelength_intensity_matrix(
                wavelengths,
                pts,
                interleave_dark=self.isr_interleave_dark.isChecked(),
                light_settle_s=self.isr_light_settle.value(),
                dark_settle_s=self.isr_dark_settle.value(),
            )
        else:
            seq = IlluminationSequence.intensity_series(
                self.isr_wavelength.currentData(),
                pts,
                interleave_dark=self.isr_interleave_dark.isChecked(),
                light_settle_s=self.isr_light_settle.value(),
                dark_settle_s=self.isr_dark_settle.value(),
            )
        self.table.setRowCount(0)
        for s in seq.steps:
            self._add_row(s.label, s.wavelength_nm, s.intensity_pct, s.settle_s)
        # Switch to the Sequence tab, which is authoritative for the run.
        self._tabs.setCurrentIndex(self._seq_tab_index)

    def _emit_changed(self) -> None:
        self.changed.emit()
        # When the Channels tab is edited, mirror to the table view so the
        # user sees the up-to-date sequence if they flip tabs.
        self._regenerate_table()

    def _regenerate_table(self) -> None:
        self.table.setRowCount(0)
        seq = self.build_sequence()
        for s in seq.steps:
            self._add_row(s.label, s.wavelength_nm, s.intensity_pct, s.settle_s)

    def _add_row(
        self,
        label: str,
        wavelength: Optional[float],
        intensity_pct: float,
        settle_s: float,
    ) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(label))
        self.table.setItem(
            row, 1, QTableWidgetItem("" if wavelength is None else f"{wavelength:g}")
        )
        self.table.setItem(row, 2, QTableWidgetItem(f"{intensity_pct:g}"))
        self.table.setItem(row, 3, QTableWidgetItem(f"{settle_s:g}"))

    def _delete_selected_rows(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def build_sequence(self) -> IlluminationSequence:
        """Build IlluminationSequence from the **Channels tab** state.

        Used both for the live-mirror table view and as the initial run
        config when the user hasn't manually edited the sequence.
        """
        steps: list[IlluminationStep] = []
        if self.dark_pre.isChecked():
            steps.append(
                IlluminationStep("dark_pre", None, 0.0, self.dark_settle.value())
            )
        for wl, cb, pct in self.channel_rows:
            if not cb.isChecked():
                continue
            steps.append(
                IlluminationStep(
                    f"{int(wl)}nm",
                    wl,
                    pct.value(),
                    self.light_settle.value(),
                )
            )
            if self.dark_post.isChecked():
                steps.append(
                    IlluminationStep(
                        f"dark_post_{int(wl)}",
                        None,
                        0.0,
                        self.dark_settle.value(),
                    )
                )
        return IlluminationSequence(steps=steps)

    def read_table_sequence(self) -> IlluminationSequence:
        """Read the **Sequence tab** table as the authoritative sequence.

        Falls back to the Channels-tab build if the table is empty.
        """
        if self.table.rowCount() == 0:
            return self.build_sequence()
        steps: list[IlluminationStep] = []
        for r in range(self.table.rowCount()):
            label = self.table.item(r, 0).text().strip() if self.table.item(r, 0) else "step"
            wl_raw = self.table.item(r, 1).text().strip() if self.table.item(r, 1) else ""
            pct = float(self.table.item(r, 2).text() or 0)
            settle = float(self.table.item(r, 3).text() or 0)
            wl = float(wl_raw) if wl_raw else None
            steps.append(IlluminationStep(label, wl, pct, settle))
        return IlluminationSequence(steps=steps)


# --- Main control panel ------------------------------------------------------


class CfControlPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)

        # ---- Sweep type ----
        st_box = QGroupBox("Measurement")
        st_form = QFormLayout(st_box)
        self.sweep_type = QComboBox()
        for st in SweepType:
            self.sweep_type.addItem(st.value, userData=st)
        st_form.addRow("Sweep type", self.sweep_type)
        layout.addWidget(st_box)

        # ---- IA group ----
        ia = QGroupBox("Impedance Analyzer")
        ia_form = QFormLayout(ia)
        self.ac_amp = _spin(0.030, 0.001, 1.0, 0.001, decimals=4)
        self.amp_unit = QComboBox()
        self.amp_unit.addItem(AmplitudeUnit.VRMS.value, userData=AmplitudeUnit.VRMS)
        self.amp_unit.addItem(AmplitudeUnit.VPK.value, userData=AmplitudeUnit.VPK)
        amp_row = QHBoxLayout()
        amp_row.addWidget(self.ac_amp, stretch=1)
        amp_row.addWidget(self.amp_unit)
        amp_holder = QWidget()
        amp_holder.setLayout(amp_row)
        self.equiv = QComboBox()
        self.equiv.addItems([e.value for e in EquivCircuit])
        self.terminal_mode = QComboBox()
        for tm in TerminalMode:
            self.terminal_mode.addItem(tm.value, userData=tm)
        # C-f default: 2-terminal (high-Z devices, full ±10 V bias range).
        self.terminal_mode.setCurrentText(TerminalMode.TWO_TERMINAL.value)
        # Current-input range: Auto by default. Pin a sensitive range for
        # high-Z / low-current sweeps where auto-range misbehaves at low f.
        self.current_range = QComboBox()
        self.current_range.addItem("Auto", userData=None)
        for r in (1e-9, 10e-9, 100e-9, 1e-6, 10e-6, 100e-6, 1e-3, 10e-3):
            self.current_range.addItem(_fmt_si_amps(r), userData=r)
        self.current_range.setToolTip(
            "Fixed MFIA current-input range. Auto is normal; pin a sensitive\n"
            "range (e.g. 10 nA) for high-impedance / low-current sweeps and\n"
            "check the result against a known reference."
        )
        ia_form.addRow("AC amplitude", amp_holder)
        ia_form.addRow("Equivalent circuit", self.equiv)
        ia_form.addRow("Terminal mode", self.terminal_mode)
        ia_form.addRow("Current range", self.current_range)
        layout.addWidget(ia)

        # ---- Swept-axis controls: C-f and C-V pages swap by sweep type ----
        # C-f page: frequency sweep + the coarse bias-point list.
        cf_page = QWidget()
        cf_layout = QVBoxLayout(cf_page)
        cf_layout.setContentsMargins(0, 0, 0, 0)

        sw = QGroupBox("Frequency sweep")
        sw_form = QFormLayout(sw)
        self.start_hz = _spin(0.01, 1e-4, 5e6, 0.1, decimals=6)
        self.stop_hz = _spin(3e5, 1.0, 5e6, 1e3, decimals=1)
        self.pts_per_dec = _int_spin(10, 1, 200)
        self.settling_tcs = _spin(5.0, 0.5, 50.0, 0.5, decimals=2)
        self.log_spacing = QCheckBox("Log spacing")
        self.log_spacing.setChecked(True)
        self.auto_bw = QCheckBox("Auto bandwidth")
        self.auto_bw.setChecked(True)
        sw_form.addRow("Start (Hz)", self.start_hz)
        sw_form.addRow("Stop (Hz)", self.stop_hz)
        sw_form.addRow("Pts / decade", self.pts_per_dec)
        sw_form.addRow("Settle (TCs)", self.settling_tcs)
        sw_form.addRow(self.log_spacing)
        sw_form.addRow(self.auto_bw)
        cf_layout.addWidget(sw)

        bias = QGroupBox("Bias points (C-f)")
        bias_form = QFormLayout(bias)
        self.bias_list = QLineEdit("-5,-4,-2,-1,0,1,2,4,5")
        self.bias_settle = _spin(90.0, 0.0, 7200.0, 5.0, decimals=1)
        self.bias_settle.setSuffix(" s")
        bias_form.addRow("Values (V)", self.bias_list)
        bias_form.addRow("Settle per bias", self.bias_settle)
        cf_layout.addWidget(bias)

        # C-V page: test-frequency list (outer loop) + swept bias axis.
        cv_page = QWidget()
        cv_layout = QVBoxLayout(cv_page)
        cv_layout.setContentsMargins(0, 0, 0, 0)

        cvf = QGroupBox("Test frequencies (C-V)")
        cvf_form = QFormLayout(cvf)
        self.cv_freqs = QLineEdit("100000")
        self.cv_freqs.setToolTip(
            "One frequency = single-frequency C-V; a comma-separated list "
            "steps through each (a C-V-f map)."
        )
        cvf_form.addRow("Frequencies (Hz)", self.cv_freqs)
        cv_layout.addWidget(cvf)

        cvb = QGroupBox("Bias sweep (C-V)")
        cvb_form = QFormLayout(cvb)
        self.cv_start = _spin(-5.0, -10.0, 10.0, 0.1, decimals=3)
        self.cv_stop = _spin(5.0, -10.0, 10.0, 0.1, decimals=3)
        self.cv_points = _int_spin(101, 2, 2001)
        self.cv_settling_tcs = _spin(5.0, 0.5, 50.0, 0.5, decimals=2)
        self.cv_auto_bw = QCheckBox("Auto bandwidth")
        self.cv_auto_bw.setChecked(True)
        cvb_form.addRow("Start (V)", self.cv_start)
        cvb_form.addRow("Stop (V)", self.cv_stop)
        cvb_form.addRow("Points", self.cv_points)
        cvb_form.addRow("Settle (TCs)", self.cv_settling_tcs)
        cvb_form.addRow(self.cv_auto_bw)
        cv_layout.addWidget(cvb)

        self.swept_stack = QStackedWidget()
        self.swept_stack.addWidget(cf_page)  # index 0 → C_F
        self.swept_stack.addWidget(cv_page)  # index 1 → C_V
        layout.addWidget(self.swept_stack)
        self.sweep_type.currentIndexChanged.connect(self.swept_stack.setCurrentIndex)

        # ---- Illumination editor ----
        illum_box = QGroupBox("Illumination sequence")
        illum_layout = QVBoxLayout(illum_box)
        self.illum_editor = IlluminationEditor()
        illum_layout.addWidget(self.illum_editor)
        layout.addWidget(illum_box)

        # ---- LED source (NI PXI-7853R via led_driver) ----
        led = QGroupBox("LED source (PXI-7853R)")
        led_form = QFormLayout(led)
        self.led_bitfile = QLineEdit("")
        self.led_bitfile.setPlaceholderText("blank = led_driver mock (no FPGA)")
        led_browse = QPushButton("…")
        led_browse.setMaximumWidth(40)
        led_browse.clicked.connect(self._browse_bitfile)
        bf_row = QHBoxLayout()
        bf_row.addWidget(self.led_bitfile, stretch=1)
        bf_row.addWidget(led_browse)
        bf_holder = QWidget()
        bf_holder.setLayout(bf_row)
        self.led_resource = QLineEdit("RIO0")
        self.led_use_cal = QCheckBox("Apply power calibration (linearize + equalize power)")
        led_form.addRow(".lvbitx bitfile", bf_holder)
        led_form.addRow("NI-RIO resource", self.led_resource)
        led_form.addRow(self.led_use_cal)
        layout.addWidget(led)

        # ---- Run metadata + output ----
        run = QGroupBox("Run / output")
        run_form = QFormLayout(run)
        self.device_id = QLineEdit("")
        self.device_id.setPlaceholderText("e.g. 2013-4")
        self.substrate = QComboBox()
        self.substrate.setEditable(True)
        self.substrate.addItems(["nitride", "p-Si", "n-Si", "unknown"])
        self.output_dir = QLineEdit("")
        self.output_dir.setPlaceholderText("folder for CSV files")
        out_browse = QPushButton("…")
        out_browse.setMaximumWidth(40)
        out_browse.clicked.connect(self._browse_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output_dir, stretch=1)
        out_row.addWidget(out_browse)
        out_holder = QWidget()
        out_holder.setLayout(out_row)
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("optional free-text notes")
        self.notes.setMaximumHeight(60)
        run_form.addRow("Device ID", self.device_id)
        run_form.addRow("Substrate", self.substrate)
        run_form.addRow("Output folder", out_holder)
        run_form.addRow("Notes", self.notes)
        layout.addWidget(run)

        # ---- Run buttons ----
        self.start_btn = QPushButton("Start campaign")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        btns = QHBoxLayout()
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        layout.addLayout(btns)
        layout.addStretch()

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Output folder", self.output_dir.text())
        if path:
            self.output_dir.setText(path)

    def _browse_bitfile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "FPGA bitfile", self.led_bitfile.text(), "LabVIEW FPGA (*.lvbitx)"
        )
        if path:
            self.led_bitfile.setText(path)

    def parse_bias_list(self) -> list[float]:
        return self._parse_float_list(self.bias_list.text())

    def parse_cv_frequencies(self) -> list[float]:
        return self._parse_float_list(self.cv_freqs.text())

    @staticmethod
    def _parse_float_list(raw: str) -> list[float]:
        out: list[float] = []
        for piece in raw.strip().split(","):
            piece = piece.strip()
            if piece:
                out.append(float(piece))
        return out

    def build_config(self) -> CfConfig:
        unit = self.amp_unit.currentData()
        amp_value = self.ac_amp.value()
        # Whatever the user typed gets normalized to V RMS in the CfConfig —
        # all the downstream code (hardware, CSV writer) assumes RMS.
        if unit == AmplitudeUnit.VPK:
            import math

            amp_rms = amp_value / math.sqrt(2.0)
        else:
            amp_rms = amp_value

        sweep_type = self.sweep_type.currentData()
        cv_freqs = self.parse_cv_frequencies()

        # Initial IA frequency: the sweeper drives it for C-f, and run_bias_sweep
        # holds the first test frequency for C-V — so seed it sensibly per mode.
        if sweep_type == SweepType.C_V:
            init_freq = cv_freqs[0] if cv_freqs else 100_000.0
        else:
            init_freq = self.start_hz.value()

        ia = IASettings(
            frequency_hz=init_freq,
            ac_amplitude_v=amp_rms,
            dc_bias_v=0.0,  # set per bias by CfExperiment
            equiv_circuit=EquivCircuit(self.equiv.currentText()),
            terminal_mode=self.terminal_mode.currentData(),
            imp_index=0,
            current_range_a=self.current_range.currentData(),  # None = auto
        )
        sweep = SweeperSettings(
            start_hz=self.start_hz.value(),
            stop_hz=self.stop_hz.value(),
            points_per_decade=self.pts_per_dec.value(),
            log_spacing=self.log_spacing.isChecked(),
            settling_tcs=self.settling_tcs.value(),
            auto_bandwidth=self.auto_bw.isChecked(),
        )
        bias = BiasSequence(
            values_v=self.parse_bias_list(),
            bias_settle_s=self.bias_settle.value(),
        )
        cv_bias = BiasSweepSettings(
            start_v=self.cv_start.value(),
            stop_v=self.cv_stop.value(),
            n_points=self.cv_points.value(),
            settling_tcs=self.cv_settling_tcs.value(),
            auto_bandwidth=self.cv_auto_bw.isChecked(),
        )
        # The Sequence-tab table wins if the user has touched it; otherwise
        # build from the Channels tab.
        illum = self.illum_editor.read_table_sequence()
        run = RunMetadata(
            device_id=self.device_id.text().strip(),
            substrate_type=self.substrate.currentText().strip(),
            notes=self.notes.toPlainText().strip(),
            output_dir=self.output_dir.text().strip(),
        )
        return CfConfig(
            ia=ia,
            amplitude_unit=unit,
            sweep_type=sweep_type,
            sweep=sweep,
            bias=bias,
            cv_frequencies_hz=cv_freqs,
            cv_bias=cv_bias,
            illumination=illum,
            run=run,
        )


# --- Main window ------------------------------------------------------------


class CfMainWindow(QMainWindow):
    def __init__(
        self,
        *,
        preselect_mock: bool = False,
        preselect_device: str | None = None,
        preselect_host: str | None = None,
        preselect_port: int | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("MFIA-Cf — Impedance Spectroscopy Campaign")
        self.backend = None
        self.led_factory: LedFactory | None = None

        self.instrument_panel = InstrumentPanel()
        self.instrument_panel.preselect(
            mock=preselect_mock,
            device=preselect_device,
            host=preselect_host,
            port=preselect_port,
        )
        self.instrument_panel.connected.connect(self._on_instrument_connected)
        self.instrument_panel.disconnected.connect(self._on_instrument_disconnected)

        self.controls = CfControlPanel()
        self.controls.start_btn.setEnabled(False)
        self.controls.start_btn.clicked.connect(self.start)
        self.controls.stop_btn.clicked.connect(self.stop)

        # ---- Live plot: |Z| (log-log) and phase ----
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.z_plot = self.plot_widget.addPlot(row=0, col=0, title="|Z|")
        self.ph_plot = self.plot_widget.addPlot(row=1, col=0, title="Phase")
        self.z_plot.setLabel("left", "|Z|", "Ω")
        self.ph_plot.setLabel("left", "phase", "°")
        self.ph_plot.setLabel("bottom", "frequency", "Hz")
        self.z_plot.setLogMode(x=True, y=True)
        self.ph_plot.setLogMode(x=True, y=False)
        self.z_plot.showGrid(x=True, y=True, alpha=0.3)
        self.ph_plot.showGrid(x=True, y=True, alpha=0.3)
        self.ph_plot.setXLink(self.z_plot)
        self._traces_z: list[pg.PlotDataItem] = []
        self._traces_ph: list[pg.PlotDataItem] = []
        self._suspect_items: list[pg.PlotDataItem] = []
        self._current_z: pg.PlotDataItem | None = None
        self._current_ph: pg.PlotDataItem | None = None
        # Plot mode state, set at Start from the active config.
        self._plot_is_cv = False
        self._plot_equiv: EquivCircuit = EquivCircuit.CP_RP

        # ---- Status + progress ----
        self.progress_bias = QProgressBar()
        self.progress_step = QProgressBar()
        self.progress_sweep = QProgressBar()
        self.progress_bias.setFormat("Bias %v / %m")
        self.progress_step.setFormat("Illum step %v / %m")
        self.progress_sweep.setFormat("Sweep %p%")
        self.status_label = QLabel("Connect to an instrument to begin.")

        # ---- Overnight queue ----
        # Stage several C-f / C-V campaigns (each a full config snapshot) and
        # run them back-to-back unattended. Set the device once, queue the
        # suites, press Run queue.
        self.queue_label = QLabel("Queue empty.")
        self.queue_list = QListWidget()
        self.queue_list.setMinimumHeight(90)
        self.load_plan_btn = QPushButton("Load plan…")
        self.queue_add_btn = QPushButton("Add current config")
        queue_rm_btn = QPushButton("Remove")
        queue_clear_btn = QPushButton("Clear")
        self.run_queue_btn = QPushButton("Run queue ▶")
        self.load_plan_btn.clicked.connect(self.load_plan_into_queue)
        self.queue_add_btn.clicked.connect(self.add_to_queue)
        queue_rm_btn.clicked.connect(self.remove_selected_from_queue)
        queue_clear_btn.clicked.connect(self.clear_queue)
        self.run_queue_btn.clicked.connect(self.run_queue)
        self.run_queue_btn.setEnabled(False)
        queue_btns = QHBoxLayout()
        queue_btns.addWidget(self.load_plan_btn)
        queue_btns.addWidget(self.queue_add_btn)
        queue_btns.addWidget(queue_rm_btn)
        queue_btns.addWidget(queue_clear_btn)
        queue_btns.addStretch()
        queue_btns.addWidget(self.run_queue_btn)
        self.queue_box = QGroupBox("Overnight queue (C-f / C-V campaigns)")
        queue_layout = QVBoxLayout(self.queue_box)
        queue_layout.addWidget(self.queue_label)
        queue_layout.addWidget(self.queue_list)
        queue_layout.addLayout(queue_btns)

        central = QWidget()
        root = QHBoxLayout(central)

        # The control column has more fields than fit a typical window height.
        # Put it in a scroll area so every widget keeps its natural size and
        # the column scrolls, rather than Qt squashing spin boxes / the
        # illumination editor below usability.
        left_panel = QWidget()
        left_col = QVBoxLayout(left_panel)
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.addWidget(self.instrument_panel)
        left_col.addWidget(self.controls)
        left_col.addStretch()

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_panel)
        left_scroll.setMinimumWidth(430)
        root.addWidget(left_scroll, stretch=0)
        right = QVBoxLayout()
        right.addWidget(self.plot_widget, stretch=1)
        right.addWidget(self.progress_bias)
        right.addWidget(self.progress_step)
        right.addWidget(self.progress_sweep)
        right.addWidget(self.status_label)
        right.addWidget(self.queue_box)
        root.addLayout(right, stretch=1)
        self.setCentralWidget(central)

        self._cfg: CfConfig | None = None
        self._thread: QThread | None = None
        self._worker: CfWorker | CfQueueWorker | None = None
        self._sweep_count = 0
        # Queued campaign snapshots, and the list actually being run.
        self._queue: list[CfConfig] = []
        self._running_queue: list[CfConfig] = []

    # ---- Instrument signal handlers ----------------------------------------

    def _on_instrument_connected(self, backend, kind: str) -> None:
        self.backend = backend
        if kind == BackendKind.MOCK_KEY:
            # Lightweight in-memory LED for the mock path — works without
            # led_driver installed.
            from ..led_source import MockLedSource

            self.led_factory = lambda: MockLedSource()
        else:
            # Real LED driver (NI PXI-7853R via led_driver). Reads the LED
            # group fields at Start time; blank bitfile → led_driver's own
            # mock backend (lets you dry-run the LED path against a real MFIA).
            from ..led_source import PxiLedSource

            def _make_pxi() -> PxiLedSource:
                bf = self.controls.led_bitfile.text().strip() or None
                return PxiLedSource(
                    bitfile=bf,
                    resource=self.controls.led_resource.text().strip() or "RIO0",
                    use_cal=self.controls.led_use_cal.isChecked(),
                )

            self.led_factory = _make_pxi
        self.controls.start_btn.setEnabled(True)
        self._update_queue_buttons()
        self.status_label.setText("Idle. Configure and press Start campaign.")

    def _on_instrument_disconnected(self) -> None:
        self.backend = None
        self.led_factory = None
        self.controls.start_btn.setEnabled(False)
        self._update_queue_buttons()
        self.status_label.setText("Connect to an instrument to begin.")

    # ---- Run lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self.backend is None:
            QMessageBox.warning(self, "Start", "Connect to an instrument first.")
            return
        try:
            cfg = self.controls.build_config()
        except ValueError as e:
            QMessageBox.critical(self, "Config", f"Invalid input:\n{e}")
            return
        is_cv = cfg.sweep_type == SweepType.C_V
        err = self._hard_validate(cfg)
        if err:
            QMessageBox.warning(self, "Config", err)
            return

        # Bias-range guard: the MFIA hard-limits DC bias by terminal mode
        # (±3 V in 4-terminal, ±10 V in 2-terminal). Out-of-range points would
        # be silently clamped on hardware, producing mislabeled data — warn
        # before a long campaign rather than after. For C-V the swept range's
        # endpoints are what can exceed the limit.
        limit = TERMINAL_BIAS_LIMIT_V[cfg.ia.terminal_mode]
        if is_cv:
            endpoints = [cfg.cv_bias.start_v, cfg.cv_bias.stop_v]
            over = [b for b in endpoints if abs(b) > limit]
        else:
            over = [b for b in cfg.bias.values_v if abs(b) > limit]
        if over:
            over_str = ", ".join(f"{b:g}" for b in over)
            resp = QMessageBox.warning(
                self,
                "Bias out of range",
                f"In {cfg.ia.terminal_mode.value} mode the MFIA limits DC bias "
                f"to ±{limit:g} V, but these values exceed it: {over_str} V.\n\n"
                f"Those points will be clamped by the hardware. Switch to "
                f"2-terminal for the full ±10 V range, or remove them.\n\n"
                f"Proceed anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
        out_dir = Path(cfg.run.output_dir)
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True)
            except Exception as e:
                QMessageBox.critical(self, "Output folder", f"Cannot create:\n{e}")
                return

        led = self.led_factory() if self.led_factory else None
        # If the run uses illuminated steps, fail fast: probe the LED source
        # now (connect + verify the requested wavelengths exist) rather than
        # dying partway through a multi-hour campaign.
        lit_wavelengths = {
            s.wavelength_nm for s in cfg.illumination.steps if not s.is_dark
        }
        if lit_wavelengths:
            if led is None:
                QMessageBox.critical(
                    self,
                    "LED",
                    "Illuminated steps configured but no LED source is wired.",
                )
                return
            try:
                led.connect()
                try:
                    available = set(led.wavelengths())
                    missing = sorted(w for w in lit_wavelengths if w not in available)
                finally:
                    led.disconnect()
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "LED",
                    f"Could not open the LED source:\n{type(e).__name__}: {e}",
                )
                return
            if missing:
                miss = ", ".join(f"{w:g}" for w in missing)
                avail = ", ".join(f"{w:g}" for w in sorted(available))
                QMessageBox.critical(
                    self,
                    "LED",
                    f"These wavelengths aren't mapped on the LED source: {miss} nm.\n"
                    f"Available: {avail} nm.",
                )
                return

        self._cfg = cfg
        self._running_queue = []  # mark this as a single run, not a queue run
        self._sweep_count = 0
        self._configure_plot_axes(cfg)
        self._clear_plot()
        # Outer loop is bias for C-f, test-frequency for C-V.
        if is_cv:
            self.progress_bias.setMaximum(len(cfg.cv_frequencies_hz))
            self.progress_bias.setFormat("Freq %v / %m")
        else:
            self.progress_bias.setMaximum(len(cfg.bias))
            self.progress_bias.setFormat("Bias %v / %m")
        self.progress_bias.setValue(0)
        self.progress_step.setMaximum(len(cfg.illumination))
        self.progress_step.setValue(0)
        self.progress_sweep.setValue(0)
        self.status_label.setText("Running…")

        self._thread = QThread()
        self._worker = CfWorker(self.backend, cfg, led=led)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.sweep_done.connect(self._on_sweep_done)
        self._worker.progress.connect(self._on_progress)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

        self.controls.start_btn.setEnabled(False)
        self.controls.stop_btn.setEnabled(True)
        self.load_plan_btn.setEnabled(False)
        self.queue_add_btn.setEnabled(False)
        self.run_queue_btn.setEnabled(False)
        self.instrument_panel.set_busy(True)

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        self.status_label.setText("Stopping…")

    def _on_sweep_done(self, result: SweepResult) -> None:
        self._save_and_plot(result)

    def _save_and_plot(self, result: SweepResult) -> None:
        """Write one finished sweep to its campaign's output folder and plot it.

        Shared by the single-run and queue paths; ``self._cfg`` is the active
        campaign (set per-campaign by the queue at ``campaign_started``), so its
        ``output_dir`` is where this sweep lands.
        """
        if self._cfg is None:
            return
        out_dir = Path(self._cfg.run.output_dir)
        path = out_dir / make_filename(result.metadata)
        try:
            write_sweep_csv(result, path)
        except Exception as e:
            self.status_label.setText(f"CSV write failed: {e}")
            return
        self._sweep_count += 1
        n_suspect, min_abs_phase = self._add_trace(result)
        msg = f"Saved {path.name}  ({self._sweep_count} sweeps total)"
        if n_suspect:
            msg += (
                f"  ⚠ {n_suspect} loss-dominated pt(s), "
                f"min |phase| {min_abs_phase:.0f}° — C unreliable there"
            )
        self.status_label.setText(msg)

    def _on_progress(self, bias_i: int, step_i: int, frac: float) -> None:
        self.progress_bias.setValue(bias_i + 1)
        self.progress_step.setValue(step_i + 1)
        self.progress_sweep.setValue(int(frac * 100))

    def _on_error(self, msg: str) -> None:
        self.status_label.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Campaign error", msg)

    def _on_finished(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None
        self.controls.start_btn.setEnabled(self.backend is not None)
        self.controls.stop_btn.setEnabled(False)
        self.load_plan_btn.setEnabled(True)
        self.queue_add_btn.setEnabled(True)
        self.instrument_panel.set_busy(False)
        if self._running_queue:
            n = len(self._running_queue)
            self.status_label.setText(
                f"Queue done. {self._sweep_count} sweeps across {n} campaign(s)."
            )
        else:
            self.status_label.setText(
                f"Done. {self._sweep_count} sweeps written to "
                f"{self._cfg.run.output_dir if self._cfg else ''}"
            )
        self._update_queue_buttons()

    # ---- Overnight queue ---------------------------------------------------

    def _hard_validate(self, cfg: CfConfig) -> Optional[str]:
        """Required-field / minimum checks shared by Start and the queue.

        Returns an error message, or ``None`` if the config is runnable.
        """
        if cfg.sweep_type == SweepType.C_V:
            if not cfg.cv_frequencies_hz:
                return "Enter at least one test frequency."
            if cfg.cv_bias.n_points < 2:
                return "C-V needs at least 2 bias points."
            if cfg.cv_bias.start_v == cfg.cv_bias.stop_v:
                return "C-V bias start and stop are equal."
        elif not cfg.bias.values_v:
            return "Bias list is empty."
        if not cfg.illumination.steps:
            return "Illumination sequence is empty."
        if not cfg.run.device_id:
            return "Enter a device ID."
        if not cfg.run.output_dir:
            return "Pick an output folder."
        return None

    def _update_queue_buttons(self) -> None:
        running = self._thread is not None
        self.run_queue_btn.setEnabled(
            bool(self._queue) and self.backend is not None and not running
        )

    def _summarize(self, cfg: CfConfig) -> str:
        if cfg.sweep_type == SweepType.C_V:
            mode = "C-V"
            axis = (
                f"{len(cfg.cv_frequencies_hz)} freq · bias "
                f"{cfg.cv_bias.start_v:g}→{cfg.cv_bias.stop_v:g}V×{cfg.cv_bias.n_points}"
            )
        else:
            mode = "C-f"
            axis = f"{len(cfg.bias)} bias"
        lit = sorted({s.wavelength_nm for s in cfg.illumination.steps if not s.is_dark})
        wl = f"{len(lit)}λ" if lit else "dark-only"
        return (
            f"{mode} · {axis} · {len(cfg.illumination)} illum steps ({wl}) · "
            f"{cfg.run.device_id or '?'}"
        )

    def load_plan_into_queue(self) -> None:
        """Load a YAML measurement plan and append its campaigns to the queue.

        The plan is self-contained (device, output folder, every block's axis +
        illumination + timing), so a loaded queue is ready to run without
        touching the controls. See docs/measurement-plan-format.md.
        """
        if self._thread is not None:
            return
        from ..cf_plan import PlanError, load_plan

        path, _ = QFileDialog.getOpenFileName(
            self, "Load measurement plan", "", "YAML plans (*.yaml *.yml);;All files (*)"
        )
        if not path:
            return
        try:
            configs = load_plan(path)
        except PlanError as e:
            QMessageBox.critical(self, "Plan", f"Could not load plan:\n{e}")
            return
        for cfg in configs:
            self._queue.append(cfg)
            self.queue_list.addItem(
                QListWidgetItem(f"{len(self._queue)}. {self._summarize(cfg)}")
            )
        self._refresh_queue_label()
        self._update_queue_buttons()
        self.status_label.setText(
            f"Loaded {len(configs)} campaign(s) from {Path(path).name}."
        )

    def add_to_queue(self) -> None:
        try:
            cfg = self.controls.build_config()
        except ValueError as e:
            QMessageBox.critical(self, "Config", f"Invalid input:\n{e}")
            return
        err = self._hard_validate(cfg)
        if err:
            QMessageBox.warning(self, "Add to queue", err)
            return
        self._queue.append(cfg)
        self.queue_list.addItem(
            QListWidgetItem(f"{len(self._queue)}. {self._summarize(cfg)}")
        )
        self._refresh_queue_label()
        self._update_queue_buttons()

    def remove_selected_from_queue(self) -> None:
        if self._thread is not None:
            return
        rows = sorted(
            (self.queue_list.row(i) for i in self.queue_list.selectedItems()),
            reverse=True,
        )
        for r in rows:
            del self._queue[r]
        self._rebuild_queue_list()

    def clear_queue(self) -> None:
        if self._thread is not None:
            return
        self._queue.clear()
        self._rebuild_queue_list()

    def _rebuild_queue_list(self) -> None:
        self.queue_list.clear()
        for n, cfg in enumerate(self._queue, 1):
            self.queue_list.addItem(QListWidgetItem(f"{n}. {self._summarize(cfg)}"))
        self._refresh_queue_label()
        self._update_queue_buttons()

    def _refresh_queue_label(self) -> None:
        n = len(self._queue)
        self.queue_label.setText("Queue empty." if n == 0 else f"{n} campaign(s) queued.")

    def _probe_led(self, lit_wavelengths: list[float]) -> Optional[str]:
        """Open the LED source and verify the requested wavelengths exist.

        Returns an error message or ``None``. Fails fast before a multi-hour
        run rather than dying partway through.
        """
        led = self.led_factory() if self.led_factory else None
        if led is None:
            return "Illuminated steps are configured but no LED source is wired."
        try:
            led.connect()
            try:
                available = set(led.wavelengths())
                missing = sorted(w for w in lit_wavelengths if w not in available)
            finally:
                led.disconnect()
        except Exception as e:
            return f"Could not open the LED source:\n{type(e).__name__}: {e}"
        if missing:
            miss = ", ".join(f"{w:g}" for w in missing)
            avail = ", ".join(f"{w:g}" for w in sorted(available))
            return (
                f"These wavelengths aren't mapped on the LED source: {miss} nm.\n"
                f"Available: {avail} nm."
            )
        return None

    def run_queue(self) -> None:
        if self.backend is None:
            QMessageBox.warning(self, "Run queue", "Connect to an instrument first.")
            return
        if not self._queue:
            QMessageBox.warning(self, "Run queue", "Queue is empty — add a config first.")
            return

        # Bias-range soft guard across every queued campaign (one prompt).
        over: list[float] = []
        for cfg in self._queue:
            limit = TERMINAL_BIAS_LIMIT_V[cfg.ia.terminal_mode]
            pts = (
                [cfg.cv_bias.start_v, cfg.cv_bias.stop_v]
                if cfg.sweep_type == SweepType.C_V
                else cfg.bias.values_v
            )
            over += [b for b in pts if abs(b) > limit]
        if over:
            over_str = ", ".join(f"{b:g}" for b in sorted(set(over)))
            resp = QMessageBox.warning(
                self,
                "Bias out of range",
                f"Some queued campaigns have bias points beyond the terminal-mode "
                f"limit; the hardware will clamp them: {over_str} V.\n\nProceed anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return

        # Make sure every output folder exists before committing to the night.
        for cfg in self._queue:
            try:
                Path(cfg.run.output_dir).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                QMessageBox.critical(
                    self, "Output folder", f"Cannot create {cfg.run.output_dir}:\n{e}"
                )
                return

        # Fail-fast LED probe over the union of lit wavelengths in the queue.
        lit = sorted(
            {
                s.wavelength_nm
                for cfg in self._queue
                for s in cfg.illumination.steps
                if not s.is_dark and s.wavelength_nm is not None
            }
        )
        if lit:
            err = self._probe_led(lit)
            if err:
                QMessageBox.critical(self, "LED", err)
                return

        # Snapshot the queue so edits during the run can't change what's running.
        self._running_queue = list(self._queue)
        self._cfg = None
        self._sweep_count = 0
        self._clear_plot()

        self._thread = QThread()
        worker = CfQueueWorker(
            self.backend, self._running_queue, led_factory=self.led_factory
        )
        self._worker = worker
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.campaign_started.connect(self._on_queue_campaign_started)
        worker.sweep_done.connect(self._on_queue_sweep_done)
        worker.progress.connect(self._on_progress)
        worker.campaign_done.connect(self._on_queue_campaign_done)
        worker.campaign_error.connect(self._on_queue_campaign_error)
        worker.finished.connect(self._on_finished)
        self._thread.start()

        self.controls.start_btn.setEnabled(False)
        self.controls.stop_btn.setEnabled(True)
        self.load_plan_btn.setEnabled(False)
        self.queue_add_btn.setEnabled(False)
        self.run_queue_btn.setEnabled(False)
        self.instrument_panel.set_busy(True)
        self.status_label.setText(
            f"Queue running: {len(self._running_queue)} campaign(s)…"
        )

    def _on_queue_campaign_started(self, index: int, total: int) -> None:
        cfg = self._running_queue[index]
        self._cfg = cfg  # active campaign — drives saving + plot axes
        self._configure_plot_axes(cfg)
        self._clear_plot()
        if cfg.sweep_type == SweepType.C_V:
            self.progress_bias.setMaximum(len(cfg.cv_frequencies_hz))
            self.progress_bias.setFormat("Freq %v / %m")
        else:
            self.progress_bias.setMaximum(len(cfg.bias))
            self.progress_bias.setFormat("Bias %v / %m")
        self.progress_bias.setValue(0)
        self.progress_step.setMaximum(len(cfg.illumination))
        self.progress_step.setValue(0)
        self.progress_sweep.setValue(0)
        self._mark_queue_item(index, "▶ ")
        self.queue_list.setCurrentRow(index)
        self.status_label.setText(
            f"Campaign {index + 1}/{total}: {self._summarize(cfg)}"
        )

    def _on_queue_sweep_done(self, result: SweepResult, _index: int) -> None:
        self._save_and_plot(result)

    def _on_queue_campaign_done(self, index: int) -> None:
        self._mark_queue_item(index, "✓ ")

    def _on_queue_campaign_error(self, index: int, msg: str) -> None:
        self._mark_queue_item(index, "✗ ")
        self.status_label.setText(f"Campaign {index + 1} failed (skipped): {msg}")

    def _mark_queue_item(self, index: int, marker: str) -> None:
        item = self.queue_list.item(index)
        if item is None:
            return
        text = item.text()
        for m in ("▶ ", "✓ ", "✗ "):
            if text.startswith(m):
                text = text[len(m):]
                break
        item.setText(marker + text)

    # ---- Plot helpers ------------------------------------------------------

    def _configure_plot_axes(self, cfg: CfConfig) -> None:
        """Set the plot axes/labels for the run's sweep type.

        C-f: |Z| (log-log) and phase (log-x) vs frequency.
        C-V: Cp (or Cs) and phase, both linear, vs bias voltage.
        """
        self._plot_is_cv = cfg.sweep_type == SweepType.C_V
        self._plot_equiv = cfg.ia.equiv_circuit
        if self._plot_is_cv:
            is_cs = cfg.ia.equiv_circuit == EquivCircuit.CS_RS
            cap = "Cs" if is_cs else "Cp"
            self.z_plot.setTitle(cap)
            self.z_plot.setLabel("left", cap, "F")
            self.z_plot.setLogMode(x=False, y=False)
            self.ph_plot.setLogMode(x=False, y=False)
            self.z_plot.setLabel("bottom", "bias", "V")
            self.ph_plot.setLabel("bottom", "bias", "V")
        else:
            self.z_plot.setTitle("|Z|")
            self.z_plot.setLabel("left", "|Z|", "Ω")
            self.z_plot.setLogMode(x=True, y=True)
            self.ph_plot.setLogMode(x=True, y=False)
            self.z_plot.setLabel("bottom", "frequency", "Hz")
            self.ph_plot.setLabel("bottom", "frequency", "Hz")
        self.ph_plot.setLabel("left", "phase", "°")

    def _clear_plot(self) -> None:
        for t in self._traces_z:
            self.z_plot.removeItem(t)
        for t in self._traces_ph:
            self.ph_plot.removeItem(t)
        for t in self._suspect_items:
            self.ph_plot.removeItem(t)
        self._traces_z.clear()
        self._traces_ph.clear()
        self._suspect_items.clear()

    def _pen_for(self, meta):
        """Pen color by illumination state — grey for dark, keyed by wavelength."""
        if meta.wavelength_nm in (0.0, None):
            return pg.mkPen((120, 120, 120), width=1)
        colors = [
            (160, 30, 220),  # 385 violet
            (30, 100, 220),  # 470 blue
            (30, 200, 200),  # 505 cyan
            (50, 200, 30),   # 530 green
            (220, 200, 30),  # 590 amber
            (220, 100, 30),  # 625 orange
            (200, 30, 30),   # 740 deep red
            (140, 20, 20),   # 850 IR
        ]
        try:
            idx = DEFAULT_CHANNEL_WAVELENGTHS.index(float(meta.wavelength_nm))
        except ValueError:
            idx = int(meta.wavelength_nm) % len(colors)
        return pg.mkPen(colors[idx % len(colors)], width=1)

    def _add_trace(self, result: SweepResult) -> tuple[int, float]:
        """Plot one trace; return (n_suspect_points, min |phase|) for the status.

        A point with |phase| < 45° is loss-dominated (loss tangent > 1): the
        capacitive term is smaller than the conductive one, so the extracted
        C is unreliable. Those points are flagged with red ✕ on the phase plot
        — most relevant at low f / forward bias where leakage takes over.
        """
        pen = self._pen_for(result.metadata)
        x = result.x_values
        phase = result.phase_deg
        if self._plot_is_cv:
            cap, _r = (
                result.cs_rs if self._plot_equiv == EquivCircuit.CS_RS else result.cp_rp
            )
            y_top = cap
        else:
            y_top = result.z_mag
        # pyqtgraph in log mode wants the raw values (it does the log itself).
        t_z = self.z_plot.plot(x, y_top, pen=pen)
        t_ph = self.ph_plot.plot(x, phase, pen=pen)
        self._traces_z.append(t_z)
        self._traces_ph.append(t_ph)

        suspect = np.abs(phase) < 45.0
        n_suspect = int(np.count_nonzero(suspect))
        if n_suspect:
            sc = self.ph_plot.plot(
                np.asarray(x)[suspect],
                np.asarray(phase)[suspect],
                pen=None,
                symbol="x",
                symbolBrush=(220, 30, 30),
                symbolPen=(220, 30, 30),
                symbolSize=7,
            )
            self._suspect_items.append(sc)
        min_abs_phase = float(np.nanmin(np.abs(phase))) if phase.size else float("nan")
        return n_suspect, min_abs_phase

    # ---- Close cleanup -----------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        if self.backend is not None and hasattr(self.backend, "disconnect"):
            try:
                self.backend.disconnect()
            except Exception:
                pass
        super().closeEvent(event)


def run(
    *,
    preselect_mock: bool = False,
    preselect_device: str | None = None,
    preselect_host: str | None = None,
    preselect_port: int | None = None,
) -> int:
    app = QApplication.instance() or QApplication([])
    win = CfMainWindow(
        preselect_mock=preselect_mock,
        preselect_device=preselect_device,
        preselect_host=preselect_host,
        preselect_port=preselect_port,
    )
    win.resize(1300, 800)
    win.show()
    return app.exec()
