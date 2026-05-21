"""PyQt6 main window for continuous photo-C-t acquisition.

Parameter panel on the left, live pyqtgraph plots of Cp(t) and Gp(t) on the
right with vertical markers at each pulse time. Acquisition runs in a worker
QThread that yields StreamChunks via a signal, where they get appended to a
growing buffer and re-drawn.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
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
    IASettings,
    PulseSettings,
)
from ..experiment import CtExperiment
from ..storage import save_run


class AcquisitionWorker(QObject):
    chunk = pyqtSignal(object)  # StreamChunk
    pulse_count = pyqtSignal(int)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, backend, cfg: CtConfig) -> None:
        super().__init__()
        self.backend = backend
        self.cfg = cfg
        self._exp: CtExperiment | None = None

    def stop(self) -> None:
        if self._exp is not None:
            self._exp.stop()

    def run(self) -> None:
        try:
            self._exp = CtExperiment(self.backend, self.cfg)
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


class ControlPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)

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
        self.aux_ch = QComboBox()
        for i in range(4):
            self.aux_ch.addItem(f"Aux Out {i + 1}", userData=i)
        self.high_v = _spin(5.0, 0.0, 10.0, 0.1, decimals=2)
        self.low_v = _spin(0.0, -10.0, 10.0, 0.1, decimals=2)
        self.pulse_width = _spin(0.010, 1e-6, 10.0, 0.001, decimals=6)
        self.period = _spin(1.0, 0.01, 60.0, 0.1, decimals=3)
        self.n_pulses = _int_spin(20, 1, 100_000)
        pulse_form.addRow("Aux Out channel", self.aux_ch)
        pulse_form.addRow("High (V)", self.high_v)
        pulse_form.addRow("Low (V)", self.low_v)
        pulse_form.addRow("Pulse width (s)", self.pulse_width)
        pulse_form.addRow("Period (s)", self.period)
        pulse_form.addRow("# pulses", self.n_pulses)
        layout.addWidget(pulse)

        # Acquisition group
        acq = QGroupBox("Acquisition")
        acq_form = QFormLayout(acq)
        self.poll_interval = _spin(0.05, 0.005, 1.0, 0.01, decimals=4)
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

    def config(self) -> CtConfig:
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
                aux_out_channel=self.aux_ch.currentData(),
                high_v=self.high_v.value(),
                low_v=self.low_v.value(),
                pulse_width_s=self.pulse_width.value(),
                period_s=self.period.value(),
                n_pulses=self.n_pulses.value(),
            ),
            acq=AcquisitionSettings(poll_interval_s=self.poll_interval.value()),
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
    def __init__(self, backend) -> None:
        super().__init__()
        self.backend = backend
        self.setWindowTitle("MFIA-Ct — Photo-Capacitance Transient")

        self.controls = ControlPanel()
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
        self.status_label = QLabel("Idle.")

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

    def start(self) -> None:
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

        self._thread = QThread()
        self._worker = AcquisitionWorker(self.backend, self._cfg)
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
        for t in new_times[len(self._pulse_times) :]:
            line_cp = pg.InfiniteLine(
                pos=t, angle=90, pen=pg.mkPen("m", width=1, style=2)
            )
            line_gp = pg.InfiniteLine(
                pos=t, angle=90, pen=pg.mkPen("m", width=1, style=2)
            )
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
        self.controls.start_btn.setEnabled(True)
        self.controls.stop_btn.setEnabled(False)
        self.controls.save_btn.setEnabled(self._t_buf.size > 0)
        self.status_label.setText(
            f"Done. {self._t_buf.size} samples, {len(self._pulse_times)} pulses."
        )

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


def run(backend) -> int:
    app = QApplication.instance() or QApplication([])
    win = MainWindow(backend)
    win.resize(1200, 700)
    win.show()
    return app.exec()
