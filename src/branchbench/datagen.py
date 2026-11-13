"""Deterministic production-shaped data generator.

Writes tab-delimited files with `\\N` as the NULL sentinel -- the native TEXT
format for Postgres COPY, so every engine loads
byte-identical data from the same files. Given a fixed seed the output is
reproducible, so every strategy at a given scale seeds from the same parent.

Production-shaped properties (benchmark methodology):
  * power-law hot keys   -- a few accounts receive most orders/events
  * real null patterns   -- email/legacy_code/note/payload null at real rates
  * duplicate keys       -- events carry repeating dedupe_key values
  * wide values          -- occasional long free-text note/payload
"""
from __future__ import annotations

import csv
import os
import random
from dataclasses import dataclass
from pathlib import Path

from . import schema

NULL = "\\N"

REGIONS = ["us-east", "us-west", "eu-central", "eu-west", "ap-south", "ap-southeast"]
TIERS = ["free", "standard", "premium", "enterprise"]
ACCT_STATUS = ["active", "active", "active", "suspended", "closed"]
ORDER_STATUS = ["paid", "paid", "paid", "pending", "refunded", "failed"]
CURRENCIES = ["USD", "USD", "USD", "EUR", "GBP", "JPY", "INR"]
EVENT_TYPES = ["login", "view", "purchase", "refund", "support", "settings_change"]
SKUS = [f"SKU-{i:05d}" for i in range(2000)]

# Skew exponent: account_id = int(n * random()**EXP). EXP>1 concentrates draws
# on low ids (the "hot" accounts). ~3 gives a realistic long tail.
SKEW_EXP = 3.0
_EPOCH = 1_700_000_000  # arbitrary fixed base for created_at spread


@dataclass
class GenStats:
    accounts: int
    orders: int
    order_items: int
    events: int
    total_bytes: int
    files: dict[str, str]


def _clean(s: str) -> str:
    """Strip characters that would break tab-delimited TEXT format."""
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ").replace("\\", "/")


def _ts(rng: random.Random, span_days: int = 720) -> str:
    secs = _EPOCH + rng.randint(0, span_days * 86400)
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(secs))


def _hot_account(rng: random.Random, n_accounts: int) -> int:
    return 1 + int(n_accounts * (rng.random() ** SKEW_EXP)) % n_accounts


def generate(rows: int, seed: int, out_dir: str | Path) -> GenStats:
    """Generate the four CSV files for `rows` orders into out_dir. Idempotent
    per (rows, seed): if a completed marker exists, reuse the files."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    marker = out / f".done-{rows}-{seed}"
    files = {t: str(out / f"{t}.tsv") for t in schema.TABLES}

    if marker.exists() and all(os.path.exists(f) for f in files.values()):
        total = sum(os.path.getsize(f) for f in files.values())
        # Row counts are recomputed cheaply from the marker payload.
        counts = dict(line.split("=") for line in marker.read_text().splitlines() if "=" in line)
        return GenStats(int(counts["accounts"]), int(counts["orders"]),
                        int(counts["order_items"]), int(counts["events"]), total, files)

    rng = random.Random(seed)
    n_accounts = max(100, rows // 50)

    def _row(fields) -> str:
        # Tab-joined TEXT format (Postgres COPY native).
        # Fields are pre-sanitized of tab/newline/backslash by _clean, so no
        # csv quoting is needed; NULL sentinel is the literal "\N".
        return "\t".join(str(x) for x in fields) + "\n"

    # accounts
    with open(files["accounts"], "w", newline="") as f:
        for aid in range(1, n_accounts + 1):
            email = NULL if rng.random() < 0.15 else f"user{aid}@ex-{rng.choice(REGIONS)}.com"
            legacy = f"LGCY-{rng.randint(0, 9999):04d}" if rng.random() < 0.15 else NULL
            f.write(_row([aid, _clean(f"Account {aid} {rng.choice(['Corp','LLC','Inc','GmbH','KK'])}"),
                          rng.choice(REGIONS), rng.choice(TIERS), email, legacy,
                          _ts(rng), rng.choice(ACCT_STATUS)]))

    # orders + order_items + events, streamed together for locality
    n_orders = rows
    item_id = 0
    event_id = 0
    dedupe_pool = [f"DEDUP-{i:06d}" for i in range(max(50, rows // 20))]

    with open(files["orders"], "w", newline="") as fo, \
         open(files["order_items"], "w", newline="") as fi, \
         open(files["events"], "w", newline="") as fe:
        for oid in range(1, n_orders + 1):
            acct = _hot_account(rng, n_accounts)
            # note: usually null; occasionally wide free text.
            if rng.random() < 0.30:
                note = _clean("promo " * rng.randint(1, 60)) if rng.random() < 0.1 else _clean(f"ref#{rng.randint(1000,9999)}")
            else:
                note = NULL
            fo.write(_row([oid, acct, rng.randint(100, 5_000_00), rng.choice(CURRENCIES),
                           rng.choice(ORDER_STATUS), note, _ts(rng)]))

            for _ in range(rng.choices([0, 1, 2, 3, 4, 6], weights=[3, 25, 30, 20, 12, 10])[0]):
                item_id += 1
                fi.write(_row([item_id, oid, rng.choice(SKUS), rng.randint(1, 20), rng.randint(50, 200_00)]))

            for _ in range(rng.choices([0, 1, 2, 4], weights=[20, 45, 25, 10])[0]):
                event_id += 1
                payload = NULL if rng.random() < 0.2 else _clean(f'{{"k":{rng.randint(0,999)}}}')
                fe.write(_row([event_id, acct, rng.choice(EVENT_TYPES),
                               rng.choice(dedupe_pool), payload, _ts(rng)]))

    stats = GenStats(n_accounts, n_orders, item_id, event_id,
                     sum(os.path.getsize(f) for f in files.values()), files)
    marker.write_text(
        f"accounts={stats.accounts}\norders={stats.orders}\n"
        f"order_items={stats.order_items}\nevents={stats.events}\n"
    )
    return stats


def sqlite_rows(tsv_path: str):
    """Yield tuples from a generated TSV, converting `\\N` -> None (for SQLite)."""
    with open(tsv_path, newline="") as f:
        for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            yield tuple(None if v == "\\N" else v for v in row)
