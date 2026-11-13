"""Provisioning-strategy abstraction.

A strategy is one way to "put the database into a known state for one
red/green/refactor cycle, then reset for the next." The runner measures
`provision()` and `reset()` wall-clock, plus storage and steady-state read
latency, exactly as defined in the benchmark methodology.

Lifecycle per (strategy, scale):
    setup_scale(...)                      # one-time, NOT measured
    for run in range(N):
        env = provision()                 # MEASURED -> provisioning time
        storage_bytes(env)                # at creation
        (optional) representative_write(env); storage_bytes(env, after_write=True)
        reset(env)                        # MEASURED -> reset time
        teardown(env)                     # NOT measured
    (separately) steady_state_latencies(provision())   # read amplification
    teardown_scale()                      # NOT measured
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Element:
    """A paper element (a/b/c/d) rating: True / False / 'partial'."""
    a_inner_loop: object
    b_real_db: object
    c_governed_prodshaped: object
    d_cow_economics: object

    def as_dict(self):
        return {"a_inner_loop": self.a_inner_loop, "b_real_db": self.b_real_db,
                "c_governed_prodshaped": self.c_governed_prodshaped,
                "d_cow_economics": self.d_cow_economics}


@dataclass
class ProvisionedEnv:
    """Opaque handle to one provisioned environment."""
    label: str
    conn_factory: object = None          # callable -> live DBAPI connection
    meta: dict = field(default_factory=dict)


class ProvisioningStrategy(abc.ABC):
    num: int = 0
    name: str = "base"
    engine: str = "unknown"
    #: paper elements this strategy satisfies
    elements: Element = Element(False, False, False, False)
    #: honest "Can't" note from the benchmark methodology
    limits: str = ""
    #: True if this strategy is expected to fail (record failure, don't crash)
    may_fail_at_large_scale: bool = False

    def __init__(self, cfg: dict, engine_cfg: dict):
        self.cfg = cfg
        self.engine_cfg = engine_cfg
        self.scale_name: str = ""
        self.scale_cfg: dict = {}
        self.data_files: dict = {}
        self.data_dir: Path | None = None

    # ---- one-time per scale (not measured) ----
    def setup_scale(self, scale_name: str, scale_cfg: dict, data_dir: Path, data_files: dict):
        self.scale_name = scale_name
        self.scale_cfg = scale_cfg
        self.data_dir = data_dir
        self.data_files = data_files

    def teardown_scale(self):  # noqa: B027 - optional hook
        pass

    # ---- measured ----
    @abc.abstractmethod
    def provision(self) -> ProvisionedEnv:
        """From 'no environment' to 'ready to run the first query'."""

    @abc.abstractmethod
    def reset(self, env: ProvisionedEnv) -> None:
        """Return to a clean known state for the next cycle."""

    # ---- non-measured hooks ----
    def teardown(self, env: ProvisionedEnv) -> None:  # noqa: B027
        pass

    def storage_bytes(self, env: ProvisionedEnv, after_write: bool = False) -> int | None:
        """Bytes one provisioned environment consumes. None if not applicable."""
        return None

    def representative_write(self, env: ProvisionedEnv) -> None:
        """A small representative write, to measure branch storage growth."""
        return None

    def steady_state_latencies(self, env: ProvisionedEnv, n: int) -> list[float]:
        """Return per-query latencies (seconds) for n representative reads."""
        return []


# ---------------------------------------------------------------------------
# Representative read workload, shared by SQL engines. Includes the paper's
# skew-sensitive "most active accounts" query (Table III), a point lookup, and
# a join-fan-out aggregate -- the reads whose first touch on CoW shared pages
# exposes read amplification.
# ---------------------------------------------------------------------------
READ_QUERIES = [
    # top active accounts (skew-sensitive; drives seq-scan vs index choice)
    "SELECT account_id, COUNT(*) c FROM orders GROUP BY account_id ORDER BY c DESC LIMIT 20",
    # point lookup on a hot account
    "SELECT order_id, amount_cents, status FROM orders WHERE account_id = 1 ORDER BY created_at DESC LIMIT 25",
    # join fan-out aggregate
    ("SELECT o.status, COUNT(i.item_id) items, SUM(i.qty) q "
     "FROM orders o JOIN order_items i ON i.order_id = o.order_id "
     "GROUP BY o.status"),
]
