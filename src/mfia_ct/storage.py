"""HDF5 save for C-t runs.

Layout::

    run.h5
    ├── /segments/0000/t   (n,)
    ├── /segments/0000/cp  (n,)
    ├── /segments/0000/gp  (n,)
    ├── /segments/0001/...
    └── attrs: full CtConfig as JSON
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import h5py

from .acquisition import CtSegment
from .config import CtConfig


def save_run(
    path: str | Path, cfg: CtConfig, segments: list[CtSegment], extra: dict | None = None
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as f:
        f.attrs["config_json"] = json.dumps(cfg.to_dict(), default=str)
        f.attrs["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
        f.attrs["n_segments"] = len(segments)
        if extra:
            for k, v in extra.items():
                f.attrs[k] = v

        grp = f.create_group("segments")
        for i, seg in enumerate(segments):
            sg = grp.create_group(f"{i:04d}")
            sg.attrs["pulse_index"] = i
            sg.attrs["t0_s"] = seg.t0_s
            sg.create_dataset("t", data=seg.t, compression="gzip")
            sg.create_dataset("cp", data=seg.cp, compression="gzip")
            sg.create_dataset("gp", data=seg.gp, compression="gzip")
    return path
