/* app.js — Medical Outlier Explorer UI.
 * Upload a SQLite database, run MedicalEngine, render over-time charts with
 * red outlier marks (patient name next to each mark) and an outlier table
 * with the outlier values printed in red.
 */
(function () {
  "use strict";

  var PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b", "#e377c2",
                 "#7f7f7f", "#bcbd22", "#17becf", "#aec7e8", "#ffbb78", "#98df8a"];
  var OUTLIER_COLOR = "#d62728";

  var $ = function (id) { return document.getElementById(id); };
  var statusEl = $("status"), errorEl = $("error"), resultsEl = $("results");

  function setStatus(msg) { statusEl.textContent = msg; }
  function showError(msg) {
    errorEl.style.display = "block";
    errorEl.textContent = "Error: " + msg;
    resultsEl.style.display = "none";
    setStatus("");
  }
  function clearError() { errorEl.style.display = "none"; }

  /* ---- outlier name labels drawn on the chart at each red mark ---------- */
  var outlierLabelPlugin = {
    id: "outlierLabels",
    afterDatasetsDraw: function (chart) {
      var ds = null;
      for (var i = 0; i < chart.data.datasets.length; i++) {
        if (chart.data.datasets[i]._outliers && chart.data.datasets[i]._outliers.length) {
          ds = chart.data.datasets[i];
          break;
        }
      }
      if (!ds) return;
      var ctx = chart.ctx;
      ctx.save();
      ctx.font = "bold 10px system-ui, sans-serif";
      ctx.fillStyle = OUTLIER_COLOR;
      ds._outliers.forEach(function (p) {
        var x = chart.scales.x.getPixelForValue(p.x);
        var y = chart.scales.y.getPixelForValue(p.y);
        ctx.fillText(p.name, x + 6, y - 6);
      });
      ctx.restore();
    },
  };

  /* ---- pipeline ---------------------------------------------------------- */
  var sqlLib = null;      // set once the engine is initialized
  var enginePromise = null;

  function base64ToBytes(b64) {
    var bin = atob(b64);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  /** Validate the SQLite header and open the database. */
  function tryOpenDb(bytes) {
    var u8 = new Uint8Array(bytes);
    var header = "";
    for (var i = 0; i < 16 && i < u8.length; i++) header += String.fromCharCode(u8[i]);
    if (header !== "SQLite format 3\u0000") {
      return { ok: false, error: "not a SQLite database (missing the SQLite header)" };
    }
    try {
      return { ok: true, db: new sqlLib.Database(u8) };
    } catch (e) {
      return { ok: false, error: e.message || String(e) };
    }
  }

  function analyzeDb(db) {
    var loaded;
    try {
      loaded = MedicalEngine.loadResults(db);
    } catch (e) {
      return showError(e.message);
    } finally {
      db.close();
    }
    if (!loaded.rows.length) return showError("No lab results found in the database.");
    render(loaded.rows);
  }

  function processBytes(bytes) {
    clearError();
    if (!sqlLib) {
      return showError("The SQLite engine is not ready yet — please wait a moment and try again.");
    }
    setStatus("Analyzing…");
    var opened = tryOpenDb(bytes);
    if (!opened.ok) {
      return showError("That file is not a valid SQLite database (" + opened.error + ").");
    }
    analyzeDb(opened.db);
  }

  function readFile(file) {
    var reader = new FileReader();
    reader.onload = function () { processBytes(reader.result); };
    reader.onerror = function () { showError("Could not read the file."); };
    reader.readAsArrayBuffer(file);
  }

  /* ---- rendering ---------------------------------------------------------- */
  function render(rows) {
    var flags = MedicalEngine.detectOutliers(rows);

    var nFlagged = 0;
    flags.forEach(function (f) { if (f.length) nFlagged++; });

    var nPatients = {}, nTests = {};
    rows.forEach(function (r) {
      nPatients[r.patient_id] = true;
      nTests[r.test_name] = true;
    });

    var s = document.createElement("span");
    s.innerHTML = "Analyzed <b>" + rows.length + "</b> results across <b>" +
      Object.keys(nTests).length + "</b> tests and <b>" + Object.keys(nPatients).length +
      "</b> patients — <b class='out'>" + nFlagged + " out-of-range result(s)</b> " +
      "(values outside the clinical reference range).";
    $("summary").replaceChildren(s);

    var chartsEl = $("charts");
    chartsEl.replaceChildren();

    var tests = Object.keys(nTests).sort();
    tests.forEach(function (test) {
      var subOrig = [];
      rows.forEach(function (r, i) { if (r.test_name === test) subOrig.push([i, r]); });
      var sub = subOrig.map(function (p) { return p[1]; });
      var subFlags = subOrig.map(function (p) { return flags[p[0]]; });

      var card = document.createElement("div");
      card.className = "card";
      var h2 = document.createElement("h2");
      h2.textContent = test + " over time";
      card.appendChild(h2);
      var subEl = document.createElement("div");
      subEl.className = "sub";
      subEl.textContent = "each line is one patient · red marks are out-of-range values, labelled with the patient's name · dashed lines are reference limits";
      card.appendChild(subEl);
      var wrap = document.createElement("div");
      wrap.className = "chart-wrap";
      var canvas = document.createElement("canvas");
      wrap.appendChild(canvas);
      card.appendChild(wrap);
      chartsEl.appendChild(card);

      buildChart(canvas, sub, subFlags);
    });

    buildOutlierTable(rows, flags);
    resultsEl.style.display = "block";
    $("printBtn").style.display = "inline-block";
    setStatus("Done — " + tests.length + " chart(s) rendered.");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function patientName(rows, pid) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].patient_id === pid && rows[i].name) return rows[i].name;
    }
    return pid;
  }

  function dateKey(r) { return MedicalEngine.dateKey(r); }

  function buildChart(canvas, sub, subFlags) {
    var dateSet = {};
    sub.forEach(function (r) { if (r.datetime) dateSet[dateKey(r)] = true; });
    var dates = Object.keys(dateSet).sort();
    if (!dates.length) dates = sub.map(function (r) { return dateKey(r); });

    var byPatient = {};
    sub.forEach(function (r, i) {
      (byPatient[r.patient_id] = byPatient[r.patient_id] || []).push({ r: r, i: i });
    });
    var pids = Object.keys(byPatient).sort();

    var datasets = [];
    pids.forEach(function (pid, k) {
      var color = PALETTE[k % PALETTE.length];
      var pts = byPatient[pid].slice().sort(function (a, b) { return a.r.datetime - b.r.datetime; });
      var data = dates.map(function (d) {
        for (var i = 0; i < pts.length; i++) {
          if (dateKey(pts[i].r) === d) return { x: d, y: pts[i].r.value };
        }
        return { x: d, y: null }; // gap — Chart.js requires {x,y}, not a bare null
      });
      datasets.push({
        label: patientName(sub, pid),
        data: data,
        borderColor: color,
        backgroundColor: color,
        pointRadius: 2.5,
        borderWidth: 1.2,
        tension: 0.15,
        spanGaps: true, // connect each patient's dots with lines
      });
    });

    MedicalEngine.refRanges(sub).forEach(function (range, ri) {
      var color = ri === 0 ? "#2f6db3" : "#9db8d8";
      var dash = ri === 0 ? [5, 4] : [2, 3];
      var low = range[0], high = range[1];
      [["low", low], ["high", high]].forEach(function (pair) {
        if (pair[1] == null) return;
        datasets.push({
          label: "ref " + pair[0] + (ri ? " (other range)" : ""),
          data: dates.map(function () { return pair[1]; }),
          borderColor: color,
          borderDash: dash,
          pointRadius: 0,
          borderWidth: 1.2,
        });
      });
    });

    var outliers = [];
    sub.forEach(function (r, i) {
      if (subFlags[i].length) {
        outliers.push({ x: dateKey(r), y: r.value, name: patientName(sub, r.patient_id) });
      }
    });
    if (outliers.length) {
      datasets.push({
        label: "outlier",
        data: outliers.map(function (p) { return { x: p.x, y: p.y }; }),
        showLine: false,
        pointRadius: 5,
        pointBackgroundColor: OUTLIER_COLOR,
        pointBorderColor: "#ffffff",
        pointBorderWidth: 1,
        order: -1,
        _outliers: outliers,
      });
    }

    new Chart(canvas, {
      type: "line",
      data: { labels: dates, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false },
        plugins: {
          legend: {
            // with many patients a legend is unreadable; the outlier name
            // labels on the chart carry the important names anyway
            display: pids.length <= 12,
            labels: { font: { size: 10 }, boxWidth: 14, padding: 10 },
            maxHeight: 90,
          },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var v = ctx.parsed.y == null ? "—" : ctx.parsed.y;
                var name = ctx.dataset._outliers && ctx.dataset._outliers[ctx.dataIndex]
                  ? ctx.dataset._outliers[ctx.dataIndex].name + ": " : "";
                return (ctx.dataset.label || "") + " " + name + v;
              },
            },
          },
        },
        scales: {
          x: { ticks: { maxRotation: 60, maxTicksLimit: 8, font: { size: 10 } } },
          y: { ticks: { font: { size: 10 } } },
        },
      },
      plugins: [outlierLabelPlugin],
    });
  }

  function buildOutlierTable(rows, flags) {
    var flagged = [];
    rows.forEach(function (r, i) { if (flags[i].length) flagged.push({ r: r, i: i }); });
    flagged.sort(function (a, b) {
      var na = a.r.name || a.r.patient_id, nb = b.r.name || b.r.patient_id;
      if (na !== nb) return na.localeCompare(nb);
      if (a.r.test_name !== b.r.test_name) return a.r.test_name.localeCompare(b.r.test_name);
      return (a.r.datetime || 0) - (b.r.datetime || 0);
    });

    var table = $("outlierTable");
    table.replaceChildren();
    var thead = document.createElement("thead");
    var hr = document.createElement("tr");
    ["Patient", "Name", "Test", "Date", "Value", "Unit", "Ref range", "Note"]
      .forEach(function (h) {
        var th = document.createElement("th");
        th.textContent = h;
        hr.appendChild(th);
      });
    thead.appendChild(hr);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    flagged.forEach(function (o) {
      var r = o.r, fs = flags[o.i];
      var tr = document.createElement("tr");
      tr.className = "outlier-row";
      [r.patient_id, r.name || "—", r.test_name,
       r.datetime ? MedicalEngine.dateKey(r) : (r.collected_at || "—"),
       r.value.toFixed(2), r.unit || "",
       MedicalEngine.fmtRef(r), MedicalEngine.directionNote(r, fs)]
        .forEach(function (cell, ci) {
          var td = document.createElement("td");
          td.textContent = cell;
          if (ci === 4) td.className = "val"; // the value column, printed in red
          tr.appendChild(td);
        });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }

  /* ---- wiring -------------------------------------------------------------- */
  var dropzone = $("dropzone"), fileInput = $("file");

  function whenEngineReady(action) {
    // queue the action until the async engine init has finished
    (enginePromise || Promise.reject(new Error("Engine not started.")))
      .then(action)
      .catch(function (e) {
        console.error("action failed:", e && e.stack ? e.stack : e);
        showError(e.message);
      });
  }

  dropzone.addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    if (fileInput.files.length) whenEngineReady(function () { readFile(fileInput.files[0]); });
  });
  ["dragenter", "dragover"].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });
  dropzone.addEventListener("drop", function (e) {
    if (e.dataTransfer.files.length) whenEngineReady(function () { readFile(e.dataTransfer.files[0]); });
  });

  $("demoBtn").addEventListener("click", function () {
    setStatus("Loading demo database…");
    whenEngineReady(function () {
      // Prefer the embedded copy (works even from file://); fall back to fetch.
      if (typeof DEMO_DB_B64 !== "undefined") {
        var opened = tryOpenDb(base64ToBytes(DEMO_DB_B64));
        if (opened.ok) { analyzeDb(opened.db); return; }
        console.warn("embedded demo database unusable:", opened.error);
      }
      fetch("demo/medical_demo.db")
        .then(function (resp) {
          if (!resp.ok) throw new Error("Demo database not found next to the page (HTTP " + resp.status + ").");
          return resp.arrayBuffer();
        })
        .then(processBytes)
        .catch(function (e) { showError(e.message); });
    });
  });

  $("printBtn").addEventListener("click", function () { window.print(); });

  function initEngine() {
    // NOTE: sql.js caches the FIRST initSqlJs() call forever (its promise is
    // stored globally), so the config below must be right on the first call.
    // When the embedded copy is present we point locateFile at a data: URL
    // built from it — fetchable from ANY origin, including file:// where the
    // vendor fetch is blocked by CORS. When served over http(s) the vendor
    // file is used instead.
    if (typeof WASM_B64 !== "undefined") {
      return window.initSqlJs({
        locateFile: function () { return "data:application/wasm;base64," + WASM_B64; },
      }).then(function (SQL) { sqlLib = SQL; return SQL; });
    }
    return window.initSqlJs({
      locateFile: function (f) { return "vendor/" + f; },
    }).then(function (SQL) { sqlLib = SQL; return SQL; });
  }

  enginePromise = initEngine();
  enginePromise
    .then(function () {
      setStatus("Engine ready — upload a database or try the demo.");
    })
    .catch(function (e) { setStatus("Failed to load engine: " + e.message); });
})();
