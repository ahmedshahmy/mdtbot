#!/usr/bin/env python3
"""Medical investigation data bot.

Pulls lab/investigation results out of a SQLite database, renders one
over-time chart per test (each out-of-range value marked and labelled with the
patient's name), and reports the out-of-range values. Everything is bundled
into a single report.pdf (plus report.md).

Detection: only values outside the clinical reference range are flagged —
values within the reference range are never reported as outliers.

Usage:
  python medical_bot.py                     # auto-seeds demo DB if missing
  python medical_bot.py --db mydata.db      # analyze your own database
  python medical_bot.py --test Creatinine   # single test
  python medical_bot.py --patient P001      # single patient
  python medical_bot.py --since 2024-01-01 --until 2024-12-31
  python medical_bot.py --out ./reports     # output directory
  python medical_bot.py --png               # also write individual PNG charts

Output: a single report.pdf bundling the outlier report and the over-time
charts, plus report.md (plain text). Individual PNGs only with --png.

Expected schema (column-name aliases are tolerated):
  patients(patient_id, name, sex, dob)
  lab_results(patient_id, test_name, value, unit, ref_low, ref_high, collected_at)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Keep matplotlib's cache inside the bot folder (home dir may be read-only).
# Must be set BEFORE importing matplotlib, which resolves it at import time.
if not os.environ.get("MPLCONFIGDIR"):
    os.environ["MPLCONFIGDIR"] = os.path.join(HERE, ".mplconfig")

import numpy as np

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["figure.max_open_warning"] = 64
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

DEFAULT_DB = os.path.join(HERE, "medical_demo.db")

OUTLIER_COLOR = "#d62728"
REF_BAND_COLOR = "#cfe3f2"
PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b", "#e377c2",
           "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a"]

# column-name aliases -> canonical name
ALIASES = {
    "patient_id": ["patient_id", "pid", "patient", "mrn"],
    "test_name": ["test_name", "test", "test_code", "investigation", "analyte"],
    "value": ["value", "result", "result_value", "measurement"],
    "unit": ["unit", "units"],
    "ref_low": ["ref_low", "ref_min", "normal_low", "low", "ref_lo"],
    "ref_high": ["ref_high", "ref_max", "normal_high", "high", "ref_hi"],
    "collected_at": ["collected_at", "result_date", "date", "datetime", "taken_at"],
}
COLUMN_CANONICAL = {alias: canon for canon, names in ALIASES.items() for alias in names}


# --------------------------------------------------------------------------- #
#  Database access
# --------------------------------------------------------------------------- #

def ensure_db(path: str, force_seed: bool) -> None:
    """Seed the demo database if the default path is missing (or --seed)."""
    if force_seed:
        if os.path.basename(path) != os.path.basename(DEFAULT_DB):
            sys.exit(f"--seed only applies to the demo database ({DEFAULT_DB}); refusing to touch {path}")
        import seed_demo_data
        seed_demo_data.build_db(path)
        return
    if os.path.exists(path):
        return
    if os.path.abspath(path) == os.path.abspath(DEFAULT_DB):
        import seed_demo_data
        print(f"Demo database not found — seeding {path} ...")
        seed_demo_data.build_db(path)
    else:
        sys.exit(f"Database not found: {path}\n"
                 f"  Create one, or use --db {DEFAULT_DB} to auto-generate the demo database.")


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


def resolve_columns(con: sqlite3.Connection, table: str) -> dict[str, str]:
    """Map canonical names to actual column names present in `table`."""
    actual = set(table_columns(con, table))
    mapping = {}
    for canon, aliases in ALIASES.items():
        for alias in aliases:
            if alias in actual:
                mapping[canon] = alias
                break
    return mapping


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def load_results(con: sqlite3.Connection, test_filter: str | None,
                 patient_filter: str | None, since: str | None, until: str | None):
    """Return (rows, meta) where rows is a list of dicts and meta explains the schema."""
    lr_cols = resolve_columns(con, "lab_results")
    if not {"patient_id", "test_name", "value"} <= set(lr_cols):
        sys.exit("lab_results table must have at least: patient_id, test_name, value "
                 f"(found columns: {table_columns(con, 'lab_results')})")

    sel = ", ".join(f"r.{c}" for c in lr_cols.values())
    q = f"SELECT {sel} FROM lab_results r"
    where, params = [], []

    if test_filter:
        where.append("r.test_name = ?")
        params.append(test_filter)
    if patient_filter:
        where.append("r.patient_id = ?")
        params.append(patient_filter)
    if since:
        where.append("r.collected_at >= ?")
        params.append(since)
    if until:
        where.append("r.collected_at <= ?")
        params.append(until + "T23:59:59")

    # patients table is optional; join it for display names when present.
    has_patients = "patients" in [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    name_col, sex_col = None, None
    if has_patients:
        p_cols = resolve_columns(con, "patients")
        if "name" in table_columns(con, "patients"):
            name_col = "name"
            sel += ", p.name AS p_name"
        if "sex" in table_columns(con, "patients"):
            sex_col = "sex"
            sel += ", p.sex AS p_sex"
        q = f"SELECT {sel} FROM lab_results r"
        q += f" LEFT JOIN patients p ON p.patient_id = r.{lr_cols['patient_id']}"

    if where:
        q += " WHERE " + " AND ".join(where)

    rows = []
    for r in con.execute(q, params):
        d = {
            "patient_id": r[lr_cols["patient_id"]],
            "test_name": r[lr_cols["test_name"]],
            "value": r[lr_cols["value"]],
            "unit": r[lr_cols["unit"]] if "unit" in lr_cols else "",
            "ref_low": r[lr_cols["ref_low"]] if "ref_low" in lr_cols else None,
            "ref_high": r[lr_cols["ref_high"]] if "ref_high" in lr_cols else None,
            "collected_at": r[lr_cols["collected_at"]] if "collected_at" in lr_cols else "",
            "name": r["p_name"] if name_col else None,
            "sex": r["p_sex"] if sex_col else None,
        }
        rows.append(d)

    # normalize: drop non-finite values, parse dates.
    clean = []
    for d in rows:
        try:
            v = float(d["value"])
            if not np.isfinite(v):
                continue
        except (TypeError, ValueError):
            continue
        d["value"] = v
        d["ref_low"] = float(d["ref_low"]) if d["ref_low"] is not None else None
        d["ref_high"] = float(d["ref_high"]) if d["ref_high"] is not None else None
        if d["collected_at"]:
            try:
                d["datetime"] = dt.datetime.fromisoformat(d["collected_at"])
            except ValueError:
                d["datetime"] = None
        else:
            d["datetime"] = None
        clean.append(d)

    return clean, {"has_patients": has_patients, "name_col": name_col}


# --------------------------------------------------------------------------- #
#  Outlier detection
# --------------------------------------------------------------------------- #

def detect_outliers(rows: list[dict]) -> dict[int, list[str]]:
    """Flag rows whose value is outside the row's clinical reference range.

    Only out-of-range (clinical) outliers are reported; values inside the
    reference range are never flagged. Returns row index -> ['ref'].
    """
    flags: dict[int, list[str]] = {i: [] for i in range(len(rows))}
    for i, r in enumerate(rows):
        if (r["ref_low"] is not None and r["value"] < r["ref_low"]) or \
           (r["ref_high"] is not None and r["value"] > r["ref_high"]):
            flags[i].append("ref")
    return flags


def direction_note(r: dict, methods_hit: list[str]) -> str:
    """Human note: CLINICAL HIGH or CLINICAL LOW."""
    side = "HIGH" if (r["ref_high"] is not None and r["value"] > r["ref_high"]) else \
           ("LOW" if (r["ref_low"] is not None and r["value"] < r["ref_low"]) else "")
    return f"CLINICAL {side}".strip() if side else "CLINICAL out-of-range"


# --------------------------------------------------------------------------- #
#  Plotting
# --------------------------------------------------------------------------- #

def ref_ranges(rows: list[dict]) -> list[tuple[float | None, float | None]]:
    """Distinct (ref_low, ref_high) pairs, most common first (sex-specific ranges)."""
    from collections import Counter
    pairs = Counter((r["ref_low"], r["ref_high"]) for r in rows)
    return [p for p, _ in pairs.most_common()]


def _ref_band(ax, ref_low, ref_high, ymin, ymax):
    if ref_low is not None and ref_high is not None:
        ax.axhspan(ref_low, ref_high, color=REF_BAND_COLOR, alpha=0.5, zorder=0,
                   label="reference range")
    for lim, style in ((ref_low, "--"), (ref_high, "--")):
        if lim is not None:
            ax.axhline(lim, color="#2f6db3", linestyle=style, linewidth=0.9, alpha=0.7)


def draw_all_ref_ranges(ax, rows, ymin, ymax):
    """Draw the modal reference range as a band, any others as dashed limits."""
    ranges = ref_ranges(rows)
    _ref_band(ax, *ranges[0], ymin, ymax)
    for ref_low, ref_high in ranges[1:]:
        for lim in (ref_low, ref_high):
            if lim is not None:
                ax.axhline(lim, color="#2f6db3", linestyle=":", linewidth=1.0,
                           alpha=0.9, label="other ref range")


def plot_trend(rows: list[dict], flags: dict[int, list[str]]):
    """Build the per-patient time-trend figure. Returns the (open) figure."""
    test = rows[0]["test_name"]
    unit = rows[0]["unit"]
    by_patient: dict[str, list[tuple]] = {}
    for i, r in enumerate(rows):
        if r["datetime"] is None:
            continue
        by_patient.setdefault(r["patient_id"], []).append((r["datetime"], r["value"], i))

    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=130)
    ymin, ymax = min(r["value"] for r in rows), max(r["value"] for r in rows)
    pad = max((ymax - ymin) * 0.08, 0.01)
    draw_all_ref_ranges(ax, rows, ymin - pad, ymax + pad)

    for color, (pid, pts) in zip(PALETTE, sorted(by_patient.items())):
        pts.sort(key=lambda t: t[0])
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, "-o", color=color, linewidth=1.0, markersize=3.5,
                alpha=0.75, label="_nolegend_")
        for _x, _y, i in pts:
            if flags.get(i):
                ax.scatter([_x], [_y], s=90, facecolor="none", edgecolor=OUTLIER_COLOR,
                           linewidth=1.6, zorder=5)

    ax.set_title(f"{test} over time — outliers named", fontsize=13, fontweight="bold")
    ax.set_ylabel(unit or "value")
    ax.set_xlabel("collection date")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()

    # Print the patient's name next to every outlier mark (no patient legend).
    for i, r in enumerate(rows):
        if not flags.get(i) or r["datetime"] is None:
            continue
        label = r["name"] or r["patient_id"]
        ax.annotate(label, (r["datetime"], r["value"]),
                    xytext=(0, 8), textcoords="offset points",
                    ha="center", va="bottom", fontsize=7.5, fontweight="bold",
                    color=OUTLIER_COLOR,
                    arrowprops=dict(arrowstyle="-", color=OUTLIER_COLOR,
                                    lw=0.6, alpha=0.5))

    ax.grid(True, alpha=0.3)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(handles, labels, fontsize=7, loc="best", framealpha=0.6)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------- #
#  Report (markdown + PDF pages)
# --------------------------------------------------------------------------- #

def fmt_ref(r: dict) -> str:
    if r["ref_low"] is not None and r["ref_high"] is not None:
        return f"{r['ref_low']:g} – {r['ref_high']:g} {r['unit']}".strip()
    if r["ref_high"] is not None:
        return f"< {r['ref_high']:g} {r['unit']}".strip()
    if r["ref_low"] is not None:
        return f"> {r['ref_low']:g} {r['unit']}".strip()
    return "n/a"


def build_report_lines(rows, flags, meta) -> list[str]:
    """The full report as markdown lines (also the source for report.md)."""
    lines = []
    ap = lines.append
    ap("# Medical Investigation Outlier Report")
    ap("")
    ap(f"- **Generated:** {dt.datetime.now().isoformat(timespec='seconds')}")
    ap(f"- **Database:** {os.path.basename(meta['db_path'])}")
    ap(f"- **Results analyzed:** {len(rows)}  |  **Patients:** "
       f"{len({r['patient_id'] for r in rows})}  |  **Tests:** {len({r['test_name'] for r in rows})}")
    ap("- Detection: values outside the clinical reference range only.")
    ap("")

    # outliers only
    flagged = [(i, r) for i, r in enumerate(rows) if flags[i]]
    ap(f"## Outliers ({len(flagged)} of {len(rows)})")
    ap("")
    if not flagged:
        ap("No out-of-range values detected.")
        ap("")
    else:
        ap("| Patient | Name | Test | Date | Value | Unit | Ref range | Note |")
        ap("|---|---|---|---|---|---|---|---|")
        for i, r in sorted(flagged, key=lambda ir: ((ir[1]["name"] or ir[1]["patient_id"]),
                                                    ir[1]["test_name"],
                                                    ir[1]["datetime"] or dt.datetime.min)):
            fs = flags[i]
            ap(f"| {r['patient_id']} | {r['name'] or '—'} | {r['test_name']} | "
               f"{(r['datetime'] or dt.datetime.min).date()} | **{r['value']:.2f}** | {r['unit']} | "
               f"{fmt_ref(r)} | {direction_note(r, fs)} |")
        ap("")
    ap("---")
    ap("*Only values outside the clinical reference range are flagged.*")
    return lines


def write_report(rows, flags, meta, out_dir: str) -> str:
    out_path = os.path.join(out_dir, "report.md")
    with open(out_path, "w") as f:
        f.write("\n".join(build_report_lines(rows, flags, meta)))
    return out_path


def build_pdf_lines(rows, flags, meta) -> list[tuple[str, str | None]]:
    """Report text for the PDF: list of (line, color) pairs.

    Outlier data rows are colored red; everything else uses the default color.
    """
    def plain(s: str) -> tuple[str, str | None]:
        return (s, None)

    lines: list[tuple[str, str | None]] = []
    ap = lines.append
    ap(plain("MEDICAL INVESTIGATION OUTLIER REPORT"))
    ap(plain("=" * 100))
    ap(plain(""))
    ap(plain(f"Generated    : {dt.datetime.now().isoformat(timespec='seconds')}"))
    ap(plain(f"Database     : {os.path.basename(meta['db_path'])}"))
    ap(plain(f"Results      : {len(rows)}  |  Patients: {len({r['patient_id'] for r in rows})}"
             f"  |  Tests: {len({r['test_name'] for r in rows})}"))
    ap(plain("Detection    : values outside the clinical reference range only"))
    ap(plain(""))

    flagged = [(i, r) for i, r in enumerate(rows) if flags[i]]
    ap(plain(f"OUTLIERS ({len(flagged)} of {len(rows)})"))
    ap(plain("-" * 100))
    if not flagged:
        ap(plain("No out-of-range values detected."))
        ap(plain(""))
    else:
        ap(plain(f"{'Patient':<7}{'Name':<16}{'Test':<18}{'Date':<11}{'Value':>8} "
                 f"{'Unit':<11}{'Ref range':<18}{'Note'}"))
        ap(plain("-" * 100))
        for i, r in sorted(flagged, key=lambda ir: ((ir[1]["name"] or ir[1]["patient_id"]),
                                                    ir[1]["test_name"],
                                                    ir[1]["datetime"] or dt.datetime.min)):
            fs = flags[i]
            date_s = (r["datetime"] or dt.datetime.min).date().isoformat()
            row = (f"{r['patient_id']:<7}{(r['name'] or '—')[:15]:<16}{r['test_name']:<18}"
                   f"{date_s:<11}{r['value']:>8.2f} {(r['unit'] or ''):<11}{fmt_ref(r):<18}"
                   f"{direction_note(r, fs)}")
            ap((row, OUTLIER_COLOR))
        ap(plain(""))
    ap(plain("-" * 100))
    ap(plain("Only values outside the clinical reference range are flagged."))
    return lines


def _paginate(lines: list[str], per_page: int = 46) -> list[list[str]]:
    return [lines[i:i + per_page] for i in range(0, len(lines), per_page)]


def _text_page(pdf: PdfPages, text_lines: list[tuple[str, str | None]], page_no: int) -> None:
    fig = plt.figure(figsize=(11.69, 8.27))  # A4 landscape
    fig.patch.set_facecolor("white")
    y = 0.965
    dy = 8.5 * 1.3 / (8.27 * 72)  # line height as a figure fraction
    for text, color in text_lines:
        fig.text(0.03, y, text, family="DejaVu Sans Mono", fontsize=8.5,
                 va="top", ha="left", color=color or "#111111")
        y -= dy
    fig.text(0.97, 0.015, f"Page {page_no}", ha="right", va="bottom",
             fontsize=8, color="#777777")
    pdf.savefig(fig)
    plt.close(fig)


def _stamp(fig, page_no: int) -> None:
    fig.text(0.985, 0.005, f"Page {page_no}", ha="right", va="bottom",
             fontsize=7, color="#999999")


def write_pdf(pdf_path: str, pdf_lines: list[str], trend_figs: dict[str, object]) -> None:
    """Bundle the report text and the per-test over-time charts into one PDF.

    Figures are closed as they are added, so callers must not reuse them
    afterwards.
    """
    page = 0
    with PdfPages(pdf_path) as pdf:
        pdf.infodict()["Title"] = "Medical Investigation Outlier Report"
        for chunk in _paginate(pdf_lines):
            page += 1
            _text_page(pdf, chunk, page)
        for test in sorted(trend_figs):
            page += 1
            f = trend_figs[test]
            _stamp(f, page)
            pdf.savefig(f)
            plt.close(f)


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="Medical investigation data bot: query, graph, flag outliers.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database (default: {DEFAULT_DB})")
    ap.add_argument("--seed", action="store_true", help="Re-generate the demo database (default path only)")
    ap.add_argument("--out", default=os.path.join(HERE, "output"), help="Output directory for graphs + report")
    ap.add_argument("--test", help="Analyze only this test (exact test_name)")
    ap.add_argument("--patient", help="Analyze only this patient")
    ap.add_argument("--since", help="Only results collected on/after YYYY-MM-DD")
    ap.add_argument("--until", help="Only results collected on/before YYYY-MM-DD")
    ap.add_argument("--png", action="store_true",
                    help="Also write the individual PNG chart files (default output is one PDF)")
    args = ap.parse_args()

    ensure_db(args.db, args.seed)
    con = connect(args.db)
    try:
        rows, meta = load_results(con, args.test, args.patient, args.since, args.until)
    finally:
        con.close()
    meta["db_path"] = args.db

    if not rows:
        sys.exit("No results match the filters — nothing to analyze.")

    flags = detect_outliers(rows)
    os.makedirs(args.out, exist_ok=True)

    # ---- figures (over-time trend chart per test, outliers named) -----------
    tests = sorted({r["test_name"] for r in rows})
    trend_figs: dict[str, object] = {}
    for test in tests:
        sub_orig = [(orig_i, r) for orig_i, r in enumerate(rows) if r["test_name"] == test]
        sub = [r for _, r in sub_orig]
        # position-aligned flags: sub_flags[k] matches sub[k] (not the full-row index)
        sub_flags = {pos: flags.get(orig_i, []) for pos, (orig_i, _r) in enumerate(sub_orig)}
        trend_figs[test] = plot_trend(sub, sub_flags)
        n_ref = sum(1 for i, r in enumerate(rows) if r["test_name"] == test and "ref" in flags.get(i, []))
        print(f"  {test:<18} n={len(sub):<4} out-of-range={n_ref}")

    # ---- optional separate PNG files (before figures are consumed by the PDF)
    if args.png:
        for test in tests:
            safe = test.replace(" ", "_").replace("/", "_")
            trend_figs[test].savefig(os.path.join(args.out, f"trend_{safe}.png"), dpi=130)

    # ---- single PDF: outlier report + over-time charts ----------------------
    pdf_lines = build_pdf_lines(rows, flags, meta)
    pdf_path = os.path.join(args.out, "report.pdf")
    write_pdf(pdf_path, pdf_lines, trend_figs)

    # ---- report.md (plain-text version of the report) ----------------------
    report_path = write_report(rows, flags, meta, args.out)

    n_flagged = sum(1 for f in flags.values() if f)
    print("\n" + "=" * 60)
    print(f"Analyzed {len(rows)} results across {len(tests)} tests.")
    print(f"Flagged {n_flagged} outliers (all outside the reference range).")
    print(f"Output written to: {args.out}")
    print(f"  report.pdf (outlier report + over-time charts): {os.path.basename(pdf_path)}")
    print(f"  report.md (text version):                       {os.path.basename(report_path)}")
    if args.png:
        for test in tests:
            safe = test.replace(" ", "_").replace("/", "_")
            print(f"  {test}: trend_{safe}.png")
    print("=" * 60)


if __name__ == "__main__":
    main()
