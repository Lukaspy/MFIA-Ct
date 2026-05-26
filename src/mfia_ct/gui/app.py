"""PyQt6 main window for continuous photo-C-t acquisition.

Parameter panel on the left, live pyqtgraph plots of Cp(t) and Gp(t) on the
right with vertical markers at each pulse time. Acquisition runs in a worker
QThread that yields StreamChunks via a signal, where they get appended to a
growing buffer and re-drawn.
"""

from __future__ import annotations

from typing import Callable

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
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..acquisition import StreamChunk
from ..config import (
    AcquisitionSettings,
    CtConfig,
    DemodSettings,
    EquivCircuit,
    FunctionGeneratorSettings,
    IASettings,
    PulseSettings,
    PulseSource,
)
from ..experiment import CtExperiment
from ..storage import save_run

FGFactory = Callable[[str], object]


class AcquisitionWorker(QObject):
    chunk = pyqtSignal(object)  # StreamChunk
    pulse_count = pyqtSignal(int)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, backend, cfg: CtConfig, fg=None) -> None:
        super().__init__()
        self.backend = backend
        self.cfg = cfg
        self.fg = fg
        self._exp: CtExperiment | None = None

    def stop(self) -> None:
        if self._exp is not None:
            self._exp.stop()

    def run(self) -> None:
        try:
            self._exp = CtExperiment(self.backend, self.cfg, fg=self.fg)
            last_pulse_count = 0
            for ch in self._exp.run():
                self.chunk.emit(ch)
                n = len(self._exp.pulse_times)
                if n != last_pulse_count:
                    self.pulse_count.emit(n)
                    last_pulse_count = n
            # Emit one final count update in case the last pulse fired between
            # the last chunk and the loop exiting.
            n = len(self._exp.pulse_times)
            if n != last_pulse_count:
                self.pulse_count.emit(n)
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


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


class BackendKind:
    MOCK = "Mock (synthetic)"
    REAL = "Real MFIA"


class ControlPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)

        # Instrument group
        instr = QGroupBox("Instrument")
        instr_form = QFormLayout(instr)
        self.backend_kind = QComboBox()
        self.backend_kind.addItems([BackendKind.MOCK, BackendKind.REAL])
        self.device_id = QLineEdit("")
        self.device_id.setPlaceholderText("e.g. dev32369")
        self.host = QLineEdit("localhost")
        self.port = _int_spin(8004, 1, 65535)
        self.connect_btn = QPushButton("Connect")
        self.instr_status = QLabel("Not connected.")
        instr_form.addRow("Backend", self.backend_kind)
        instr_form.addRow("Device ID", self.device_id)
        instr_form.addRow("LabOne host", self.host)
        instr_form.addRow("LabOne port", self.port)
        instr_form.addRow(self.connect_btn)
        instr_form.addRow(self.instr_status)
        layout.addWidget(instr)

        self._real_only_instr = [self.device_id, self.host, self.port]
        self.backend_kind.currentTextChanged.connect(self._update_instr_widgets)
        self._update_instr_widgets()

        # IA group
        ia = QGroupBox("Impedance Analyzer")
        ia_form = QFormLayout(ia)
        self.freq = _spin(100_000, 1e3, 5e6, 1e3, decimals=0)
        self.ac_amp = _spin(0.05, 0.001, 1.0, 0.01, decimals=4)
        self.dc_bias = _spin(0.0, -10.0, 10.0, 0.1, decimals=3)
        self.equiv = QComboBox()
        self.equiv.addItems([e.value for e in EquivCircuit])
        ia_form.addRow("Test frequency (Hz)", self.freq)
        ia_form.addRow("AC amplitude (V rms)", self.ac_amp)
        ia_form.addRow("DC bias (V)", self.dc_bias)
        ia_form.addRow("Equivalent circuit", self.equiv)
        layout.addWidget(ia)

        # Demod group
        demod = QGroupBox("Demodulator")
        demod_form = QFormLayout(demod)
        self.tc = _spin(1e-5, 1e-7, 1.0, 1e-6, decimals=7)
        self.order = _int_spin(4, 1, 8)
        self.demod_rate = _spin(100_000, 100, 1_000_000, 1000, decimals=0)
        demod_form.addRow("Time constant (s)", self.tc)
        demod_form.addRow("Filter order", self.order)
        demod_form.addRow("Sample rate (Hz)", self.demod_rate)
        layout.addWidget(demod)

        # Pulse group
        pulse = QGroupBox("Optical pulse")
        pulse_form = QFormLayout(pulse)
        self.pulse_source = QComboBox()
        for s in PulseSource:
            self.pulse_source.addItem(s.value, userData=s)
        self.aux_ch = QComboBox()
        for i in range(4):
            self.aux_ch.addItem(f"Aux Out {i + 1}", userData=i)
        self.high_v = _spin(5.0, 0.0, 10.0, 0.1, decimals=2)
        self.low_v = _spin(0.0, -10.0, 10.0, 0.1, decimals=2)
        self.pulse_width = _spin(0.010, 1e-6, 10.0, 0.001, decimals=6)
        self.rest_period = _spin(0.99, 0.0, 60.0, 0.1, decimals=4)
        self.n_pulses = _int_spin(20, 1, 100_000)
        self.sync_in_ch = QComboBox()
        for i in range(2):
            self.sync_in_ch.addItem(f"Aux In {i + 1}", userData=i)
        self.sync_threshold = _spin(1.0, -10.0, 10.0, 0.1, decimals=3)
        pulse_form.addRow("Pulse source", self.pulse_source)
        pulse_form.addRow("Aux Out channel", self.aux_ch)
        pulse_form.addRow("High (V)", self.high_v)
        pulse_form.addRow("Low (V)", self.low_v)
        pulse_form.addRow("Pulse width (s)", self.pulse_width)
        pulse_form.addRow("Sync input", self.sync_in_ch)
        pulse_form.addRow("Sync threshold (V)", self.sync_threshold)
        pulse_form.addRow("Rest between pulses (s)", self.rest_period)
        pulse_form.addRow("# pulses", self.n_pulses)
        layout.addWidget(pulse)

        # Function generator group (Agilent 33250A over GPIB)
        fg = QGroupBox("Function generator (Agilent 33250A)")
        fg_form = QFormLayout(fg)
        self.fg_enabled = QCheckBox("Drive FG over GPIB")
        self.fg_resource = QLineEdit("GPIB0::10::INSTR")
        self.fg_high_v = _spin(5.0, -10.0, 10.0, 0.1, decimals=3)
        self.fg_low_v = _spin(0.0, -10.0, 10.0, 0.1, decimals=3)
        self.fg_load = QComboBox()
        self.fg_load.addItem("High-Z (INF)", userData="INF")
        self.fg_load.addItem("50 Ω", userData="50")
        self.fg_test_btn = QPushButton("Test connection")
        fg_form.addRow(self.fg_enabled)
        fg_form.addRow("VISA resource", self.fg_resource)
        fg_form.addRow("High (V)", self.fg_high_v)
        fg_form.addRow("Low (V)", self.fg_low_v)
        fg_form.addRow("Output load", self.fg_load)
        fg_form.addRow(self.fg_test_btn)
        layout.addWidget(fg)

        self._fg_widgets = [
            self.fg_resource,
            self.fg_high_v,
            self.fg_low_v,
            self.fg_load,
            self.fg_test_btn,
        ]
        self.fg_enabled.toggled.connect(self._update_fg_widgets)
        self._update_fg_widgets()

        # Track which rows are internal/external-only so we can grey them out.
        self._internal_only = [self.aux_ch, self.high_v, self.low_v]
        self._external_only = [
            self.sync_in_ch,
            self.sync_threshold,
            self.fg_enabled,
        ]
        self.pulse_source.currentIndexChanged.connect(self._update_pulse_mode_widgets)
        self.pulse_source.currentIndexChanged.connect(self._update_fg_widgets)
        self._update_pulse_mode_widgets()

        # Acquisition group
        acq = QGroupBox("Acquisition")
        acq_form = QFormLayout(acq)
        self.poll_interval = _spin(0.05, 0.001, 1.0, 0.01, decimals=4)
        acq_form.addRow("Poll interval (s)", self.poll_interval)
        layout.addWidget(acq)

        # Run / save buttons
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.save_btn = QPushButton("Save HDF5…")
        self.save_btn.setEnabled(False)
        btns = QHBoxLayout()
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.save_btn)
        layout.addLayout(btns)
        layout.addStretch()

    def _update_instr_widgets(self) -> None:
        is_real = self.backend_kind.currentText() == BackendKind.REAL
        for w in self._real_only_instr:
            w.setEnabled(is_real)

    def _update_pulse_mode_widgets(self) -> None:
        is_internal = self.pulse_source.currentData() == PulseSource.INTERNAL
        for w in self._internal_only:
            w.setEnabled(is_internal)
        for w in self._external_only:
            w.setEnabled(not is_internal)

    def _update_fg_widgets(self) -> None:
        is_external = self.pulse_source.currentData() == PulseSource.EXTERNAL
        # FG group only meaningful in external mode AND when checkbox is on.
        fg_active = is_external and self.fg_enabled.isChecked()
        for w in self._fg_widgets:
            w.setEnabled(fg_active)

    def config(self) -> CtConfig:
        width = self.pulse_width.value()
        rest = self.rest_period.value()
        return CtConfig(
            ia=IASettings(
                frequency_hz=self.freq.value(),
                ac_amplitude_v=self.ac_amp.value(),
                dc_bias_v=self.dc_bias.value(),
                equiv_circuit=EquivCircuit(self.equiv.currentText()),
            ),
            demod=DemodSettings(
                time_constant_s=self.tc.value(),
                filter_order=self.order.value(),
                sample_rate_hz=self.demod_rate.value(),
            ),
            pulse=PulseSettings(
                source=self.pulse_source.currentData(),
                aux_out_channel=self.aux_ch.currentData(),
                high_v=self.high_v.value(),
                low_v=self.low_v.value(),
                pulse_width_s=width,
                period_s=width + rest,
                n_pulses=self.n_pulses.value(),
                sync_aux_in_channel=self.sync_in_ch.currentData(),
                sync_threshold_v=self.sync_threshold.value(),
            ),
            acq=AcquisitionSettings(poll_interval_s=self.poll_interval.value()),
            fg=FunctionGeneratorSettings(
                enabled=self.fg_enabled.isChecked(),
                resource=self.fg_resource.text().strip(),
                high_v=self.fg_high_v.value(),
                low_v=self.fg_low_v.value(),
                load_ohms=self.fg_load.currentData(),
            ),
        )


