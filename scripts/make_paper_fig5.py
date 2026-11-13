#!/usr/bin/env python3
"""Paper-ready Fig. 5: measured per-cycle provisioning time vs parent size, 1-256 GB.

Single IEEE column (3.5 in), no in-image title (the LaTeX caption carries it), serif
type, every point a measured median read from results/paper_results.json -- no projected or
interpolated segments. A strategy that could not run beyond some size stops at its
last measured point, marked with a bold cross.

Shared-fixture rollback is left out of the paper figure: it sits on the copy-on-write line
near 0.4 s and is flat only because cycles share one database (Table II keeps it). Pass
--with-rollback for the complete chart used in the results write-up.

Writes results/paper_fig5_provisioning[_with_rollback].pdf (vector, fonts embedded) and .png.
"""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

R = Path(__file__).resolve().parent.parent / "results"
T2 = json.load(open(R / "paper_results.json"))["table2"]   # built by build_paper_results.py
SIZES = [1, 2, 8, 32, 64, 128, 256]


def measured(row):
    """GB -> median seconds for the measured cells of a Table II row."""
    return {int(gb): c["median_s"] for gb, c in T2[row]["cells"].items() if c["status"] == "measured"}


# label, slot colour (validated categorical order), marker, line width, data, stops-early
SERIES = [
    ("Copy-on-write branch (Neon)", "#2a78d6", "o", 1.6, measured("cow_branch"), False),
    ("Copy-on-write clone (Aurora)", "#eb6834", "*", 1.6, measured("cow_clone"), False),
    ("Copy-based branch (TEMPLATE)", "#1baf7a", "s", 1.0, measured("copy_based_branch"), False),
    ("Fresh fixture", "#eda100", "D", 1.0, measured("fresh_fixture"), False),
    ("Full restore", "#e87ba4", "^", 1.0, measured("full_restore"), True),
    ("In-memory fake", "#4a3aa7", "P", 1.0, measured("in_memory_fake"), True),
]
WITH_ROLLBACK = "--with-rollback" in sys.argv
if WITH_ROLLBACK:
    SERIES.insert(5, ("Shared fixture (rollback)", "#008300", "v", 1.0, measured("shared_fixture_rollback"), False))

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Times", "STIXGeneral"],
    "mathtext.fontset": "stix",
    "font.size": 7.5,
    "axes.labelsize": 8,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "legend.fontsize": 6.6,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "axes.edgecolor": "#52514e",
    "axes.linewidth": 0.6,
    "xtick.color": "#52514e",
    "ytick.color": "#52514e",
    "axes.labelcolor": "#0b0b0b",
})

fig, ax = plt.subplots(figsize=(3.5, 3.05))
for label, color, mk, lw, data, stops in SERIES:
    xs = sorted(data)
    ax.plot(xs, [data[x] for x in xs], color=color, marker=mk, linewidth=lw,
            markersize=4.2 if mk != "*" else 6.0, markeredgecolor="white",
            markeredgewidth=0.5, label=label, zorder=4 if "Copy-on-write" in label else 3)
    if stops:  # last size the strategy reached
        x = xs[-1]
        ax.plot([x], [data[x]], marker="X", markersize=5.0, color="#0b0b0b",
                markeredgecolor="white", markeredgewidth=0.6, zorder=6)

ax.set_xscale("log", base=2)
ax.set_yscale("log")
ax.set_xticks(SIZES)
ax.set_xticklabels([f"{s}" for s in SIZES])
ax.set_xlim(0.8, 320)
ax.set_ylim(0.2, 2e4)
ax.set_xlabel("Parent data size (GB, log scale)")
ax.set_ylabel("Per-cycle provisioning time (s, log scale)")
ax.grid(True, which="major", color="#e1e0d9", linewidth=0.5)
ax.grid(True, which="minor", axis="y", color="#e1e0d9", linewidth=0.3, alpha=0.6)
ax.set_axisbelow(True)
for side in ("top", "right"):
    ax.spines[side].set_visible(False)

handles, labels = ax.get_legend_handles_labels()
handles.append(Line2D([], [], marker="X", linestyle="none", color="#0b0b0b", markersize=5))
labels.append("Last size reached")
fig.legend(handles, labels, loc="lower center", ncol=2, frameon=False,
           bbox_to_anchor=(0.5, -0.005), handlelength=2.0, columnspacing=1.0,
           handletextpad=0.5, borderaxespad=0.2)
fig.subplots_adjust(left=0.15, right=0.98, top=0.98, bottom=0.36)

for ext, kw in (("pdf", {}), ("png", {"dpi": 300})):
    out = R / f"paper_fig5_provisioning{'_with_rollback' if WITH_ROLLBACK else ''}.{ext}"
    fig.savefig(out, **kw)
    print("wrote", out)
