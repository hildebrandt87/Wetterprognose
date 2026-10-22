"use strict";
/* Archiv-Ansicht – nutzt $, api, fmt1, esc aus app.js */

const MONTH_NAMES = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
  "August", "September", "Oktober", "November", "Dezember"];

const ar = { corrMetric: "sun", mode: "month", year: 0, month: 1, week: 1, day: "", meta: null, inited: false };

const pad = (n) => String(n).padStart(2, "0");
const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseDate = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const fmtH = (v) => (v == null ? "–" : `${fmt1(v)} h`);

function isoWeek(date) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const day = (d.getDay() + 6) % 7;                 // Mo=0
  d.setDate(d.getDate() - day + 3);                 // Donnerstag dieser Woche
  const firstThu = new Date(d.getFullYear(), 0, 4);
  const week = 1 + Math.round(((d - firstThu) / 864e5 - 3 + ((firstThu.getDay() + 6) % 7)) / 7);
  return [d.getFullYear(), week];
}
function mondayOfWeek(year, week) {
  const jan4 = new Date(year, 0, 4);
  const monday = new Date(jan4);
  monday.setDate(jan4.getDate() - ((jan4.getDay() + 6) % 7) + (week - 1) * 7);
  return monday;
}
const weeksInYear = (year) => isoWeek(new Date(year, 11, 28))[1];

/* ---------------- Filter ---------------- */
function setToday() {
  const now = new Date();
  ar.year = now.getFullYear();
  ar.month = now.getMonth() + 1;
  [ar.weekYear, ar.week] = isoWeek(now);
  ar.day = isoDate(now);
}

