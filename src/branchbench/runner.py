"""Benchmark runner: executes the strategy x scale matrix and records results.

Usage:
    python -m branchbench.runner --config config/benchmark.yaml \
        [--scales mb,gb] [--engines sqlite,postgres,rds_postgres,aurora_postgres,neon] \
        [--strategies pg_fresh_fixture,neon_cow_branch] \
        [--runs 20] [--allow-large] [--data-dir <dir>]
"""
from __future__ import annotations

import argparse
import importlib
import shutil
import sys
import traceback
from pathlib import Path

import yaml

from .datagen import generate
from .results import CellResult, ResultStore
from .timing import Samples, timed


def load_config(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def import_driver(dotted: str):
    module, cls = dotted.split(":")
    return getattr(importlib.import_module(module), cls)


def free_disk_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def run_cell(cfg, strat_cfg, scale_name, scale_cfg, data_dir, data_files, store):
    engine = strat_cfg["engine"]
    engine_cfg = cfg["engines"].get(engine, {})
    cls = import_driver(strat_cfg["driver"])
    drv = cls(cfg, engine_cfg)

    n_runs = cfg.get("runs", 20)
    cold_separate = cfg.get("cold_run_separate", True)
    total = n_runs + (1 if cold_separate else 0)

    cell = CellResult(
        strategy_num=strat_cfg.get("num", drv.num), strategy=drv.name, engine=engine,
        scale=scale_name, scale_target_bytes=scale_cfg["target_bytes"],
        elements=drv.elements.as_dict(), limits=drv.limits)

    print(f"\n=== [{drv.name}] scale={scale_name} (N={n_runs}{' +cold' if cold_separate else ''}) ===",
          flush=True)
    try:
        drv.setup_scale(scale_name, scale_cfg, Path(data_dir), data_files)
    except Exception as e:
        cell.status = "failed"
        cell.error = f"setup: {e}"
        print(f"  setup FAILED: {e}", flush=True)
        traceback.print_exc()
        store.add_cell(cell)
        return

    prov, rst, branch_create = Samples(), Samples(), Samples()
    storage_captured = False
    try:
        for i in range(total):
            is_cold = cold_separate and i == 0
            t_prov, env = timed(drv.provision)
            # Isolate the pure CoW metadata op (branch create) from compute spin-up.
            if env.meta.get("branch_create_s") is not None and not is_cold:
                branch_create.add(env.meta["branch_create_s"])
            if is_cold:
                cell.cold_run_s = t_prov
            else:
                prov.add(t_prov)

            # Storage: capture once on a measured run.
            if not storage_captured and not is_cold:
                cell.storage_at_creation_bytes = drv.storage_bytes(env)
                try:
                    drv.representative_write(env)
                    cell.storage_after_write_bytes = drv.storage_bytes(env, after_write=True)
                    if env.meta.get("cow_write_wal_bytes") is not None:
                        cell.notes += f" cow_write_wal_bytes={env.meta['cow_write_wal_bytes']};"
                except Exception:
                    cell.storage_after_write_bytes = cell.storage_at_creation_bytes
                storage_captured = True

            t_rst, _ = timed(drv.reset, env)
            if not is_cold:
                rst.add(t_rst)
            drv.teardown(env)
            print(f"  run {i:>2}{' (cold)' if is_cold else '      '}  "
                  f"provision={t_prov:8.3f}s  reset={t_rst:8.3f}s", flush=True)
    except (MemoryError, Exception) as e:  # record failure, do not crash the matrix
        cell.status = "failed"
        cell.error = f"{type(e).__name__}: {e}"
        print(f"  run FAILED (recorded as result): {cell.error}", flush=True)

    cell.runs = prov.n
    cell.provision = prov.as_dict()
    cell.reset = rst.as_dict()
    if branch_create.n:
        cell.notes += (f" branch_create_median_s={branch_create.median:.3f} "
                       f"range={branch_create.min:.3f}-{branch_create.max:.3f};")

    # Steady-state read latency on the configured strategies.
    ss = cfg.get("steady_state", {})
    if (cell.status == "ok" and ss.get("enabled", True)
            and drv.name in ss.get("on_strategies", [])):
        try:
            env = drv.provision()
            lat_ms = [x * 1000.0 for x in drv.steady_state_latencies(env, ss.get("queries", 200))]
            cell.steady_state_read = Samples(lat_ms).as_dict()
            if hasattr(drv, "telemetry"):
                store.record_raw({"type": "telemetry", "strategy": drv.name,
                                  "scale": scale_name, **drv.telemetry(env)})
            drv.teardown(env)
            print(f"  steady-state read: median={Samples(lat_ms).median:.2f}ms "
                  f"range={min(lat_ms):.2f}-{max(lat_ms):.2f}ms (n={len(lat_ms)})", flush=True)
        except Exception as e:
            cell.notes += f" steady_state_error={e};"

    try:
        drv.teardown_scale()
    except Exception:
        pass

    store.add_cell(cell)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/benchmark.yaml")
    ap.add_argument("--scales", default="", help="comma list; default all enabled")
    ap.add_argument("--engines", default="", help="comma list filter")
    ap.add_argument("--strategies", default="", help="comma list filter")
    ap.add_argument("--runs", type=int, default=0, help="override N runs")
    ap.add_argument("--allow-large", action="store_true", help="permit 100gb scale locally")
    ap.add_argument("--data-dir", default="", help="where generated data lives")
    ap.add_argument("--rds", default="", help="yaml whose engines.* deep-merge over the base (RDS phase)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.rds:
        overlay = load_config(args.rds)
        for eng, kv in (overlay.get("engines") or {}).items():
            cfg.setdefault("engines", {}).setdefault(eng, {}).update(kv)
        cfg.setdefault("_overlays", []).append(args.rds)
    if args.runs:
        cfg["runs"] = args.runs
    data_root = Path(args.data_dir or (Path(cfg.get("output_dir", "results")) / "data"))
    data_root.mkdir(parents=True, exist_ok=True)

    all_scales = list(cfg["scales"].keys())
    scales = [s for s in (args.scales.split(",") if args.scales else all_scales) if s in cfg["scales"]]
    eng_filter = set(args.engines.split(",")) if args.engines else None
    strat_filter = set(args.strategies.split(",")) if args.strategies else None

    store = ResultStore(cfg.get("output_dir", "results"))
    store.meta = {
        "config": args.config,
        "runs": cfg["runs"],
        "data_seed": cfg["data_seed"],
        "primary_engine": cfg.get("primary_engine"),
        "engine_versions": {k: v.get("version_note", "") for k, v in cfg["engines"].items()},
        "scales_requested": scales,
    }

    for scale_name in scales:
        scale_cfg = cfg["scales"][scale_name]
        # Disk guard: full-restore duplicates data (~2.2x target).
        need = int(scale_cfg["target_bytes"] * 2.2)
        free = free_disk_bytes(str(data_root))
        if scale_cfg["target_bytes"] >= 50_000_000_000 and not args.allow_large:
            print(f"\n### SKIP scale={scale_name}: large scale is RDS-phase; "
                  f"pass --allow-large to force locally.", flush=True)
            for sc in cfg["strategies"]:
                if not sc.get("enabled"):
                    continue
                if eng_filter and sc["engine"] not in eng_filter:
                    continue
                if strat_filter and sc["name"] not in strat_filter:
                    continue
                store.add_cell(CellResult(
                    strategy_num=sc.get("num", 0), strategy=sc["name"], engine=sc["engine"],
                    scale=scale_name, scale_target_bytes=scale_cfg["target_bytes"],
                    status="skipped", notes="large scale deferred to RDS phase"))
            continue
        if need > free and args.allow_large:
            print(f"\n### WARNING scale={scale_name}: needs ~{need/1e9:.0f}GB, "
                  f"free ~{free/1e9:.0f}GB. Full-restore/copy strategies may fail.", flush=True)

        print(f"\n########## SCALE {scale_name} :: generating data ##########", flush=True)
        gen = generate(scale_cfg["rows"], cfg["data_seed"], data_root / scale_name)
        print(f"  parent: accounts={gen.accounts:,} orders={gen.orders:,} "
              f"items={gen.order_items:,} events={gen.events:,} "
              f"bytes={gen.total_bytes/1e6:.1f}MB", flush=True)

        for sc in cfg["strategies"]:
            if not sc.get("enabled"):
                continue
            if eng_filter and sc["engine"] not in eng_filter:
                continue
            if strat_filter and sc["name"] not in strat_filter:
                continue
            try:
                run_cell(cfg, sc, scale_name, scale_cfg, data_root / scale_name, gen.files, store)
            except Exception as e:
                print(f"  cell crashed unexpectedly: {e}", flush=True)
                traceback.print_exc()
            store.write_summary()  # incremental persistence

    path = store.write_summary()
    print(f"\nWrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
