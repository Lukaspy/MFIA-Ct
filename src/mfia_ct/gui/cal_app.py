"""PyQt6 GUI for automated LED power calibration via the PM16 meter.

Drives the LED source (NI PXI-7853R via led_driver) and reads the Thorlabs
PM16 to build per-wavelength (drive%, power_mW) curves, then persists them
in the led_driver calibration format so LEDController(use_cal=True) /
mfia-cf's "apply power calibration" use them.

This is the automated replacement for the led_driver GUI's manual
"type the power reading" Calibrate… dialog.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
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

from ..led_calibration import CalPoint, CalSpec, calibrate, save_calibration
from ..led_source import DEFAULT_WAVELENGTHS_NM

# Distinct trace colors per canonical wavelength (UV→IR).
_WL_COLORS = {
    385.0: (160, 30, 220),
    470.0: (30, 100, 220),
    505.0: (30, 200, 200),
    530.0: (50, 200, 30),
    590.0: (220, 200, 30),
    625.0: (220, 100, 30),
    740.0: (200, 30, 30),
    850.0: (140, 20, 20),
}


def _spin(value, lo, hi, step, decimals=2):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    return s


class CalWorker(QObject):
    point = pyqtSignal(object)  # CalPoint
    progress = pyqtSignal(int, int, float, float)
    done = pyqtSignal(object)  # curves dict
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, led, meter, spec: CalSpec) -> None:
        super().__init__()
        self.led = led
        self.meter = meter
        self.spec = spec
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            self.led.connect()
            self.meter.connect()
            curves = calibrate(
                self.led,
                self.meter,
                self.spec,
                progress_cb=lambda wi, pi, nm, drive: self.progress.emit(wi, pi, nm, drive),
                point_cb=lambda p: self.point.emit(p),
                stop_check=lambda: self._stop,
            )
            self.done.emit(curves)
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            try:
                self.led.all_off()
            except Exception:
                pass
            try:
                self.led.disconnect()
            except Exception:
                pass
            try:
                self.meter.disconnect()
            except Exception:
                pass
            self.finished.emit()


class CalMainWindow(QMainWindow):
    def __init__(self, *, preselect_mock: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("MFIA — LED Power Calibration (PM16)")
        self._mock = preselect_mock
        self._curves: dict[float, list[CalPoint]] = {}
        self._thread: Optional[QThread] = None
        self._worker: Optional[CalWorker] = None

        # ---- LED source ----
        led = QGroupBox("LED source (PXI-7853R)")
        led_form = QFormLayout(led)
        self.led_bitfile = QLineEdit("")
        self.led_bitfile.setPlaceholderText("blank = led_driver mock (no FPGA)")
        bf_browse = QPushButton("…")
        bf_browse.setMaximumWidth(40)
        bf_browse.clicked.connect(self._browse_bitfile)
        bf_row = QHBoxLayout()
        bf_row.addWidget(self.led_bitfile, stretch=1)
        bf_row.addWidget(bf_browse)
        bf_holder = QWidget()
        bf_holder.setLayout(bf_row)
        self.led_resource = QLineEdit("RIO0")
        led_form.addRow(".lvbitx bitfile", bf_holder)
        led_form.addRow("NI-RIO resource", self.led_resource)

        # ---- PM16 meter ----
        meter = QGroupBox("Power meter (PM16)")
        meter_form = QFormLayout(meter)
        self.meter_resource = QLineEdit("")
        self.meter_resource.setPlaceholderText("blank = auto-discover Thorlabs USB")
        meter_form.addRow("VISA resource", self.meter_resource)

        # ---- Channels + sweep params ----
        chans = QGroupBox("Channels to calibrate")
        chans_layout = QVBoxLayout(chans)
        self.channel_boxes: list[tuple[float, QCheckBox]] = []
        for wl in DEFAULT_WAVELENGTHS_NM:
            cb = QCheckBox(f"{int(wl)} nm")
            cb.setChecked(True)
            chans_layout.addWidget(cb)
            self.channel_boxes.append((wl, cb))

        params = QGroupBox("Sweep")
        params_form = QFormLayout(params)
        self.drive_points = QLineEdit("0,25,50,75,100")
        self.settle_s = _spin(2.0, 0.0, 60.0, 0.5, decimals=2)
        self.settle_s.setSuffix(" s")
        self.averages = QSpinBox()
        self.averages.setRange(1, 1000)
        self.averages.setValue(10)
        self.equalize = QCheckBox("Equalize power across channels")
        self.equalize.setChecked(True)
        params_form.addRow("Drive points (%)", self.drive_points)
        params_form.addRow("Settle per point", self.settle_s)
        params_form.addRow("Meter averages", self.averages)
        params_form.addRow(self.equalize)

        # ---- Buttons ----
        self.run_btn = QPushButton("Run calibration")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.save_btn = QPushButton("Save calibration")
        self.save_btn.setEnabled(False)
        btns = QHBoxLayout()
        btns.addWidget(self.run_btn)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.save_btn)

        left = QVBoxLayout()
        left.addWidget(led)
        left.addWidget(meter)
        left.addWidget(chans)
        left.addWidget(params)
        left.addLayout(btns)
        left.addStretch()
        left_holder = QWidget()
        left_holder.setLayout(left)

        # ---- Plot: power vs drive ----
        self.plot = pg.PlotWidget()
        self.plot.setLabel("left", "Power", "mW")
        self.plot.setLabel("bottom", "Drive", "%")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend()
        self._curve_items: dict[float, pg.PlotDataItem] = {}

        self.progress = QProgressBar()
        self.status = QLabel("Configure and press Run calibration.")

        right = QVBoxLayout()
        right.addWidget(self.plot, stretch=1)
        right.addWidget(self.progress)
        right.addWidget(self.status)
        right_holder = QWidget()
        right_holder.setLayout(right)

        central = QWidget()
        root = QHBoxLayout(central)
        root.addWidget(left_holder, stretch=0)
        root.addWidget(right_holder, stretch=1)
        self.setCentralWidget(central)

        self.run_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.save_btn.clicked.connect(self.save)

    # ---- helpers ----

    def _browse_bitfile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "FPGA bitfile", self.led_bitfile.text(), "LabVIEW FPGA (*.lvbitx)"
        )
        if path:
            self.led_bitfile.setText(path)

    def _selected_wavelengths(self) -> list[float]:
        return [wl for wl, cb in self.channel_boxes if cb.isChecked()]

    def _parse_drive_points(self) -> list[float]:
        out = []
        for piece in self.drive_points.text().split(","):
            piece = piece.strip()
            if piece:
                out.append(float(piece))
        return out

    def _make_led(self):
        from ..led_source import MockLedSource, PxiLedSource

        if self._mock:
            return MockLedSource()
        bf = self.led_bitfile.text().strip() or None
        return PxiLedSource(bitfile=bf, resource=self.led_resource.text().strip() or "RIO0")

    def _make_meter(self, led):
        from ..pm16 import MockPowerMeter, ThorlabsPM16

        if self._mock:
            return MockPowerMeter(led=led)
        return ThorlabsPM16(self.meter_resource.text().strip() or None)

    # ---- run lifecycle ----

    def start(self) -> None:
        wls = self._selected_wavelengths()
        if not wls:
            QMessageBox.warning(self, "Calibration", "Select at least one channel.")
            return
        try:
            drive = self._parse_drive_points()
        except ValueError as e:
            QMessageBox.critical(self, "Calibration", f"Bad drive points:\n{e}")
            return
        if len(drive) < 2:
            QMessageBox.warning(self, "Calibration", "Need at least 2 drive points (e.g. 0,100).")
            return

        spec = CalSpec(
            wavelengths_nm=wls,
            drive_points_pct=drive,
            settle_s=self.settle_s.value(),
            meter_averages=self.averages.value(),
        )
        led = self._make_led()
        meter = self._make_meter(led)

        self._curves = {}
        self._clear_plot()
        total_points = len(wls) * len(drive)
        self.progress.setMaximum(total_points)
        self.progress.setValue(0)
        self._points_done = 0
        self.status.setText("Calibrating…")

        self._thread = QThread()
        self._worker = CalWorker(led, meter, spec)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.point.connect(self._on_point)
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.save_btn.setEnabled(False)

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        self.status.setText("Stopping…")

    def _on_progress(self, wi: int, pi: int, nm: float, drive: float) -> None:
        self._points_done += 1
        self.progress.setValue(self._points_done)
        self.status.setText(f"{int(nm)} nm @ {drive:g}% …")

    def _on_point(self, p: CalPoint) -> None:
        self._curves.setdefault(p.wavelength_nm, []).append(p)
        self._redraw(p.wavelength_nm)

    def _on_done(self, curves: dict) -> None:
        self._curves = curves
        self.save_btn.setEnabled(bool(curves))

    def _on_error(self, msg: str) -> None:
        self.status.setText(f"Error: {msg}")
        QMessageBox.critical(self, "Calibration error", msg)

    def _on_finished(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        n = sum(len(v) for v in self._curves.values())
        if n:
            self.status.setText(f"Done. {len(self._curves)} channels, {n} points. Save to persist.")

    def _clear_plot(self) -> None:
        self.plot.clear()
        self._curve_items = {}

    def _redraw(self, nm: float) -> None:
        pts = sorted(self._curves.get(nm, []), key=lambda p: p.drive_pct)
        x = [p.drive_pct for p in pts]
        y = [p.power_mw for p in pts]
        color = _WL_COLORS.get(float(nm), (200, 200, 200))
        if nm not in self._curve_items:
            self._curve_items[nm] = self.plot.plot(
                x, y, pen=pg.mkPen(color, width=2),
                symbol="o", symbolSize=6, symbolBrush=color,
                name=f"{int(nm)} nm",
            )
        else:
            self._curve_items[nm].setData(x, y)

    def save(self) -> None:
        if not self._curves:
            return
        led = self._make_led()
        try:
            led.connect()
            try:
                save_calibration(
                    self._curves, led, equalize=self.equalize.isChecked(), enabled=True
                )
            finally:
                led.disconnect()
        except Exception as e:
            QMessageBox.critical(self, "Save", f"Could not save calibration:\n{e}")
            return
        self.status.setText("Calibration saved to led_driver calibration.json.")
        QMessageBox.information(
            self, "Saved",
            "Calibration written. LEDController(use_cal=True) and mfia-cf's "
            "'apply power calibration' will now use it.",
        )

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)


def run(*, preselect_mock: bool = False) -> int:
    app = QApplication.instance() or QApplication([])
    win = CalMainWindow(preselect_mock=preselect_mock)
    win.resize(1100, 700)
    win.show()
    return app.exec()
