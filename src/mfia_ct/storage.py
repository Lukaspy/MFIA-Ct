"""HDF5 save for continuous photo-C-t runs.

Layout::

    run.h5
    ├── /stream/t      (N,)  seconds since start of run
    ├── /stream/cp     (N,)  Cp in farads
    ├── /stream/gp     (N,)  Gp in siemens
    ├── /pulse_times   (M,)  seconds since start of run
    └── attrs: full CtConfig as JSON
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np

from .config import CtConfig


def save_run(
    path: str | Path,
    cfg: CtConfig,
    t: np.ndarray,
    cp: np.ndarray,
    gp: np.ndarray,
    pulse_times: list[float],
    extra: dict | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as f:
        f.attrs["config_json"] = json.dumps(cfg.to_dict(), default=str)
        f.attrs["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
        f.attrs["n_samples"] = int(t.size)
        f.attrs["n_pulses"] = len(pulse_times)
        if extra:
            for k, v in extra.items():
                f.attrs[k] = v

        f.create_dataset("stream/t", data=t, compression="gzip")
        f.create_dataset("stream/cp", data=cp, compression="gzip")
        f.create_dataset("stream/gp", data=gp, compression="gzip")
        f.create_dataset(
            "pulse_times", data=np.asarray(pulse_times, dtype=float), compression="gzip"
        )
    return path
