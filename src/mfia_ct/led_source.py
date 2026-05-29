"""Multi-channel LED source interface (Mightex 8-ch controller).

The intent is a Mightex Sirius or BLS-series multi-channel LED controller
driven from Python over USB. The concrete interface (Windows DLL via
ctypes vs USB-serial ASCII) depends on the exact model number, which
isn't pinned yet — so this module provides:

- ``LedSource`` Protocol: the interface the rest of the project codes to.
- ``MightexStub``: real-driver placeholder; raises ``NotImplementedError``
  on connect with a clear next-step message. The CfExperiment will refuse
  to start an illuminated step against the stub, so a partial deployment
  can still run dark-only campaigns.
- ``MockLedSource``: in-memory implementation that records the call
  sequence and tracks per-channel state. Used by the GUI's --mock path
  and by tests to assert ordering ("disable before enable next", etc.).

When the Mightex SDK choice is made, replace MightexStub's connect()
with the real implementation; everything upstream stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@runtime_checkable
class LedSource(Protocol):
    n_channels: int

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def idn(self) -> str: ...
    def set_channel_current(self, channel: int, current_ma: float) -> None: ...
    def enable_channel(self, channel: int) -> None:
        """Turn on a single channel; implicitly turns off all others."""
        ...
    def disable_all(self) -> None: ...


class MightexStub:
    """Placeholder for the eventual real Mightex driver.

    Refusing connect() at construction time would prevent the GUI from
    even loading; refusing at connect() time lets the user pick "real
    LED source" in the GUI, see the helpful error, then switch to the
    mock without restarting.
    """

    n_channels = 8

    def __init__(self, resource: str = "") -> None:
        self.resource = resource

    def connect(self) -> None:
        raise NotImplementedError(
            "Mightex driver not yet implemented. Pin the model number "
            "(BLS-1000-2 / Sirius LED / etc.) and choose the interface "
            "(Mightex Windows SDK via ctypes, or USB serial ASCII), then "
            "fill in this method. The CfExperiment loop will work as soon "
            "as the methods of LedSource are implemented."
        )

    def disconnect(self) -> None:
        pass

    def idn(self) -> str:
        return "Mightex (stub — not implemented)"

    def set_channel_current(self, channel: int, current_ma: float) -> None:
        raise NotImplementedError

    def enable_channel(self, channel: int) -> None:
        raise NotImplementedError

    def disable_all(self) -> None:
        raise NotImplementedError


@dataclass
class _Call:
    name: str
    kwargs: dict = field(default_factory=dict)


class MockLedSource:
    """Records the call sequence + tracks per-channel state for tests.

    ``active_channel`` is None when all channels are off, otherwise the
    index of the single channel that was last enabled (matching the
    "only one on at a time" Mightex behavior most C-f campaigns want).
    """

    def __init__(self, n_channels: int = 8) -> None:
        self.n_channels = n_channels
        self.calls: list[_Call] = []
        self.currents_ma = [0.0] * n_channels
        self.active_channel: int | None = None
        self.connected = False

    def connect(self) -> None:
        self.calls.append(_Call("connect"))
        self.connected = True

    def disconnect(self) -> None:
        self.calls.append(_Call("disconnect"))
        self.connected = False

    def idn(self) -> str:
        return "MOCK,MightexLed,0,0"

    def set_channel_current(self, channel: int, current_ma: float) -> None:
        if not 0 <= channel < self.n_channels:
            raise ValueError(f"channel {channel} out of range 0..{self.n_channels - 1}")
        self.calls.append(_Call("set_channel_current", {"channel": channel, "current_ma": current_ma}))
        self.currents_ma[channel] = current_ma

    def enable_channel(self, channel: int) -> None:
        if not 0 <= channel < self.n_channels:
            raise ValueError(f"channel {channel} out of range 0..{self.n_channels - 1}")
        self.calls.append(_Call("enable_channel", {"channel": channel}))
        self.active_channel = channel

    def disable_all(self) -> None:
        self.calls.append(_Call("disable_all"))
        self.active_channel = None