function syncFilters() {
  const years = new Set([...(ar.meta?.years || []), new Date().getFullYear(), ar.year]);
  if (ar.mode === "week") years.add(ar.weekYear);
  const yearVal = ar.mode === "week" ? ar.weekYear : ar.year;
  $("#ar-year").innerHTML = [...years].sort().map((y) => `<option ${y === yearVal ? "selected" : ""}>${y}</option>`).join("");
  $("#ar-month").innerHTML = MONTH_NAMES.map((n, i) => `<option value="${i + 1}" ${i + 1 === ar.month ? "selected" : ""}>${n}</option>`).join("");
  const wy = ar.weekYear;
  $("#ar-week").innerHTML = Array.from({ length: weeksInYear(wy) }, (_, i) => {
    const mon = mondayOfWeek(wy, i + 1), sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    const f = (d) => `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.`;
    return `<option value="${i + 1}" ${i + 1 === ar.week ? "selected" : ""}>KW ${i + 1} (${f(mon)}–${f(sun)})</option>`;
  }).join("");
  $("#ar-day").value = ar.day;

  $("#ar-year").hidden = ar.mode === "day";
  $("#ar-month").hidden = ar.mode !== "month";
  $("#ar-week").hidden = ar.mode !== "week";
  $("#ar-day").hidden = ar.mode !== "day";
  document.querySelectorAll("#ar-mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === ar.mode));
}

function step(dir) {
  if (ar.mode === "year") ar.year += dir;
  if (ar.mode === "month") {
    ar.month += dir;
    if (ar.month < 1) { ar.month = 12; ar.year--; }
    if (ar.month > 12) { ar.month = 1; ar.year++; }
  }
  if (ar.mode === "week") {
    const d = mondayOfWeek(ar.weekYear, ar.week);
    d.setDate(d.getDate() + 7 * dir);
    [ar.weekYear, ar.week] = isoWeek(d);
  }
  if (ar.mode === "day") {
    const d = parseDate(ar.day);
    d.setDate(d.getDate() + dir);
    ar.day = isoDate(d);
  }
  loadArchive();
}

function setMode(mode) {
  // Beim Wechsel den Zeitraum möglichst beibehalten
  if (mode === "week" && ar.mode !== "week") {
    const ref = ar.mode === "day" ? parseDate(ar.day) : new Date(ar.year, ar.month - 1, ar.mode === "year" ? 1 : 15);
    [ar.weekYear, ar.week] = isoWeek(ref);
  }
  if (mode === "day" && ar.mode !== "day") {
    const ref = ar.mode === "week" ? mondayOfWeek(ar.weekYear, ar.week) : new Date(ar.year, ar.month - 1, 1);
    const cur = parseDate(ar.day);
    const inMonth = ar.mode === "month" && cur.getFullYear() === ar.year && cur.getMonth() + 1 === ar.month;
    if (!inMonth) ar.day = isoDate(ref);
  }
  if ((mode === "month" || mode === "year") && ar.mode === "day") {
    const d = parseDate(ar.day); ar.year = d.getFullYear(); ar.month = d.getMonth() + 1;
  }
  if ((mode === "month" || mode === "year") && ar.mode === "week") {
    const d = mondayOfWeek(ar.weekYear, ar.week); d.setDate(d.getDate() + 3);
    ar.year = d.getFullYear(); ar.month = d.getMonth() + 1;
  }
  ar.mode = mode;
  loadArchive();
}

/* ---------------- Laden & Darstellen ---------------- */
async function loadArchive() {
  if (!ar.inited) initArchive();
  syncFilters();
  let q = `mode=${ar.mode}`;
  if (ar.mode === "year") q += `&year=${ar.year}`;
  if (ar.mode === "month") q += `&year=${ar.year}&month=${ar.month}`;
  if (ar.mode === "week") q += `&year=${ar.weekYear}&week=${ar.week}`;
  if (ar.mode === "day") q += `&day=${ar.day}`;
  $("#ar-error").hidden = true;
  try {
    const [data, meta, corr] = await Promise.all(
      [api(`/api/archive?${q}`), api("/api/archive/meta"), api(`/api/correction?metric=${ar.corrMetric}`)]);
    ar.meta = meta;
    renderMeta(meta);
    renderCorrection(corr);
    renderArchive(data);
  } catch (err) {
    $("#ar-content").hidden = true;
    $("#ar-error").textContent = `Archiv konnte nicht geladen werden: ${err.message}`;
    $("#ar-error").hidden = false;
  }
}

function renderArchive(data) {
  $("#ar-title").textContent = data.title;
  const s = data.summary;
  const empty = ar.mode === "day" ? !data.day : !s.days;
  $("#ar-empty").hidden = !empty;
  $("#ar-content").hidden = empty;
  if (empty) return;

  // Kennzahlen
  $("#ar-k-fc").textContent = fmtH(s.forecast_h);
  $("#ar-k-fc-sub").textContent = s.possible_h != null ? `von ${fmt1(s.possible_h)} h möglich` : "";
  $("#ar-k-act").textContent = fmtH(s.actual_h);
  $("#ar-k-act-sub").textContent = ar.mode === "day"
    ? (data.day?.actual_station ? `Station ${data.day.actual_station}` : "noch nicht gemessen")
    : `${s.actual_days} von ${s.days} Tagen gemessen`;
  if (ar.mode === "day") {
    $("#ar-k-avg-label").textContent = "Sonnenauf-/untergang";
    $("#ar-k-avg").textContent = data.day.sunrise ? `${data.day.sunrise}–${data.day.sunset}` : "–";
    $("#ar-k-avg-sub").textContent = data.day.daylength_h ? `Tageslänge ${fmt1(data.day.daylength_h)} h` : "";
  } else {
    const basis = s.actual_days ? s.actual_h / s.actual_days : s.forecast_days ? s.forecast_h / s.forecast_days : null;
    $("#ar-k-avg-label").textContent = "Ø pro Tag";
    $("#ar-k-avg").textContent = fmtH(basis);
    $("#ar-k-avg-sub").textContent = data.best
      ? `sonnigster Tag: ${parseDate(data.best.date).toLocaleDateString("de-DE")} (${fmt1(data.best.hours)} h)`
      : "";
  }
  if (s.mae_h != null) {
    $("#ar-k-err").textContent = ar.mode === "day" ? `${s.bias_h > 0 ? "+" : ""}${fmt1(s.bias_h)} h` : `± ${fmt1(s.mae_h)} h`;
    $("#ar-k-err-sub").textContent = ar.mode === "day"
      ? (s.bias_h > 0 ? "Prognose zu sonnig" : s.bias_h < 0 ? "Prognose zu trüb" : "exakt getroffen")
      : `Ø je Tag · Tendenz ${s.bias_h > 0 ? "+" : ""}${fmt1(s.bias_h)} h (${s.bias_h > 0 ? "zu sonnig" : "zu trüb"})`;
  } else {
    $("#ar-k-err").textContent = "–";
    $("#ar-k-err-sub").textContent = "erst mit Messwerten";
  }

  if (ar.mode === "day") return renderDay(data);
  $("#ar-day-info").hidden = true;
  $("#ar-table-card").hidden = false;
  $("#ar-drill-hint").hidden = false;
  $("#ar-chart-title").textContent = ar.mode === "year" ? "Sonnenstunden je Monat" : "Sonnenstunden je Tag";
  renderBars(data.rows.map((r) => ({
    key: r.key, label: r.label, fc: r.forecast_h, act: r.actual_h, possible: r.possible_h,
  })), (key) => drill(data.drill, key), "h");
  renderTable(data);
  renderPvArchive(data.rows.map((r) => ({ key: r.key, label: r.label, fc: r.pv_kwh, act: r.pv_actual_kwh })),
    s, (key) => drill(data.drill, key), ar.mode === "year" ? "je Monat" : "je Tag");
}

function renderPvArchive(items, s, onClick, per) {
  const has = items.some((i) => i.fc != null || i.act != null);
  $("#ar-pv-card").hidden = !has;
  if (!has) return;
  const kwh = (v) => (v == null ? "–" : `${v.toLocaleString("de-DE", { maximumFractionDigits: 1 })} kWh`);
  const pairs = items.filter((i) => i.fc != null && i.act != null);
  const mae = pairs.length ? pairs.reduce((a, i) => a + Math.abs(i.fc - i.act), 0) / pairs.length : null;
  const best = items.filter((i) => i.act != null).sort((a, b) => b.act - a.act)[0];
  $("#ar-pv-title").textContent = `Solarertrag ${per}`;
  $("#ar-pv-kpis").innerHTML = `
    <div><span class="label">Prognose</span><b>${kwh(s.pv_kwh)}</b></div>
    <div><span class="label">Aus Messung</span><b>${kwh(s.pv_actual_kwh)}</b></div>
    <div><span class="label">Ø Abweichung ${per}</span><b>${kwh(mae)}</b></div>
    <div><span class="label">Bester Wert</span><b>${best ? `${kwh(best.act)} (${esc(best.label)})` : "–"}</b></div>`;
  renderBars(items, onClick, "kWh", { svg: "#ar-pv-chart", fcClass: "bar-pv", actLabel: "aus Messung" });
}

function drill(target, key) {
  if (target === "month") { const [y, m] = key.split("-").map(Number); ar.year = y; ar.month = m; ar.mode = "month"; }
  if (target === "day") { ar.day = key; ar.mode = "day"; }
  loadArchive();
}

function renderTable(data) {
  const isYear = ar.mode === "year";
  const hasPv = data.rows.some((r) => r.pv_kwh != null || r.pv_actual_kwh != null);
  const pvCell = (v, a) => (hasPv ? `<td class="num">${v == null ? "–" : `${fmt1(v)} kWh`}</td>`
    + `<td class="num">${a == null ? "–" : `${fmt1(a)} kWh`}</td>` : "");
  const head = `<tr><th>${isYear ? "Monat" : "Tag"}</th><th class="num">Prognose</th><th class="num">Gemessen</th>
    <th class="num">Abweichung</th><th class="num">möglich</th>${hasPv ? '<th class="num">Solar Prognose</th><th class="num">Solar aus Messung</th>' : ""}
    <th>${isYear ? "Tage" : "Art"}</th></tr>`;
  const rows = data.rows.filter((r) => !isYear || r.days).map((r) => {
    const diff = r.forecast_h != null && r.actual_h != null ? r.forecast_h - r.actual_h : null;
    const diffCell = diff == null ? "–"
      : `<span class="${diff > 0 ? "diff-pos" : diff < 0 ? "diff-neg" : ""}">${diff > 0 ? "+" : ""}${fmt1(diff)} h</span>`;
    const last = isYear
      ? `${r.days} (${r.actual_days} gemessen)`
      : `<span class="tag">${{ vortag: "Vortag", gleicher_tag: "selber Tag", rekonstruiert: "rekonstruiert" }[r.kind] || "nur Messung"}</span>`;
    const label = isYear ? MONTH_NAMES[Number(r.key.slice(5)) - 1] : r.label;
    return `<tr class="clickable" data-key="${r.key}"><td>${label}</td><td class="num">${fmtH(r.forecast_h)}</td>
      <td class="num">${fmtH(r.actual_h)}</td><td class="num">${diffCell}</td><td class="num">${fmtH(r.possible_h)}</td>${pvCell(r.pv_kwh, r.pv_actual_kwh)}<td>${last}</td></tr>`;
  }).join("");
  const s = data.summary;
  const total = `<tr><td><b>Summe</b></td><td class="num"><b>${fmtH(s.forecast_h)}</b></td><td class="num"><b>${fmtH(s.actual_h)}</b></td>
    <td></td><td class="num">${fmtH(s.possible_h)}</td>${pvCell(s.pv_kwh, s.pv_actual_kwh)}<td></td></tr>`;
  $("#ar-table").innerHTML = head + rows + total;
  $("#ar-table").onclick = (e) => {
    const tr = e.target.closest("tr[data-key]");
    if (tr) drill(data.drill, tr.dataset.key);
  };
}

function renderDay(data) {
  const det = data.details || {};
  const fcHours = det.forecast?.hours || [];
  const actual = det.actual || {};
  const hours = new Map();
  fcHours.forEach((h) => hours.set(h.hour, { fc: h.expected_min, possible: h.possible_min }));
  Object.entries(actual).forEach(([h, m]) => hours.set(h, { ...(hours.get(h) || { fc: null, possible: null }), act: m }));
  const list = [...hours.entries()].sort(([a], [b]) => a.localeCompare(b))
    .filter(([, v]) => (v.possible ?? 0) > 0 || (v.act ?? 0) > 0)
    .map(([h, v]) => ({ key: h, label: h.slice(0, 2), fc: v.fc ?? null, act: v.act ?? null, possible: v.possible ?? null }));
  Object.values(list).forEach((v) => { if (v.act == null && Object.keys(actual).length) v.act = 0; });

  $("#ar-chart-title").textContent = "Sonnenminuten je Stunde";
  $("#ar-drill-hint").hidden = true;
  $("#ar-table-card").hidden = true;
  renderBars(list, null, "min");
  const ph = data.pv_hours;
  if (ph) {
    const keys = [...new Set([...Object.keys(ph.forecast), ...Object.keys(ph.measured)])].sort();
    const pvItems = keys.map((h) => ({ key: h, label: h.slice(0, 2), fc: ph.forecast[h] ?? null, act: ph.measured[h] ?? null }));
    renderPvArchive(pvItems, { pv_kwh: data.day.pv_kwh, pv_actual_kwh: data.day.pv_actual_kwh }, null, "je Stunde");
  } else {
    $("#ar-pv-card").hidden = true;
  }

  const win = det.forecast?.windows || [];
  const src = det.forecast?.sources || [];
  $("#ar-day-info").innerHTML = `
    <h2>Details</h2>
    <div class="meta-grid">
      <div><span class="label">Art der Prognose</span><b>${esc(data.kind_label || "keine Prognose")}</b></div>
      <div><span class="label">Erstellt</span><b>${esc(data.day.forecast_at || "–")}</b></div>
      <div><span class="label">Zuverlässigkeit</span><b>${esc(det.forecast?.confidence_label || "–")}</b></div>
      <div><span class="label">Globalstrahlung Prognose / gemessen</span><b>${data.day.ghi_raw == null ? "–"
        : data.day.ghi_raw.toLocaleString("de-DE")} / ${data.day.ghi_actual == null ? "–"
        : data.day.ghi_actual.toLocaleString("de-DE")} kWh/m²</b></div>
      ${data.day.pv_kwh != null ? `<div><span class="label">Solarertrag (Prognose)</span><b>${fmt1(data.day.pv_kwh)} kWh</b></div>` : ""}
      <div><span class="label">Korrektur</span><b>${det.forecast?.correction
        ? `Faktor ${det.forecast.correction.factor.toLocaleString("de-DE")} (Modelle ${fmtH(data.day.raw_h)})`
        : "keine"}</b></div>
      <div><span class="label">Standort</span><b>${esc(data.day.place || `${data.day.lat}, ${data.day.lon}`)}</b></div>
    </div>
    <p><span class="label">Prognostizierte Sonnenfenster</span><br>${win.length
      ? win.map((w) => `<span class="window-chip">${w.from}–${w.to} Uhr<span>≈ ${w.minutes} min</span></span>`).join(" ")
      : '<span class="muted">keine</span>'}</p>
    ${src.length ? `<div class="table-wrap"><table class="tbl"><tr><th>Quelle</th><th class="num">Prognose</th></tr>
      ${src.map((x) => `<tr><td>${esc(x.label)}</td><td class="num">${fmtH(x.total_h)}</td></tr>`).join("")}</table></div>` : ""}
    ${data.day.packed ? '<p class="muted small">Aus komprimiertem Monatsarchiv entpackt.</p>' : ""}`;
  $("#ar-day-info").hidden = false;
}

/* Gruppierte Balken: Prognose + Gemessen vor „möglich“ */
function renderBars(items, onClick, unit, opts = {}) {
  const sel = opts.svg || "#ar-chart";
  ar.lastBars = { ...ar.lastBars, [sel]: [items, onClick, unit, opts] };
  const svg = $(sel);
  const W = Math.max(520, svg.clientWidth || 800), H = 260;
  const m = { l: 40, r: 10, t: 12, b: 26 };
  const cw = W - m.l - m.r, ch = H - m.t - m.b;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!items.length) { svg.innerHTML = ""; return; }

  const max = Math.max(1, ...items.flatMap((i) => [i.fc ?? 0, i.act ?? 0, i.possible ?? 0]));
  const nice = unit === "min" ? 60 : niceMax(max);
  const fcClass = opts.fcClass || "bar-fc";
  const decimals = unit === "kWh" && nice <= 2 ? 2 : 1;
  const num = (v) => v.toLocaleString("de-DE", { maximumFractionDigits: decimals });
  const y = (v) => m.t + ch - (v / nice) * ch;
  const stepW = cw / items.length, group = Math.min(stepW * 0.78, 64), bw = group / 2;
  let out = "";
  for (let i = 0; i <= 4; i++) {
    const v = (nice / 4) * i;
    out += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>`;
    out += `<text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${num(v)}</text>`;
  }
  const labelEvery = Math.ceil(items.length / Math.max(1, Math.floor(cw / 34)));
  items.forEach((it, i) => {
    const gx = m.l + i * stepW + (stepW - group) / 2;
    const bar = (cls, x, w, v) => (v == null ? "" : `<rect class="${cls}" x="${x}" y="${y(v)}" width="${w}" height="${Math.max(0, m.t + ch - y(v))}" rx="2"/>`);
    const tip = `${it.label}: Prognose ${it.fc == null ? "–" : num(it.fc)} · ${opts.actLabel || "gemessen"} ${it.act == null ? "–" : num(it.act)}` +
      (it.possible != null ? ` · möglich ${num(it.possible)}` : "") + ` ${unit}`;
    out += `<g data-key="${it.key}"><title>${esc(tip)}</title>`;
    out += bar("bar-possible", gx, group, it.possible);
    out += bar(fcClass, gx + 1, bw - 1.5, it.fc);
    out += bar("bar-act", gx + bw + 0.5, bw - 1.5, it.act);
    if (onClick) out += `<rect class="hit" x="${m.l + i * stepW}" y="${m.t}" width="${stepW}" height="${ch}"/>`;
    out += "</g>";
    if (i % labelEvery === 0) out += `<text x="${gx + group / 2}" y="${H - 8}" text-anchor="middle">${esc(it.label.replace(/^\w\w /, ""))}</text>`;
  });
  svg.innerHTML = out;
  svg.onclick = onClick ? (e) => { const g = e.target.closest("g[data-key]"); if (g) onClick(g.dataset.key); } : null;
}

function niceMax(v) {
  const steps = [1, 2, 2.5, 5, 10];
  const mag = 10 ** Math.floor(Math.log10(v));
  for (const s of steps) if (s * mag >= v) return s * mag;
  return 10 * mag;
}

/* ---------------- Lernende Korrektur ---------------- */
function renderCorrection(c) {
  const ghi = c.metric === "ghi";
  const unit = ghi ? "kWh/m²" : "h";
  const num = (v) => v.toLocaleString("de-DE", { minimumFractionDigits: ghi ? 2 : 1, maximumFractionDigits: ghi ? 2 : 1 });
  const val = (v) => (v == null ? "–" : `${num(v)} ${unit}`);
  const total = (v) => (v == null ? "–" : `${fmt1(v)} ${unit}`);
  const improve = c.mae_raw ? Math.round((1 - c.mae_corr / c.mae_raw) * 100) : null;
  const sign = (v) => (v > 0 ? "+" : "") + num(v);
  document.querySelectorAll("#corr-metric button").forEach((b) => b.classList.toggle("active", b.dataset.metric === c.metric));
  $("#corr-summary").innerHTML = c.days ? `
    <div><span class="label">Lerntage</span><b>${c.days}</b></div>
    <div><span class="label">Ø Fehler ohne Korrektur</span><b>${val(c.mae_raw)}</b></div>
    <div><span class="label">Ø Fehler mit Korrektur</span><b class="${c.mae_corr < c.mae_raw ? "better" : "worse"}">${val(c.mae_corr)}${
      improve != null ? ` (${improve > 0 ? "−" : "+"}${Math.abs(improve)} %)` : ""}</b></div>
    <div><span class="label">Tendenz ohne → mit</span><b>${sign(c.bias_raw)} → ${sign(c.bias_corr)} ${unit}</b></div>`
    : `<div class="muted">Noch keine Tage mit Prognose und Messwert – Daten unten nachladen.</div>`;
  const rows = c.months.map((m) => {
    const cls = m.mae_corr == null ? "" : m.mae_corr < m.mae_raw ? "better" : "worse";
    const basis = { monat: "", jahr: " <span class=\"tag\">Jahresmittel</span>", keine: " <span class=\"tag\">zu wenig Daten</span>" }[m.basis];
    return `<tr><td>${m.label}</td><td class="num">${m.factor.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${basis}</td>
      <td class="num">${m.days}</td><td class="num">${m.days ? total(m.raw_h) : "–"}</td><td class="num">${m.days ? total(m.actual_h) : "–"}</td>
      <td class="num">${val(m.mae_raw)}</td>
      <td class="num ${cls}">${val(m.mae_corr)}</td></tr>`;
  }).join("");
  $("#corr-table").innerHTML = `<tr><th>Monat</th><th class="num">Faktor</th><th class="num">Tage</th>
    <th class="num">Modelle</th><th class="num">Gemessen</th><th class="num">Ø Fehler vorher</th><th class="num">nachher</th></tr>${rows}`;
}

/* ---------------- Datenbestand & Nachladen ---------------- */
function fmtBytes(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toLocaleString("de-DE", { maximumFractionDigits: 1 })} KB`;
  return `${(b / 1024 ** 2).toLocaleString("de-DE", { maximumFractionDigits: 1 })} MB`;
}

function renderMeta(meta) {
  const ratio = meta.packed_bytes ? (meta.raw_bytes / meta.packed_bytes).toFixed(1).replace(".", ",") : null;
  const d = (s) => (s ? parseDate(s).toLocaleDateString("de-DE") : "–");
  $("#ar-meta").innerHTML = `
    <div><span class="label">Zeitraum</span><b>${d(meta.first)} – ${d(meta.last)}</b></div>
    <div><span class="label">Archivierte Tage</span><b>${meta.days || 0}</b></div>
    <div><span class="label">Komprimierte Monate</span><b>${meta.packed_months} (${meta.packed_days} Tage)</b></div>
    <div><span class="label">Kompression</span><b>${ratio ? `${fmtBytes(meta.raw_bytes)} → ${fmtBytes(meta.packed_bytes)} (${ratio}×)` : "–"}</b></div>
    <div><span class="label">Offene Details</span><b>${fmtBytes(meta.open_detail_bytes)}</b></div>
    <div><span class="label">Datenbank</span><b>${fmtBytes(meta.db_bytes)}</b></div>`;
  const bf = meta.backfill;
  $("#ar-bf-start").disabled = bf.running;
  $("#ar-bf-status").textContent = bf.running ? `${bf.message} ${bf.done}/${bf.total} Tage` : bf.message;
}

async function pollBackfill() {
  const meta = await api("/api/archive/meta");
  renderMeta(meta);
  if (meta.backfill.running) setTimeout(pollBackfill, 1500);
  else loadArchive();
}

function initArchive() {
  ar.inited = true;
  setToday();
  document.querySelectorAll("#ar-mode button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  $("#ar-prev").addEventListener("click", () => step(-1));
  $("#ar-next").addEventListener("click", () => step(1));
  $("#ar-now").addEventListener("click", () => { setToday(); loadArchive(); });
  document.querySelectorAll("#corr-metric button").forEach((b) => b.addEventListener("click", async () => {
    ar.corrMetric = b.dataset.metric;
    renderCorrection(await api(`/api/correction?metric=${ar.corrMetric}`));
  }));
  $("#ar-year").addEventListener("change", (e) => {
    const y = Number(e.target.value);
    if (ar.mode === "week") { ar.weekYear = y; ar.week = Math.min(ar.week, weeksInYear(y)); } else ar.year = y;
    loadArchive();
  });
  $("#ar-month").addEventListener("change", (e) => { ar.month = Number(e.target.value); loadArchive(); });
  $("#ar-week").addEventListener("change", (e) => { ar.week = Number(e.target.value); loadArchive(); });
  $("#ar-day").addEventListener("change", (e) => { if (e.target.value) { ar.day = e.target.value; loadArchive(); } });

  const yesterday = new Date(); yesterday.setDate(yesterday.getDate() - 1);
  $("#ar-bf-to").value = isoDate(yesterday);
  $("#ar-bf-from").value = `${yesterday.getFullYear()}-01-01`;
  $("#ar-bf-start").addEventListener("click", async () => {
    try {
      await api("/api/archive/backfill", { method: "POST", body: { from: $("#ar-bf-from").value, to: $("#ar-bf-to").value } });
      $("#ar-bf-start").disabled = true;
      pollBackfill();
    } catch (err) {
      $("#ar-bf-status").textContent = `Fehler: ${err.message}`;
    }
  });
}

let arResizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(arResizeTimer);
  arResizeTimer = setTimeout(() => {
    if (ar.lastBars && !$("#view-archive").hidden) Object.values(ar.lastBars).forEach((args) => renderBars(...args));
  }, 150);
});
