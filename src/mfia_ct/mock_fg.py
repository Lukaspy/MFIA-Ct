"""Synthetic function-generator backend.

Records the sequence of high-level calls the experiment makes so tests can
assert that configure → arm → trigger happens after MFIA subscribe and that
disarm → disconnect happen on exit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Call:
    name: str
    kwargs: dict[str, Any] = field(default_factory=dict)


class MockFunctionGenerator:
    def __init__(self, resource: str = "MOCK::FG::INSTR") -> None:
        self.resource = resource
        self.calls: list[_Call] = []
        self.connected: bool = False
        self.output_on: bool = False
        self.last_config: dict[str, Any] | None = None
        self.trigger_count: int = 0

    def connect(self) -> None:
        self.calls.append(_Call("connect"))
        self.connected = True

    def disconnect(self) -> None:
        self.calls.append(_Call("disconnect"))
        self.connected = False

    def __enter__(self) -> "MockFunctionGenerator":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.disconnect()

    def idn(self) -> str:
        return "MOCK,33250A,0,0"

    def reset(self) -> None:
        self.calls.append(_Call("reset"))

    def configure_pulse_burst(
        self,
        *,
        freq_hz: float,
        width_s: float,
        n_cycles: int,
        high_v: float,
        low_v: float,
        load_ohms: str = "INF",
    ) -> None:
        if width_s >= 1.0 / freq_hz:
            raise ValueError(
                f"pulse width ({width_s} s) must be less than period "
                f"({1.0 / freq_hz} s)"
            )
        self.last_config = dict(
            freq_hz=freq_hz,
            width_s=width_s,
            n_cycles=n_cycles,
            high_v=high_v,
            low_v=low_v,
            load_ohms=load_ohms,
        )
        self.calls.append(_Call("configure_pulse_burst", dict(self.last_config)))
        self.output_on = True

    def trigger(self) -> None:
        self.calls.append(_Call("trigger"))
        self.trigger_count += 1

    def disarm(self) -> None:
        self.calls.append(_Call("disarm"))
        self.output_on = False
