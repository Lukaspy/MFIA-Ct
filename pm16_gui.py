#!/usr/bin/env python3
"""Thorlabs PM16-121 desktop readout.

A small standalone PyQt6 GUI with two views:
  - Rolling graph: pyqtgraph plot of power vs time, fixed-width window.
  - Indicator: big SI-formatted readout + running min / max / mean / stddev / N.

Both views are driven by the same reader thread, so stats and graph share data
and switching views never loses samples. Optional CSV logging mirrors what the
CLI ``pm16_read.py`` writes.

Dependencies (one-time):
    pip install pyvisa pyvisa-py pyusb ThorlabsPM100 PyQt6 pyqtgraph

Run with either the project venv or any Python that has the deps above:
    .venv/bin/python pm16_gui.py
    ./pm16_gui.py
"""

from __future__ import annotations

import csv
import math
import sys
import time
import warnings
from collections import deque
from typing import Optional

# pyvisa-py prints noisy LAN-discovery warnings on import; suppress at source.
warnings.filterwarnings("ignore", module="pyvisa_py.tcpip")

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
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
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

THORLABS_VID = 0x1313


def find_pm(rm) -> Optional[str]:
    for r in rm.list_resources():
        parts = r.split("::")
        if len(parts) < 3:
            continue
        try:
            vid = int(parts[1], 0)
        except ValueError:
            continue
        if vid == THORLABS_VID:
            return r
    return None


def fmt_power(w: float) -> str:
    """Format a power value with an SI prefix (4 sig figs)."""
    if not math.isfinite(w):
        return "—"
    a = abs(w)
    if a >= 1:
        return f"{w:.4f} W"
    if a >= 1e-3:
        return f"{w * 1e3:.4f} mW"
    if a >= 1e-6:
        return f"{w * 1e6:.4f} µW"
    if a >= 1e-9:
        return f"{w * 1e9:.4f} nW"
    if a == 0.0:
        return "0 W"
    return f"{w * 1e12:.4f} pW"


# --- Reader thread ----------------------------------------------------------


class PowerReader(QObject):
    """Polls ``pm.read`` at a fixed interval and emits each sample.

    Lives on its own QThread; the GUI never touches the VISA instrument
    directly so a slow USB query can't freeze the event loop.
    """

    sample = pyqtSignal(float, float)  # t_s, power_W
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, pm, interval_s: float) -> None:
        super().__init__()
        self._pm = pm
        self._interval = interval_s
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def set_interval(self, s: float) -> None:
        self._interval = s

    def run(self) -> None:
        t0 = time.monotonic()
        try:
            while not self._stop:
                try:
                    p = self._pm.read
                except Exception as e:
                    self.error.emit(f"{type(e).__name__}: {e}")
                    break
                t = time.monotonic() - t0
                self.sample.emit(t, float(p))
                # Sleep in small slices so stop() is responsive.
                remaining = self._interval
                while remaining > 0 and not self._stop:
                    chunk = min(0.05, remaining)
                    time.sleep(chunk)
                    remaining -= chunk
        finally:
            self.finished.emit()


# --- Views -------------------------------------------------------------------


class RollingGraphView(QWidget):
    """Scrolling power-vs-time plot, last ``window_s`` seconds."""

    def __init__(self, window_s: float = 60.0) -> None:
        super().__init__()
        self.window_s = window_s
        self.plot = pg.PlotWidget()
        self.plot.setLabel("left", "Power", "W")
        self.plot.setLabel("bottom", "Time", "s")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.curve = self.plot.plot(pen=pg.mkPen("c", width=1))
        self._t: deque[float] = deque()
        self._p: deque[float] = deque()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot)

    def set_window(self, s: float) -> None:
        self.window_s = s

    def add_sample(self, t: float, p: float) -> None:
        self._t.append(t)
        self._p.append(p)
        cutoff = t - self.window_s
        while self._t and self._t[0] < cutoff:
            self._t.popleft()
            self._p.popleft()
        self.curve.setData(list(self._t), list(self._p))
        self.plot.setXRange(cutoff, t, padding=0)

    def clear(self) -> None:
        self._t.clear()
        self._p.clear()
        self.curve.setData([], [])


