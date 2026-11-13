"""Reporter: Table II (provisioning-cost) + Figure 5 (prov-time vs data size)
+ steady-state read-latency panel, from a results summary JSON.

Usage:
    python -m branchbench.report [results/summary-latest.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _fmt_s(d):
    m = d.get("median_s") if isinstance(d, dict) else None
    return f"{m:.3f}" if m is not None else "-"


def _fmt_bytes(b):
    if b is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}PB"


def build_table(cells, scales):
    """Return a markdown Table II: rows=strategy, cols=prov-time per scale + reset + storage."""
    by = {}
    for c in cells:
        by[(c["strategy"], c["scale"])] = c
    strategies = []
    seen = set()
    for c in sorted(cells, key=lambda c: (c["engine"], c["strategy_num"], c["strategy"])):
        if c["strategy"] not in seen:
            strategies.append((c["strategy_num"], c["strategy"], c["engine"]))
            seen.add(c["strategy"])

    big = scales[-1] if scales else ""
    hdr = ["#", "Strategy", "Engine"] + [f"Prov {s} (s)" for s in scales] + [
        "Reset (s)", f"Storage@create ({big})", f"Storage@write ({big})"]
    lines = ["| " + " | ".join(hdr) + " |",
             "|" + "|".join(["---"] * len(hdr)) + "|"]
    for num, strat, engine in strategies:
        row = [str(num), strat, engine]
        reset_repr = storage_c = storage_w = "-"
        for s in scales:
            c = by.get((strat, s))
            if not c:
                row.append("-")
            elif c["status"] == "failed":
                row.append("FAIL")
            elif c["status"] == "skipped":
                row.append("skip")
            else:
                row.append(_fmt_s(c["provision"]))
        # reset from the finest scale; storage from the LARGEST scale (so the
        # copy-on-write near-zero-storage result stands out against full copies).
        for s in scales:
            c = by.get((strat, s))
            if c and c["status"] == "ok":
                reset_repr = _fmt_s(c["reset"])
                break
        for s in reversed(scales):
            c = by.get((strat, s))
            if c and c["status"] == "ok":
                storage_c = _fmt_bytes(c.get("storage_at_creation_bytes"))
                storage_w = _fmt_bytes(c.get("storage_after_write_bytes"))
                break
        row += [reset_repr, storage_c, storage_w]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def figure_prov_vs_size(cells, scales, scale_bytes, out_path):
    """Figure 5: provisioning time (log y) vs data size (log x), one line per strategy."""
    fig, ax = plt.subplots(figsize=(9, 6))
    series = {}
    for c in cells:
        if c["status"] != "ok" or not c["provision"].get("median_s"):
            continue
        series.setdefault(c["strategy"], []).append(
            (scale_bytes[c["scale"]], c["provision"]["median_s"]))
    for strat, pts in sorted(series.items()):
        pts.sort()
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        style = "-o"
        lw = 3 if "cow_branch" in strat else 1.6
        ax.plot(xs, ys, style, label=strat, linewidth=lw,
                zorder=5 if "cow_branch" in strat else 2)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("data size (bytes, log)")
    ax.set_ylabel("provisioning time (s, log)")
    ax.set_title("Per-cycle provisioning time vs data size\n"
                 "(flat = copy-on-write branch; rising = fidelity-preserving copies)")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def figure_read_latency(cells, out_path):
    """Steady-state read-latency panel: branch vs full-restore baseline."""
    rows = [(c["strategy"], c["scale"], c["steady_state_read"])
            for c in cells if c.get("steady_state_read")]
    if not rows:
        return False
    fig, ax = plt.subplots(figsize=(9, 5))
    labels, med, lo, hi = [], [], [], []
    for strat, scale, ss in sorted(rows):
        labels.append(f"{strat}\n{scale}")
        m = ss.get("median_s")           # already ms in the store
        med.append(m)
        lo.append(m - ss["min_s"] if ss.get("min_s") is not None else 0)
        hi.append(ss["max_s"] - m if ss.get("max_s") is not None else 0)
    x = range(len(labels))
    ax.bar(list(x), med, width=0.5, yerr=[lo, hi], capsize=3, label="median (ms), bars = min-max")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=7, rotation=0)
    ax.set_ylabel("read latency (ms)")
    ax.set_title("Steady-state read latency (read amplification): branch vs full-restore")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return True


def main(argv=None):
    argv = argv or sys.argv[1:]
    summary_path = Path(argv[0]) if argv else Path("results/summary-latest.json")
    data = json.loads(summary_path.read_text())
    cells = data["cells"]
    out_dir = summary_path.parent

    scales = data["meta"].get("scales_requested") or sorted({c["scale"] for c in cells})
    scale_bytes = {c["scale"]: c["scale_target_bytes"] for c in cells}

    table = build_table(cells, scales)
    fig1 = out_dir / "figure5_provisioning_vs_size.png"
    fig2 = out_dir / "figure_read_latency.png"
    figure_prov_vs_size(cells, scales, scale_bytes, fig1)
    has_lat = figure_read_latency(cells, fig2)

    md = ["# Branched-Database TDD benchmark results",
          "",
          f"Run: `{data['run_id']}`  ·  generated {data['generated_at']}",
          f"N runs/cell: {data['meta'].get('runs')}  ·  data seed: {data['meta'].get('data_seed')}",
          "",
          "## Per-cycle provisioning cost (medians; min-max in raw JSONL)",
          "",
          table,
          "",
          f"![Figure 5]({fig1.name})",
          ""]
    if has_lat:
        md += [f"![Read latency]({fig2.name})", ""]
    md += ["## Paper-element coverage (why the head-to-head matters)", "",
           "| Strategy | a: inner loop | b: real DB | c: governed/prod-shaped | d: CoW economics | Can't |",
           "|---|---|---|---|---|---|"]
    seen = set()
    for c in sorted(cells, key=lambda c: (c["engine"], c["strategy_num"])):
        if c["strategy"] in seen or not c.get("elements"):
            continue
        seen.add(c["strategy"])
        e = c["elements"]
        md.append(f"| {c['strategy']} | {e.get('a_inner_loop')} | {e.get('b_real_db')} | "
                  f"{e.get('c_governed_prodshaped')} | {e.get('d_cow_economics')} | {c.get('limits','')} |")

    report_path = out_dir / "REPORT.md"
    report_path.write_text("\n".join(md))
    print(f"Wrote {report_path}")
    print(f"Wrote {fig1}")
    if has_lat:
        print(f"Wrote {fig2}")
    print("\n" + table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
