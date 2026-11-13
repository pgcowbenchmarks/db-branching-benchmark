#!/usr/bin/env python3
"""Render the paper's Table II from results/paper_results.json (Markdown + LaTeX).

Medians in seconds at each nominal parent size. A cell that is not a measurement says why:
fail (died before finishing), OOM, skip, or n.r. (not run). Writes results/paper_table2.md
and results/paper_table2.tex.
"""
import json
from pathlib import Path

R = Path(__file__).resolve().parent.parent / "results"
P = json.load(open(R / "paper_results.json"))
SIZES = P["meta"]["sizes_gb"]
ORDER = ["in_memory_fake", "fresh_fixture", "shared_fixture_rollback", "full_restore",
         "copy_based_branch", "cow_branch", "cow_clone"]
WORD = {"failed": "fail", "oom": "OOM", "skipped": "skip", "not_run": "n.r."}


def fmt(c):
    if c["status"] != "measured":
        return WORD[c["status"]]
    v = c["median_s"]
    return f"{v:.3f}" if v < 1 else f"{v:.1f}" if v < 100 else f"{v:,.0f}"


rows = [(P["table2"][k]["label"], P["table2"][k]["engine"], [fmt(P["table2"][k]["cells"][g]) for g in SIZES])
        for k in ORDER]
ttf = P["table2"]["full_restore"]["cells"]["32"]

md = ["| Strategy | Engine | " + " | ".join(f"{g} GB" for g in SIZES) + " |",
      "|---|---|" + "---|" * len(SIZES)]
md += [f"| {lab} | {eng} | " + " | ".join(cells) + " |" for lab, eng, cells in rows]
md += ["", "Medians in seconds at nominal parent sizes. fail = the attempt died before finishing; OOM = out of "
       "memory; skip = not attempted because the dump would not fit on local disk; n.r. = not run.",
       f"Full restore at 32 GB ran {ttf['time_to_failure_s']:.0f} s (a {ttf['dump_s']:.0f} s dump plus "
       f"{ttf['restore_until_failure_s']:.0f} s of restore) before the restore died; the 64 GB attempt failed the "
       "same way (time not recorded).",
       "Shared fixture (transactional rollback) is a no-isolation baseline: it is flat only because cycles share "
       "one database."]
(R / "paper_table2.md").write_text("\n".join(md) + "\n")


def tex(s):
    return s.replace("&", "\\&").replace(",", "{,}")


tx = ["\\begin{tabular}{@{}ll" + "r" * len(SIZES) + "@{}}", "\\toprule",
      "Provisioning strategy & Engine & " + " & ".join(f"{g}\\,GB" for g in SIZES) + " \\\\", "\\midrule"]
tx += [f"{lab} & {eng} & " + " & ".join(tex(c) for c in cells) + " \\\\" for lab, eng, cells in rows]
tx += ["\\bottomrule", "\\end{tabular}"]
(R / "paper_table2.tex").write_text("\n".join(tx) + "\n")
print("\n".join(md[:2 + len(rows)]))
print("wrote", R / "paper_table2.md", "and", R / "paper_table2.tex")
