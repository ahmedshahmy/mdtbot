/* engine.js — medical-lab outlier engine (port of the Python bot's logic).
 *
 * Pure logic, no DOM: works in the browser (attaches to window.MedicalEngine)
 * and in Node (module.exports). Detection mirrors the Python bot: only values
 * OUTSIDE the row's clinical reference range are flagged — values within the
 * reference range are never reported.
 */
(function (global) {
  "use strict";

  var ALIASES = {
    patient_id: ["patient_id", "pid", "patient", "mrn"],
    test_name: ["test_name", "test", "test_code", "investigation", "analyte"],
    value: ["value", "result", "result_value", "measurement"],
    unit: ["unit", "units"],
    ref_low: ["ref_low", "ref_min", "normal_low", "low", "ref_lo"],
    ref_high: ["ref_high", "ref_max", "normal_high", "high", "ref_hi"],
    collected_at: ["collected_at", "result_date", "date", "datetime", "taken_at"],
  };

  /* ---------- sql.js helpers ---------- */

  function tableNames(db) {
    var res = db.exec("SELECT name FROM sqlite_master WHERE type='table'");
    if (!res.length) return [];
    return res[0].values.map(function (row) { return row[0]; });
  }

  function tableColumns(db, table) {
    var res = db.exec("PRAGMA table_info(" + table + ")");
    if (!res.length) return [];
    return res[0].values.map(function (row) { return row[1]; });
  }

  function resolveColumns(db, table) {
    var actual = tableColumns(db, table);
    var mapping = {};
    Object.keys(ALIASES).forEach(function (canon) {
      ALIASES[canon].some(function (alias) {
        if (actual.indexOf(alias) !== -1) { mapping[canon] = alias; return true; }
        return false;
      });
    });
    return mapping;
  }

  /* ---------- data loading ---------- */

  /** @returns {{rows:Array<object>, meta:object}} */
  function loadResults(db) {
    var lr = resolveColumns(db, "lab_results");
    var required = ["patient_id", "test_name", "value"];
    for (var i = 0; i < required.length; i++) {
      if (!lr[required[i]]) {
        throw new Error("lab_results table must have at least: " + required.join(", ") +
                        " (found: " + tableColumns(db, "lab_results").join(", ") + ")");
      }
    }

    var hasPatients = tableNames(db).indexOf("patients") !== -1;
    var meta = { has_patients: hasPatients };

    var sel = Object.keys(lr).map(function (c) { return "r." + lr[c]; }).join(", ");
    var q = "SELECT " + sel + " FROM lab_results r";
    if (hasPatients) {
      var pc = tableColumns(db, "patients");
      meta.name_col = pc.indexOf("name") !== -1 ? "name" : null;
      meta.sex_col = pc.indexOf("sex") !== -1 ? "sex" : null;
      if (meta.name_col) sel += ", p." + meta.name_col + " AS p_name";
      if (meta.sex_col) sel += ", p." + meta.sex_col + " AS p_sex";
      q = "SELECT " + sel + " FROM lab_results r";
      q += " LEFT JOIN patients p ON p." +
           (resolveColumns(db, "patients").patient_id || "patient_id") +
           " = r." + lr.patient_id;
    }

    var res = db.exec(q);
    var cols = res.length ? res[0].columns : [];
    var rows = [];
    if (res.length) {
      res[0].values.forEach(function (v) {
        var d = {};
        cols.forEach(function (c, idx) { d[c] = v[idx]; });
        var val = Number(d[lr.value]);
        if (!isFinite(val)) return;
        var row = {
          patient_id: String(d[lr.patient_id]),
          test_name: String(d[lr.test_name]),
          value: val,
          unit: lr.unit ? String(d[lr.unit] == null ? "" : d[lr.unit]) : "",
          ref_low: lr.ref_low && d[lr.ref_low] != null ? Number(d[lr.ref_low]) : null,
          ref_high: lr.ref_high && d[lr.ref_high] != null ? Number(d[lr.ref_high]) : null,
          collected_at: lr.collected_at ? String(d[lr.collected_at] || "") : "",
          name: meta.name_col ? String(d.p_name == null ? "" : d.p_name) : null,
          sex: meta.sex_col ? String(d.p_sex == null ? "" : d.p_sex) : null,
        };
        row.datetime = row.collected_at ? parseDate(row.collected_at) : null;
        rows.push(row);
      });
    }
    return { rows: rows, meta: meta };
  }

  function parseDate(s) {
    // accepts "2023-01-15", "2023-01-15T08:00:00", "2023/01/15"
    var m = String(s).match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ](\d{1,2}):(\d{2}))?/);
    if (!m) return null;
    var dt = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3],
                             m[4] ? +m[4] : 0, m[5] ? +m[5] : 0));
    return isNaN(dt.getTime()) ? null : dt;
  }

  /* ---------- outlier detection ---------- */

  /** @returns {Array<Array<string>>} flags[rowIndex] — only ["ref"] or [] */
  function detectOutliers(rows) {
    var flags = rows.map(function () { return []; });
    rows.forEach(function (r, i) {
      if ((r.ref_low != null && r.value < r.ref_low) ||
          (r.ref_high != null && r.value > r.ref_high)) {
        flags[i].push("ref");
      }
    });
    return flags;
  }

  /** Distinct (ref_low, ref_high) pairs, most common first. */
  function refRanges(rows) {
    var counts = {};
    rows.forEach(function (r) {
      var key = r.ref_low + "|" + r.ref_high;
      counts[key] = (counts[key] || 0) + 1;
    });
    return Object.keys(counts)
      .sort(function (a, b) { return counts[b] - counts[a]; })
      .map(function (k) {
        var parts = k.split("|");
        return [parts[0] === "null" ? null : Number(parts[0]),
                parts[1] === "null" ? null : Number(parts[1])];
      });
  }

  function fmtRef(r) {
    var u = r.unit || "";
    if (r.ref_low != null && r.ref_high != null) return r.ref_low + " \u2013 " + r.ref_high + " " + u;
    if (r.ref_high != null) return "< " + r.ref_high + " " + u;
    if (r.ref_low != null) return "> " + r.ref_low + " " + u;
    return "n/a";
  }

  function directionNote(r, flagsHit) {
    var side = "";
    if (r.ref_high != null && r.value > r.ref_high) side = " HIGH";
    else if (r.ref_low != null && r.value < r.ref_low) side = " LOW";
    return side ? "CLINICAL" + side : "CLINICAL out-of-range";
  }

  function dateKey(r) {
    if (!r.datetime) return "";
    return r.datetime.toISOString().slice(0, 10);
  }

  var MedicalEngine = {
    loadResults: loadResults,
    detectOutliers: detectOutliers,
    refRanges: refRanges,
    fmtRef: fmtRef,
    directionNote: directionNote,
    dateKey: dateKey,
    parseDate: parseDate,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = MedicalEngine;
  } else {
    global.MedicalEngine = MedicalEngine;
  }
})(typeof window !== "undefined" ? window : this);
