"""Data types for continuous acquisition.

The real and mock backends each implement
``start_continuous() / poll_continuous(length_s) / stop_continuous()`` and
return ``StreamChunk`` instances. The experiment orchestrator just polls in a
loop and yields whatever arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class StreamChunk:
    """A contiguous chunk of continuously-recorded IA samples.

    ``t`` is in seconds since ``start_continuous()`` was called on the backend
    (so it begins at ~0 and grows monotonically across successive chunks).

    ``pulse_edges_s`` carries pulse-rising-edge times in the same time base.
    Only populated when external-sync mode is active (the backend detects Aux
    In threshold crossings inside ``poll_continuous``).
    """

    t: np.ndarray
    cp: np.ndarray
    gp: np.ndarray
    pulse_edges_s: list[float] = field(default_factory=list)