class IndicatorView(QWidget):
    """Large numeric readout with running stats."""

    def __init__(self) -> None:
        super().__init__()
        self.current = QLabel("—")
        self.current.setAlignment(Qt.AlignmentFlag.AlignCenter)
        big = QFont()
        big.setPointSize(48)
        big.setBold(True)
        self.current.setFont(big)

        self.min_lbl = QLabel("—")
        self.max_lbl = QLabel("—")
        self.mean_lbl = QLabel("—")
        self.std_lbl = QLabel("—")
        self.n_lbl = QLabel("0")
        for lbl in (self.min_lbl, self.max_lbl, self.mean_lbl, self.std_lbl, self.n_lbl):
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            f = QFont("monospace")
            lbl.setFont(f)

        self.reset_btn = QPushButton("Reset stats")

        stats_form = QFormLayout()
        stats_form.addRow("Min", self.min_lbl)
        stats_form.addRow("Max", self.max_lbl)
        stats_form.addRow("Mean", self.mean_lbl)
        stats_form.addRow("Std dev", self.std_lbl)
        stats_form.addRow("Samples (N)", self.n_lbl)
        stats_form.addRow(self.reset_btn)
        stats_box = QGroupBox("Running stats")
        stats_box.setLayout(stats_form)

        layout = QVBoxLayout(self)
        layout.addWidget(self.current, stretch=1)
        layout.addWidget(stats_box, stretch=0)

        self.reset_btn.clicked.connect(self.reset_stats)
        self.reset_stats()

    def reset_stats(self) -> None:
        # Welford's online stats: avoids growing memory and stays numerically
        # stable for long-running sessions (the meter's nominal noise floor is
        # tiny compared to its absolute level — the naive sum-of-squares form
        # loses precision over hours).
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0
        self._min = math.inf
        self._max = -math.inf
        self.min_lbl.setText("—")
        self.max_lbl.setText("—")
        self.mean_lbl.setText("—")
        self.std_lbl.setText("—")
        self.n_lbl.setText("0")

    def add_sample(self, _t: float, p: float) -> None:
        self.current.setText(fmt_power(p))
        self._n += 1
        delta = p - self._mean
        self._mean += delta / self._n
        delta2 = p - self._mean
        self._m2 += delta * delta2
        if p < self._min:
            self._min = p
        if p > self._max:
            self._max = p

        self.min_lbl.setText(fmt_power(self._min))
        self.max_lbl.setText(fmt_power(self._max))
        self.mean_lbl.setText(fmt_power(self._mean))
        std = math.sqrt(self._m2 / (self._n - 1)) if self._n > 1 else 0.0
        self.std_lbl.setText(fmt_power(std))
        self.n_lbl.setText(str(self._n))


