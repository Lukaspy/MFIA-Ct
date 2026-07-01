"""Shared "Instrument" group: backend selection + connect/disconnect.

The C-t and C-f GUIs both need the same MFIA-or-mock selection up top.
Factoring it out keeps the two apps from duplicating ~80 lines of widget
plumbing and the connect-failure dialogs.

Emits two signals so each app can wire up its own secondary instruments
(C-t connects a function generator, C-f connects a Mightex LED source)
based on whether the backend is mock or real:

    connected(backend, kind)   # kind ∈ {"mock", "real"}
    disconnected()

The host app calls ``set_busy(True)`` while a long-running operation
(acquisition / sweep campaign) is running to grey out Connect, then
``set_busy(False)`` when it's done.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)


class BackendKind:
    MOCK = "Mock (synthetic)"
    REAL = "Real MFIA"
    B1500 = "Real B1500"
    # Normalized values emitted in signals — string is cleaner than passing
    # the display label, and survives translation/relabeling.
    MOCK_KEY = "mock"
    REAL_KEY = "real"
    B1500_KEY = "b1500"


def _int_spin(value: int, lo: int, hi: int) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    return s


class InstrumentPanel(QGroupBox):
    connected = pyqtSignal(object, str)  # backend, BackendKind.*_KEY
    disconnected = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Instrument", parent)
        form = QFormLayout(self)
        self.backend_kind = QComboBox()
        self.backend_kind.addItems(
            [BackendKind.MOCK, BackendKind.REAL, BackendKind.B1500]
        )
        self.device_id = QLineEdit("")
        self.device_id.setPlaceholderText("e.g. dev32369")
        self.host = QLineEdit("localhost")
        self.port = _int_spin(8004, 1, 65535)
        # B1500 (VISA/GPIB) — the FLEX command set is reached over GPIB
        # (default address 17) or USB; there is no native LAN socket.
        self.resource = QLineEdit("GPIB0::18::INSTR")
        self.resource.setPlaceholderText("VISA resource, e.g. GPIB0::18::INSTR")
        self.connect_btn = QPushButton("Connect")
        self.status = QLabel("Not connected.")

        form.addRow("Backend", self.backend_kind)
        form.addRow("Device ID", self.device_id)
        form.addRow("LabOne host", self.host)
        form.addRow("LabOne port", self.port)
        form.addRow("VISA resource", self.resource)
        form.addRow(self.connect_btn)
        form.addRow(self.status)

        # Which fields belong to which backend (enabled only when selected).
        self._mfia_only = [self.device_id, self.host, self.port]
        self._b1500_only = [self.resource]
        self.backend_kind.currentTextChanged.connect(self._update_field_state)
        self.connect_btn.clicked.connect(self._toggle)
        self._update_field_state()

        self.backend: object | None = None
        self._kind: str | None = None

    # ---- Public API --------------------------------------------------------

    def preselect(
        self,
        *,
        mock: bool = False,
        device: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        if mock:
            self.backend_kind.setCurrentText(BackendKind.MOCK)
        elif device is not None:
            self.backend_kind.setCurrentText(BackendKind.REAL)
        if device is not None:
            self.device_id.setText(device)
        if host is not None:
            self.host.setText(host)
        if port is not None:
            self.port.setValue(port)

    def kind(self) -> str | None:
        return self._kind

    def is_connected(self) -> bool:
        return self.backend is not None

    def set_busy(self, busy: bool) -> None:
        """Greys out Connect while the host app's worker is busy."""
        self.connect_btn.setEnabled(not busy)

    # ---- Internals ---------------------------------------------------------

    def _toggle(self) -> None:
        if self.backend is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        kind_label = self.backend_kind.currentText()
        try:
            if kind_label == BackendKind.MOCK:
                from ..mock_hardware import MockMFIA

                self.backend = MockMFIA()
                self._kind = BackendKind.MOCK_KEY
                label = "Mock backend"
            elif kind_label == BackendKind.B1500:
                from ..b1500 import B1500

                resource = self.resource.text().strip()
                if not resource:
                    QMessageBox.warning(
                        self, "Instrument",
                        "Enter a VISA resource (e.g. GPIB0::17::INSTR).",
                    )
                    return
                backend = B1500(resource)
                backend.connect()
                self.backend = backend
                self._kind = BackendKind.B1500_KEY
                label = f"B1500 {resource}"
            else:
                from ..hardware import MFIA

                device = self.device_id.text().strip()
                if not device:
                    QMessageBox.warning(
                        self, "Instrument", "Enter a device ID (e.g. dev32369)."
                    )
                    return
                backend = MFIA(
                    device,
                    server_host=self.host.text().strip() or "localhost",
                    server_port=self.port.value(),
                )
                backend.connect()
                self.backend = backend
                self._kind = BackendKind.REAL_KEY
                label = f"MFIA {device}"
        except Exception as e:
            self.backend = None
            self._kind = None
            QMessageBox.critical(
                self,
                "Instrument",
                f"Could not connect:\n{type(e).__name__}: {e}",
            )
            self.status.setText("Connection failed.")
            return

        self.status.setText(f"Connected: {label}.")
        self.connect_btn.setText("Disconnect")
        self.backend_kind.setEnabled(False)
        for w in self._mfia_only + self._b1500_only:
            w.setEnabled(False)
        self.connected.emit(self.backend, self._kind)

    def _disconnect(self) -> None:
        if self.backend is not None and hasattr(self.backend, "disconnect"):
            try:
                self.backend.disconnect()
            except Exception:
                pass
        self.backend = None
        self._kind = None
        self.status.setText("Not connected.")
        self.connect_btn.setText("Connect")
        self.backend_kind.setEnabled(True)
        self._update_field_state()
        self.disconnected.emit()

    def _update_field_state(self) -> None:
        """Enable only the fields for the currently-selected backend."""
        kind = self.backend_kind.currentText()
        for w in self._mfia_only:
            w.setEnabled(kind == BackendKind.REAL)
        for w in self._b1500_only:
            w.setEnabled(kind == BackendKind.B1500)
