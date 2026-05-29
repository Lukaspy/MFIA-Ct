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
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
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
    CfConfig,
    IlluminationSequence,
    IlluminationStep,
    RunMetadata,
    SweeperSettings,
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


def _spin(value: float, lo: float, hi: float, step: float, decimals: int = 6) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    return s


def _int_spin(value: int, lo: int, hi: int) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    return s


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

        tabs.addTab(chan, "Channels")
        tabs.addTab(seq, "Sequence")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(tabs)

        self._regenerate_table()

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
        ia_form.addRow("AC amplitude", amp_holder)
        ia_form.addRow("Equivalent circuit", self.equiv)
        ia_form.addRow("Terminal mode", self.terminal_mode)
        layout.addWidget(ia)

        # ---- Sweeper group ----
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
        layout.addWidget(sw)

        # ---- Bias list group ----
        bias = QGroupBox("Bias sweep")
        bias_form = QFormLayout(bias)
        self.bias_list = QLineEdit("-5,-4,-2,-1,0,1,2,4,5")
        self.bias_settle = _spin(90.0, 0.0, 7200.0, 5.0, decimals=1)
        self.bias_settle.setSuffix(" s")
        bias_form.addRow("Values (V)", self.bias_list)
        bias_form.addRow("Settle per bias", self.bias_settle)
        layout.addWidget(bias)

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
        raw = self.bias_list.text().strip()
        if not raw:
            return []
        out: list[float] = []
        for piece in raw.split(","):
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

        ia = IASettings(
            frequency_hz=self.start_hz.value(),  # placeholder; sweeper drives freq
            ac_amplitude_v=amp_rms,
            dc_bias_v=0.0,  # set per bias by CfExperiment
            equiv_circuit=EquivCircuit(self.equiv.currentText()),
            terminal_mode=self.terminal_mode.currentData(),
            imp_index=0,
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
            sweep=sweep,
            bias=bias,
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
        self._current_z: pg.PlotDataItem | None = None
        self._current_ph: pg.PlotDataItem | None = None

        # ---- Status + progress ----
        self.progress_bias = QProgressBar()
        self.progress_step = QProgressBar()
        self.progress_sweep = QProgressBar()
        self.progress_bias.setFormat("Bias %v / %m")
        self.progress_step.setFormat("Illum step %v / %m")
        self.progress_sweep.setFormat("Sweep %p%")
        self.status_label = QLabel("Connect to an instrument to begin.")

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
        root.addLayout(right, stretch=1)
        self.setCentralWidget(central)

        self._cfg: CfConfig | None = None
        self._thread: QThread | None = None
        self._worker: CfWorker | None = None
        self._sweep_count = 0

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
        self.status_label.setText("Idle. Configure and press Start campaign.")

    def _on_instrument_disconnected(self) -> None:
        self.backend = None
        self.led_factory = None
        self.controls.start_btn.setEnabled(False)
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
        if not cfg.bias.values_v:
            QMessageBox.warning(self, "Config", "Bias list is empty.")
            return
        if not cfg.illumination.steps:
            QMessageBox.warning(self, "Config", "Illumination sequence is empty.")
            return
        if not cfg.run.device_id:
            QMessageBox.warning(self, "Config", "Enter a device ID.")
            return
        if not cfg.run.output_dir:
            QMessageBox.warning(self, "Config", "Pick an output folder.")
            return

        # Bias-range guard: the MFIA hard-limits DC bias by terminal mode
        # (±3 V in 4-terminal, ±10 V in 2-terminal). Out-of-range points would
        # be silently clamped on hardware, producing mislabeled data — warn
        # before a long campaign rather than after.
        limit = TERMINAL_BIAS_LIMIT_V[cfg.ia.terminal_mode]
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
        self._sweep_count = 0
        self._clear_plot()
        self.progress_bias.setMaximum(len(cfg.bias))
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
        self.instrument_panel.set_busy(True)

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        self.status_label.setText("Stopping…")

    def _on_sweep_done(self, result: SweepResult) -> None:
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
        self._add_trace(result)
        self.status_label.setText(
            f"Saved {path.name}  ({self._sweep_count} sweeps total)"
        )

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
        self.controls.start_btn.setEnabled(self.backend is not None)
        self.controls.stop_btn.setEnabled(False)
        self.instrument_panel.set_busy(False)
        self.status_label.setText(
            f"Done. {self._sweep_count} sweeps written to {self._cfg.run.output_dir if self._cfg else ''}"
        )

    # ---- Plot helpers ------------------------------------------------------

    def _clear_plot(self) -> None:
        for t in self._traces_z:
            self.z_plot.removeItem(t)
        for t in self._traces_ph:
            self.ph_plot.removeItem(t)
        self._traces_z.clear()
        self._traces_ph.clear()

    def _add_trace(self, result: SweepResult) -> None:
        # Color by illumination state — dark grey, lit keyed by wavelength.
        meta = result.metadata
        if meta.wavelength_nm in (0.0, None):
            pen = pg.mkPen((120, 120, 120), width=1)
        else:
            # Map wavelength to its index in the canonical list for a stable
            # color; fall back to a hash for off-list wavelengths.
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
            pen = pg.mkPen(colors[idx % len(colors)], width=1)
        f = np.asarray(result.frequency_hz)
        zmag = result.z_mag
        phase = result.phase_deg
        # pyqtgraph in log mode wants the raw values (it does the log itself).
        t_z = self.z_plot.plot(f, zmag, pen=pen)
        t_ph = self.ph_plot.plot(f, phase, pen=pen)
        self._traces_z.append(t_z)
        self._traces_ph.append(t_ph)

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