# --- Main window -------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Thorlabs PM16-121")

        self.inst = None
        self.pm = None
        self._thread: Optional[QThread] = None
        self._reader: Optional[PowerReader] = None
        self._csv_file = None
        self._csv_writer = None

        # ---- Connection group ----
        conn = QGroupBox("Connection")
        conn_form = QFormLayout(conn)
        self.resource = QLineEdit("")
        self.resource.setPlaceholderText("auto-discover Thorlabs USB if blank")
        self.wavelength = QDoubleSpinBox()
        self.wavelength.setRange(185, 25000)
        self.wavelength.setDecimals(1)
        self.wavelength.setValue(532.0)
        self.wavelength.setSuffix(" nm")
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.01, 10.0)
        self.interval.setDecimals(3)
        self.interval.setValue(0.1)
        self.interval.setSuffix(" s")
        self.connect_btn = QPushButton("Connect")
        self.status_lbl = QLabel("Not connected.")
        conn_form.addRow("VISA resource", self.resource)
        conn_form.addRow("Wavelength", self.wavelength)
        conn_form.addRow("Sample interval", self.interval)
        conn_form.addRow(self.connect_btn)
        conn_form.addRow(self.status_lbl)

        # ---- View switcher ----
        mode_box = QGroupBox("View")
        mode_layout = QHBoxLayout(mode_box)
        self.mode_graph = QRadioButton("Rolling graph")
        self.mode_indicator = QRadioButton("Indicator + stats")
        self.mode_graph.setChecked(True)
        mode_layout.addWidget(self.mode_graph)
        mode_layout.addWidget(self.mode_indicator)

        self.window_s = QDoubleSpinBox()
        self.window_s.setRange(2.0, 3600.0)
        self.window_s.setDecimals(1)
        self.window_s.setValue(60.0)
        self.window_s.setSuffix(" s window")
        mode_layout.addWidget(self.window_s)
        mode_layout.addStretch()

        # ---- CSV log group ----
        log_box = QGroupBox("CSV log")
        log_layout = QHBoxLayout(log_box)
        self.log_enable = QCheckBox("Append to:")
        self.log_path = QLineEdit("")
        self.log_browse = QPushButton("…")
        self.log_browse.setMaximumWidth(40)
        log_layout.addWidget(self.log_enable)
        log_layout.addWidget(self.log_path, stretch=1)
        log_layout.addWidget(self.log_browse)

        # ---- Views (stacked) ----
        self.graph_view = RollingGraphView(window_s=self.window_s.value())
        self.indicator_view = IndicatorView()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.graph_view)
        self.stack.addWidget(self.indicator_view)

        # ---- Layout ----
        central = QWidget()
        root = QVBoxLayout(central)
        top = QHBoxLayout()
        top.addWidget(conn, stretch=1)
        top.addWidget(mode_box, stretch=1)
        root.addLayout(top)
        root.addWidget(log_box)
        root.addWidget(self.stack, stretch=1)
        self.setCentralWidget(central)

        # ---- Wiring ----
        self.connect_btn.clicked.connect(self._toggle_connection)
        self.mode_graph.toggled.connect(self._update_mode)
        self.mode_indicator.toggled.connect(self._update_mode)
        self.window_s.valueChanged.connect(self.graph_view.set_window)
        self.wavelength.valueChanged.connect(self._on_wavelength_changed)
        self.interval.valueChanged.connect(self._on_interval_changed)
        self.log_browse.clicked.connect(self._browse_log)
        self._update_mode()

    # ---- Connection management ---------------------------------------------

    def _toggle_connection(self) -> None:
        if self.inst is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        try:
            import pyvisa
            from ThorlabsPM100 import ThorlabsPM100
        except ImportError as e:
            QMessageBox.critical(
                self,
                "Missing dependency",
                f"{e}\n\nInstall:\n  pip install pyvisa pyvisa-py pyusb ThorlabsPM100",
            )
            return

        rm = pyvisa.ResourceManager("@py")
        resource = self.resource.text().strip() or find_pm(rm)
        if not resource:
            QMessageBox.warning(
                self,
                "No meter found",
                "No Thorlabs USB device detected. Plug it in, or enter a "
                "VISA resource string manually.",
            )
            return

        try:
            inst = rm.open_resource(resource)
            inst.timeout = 3000
            pm = ThorlabsPM100(inst=inst)
            idn = inst.query("*IDN?").strip()
            pm.sense.correction.wavelength = self.wavelength.value()
        except Exception as e:
            QMessageBox.critical(
                self,
                "Connection failed",
                f"Could not connect to {resource}:\n{type(e).__name__}: {e}",
            )
            return

        self.inst = inst
        self.pm = pm
        self.status_lbl.setText(f"Connected: {idn}")
        self.resource.setText(resource)
        self.connect_btn.setText("Disconnect")

        # Start the reader thread.
        self._thread = QThread()
        self._reader = PowerReader(pm, self.interval.value())
        self._reader.moveToThread(self._thread)
        self._thread.started.connect(self._reader.run)
        self._reader.sample.connect(self._on_sample)
        self._reader.error.connect(self._on_reader_error)
        self._reader.finished.connect(self._on_reader_finished)
        self._thread.start()

        self._open_csv_if_needed()

    def _disconnect(self) -> None:
        if self._reader is not None:
            self._reader.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._reader = None
        if self.inst is not None:
            try:
                self.inst.close()
            except Exception:
                pass
        self.inst = None
        self.pm = None
        self._close_csv()
        self.status_lbl.setText("Disconnected.")
        self.connect_btn.setText("Connect")

    # ---- Reader callbacks --------------------------------------------------

    def _on_sample(self, t: float, p: float) -> None:
        self.graph_view.add_sample(t, p)
        self.indicator_view.add_sample(t, p)
        if self._csv_writer is not None:
            self._csv_writer.writerow([f"{t:.6f}", f"{p:.9e}", self.wavelength.value()])
            self._csv_file.flush()

    def _on_reader_error(self, msg: str) -> None:
        QMessageBox.warning(self, "Reader error", msg)
        self._disconnect()

    def _on_reader_finished(self) -> None:
        pass  # handled in _disconnect

    # ---- Settings hooks ----------------------------------------------------

    def _on_wavelength_changed(self, val: float) -> None:
        # Apply live so the meter's wavelength correction tracks the spinbox.
        if self.pm is not None:
            try:
                self.pm.sense.correction.wavelength = val
            except Exception:
                pass

    def _on_interval_changed(self, val: float) -> None:
        if self._reader is not None:
            self._reader.set_interval(val)

    def _update_mode(self) -> None:
        self.stack.setCurrentIndex(0 if self.mode_graph.isChecked() else 1)
        self.window_s.setEnabled(self.mode_graph.isChecked())

    # ---- CSV logging -------------------------------------------------------

    def _browse_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "CSV log", "pm16_log.csv", "CSV files (*.csv)"
        )
        if path:
            self.log_path.setText(path)

    def _open_csv_if_needed(self) -> None:
        if not self.log_enable.isChecked():
            return
        path = self.log_path.text().strip()
        if not path:
            QMessageBox.information(self, "CSV log", "Pick a log file path first.")
            return
        try:
            self._csv_file = open(path, "a", newline="")
            self._csv_writer = csv.writer(self._csv_file)
            if self._csv_file.tell() == 0:
                self._csv_writer.writerow(["t_s", "power_W", "wavelength_nm"])
        except Exception as e:
            QMessageBox.warning(self, "CSV log", f"Could not open {path}:\n{e}")
            self._csv_file = None
            self._csv_writer = None

    def _close_csv(self) -> None:
        if self._csv_file is not None:
            try:
                self._csv_file.close()
            except Exception:
                pass
        self._csv_file = None
        self._csv_writer = None

    # ---- Window close ------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._disconnect()
        super().closeEvent(event)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow()
    win.resize(900, 600)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
