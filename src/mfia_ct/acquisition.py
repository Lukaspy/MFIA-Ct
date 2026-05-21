"""Data types for continuous acquisition.

The real and mock backends each implement
``start_continuous() / poll_continuous(length_s) / stop_continuous()`` and
return ``StreamChunk`` instances. The experiment orchestrator just polls in a
loop and yields whatever arrives.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class StreamChunk:
    """A contiguous chunk of continuously-recorded IA samples.

    ``t`` is in seconds since ``start_continuous()`` was called on the backend
    (so it begins at ~0 and grows monotonically across successive chunks).
    """

    t: np.ndarray
    cp: np.ndarray
    gp: np.ndarray
