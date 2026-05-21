"""PyQt6 main window for the photo-C-t experiment.

Parameter panel on the left, live pyqtgraph plots of Cp(t) and Gp(t) on the
right. Acquisition runs in a worker QThread that yields CtSegments back to
the main thread via a signal, where they get accumulated and drawn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QThread, pyqtSignal
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
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..acquisition import CtSegment
from ..config import (
    AcquisitionSettings,
    CtConfig,
    DemodSettings,
    EquivCircuit,
    IASettings,
    PulseSettings,
    TriggerSource,
)
from ..experiment import CtExperiment
from ..storage import save_run


class AcquisitionWorker(QObject):
    segment = pyqtSignal(object)  # CtSegment
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, backend, cfg: CtConfig) -> None:
        super().__init__()
        self.backend = backend
        self.cfg = cfg
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            exp = CtExperiment(self.backend, self.cfg)
            for seg in exp.run():
                if self._stop:
                    break
                self.segment.emit(seg)
        except Exception as e:  # surface to GUI rather than killing the thread
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
        # Front panel labels Aux Outputs 1-4; the API uses indices 0-3.
        for i in range(4):
            self.aux_ch.addItem(f"Aux Out {i + 1}", userData=i)
        self.high_v = _spin(5.0, 0.0, 10.0, 0.1, decimals=2)
        self.low_v = _spin(0.0, -10.0, 10.0, 0.1, decimals=2)
        self.pulse_width = _spin(0.010, 1e-6, 10.0, 0.001, decimals=6)
        self.period = _spin(1.0, 0.01, 60.0, 0.1, decimals=3)
        self.n_pulses = _int_spin(20, 1, 10_000)
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
        self.pre_trig = _spin(0.001, 0.0, 1.0, 0.001, decimals=6)
        self.duration = _spin(0.5, 1e-3, 60.0, 0.05, decimals=4)
        self.reps = _int_spin(1, 1, 1000)
        self.trig_src = QComboBox()
        self.trig_src.addItems([s.value for s in TriggerSource])
        self.trig_level = _spin(1.0, -10.0, 10.0, 0.1, decimals=3)
        acq_form.addRow("Pre-trigger (s)", self.pre_trig)
        acq_form.addRow("Duration (s)", self.duration)
        acq_form.addRow("Repetitions", self.reps)
        acq_form.addRow("Trigger source", self.trig_src)
        acq_form.addRow("Trigger level (V)", self.trig_level)
        layout.addWidget(acq)

        # Run / save buttons
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.save_btn = QPushButton("Save HDF5…")
        self.save_btn.setEnabled(False)
        self.show_average = QCheckBox("Show average")
        self.show_average.setChecked(True)
        btns = QHBoxLayout()
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.save_btn)
        layout.addLayout(btns)
        layout.addWidget(self.show_average)
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
            acq=AcquisitionSettings(
                pre_trigger_s=self.pre_trig.value(),
                duration_s=self.duration.value(),
                repetitions=self.reps.value(),
                trigger_source=TriggerSource(self.trig_src.currentText()),
                trigger_level_v=self.trig_level.value(),
            ),
        )


class MainWindow(QMainWindow):
    def __init__(self, backend) -> None:
        super().__init__()
        self.backend = backend
        self.setWindowTitle("MFIA-Ct — Photo-Capacitance Transient")

        self.controls = ControlPanel()
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.cp_plot = self.plot_widget.addPlot(row=0, col=0, title="Cp (F)")
        self.gp_plot = self.plot_widget.addPlot(row=1, col=0, title="Gp (S)")
        self.cp_plot.setLabel("bottom", "Time", "s")
        self.gp_plot.setLabel("bottom", "Time", "s")
        self.cp_plot.showGrid(x=True, y=True, alpha=0.3)
        self.gp_plot.showGrid(x=True, y=True, alpha=0.3)
        self.cp_plot.addLegend()
        self.gp_plot.addLegend()
        self.cp_avg = self.cp_plot.plot(pen=pg.mkPen("y", width=2), name="avg")
        self.gp_avg = self.gp_plot.plot(pen=pg.mkPen("y", width=2), name="avg")
        self.cp_last = self.cp_plot.plot(pen=pg.mkPen("c", width=1), name="last")
        self.gp_last = self.gp_plot.plot(pen=pg.mkPen("c", width=1), name="last")

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

        self._segments: list[CtSegment] = []
        self._cfg: CtConfig | None = None
        self._thread: QThread | None = None
        self._worker: AcquisitionWorker | None = None

    def start(self) -> None:
        self._cfg = self.controls.config()
        self._segments = []
        self.progress.setMaximum(self._cfg.pulse.n_pulses)
        self.progress.setValue(0)
        self.status_label.setText("Running…")

        self._thread = QThread()
        self._worker = AcquisitionWorker(self.backend, self._cfg)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.segment.connect(self._on_segment)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

        self.controls.start_btn.setEnabled(False)
        self.controls.stop_btn.setEnabled(True)
        self.controls.save_btn.setEnabled(False)

    def stop(self) -> None:
        if self._worker:
            self._worker.stop()

    def _on_segment(self, seg: CtSegment) -> None:
        self._segments.append(seg)
        self.cp_last.setData(seg.t, seg.cp)
        self.gp_last.setData(seg.t, seg.gp)
        if self.controls.show_average.isChecked() and len(self._segments) > 1:
            cp_avg = np.mean([s.cp for s in self._segments], axis=0)
            gp_avg = np.mean([s.gp for s in self._segments], axis=0)
            self.cp_avg.setData(self._segments[0].t, cp_avg)
            self.gp_avg.setData(self._segments[0].t, gp_avg)
        self.progress.setValue(len(self._segments))

    def _on_error(self, msg: str) -> None:
        self.status_label.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Acquisition error", msg)

    def _on_finished(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self.controls.start_btn.setEnabled(True)
        self.controls.stop_btn.setEnabled(False)
        self.controls.save_btn.setEnabled(bool(self._segments))
        self.status_label.setText(f"Done. {len(self._segments)} segments.")

    def save(self) -> None:
        if not self._segments or self._cfg is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save run", "ct_run.h5", "HDF5 files (*.h5 *.hdf5)"
        )
        if not path:
            return
        out = save_run(path, self._cfg, self._segments)
        self.status_label.setText(f"Saved to {out}")


def run(backend) -> int:
    app = QApplication.instance() or QApplication([])
    win = MainWindow(backend)
    win.resize(1200, 700)
    win.show()
    return app.exec()
