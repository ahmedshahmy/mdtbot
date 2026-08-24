# Medical Investigation Bot

A standalone Python bot that pulls medical investigation (lab) results out of a
SQLite database, renders the over-time charts, and flags outlier values — plus
a companion **publishable web page** (`medical-web/`) where you can upload a
database and get the same charts in the browser.

It ships with a deterministic **demo database** (`medical_demo.db`, 20
patients, 100 visits in total — 300 results across Creatinine, Hemoglobin and
CRP over ~2.5 years) that contains deliberately injected outliers, so you can
run it end-to-end immediately — then point it at your own database.

## Web app (upload → charts)

```sh
cd medical-web
python3 -m http.server 8000     # then open http://127.0.0.1:8000
```

Upload a SQLite database (or click "Use demo database") — analysis runs 100%
in the browser, no data leaves your machine. See `medical-web/README.md` for
schema and publishing (GitHub Pages / Netlify / any static host).

## Setup

```sh
cd medical-bot
python3 -m venv .venv
.venv/bin/pip install matplotlib numpy
```

## Usage

```sh
.venv/bin/python medical_bot.py                    # full analysis (auto-seeds demo DB if missing)
.venv/bin/python medical_bot.py --seed             # re-generate the demo database
.venv/bin/python medical_bot.py --db mydata.db     # analyze your own database
.venv/bin/python medical_bot.py --test Creatinine  # one test only
.venv/bin/python medical_bot.py --patient P001     # one patient only
.venv/bin/python medical_bot.py --since 2024-01-01 --until 2024-12-31
.venv/bin/python medical_bot.py --out ./reports    # output directory (default: ./output)
.venv/bin/python medical_bot.py --png              # also write separate PNG charts
```

## Expected database schema

Column-name aliases are tolerated (`test`, `result`, `date`, `low`, `high`, …).

```sql
CREATE TABLE patients (
    patient_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    sex        TEXT NOT NULL,   -- "M" / "F", used for sex-matched statistics
    dob        TEXT NOT NULL
);
CREATE TABLE lab_results (
    result_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id   TEXT NOT NULL REFERENCES patients(patient_id),
    test_name    TEXT NOT NULL,
    value        REAL NOT NULL,
    unit         TEXT,
    ref_low      REAL,          -- clinical reference range (NULL = no limit)
    ref_high     REAL,
    collected_at TEXT NOT NULL  -- ISO date or datetime
);
```

The `patients` table is optional (the bot then identifies patients by ID only).
Reference ranges may vary per row — e.g. sex-specific hemoglobin ranges — and
the bot treats the most common range as the primary band while drawing the
others as dotted lines.

## How outliers are flagged

**Only values outside the clinical reference range are flagged.** A result is
an outlier if its value is below `ref_low` or above `ref_high` of its own
row — values inside the reference range are never reported. Each flagged row
is noted as CLINICAL HIGH or CLINICAL LOW.

## Output

Everything is bundled into **one file**:

- `output/report.pdf` — the full deliverable:
  - the outlier report — a table listing only the out-of-range results
    (patient name, test, date, value, reference range, CLINICAL HIGH/LOW
    note), grouped by patient name;
  - one **over-time chart per test** — each patient's values over time with
    the reference-range band, out-of-range points circled in red and the
    patient's name printed right next to the mark.
- `output/report.md` — the same outlier report as plain text (handy for searching).

Run with `--png` to additionally write the over-time charts as separate PNG
files (`trend_<Test>.png`).

## Demo data

`seed_demo_data.py` generates realistic serial results for **Creatinine,
Hemoglobin and CRP**: 20 patients, 5 visits each (100 visits total) over ~2.5
years — 300 results — and injects 3 clinical outliers, all outside the
reference range (e.g. a marked CRP elevation and a polycythemia-range
hemoglobin). The total visit count stays 100 regardless of the patient count
(`--patients N` when seeding). Re-generate with `--seed`. In the PDF report,
outlier rows are printed in red.
