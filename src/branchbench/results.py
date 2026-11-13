"""Result records + persistence (raw JSONL + aggregated JSON)."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .timing import Samples


@dataclass
class CellResult:
    """One (strategy, scale) cell of the matrix."""
    strategy_num: int
    strategy: str
    engine: str
    scale: str
    scale_target_bytes: int
    status: str = "ok"                       # ok | failed | skipped
    error: str = ""
    runs: int = 0
    cold_run_s: float | None = None          # first run, reported separately
    provision: dict = field(default_factory=dict)   # Samples.as_dict()
    reset: dict = field(default_factory=dict)
    storage_at_creation_bytes: int | None = None
    storage_after_write_bytes: int | None = None
    steady_state_read: dict = field(default_factory=dict)   # Samples.as_dict() (ms)
    elements: dict = field(default_factory=dict)
    limits: str = ""
    notes: str = ""

    def as_dict(self):
        return asdict(self)


class ResultStore:
    def __init__(self, out_dir: str | Path, run_id: str | None = None):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or time.strftime("%Y%m%dT%H%M%S")
        self.raw_path = self.out_dir / f"raw-{self.run_id}.jsonl"
        self.cells: list[CellResult] = []
        self.meta: dict = {}

    def record_raw(self, event: dict) -> None:
        with open(self.raw_path, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def add_cell(self, cell: CellResult) -> None:
        self.cells.append(cell)
        self.record_raw({"type": "cell", **cell.as_dict()})

    def write_summary(self) -> Path:
        summary = {
            "run_id": self.run_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "meta": self.meta,
            "cells": [c.as_dict() for c in self.cells],
        }
        path = self.out_dir / f"summary-{self.run_id}.json"
        path.write_text(json.dumps(summary, indent=2, default=str))
        # Also keep a stable "latest" pointer for the reporter.
        (self.out_dir / "summary-latest.json").write_text(json.dumps(summary, indent=2, default=str))
        return path