class _GrowingBuffer:
    """Append-only float buffer with amortized O(1) growth."""

    def __init__(self, initial: int = 100_000) -> None:
        self._buf = np.empty(initial, dtype=float)
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def view(self) -> np.ndarray:
        return self._buf[: self._size]

    def append(self, data: np.ndarray) -> None:
        need = self._size + data.size
        if need > self._buf.size:
            new_cap = max(need, 2 * self._buf.size)
            self._buf = np.resize(self._buf, new_cap)
        self._buf[self._size : need] = data
        self._size = need

    def clear(self) -> None:
        self._size = 0


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        preselect_mock: bool = False,
        preselect_device: str | None = None,
        preselect_host: str | None = None,
        preselect_port: int | None = None,
    ) -> None:
        super().__init__()
        self.backend = None
        self.fg_factory: FGFactory | None = None
        self.setWindowTitle("MFIA-Ct — Photo-Capacitance Transient")

        self.controls = ControlPanel()
        if preselect_mock:
            self.controls.backend_kind.setCurrentText(BackendKind.MOCK)
        elif preselect_device is not None:
            self.controls.backend_kind.setCurrentText(BackendKind.REAL)
        if preselect_device is not None:
            self.controls.device_id.setText(preselect_device)
        if preselect_host is not None:
            self.controls.host.setText(preselect_host)
        if preselect_port is not None:
            self.controls.port.setValue(preselect_port)
        self.controls.connect_btn.clicked.connect(self._toggle_connection)
        self.controls.fg_test_btn.clicked.connect(self._test_fg_connection)
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.cp_plot = self.plot_widget.addPlot(row=0, col=0, title="Cp")
        self.gp_plot = self.plot_widget.addPlot(row=1, col=0, title="Gp")
        self.cp_plot.setLabel("left", "Cp", "F")
        self.gp_plot.setLabel("left", "Gp", "S")
        self.cp_plot.setLabel("bottom", "Experiment time", "s")
        self.gp_plot.setLabel("bottom", "Experiment time", "s")
        self.cp_plot.showGrid(x=True, y=True, alpha=0.3)
        self.gp_plot.showGrid(x=True, y=True, alpha=0.3)
        self.gp_plot.setXLink(self.cp_plot)
        # Subsample on display for performance while keeping full data in memory.
        for p in (self.cp_plot, self.gp_plot):
            p.setDownsampling(auto=True, mode="peak")
            p.setClipToView(True)
        self.cp_curve = self.cp_plot.plot(pen=pg.mkPen("c", width=1))
        self.gp_curve = self.gp_plot.plot(pen=pg.mkPen("c", width=1))

        self.progress = QProgressBar()
        self.status_label = QLabel("Connect to an instrument to begin.")
        self.controls.start_btn.setEnabled(False)

        central = QWidget()
        root = QHBoxLayout(central)
        root.addWidget(self.controls, stretch=0)
        right = QVBoxLayout()
        right.addWidget(self.plot_widget, stretch=1)
        right.addWidget(self.progress)
        right.addWidget(self.status_label)
        root.addLayout(right, stretch=1)
        self.setCentralWidget(central)

        self.controls.start_btn.clicked.connect(self.start)
        self.controls.stop_btn.clicked.connect(self.stop)
        self.controls.save_btn.clicked.connect(self.save)

        self._t_buf = _GrowingBuffer()
        self._cp_buf = _GrowingBuffer()
        self._gp_buf = _GrowingBuffer()
        self._pulse_lines_cp: list[pg.InfiniteLine] = []
        self._pulse_lines_gp: list[pg.InfiniteLine] = []
        self._pulse_times: list[float] = []
        self._cfg: CtConfig | None = None
        self._thread: QThread | None = None
        self._worker: AcquisitionWorker | None = None

    def _toggle_connection(self) -> None:
        if self.backend is None:
            self._connect_backend()
        else:
            self._disconnect_backend()

    def _connect_backend(self) -> None:
        kind = self.controls.backend_kind.currentText()
        try:
            if kind == BackendKind.MOCK:
                from ..mock_fg import MockFunctionGenerator
                from ..mock_hardware import MockMFIA

                self.backend = MockMFIA()
                self.fg_factory = lambda r: MockFunctionGenerator(r)
                label = "Mock backend"
            else:
                from ..fg33250a import Agilent33250A
                from ..hardware import MFIA

                device = self.controls.device_id.text().strip()
                if not device:
                    QMessageBox.warning(
                        self, "Instrument", "Enter a device ID (e.g. dev32369)."
                    )
                    return
                backend = MFIA(
                    device,
                    server_host=self.controls.host.text().strip() or "localhost",
                    server_port=self.controls.port.value(),
                )
                backend.connect()
                self.backend = backend
                self.fg_factory = lambda r: Agilent33250A(r)
                label = f"MFIA {device}"
        except Exception as e:
            self.backend = None
            self.fg_factory = None
            QMessageBox.critical(
                self,
                "Instrument",
                f"Could not connect:\n{type(e).__name__}: {e}",
            )
            self.controls.instr_status.setText("Connection failed.")
            return

        self.controls.instr_status.setText(f"Connected: {label}.")
        self.controls.connect_btn.setText("Disconnect")
        self.controls.backend_kind.setEnabled(False)
        for w in self.controls._real_only_instr:
            w.setEnabled(False)
        self.controls.start_btn.setEnabled(True)
        self.status_label.setText("Idle.")

    def _disconnect_backend(self) -> None:
        if self.backend is not None and hasattr(self.backend, "disconnect"):
            try:
                self.backend.disconnect()
            except Exception:
                pass
        self.backend = None
        self.fg_factory = None
        self.controls.instr_status.setText("Not connected.")
        self.controls.connect_btn.setText("Connect")
        self.controls.backend_kind.setEnabled(True)
        self.controls._update_instr_widgets()
        self.controls.start_btn.setEnabled(False)
        self.status_label.setText("Connect to an instrument to begin.")

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

    def start(self) -> None:
        if self.backend is None:
            QMessageBox.warning(self, "Start", "Connect to an instrument first.")
            return
        self._cfg = self.controls.config()
        self._t_buf.clear()
        self._cp_buf.clear()
        self._gp_buf.clear()
        self._pulse_times = []
        for line in self._pulse_lines_cp:
            self.cp_plot.removeItem(line)
        for line in self._pulse_lines_gp:
            self.gp_plot.removeItem(line)
        self._pulse_lines_cp = []
        self._pulse_lines_gp = []
        self.cp_curve.setData([], [])
        self.gp_curve.setData([], [])
        self.progress.setMaximum(self._cfg.pulse.n_pulses)
        self.progress.setValue(0)
        self.status_label.setText("Running…")

        fg = None
        if (
            self._cfg.pulse.source == PulseSource.EXTERNAL
            and self._cfg.fg.enabled
            and self.fg_factory is not None
        ):
            fg = self.fg_factory(self._cfg.fg.resource)

        self._thread = QThread()
        self._worker = AcquisitionWorker(self.backend, self._cfg, fg=fg)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.pulse_count.connect(self._on_pulse_count)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

        self.controls.start_btn.setEnabled(False)
        self.controls.stop_btn.setEnabled(True)
        self.controls.save_btn.setEnabled(False)
        self.controls.connect_btn.setEnabled(False)

    def stop(self) -> None:
        if self._worker:
            self._worker.stop()

    def _on_chunk(self, chunk: StreamChunk) -> None:
        self._t_buf.append(chunk.t)
        self._cp_buf.append(chunk.cp)
        self._gp_buf.append(chunk.gp)
        t_view = self._t_buf.view()
        self.cp_curve.setData(t_view, self._cp_buf.view())
        self.gp_curve.setData(t_view, self._gp_buf.view())

    def _on_pulse_count(self, n: int) -> None:
        # Mirror the pulser's pulse_times list — read from the experiment via worker.
        assert self._worker is not None and self._worker._exp is not None
        new_times = self._worker._exp.pulse_times
        # Add lines for any pulses we haven't drawn yet.
        pen = pg.mkPen("m", width=1, style=Qt.PenStyle.DashLine)
        for t in new_times[len(self._pulse_times) :]:
            line_cp = pg.InfiniteLine(pos=t, angle=90, pen=pen)
            line_gp = pg.InfiniteLine(pos=t, angle=90, pen=pen)
            self.cp_plot.addItem(line_cp)
            self.gp_plot.addItem(line_gp)
            self._pulse_lines_cp.append(line_cp)
            self._pulse_lines_gp.append(line_gp)
        self._pulse_times = new_times
        self.progress.setValue(n)

    def _on_error(self, msg: str) -> None:
        self.status_label.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Acquisition error", msg)

    def _on_finished(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self.controls.start_btn.setEnabled(self.backend is not None)
        self.controls.stop_btn.setEnabled(False)
        self.controls.save_btn.setEnabled(self._t_buf.size > 0)
        self.controls.connect_btn.setEnabled(True)
        self.status_label.setText(
            f"Done. {self._t_buf.size} samples, {len(self._pulse_times)} pulses."
        )

    def _test_fg_connection(self) -> None:
        if self.fg_factory is None:
            QMessageBox.warning(
                self,
                "Function generator",
                "Connect to an instrument first — the FG driver follows the backend type.",
            )
            return
        resource = self.controls.fg_resource.text().strip()
        if not resource:
            QMessageBox.warning(self, "Function generator", "Enter a VISA resource string.")
            return
        try:
            fg = self.fg_factory(resource)
            fg.connect()
            try:
                idn = fg.idn()
            finally:
                fg.disconnect()
        except Exception as e:
            QMessageBox.critical(
                self,
                "Function generator",
                f"Could not connect to {resource}:\n{type(e).__name__}: {e}",
            )
            return
        QMessageBox.information(self, "Function generator", f"Connected:\n{idn}")

    def save(self) -> None:
        if self._t_buf.size == 0 or self._cfg is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save run", "ct_run.h5", "HDF5 files (*.h5 *.hdf5)"
        )
        if not path:
            return
        out = save_run(
            path,
            self._cfg,
            self._t_buf.view().copy(),
            self._cp_buf.view().copy(),
            self._gp_buf.view().copy(),
            self._pulse_times,
        )
        self.status_label.setText(f"Saved to {out}")


def run(
    *,
    preselect_mock: bool = False,
    preselect_device: str | None = None,
    preselect_host: str | None = None,
    preselect_port: int | None = None,
) -> int:
    app = QApplication.instance() or QApplication([])
    win = MainWindow(
        preselect_mock=preselect_mock,
        preselect_device=preselect_device,
        preselect_host=preselect_host,
        preselect_port=preselect_port,
    )
    win.resize(1200, 700)
    win.show()
    return app.exec()
