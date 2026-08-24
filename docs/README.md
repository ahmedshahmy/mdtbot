# Medical Outlier Explorer — web app

A fully client-side web page: upload a SQLite lab-results database and get
over-time charts with outlier marks (patient name printed next to each red
mark) and an outlier table with the outlier values in red.

**Nothing is uploaded anywhere** — sql.js runs the database in your browser
and all analysis happens locally. That makes it safe for medical data.

## Files

```
index.html              the page
app.js                  UI + chart rendering (Chart.js)
engine.js               outlier engine (port of the Python bot's logic)
wasm_b64.js             embedded SQLite WebAssembly (base64) — enables file:// use
demo_db.js              embedded demo database (base64) — demo button works anywhere
vendor/sql-wasm.js      SQLite compiled to WebAssembly (sql.js)
vendor/sql-wasm.wasm
vendor/chart.umd.js     Chart.js
demo/medical_demo.db    demo database (20 patients, 100 visits, 300 results: Creatinine, Hemoglobin, CRP)
```

## Run locally

The page is fully self-contained: open `index.html` directly in a browser
(double-click works — the SQLite engine and demo database are embedded), or
serve it with any static file server:

```sh
cd medical-web
python3 -m http.server 8000
# open http://127.0.0.1:8000
```

## Publish

The folder is self-contained — host it on any static host:

- **GitHub Pages** — push the `medical-web/` contents to a repo and enable Pages,
  or `npx gh-pages -d medical-web`.
- **Netlify / Vercel / Cloudflare Pages** — drag & drop the `medical-web/`
  folder (or point the build at it; no build step needed).
- **Any web server / S3 bucket** — just serve the folder.

Open the published URL, upload a database, and it works — the "Use demo
database" button exercises the whole pipeline.

## Expected schema

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
    test_name    TEXT NOT NULL,   -- e.g. Creatinine, Hemoglobin, CRP
    value        REAL NOT NULL,
    unit         TEXT,
    ref_low      REAL,            -- clinical reference range (NULL = no limit)
    ref_high     REAL,
    collected_at TEXT NOT NULL    -- ISO date or datetime
);
```

The `patients` table is optional. Reference ranges may vary per row (e.g.
sex-specific hemoglobin) — the engine detects that and compares against
sex-matched cohorts.

## Outlier detection

**Only values outside the clinical reference range are flagged.** A result is
an outlier if its value is below `ref_low` or above `ref_high` of its own
row; values inside the reference range are never reported. Each flagged row is
noted CLINICAL HIGH or CLINICAL LOW.

The outlier engine (`engine.js`) is a port of the Python bot in
`../medical_bot.py` and is verified to produce identical flags.
