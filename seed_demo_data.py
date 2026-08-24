#!/usr/bin/env python3
"""Seed a demo SQLite database with realistic medical investigation (lab) data.

Generates a compact cohort: 20 patients with 100 visits in total (5 visits
per patient over ~2.5 years, 3 tests per visit -> 300 results), and
deliberately injects clinical outliers -> values placed deterministically
outside the reference range (computed from the row's reference limits).

Reference ranges are sex-specific for hemoglobin, as in real labs.

Output: medical_demo.db (SQLite), deterministic for a fixed RNG seed.
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
from datetime import date, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medical_demo.db")

# test_name -> (unit, ref_low, ref_high, typical_value, variability)
# ref_low/ref_high: None means "no clinical lower/upper limit".
TESTS = {
    "Creatinine":       ("mg/dL",      0.7, 1.3, 0.95, 0.10),
    "Hemoglobin":       ("g/dL",       13.5, 17.5, 14.6, 0.55),
    "CRP":              ("mg/L",       None, 10.0, 2.5, 1.5),
}

# Sex-specific hemoglobin ranges (females run lower), like real labs.
HB_REF = {"M": (13.5, 17.5), "F": (12.0, 15.5)}

FIRST_NAMES = [
    "Ava", "Liam", "Mia", "Noah", "Zoe", "Ethan", "Lily", "Lucas", "Emma", "Owen",
    "Nora", "Caleb", "Isla", "Mateo", "Freya", "Hugo", "Amara", "Jonas", "Leila", "Rafael",
    "Sienna", "Theo", "Maya", "Felix", "Ingrid", "Omar", "Priya", "Marco", "Hana", "Dmitri",
]
LAST_NAMES = [
    "Chen", "Okafor", "Reyes", "Novak", "Sato", "Andersson", "Diallo", "Petrov", "Kim", "Silva",
    "Haddad", "Ivanov", "Moreau", "Kowalski", "Osei", "Tanaka", "Rossi", "Lindqvist", "Amari", "Berg",
    "Fontaine", "Dube", "Nakamura", "Costa", "Mensah", "Horvath", "Alvarez", "Jansen", "Farah", "Wolfe",
]

START = date(2023, 1, 10)
END = date(2025, 8, 20)

# Pool of clinical-outlier styles: (test, direction, factor).
#   direction "high" -> value = ref_high * factor   (> ref range)
#   direction "low"  -> value = ref_low  * factor   (< ref range)
CLINICAL_STYLES = [
    ("Creatinine", "high", 1.6),    # acute kidney injury spike
    ("Creatinine", "low",  0.72),   # unusually low creatinine
    ("CRP",        "high", 4.5),    # marked inflammation/infection
    ("CRP",        "high", 2.2),    # moderate inflammation
    ("Hemoglobin", "low",  0.75),   # anemia
    ("Hemoglobin", "high", 1.18),   # polycythemia-range
]

# Roughly 6% of patients get one clinical outlier (min 3).
N_CLINICAL_FRACTION = 0.06

# Total number of visits across all patients (distributed evenly: 5 each).
TOTAL_VISITS = 100


def make_patients(rng: random.Random, n: int = 20):
    """n patients with unique (mostly) names, random sex and birth dates."""
    rng.seed(7)  # patient roster is independent of the results stream
    combos = [(f, l) for f in FIRST_NAMES for l in LAST_NAMES]
    rng.shuffle(combos)
    patients = []
    for i in range(1, n + 1):
        pid = f"P{i:03d}"
        fname, lname = combos[(i - 1) % len(combos)]
        sex = "M" if rng.random() < 0.5 else "F"
        dob = date(rng.randint(1945, 2004), rng.randint(1, 12), rng.randint(1, 28))
        patients.append((pid, f"{fname} {lname}", sex, dob.isoformat()))
    return patients


def make_results(rng: random.Random, patients):
    """Return (rows, targets).

    rows: (patient_id, test_name, value, unit, ref_low, ref_high, collected_at)
    targets: [(patient_id, test_name, direction, factor)] injected outliers.

    TOTAL_VISITS is split evenly across patients, so total visits stay fixed
    regardless of the number of patients.
    """
    rows = []
    rng.seed(42)  # deterministic

    visits_per_patient, extra = divmod(TOTAL_VISITS, len(patients))

    for pi, (pid, _name, sex, dob_iso) in enumerate(patients):
        age = (START - date.fromisoformat(dob_iso)).days / 365.25
        visits = visits_per_patient + (1 if pi < extra else 0)
        visit_days = sorted(rng.sample(range(0, (END - START).days + 1), visits))

        for visit_i, day_offset in enumerate(visit_days):
            when = START + timedelta(days=day_offset)
            for test_name, (unit, ref_lo, ref_hi, base, spread) in TESTS.items():
                ref_low, ref_high = ref_lo, ref_hi
                if test_name == "Hemoglobin":
                    ref_low, ref_high = HB_REF[sex]
                    base = 13.4 if sex == "F" else 14.6

                # Mild aging effect: creatinine drifts up with age.
                if test_name == "Creatinine":
                    base += max(0.0, (age - 50) * 0.006)

                value = base + rng.gauss(0, spread)

                # Patient-specific slow drift over visits (time trends).
                value += rng.uniform(-0.5, 0.5) * spread * (visit_i / max(visits - 1, 1))

                rows.append((pid, test_name, value, unit, ref_low, ref_high,
                             when.isoformat() + "T" + f"{rng.randint(6, 16):02d}:00:00"))

    # ---- Choose clinical-outlier targets (deterministic, from the latter
    # half of the roster so their rows are spread through the dataset) -------
    n_targets = max(3, round(len(patients) * N_CLINICAL_FRACTION))
    pool = patients[len(patients) * 2 // 5:]
    rng.shuffle(pool)
    targets = []
    for pid, _name, _sex, _dob in pool[:n_targets]:
        test_name, direction, factor = rng.choice(CLINICAL_STYLES)
        targets.append((pid, test_name, direction, factor))

    # ---- Inject clinical outliers (deterministic, outside reference range) --
    for pid, test_name, direction, factor in targets:
        cands = [i for i, r in enumerate(rows)
                 if r[0] == pid and r[1] == test_name and i > len(rows) // 3]
        if not cands:
            cands = [i for i, r in enumerate(rows) if r[0] == pid and r[1] == test_name]
        idx = rng.choice(cands)
        _pid, _test, _val, unit, ref_low, ref_high, when = rows[idx]
        if direction == "high":
            value = (ref_high if ref_high is not None else ref_low) * factor
        else:
            value = ref_low * factor
        rows[idx] = (_pid, _test, value, unit, ref_low, ref_high, when)

    return rows, targets


def build_db(path: str, n_patients: int = 20) -> None:
    rng = random.Random()
    patients = make_patients(rng, n_patients)
    results, targets = make_results(rng, patients)

    if os.path.exists(path):
        os.remove(path)

    con = sqlite3.connect(path)
    try:
        con.executescript("""
            CREATE TABLE patients (
                patient_id  TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                sex         TEXT NOT NULL,
                dob         TEXT NOT NULL
            );
            CREATE TABLE lab_results (
                result_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id  TEXT NOT NULL REFERENCES patients(patient_id),
                test_name   TEXT NOT NULL,
                value       REAL NOT NULL,
                unit        TEXT NOT NULL,
                ref_low     REAL,
                ref_high    REAL,
                collected_at TEXT NOT NULL
            );
            CREATE INDEX idx_lab_patient ON lab_results(patient_id);
            CREATE INDEX idx_lab_test ON lab_results(test_name);
        """)
        con.executemany(
            "INSERT INTO patients (patient_id, name, sex, dob) VALUES (?,?,?,?)",
            patients,
        )
        con.executemany(
            """INSERT INTO lab_results
               (patient_id, test_name, value, unit, ref_low, ref_high, collected_at)
               VALUES (?,?,?,?,?,?,?)""",
            results,
        )
        con.commit()
    finally:
        con.close()

    print(f"Seeded {path}: {len(patients)} patients, {len(results)} lab results, "
          f"{len(TESTS)} tests, {len(targets)} injected outliers "
          f"({targets[0][1]} etc.).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate demo medical-lab SQLite database.")
    ap.add_argument("--db", default=DB_PATH, help="Output database path.")
    ap.add_argument("--patients", type=int, default=20, help="Number of patients (default: 20; total visits stay 100).")
    args = ap.parse_args()
    build_db(args.db, args.patients)


if __name__ == "__main__":
    main()
