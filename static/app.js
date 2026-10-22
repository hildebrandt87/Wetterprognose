"use strict";

const $ = (sel) => document.querySelector(sel);
const DIRS = ["N", "NNO", "NO", "ONO", "O", "OSO", "SO", "SSO", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
const WEEKDAYS = ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"];

let settings = null;
let currentDay = "tomorrow";
let map, marker;
let lastResult = null;

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const fmt1 = (n) => n.toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------------- Navigation ---------------- */
function showView(name) {
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${name}`));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  if (name === "settings") setTimeout(initMap, 0);
  if (name === "forecast") loadForecast();
  if (name === "archive") loadArchive();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));
document.addEventListener("click", (e) => {
  const link = e.target.closest("[data-goto]");
  if (link) { e.preventDefault(); showView(link.dataset.goto); }
});
document.querySelectorAll("#day-switch button").forEach((b) => b.addEventListener("click", () => {
  currentDay = b.dataset.day;
  document.querySelectorAll("#day-switch button").forEach((x) => x.classList.toggle("active", x === b));
  loadForecast();
}));

/* ---------------- Prognose ---------------- */
async function loadForecast() {
  const hasLoc = settings && settings.location.lat != null;
  $("#fc-empty").hidden = hasLoc;
  $("#fc-error").hidden = true;
  if (!hasLoc) { $("#fc-content").hidden = true; return; }
  $("#fc-loading").hidden = false;
  try {
    renderForecast(await api(`/api/forecast?day=${currentDay}`));
  } catch (err) {
    $("#fc-content").hidden = true;
    $("#fc-error").textContent = `Prognose konnte nicht geladen werden: ${err.message}`;
    $("#fc-error").hidden = false;
  } finally {
    $("#fc-loading").hidden = true;
  }
}

function renderForecast(r) {
  lastResult = r;
  const d = new Date(`${r.date}T12:00:00`);
  $("#fc-date").textContent = `${WEEKDAYS[d.getDay()]}, ${d.toLocaleDateString("de-DE")}`;
  $("#fc-place").textContent = r.location.name || `${r.location.lat.toFixed(4)}, ${r.location.lon.toFixed(4)}`;
  $("#fc-total").textContent = fmt1(r.total_hours);
  $("#fc-meter").style.width = r.possible_hours ? `${Math.min(100, (100 * r.total_hours) / r.possible_hours)}%` : "0";
  $("#fc-possible").textContent =
    `von ${fmt1(r.possible_hours)} h möglich (Tageslänge ${fmt1(r.daylength_hours)} h)`;
  const c = r.correction;
  $("#fc-corr").hidden = !c || Math.abs(r.raw_total_hours - r.total_hours) < 0.05;
  if (c) {
    $("#fc-corr").textContent = `Korrigiert: Modelle ${fmt1(r.raw_total_hours)} h × Faktor ` +
      `${c.factor.toLocaleString("de-DE")} (${c.basis === "jahr" ? "Jahresmittel" : c.month}, ${c.days} Archivtage)`;
  }
  $("#fc-sunrise").textContent = r.sunrise ?? "–";
  $("#fc-sunset").textContent = r.sunset ?? "–";
  $("#fc-visible").textContent = r.visible_from ? `${r.visible_from}–${r.visible_to}` : "–";
  $("#fc-elev").textContent = `${r.max_elevation.toLocaleString("de-DE")}°`;
  $("#fc-conf").textContent = r.confidence == null
    ? r.confidence_label
    : `${r.confidence_label} (${Math.round(r.confidence * 100)} %)`;

  $("#fc-windows").innerHTML = r.windows.length
    ? r.windows.map((w) => `<div class="window-chip">${w.from}–${w.to} Uhr<span>≈ ${w.minutes} min Sonne</span></div>`).join("")
    : `<span class="muted">Keine Stunde erreicht ${r.threshold_min} Sonnenminuten.</span>`;

  renderChart(r.hours);

  const rows = r.sources.map((s) => {
    const meta = s.meta?.station ? `Station ${esc(s.meta.station)} (${s.meta.distance_km} km)` : "";
    return `<tr><td>${esc(s.label)}</td>
      <td class="num">${s.total_h != null ? fmt1(s.total_h) + " h" : "–"}</td>
      <td class="num">${s.weight != null ? s.weight.toLocaleString("de-DE") : "–"}</td>
      <td>${s.error ? `<span class="err">${esc(s.error)}</span>` : meta}</td></tr>`;
  }).join("");
  $("#fc-sources").innerHTML = `<tr><th>Quelle</th><th class="num">Sonne</th><th class="num">Gewicht</th><th>Hinweis</th></tr>${rows}
    ${r.correction ? `<tr><td>Kombiniert, unkorrigiert</td><td class="num">${fmt1(r.raw_total_hours)} h</td><td></td><td></td></tr>` : ""}
    <tr><td><b>Kombinierte Prognose${r.correction ? " (korrigiert)" : ""}</b></td><td class="num"><b>${fmt1(r.total_hours)} h</b></td><td></td><td></td></tr>`;
  $("#fc-generated").textContent = `Berechnet ${r.generated} · Daten werden 30 Minuten zwischengespeichert.`;
  $("#fc-content").hidden = false;
}

function renderChart(hours) {
  const svg = $("#fc-chart");
  const W = Math.max(520, svg.clientWidth || 800), H = 240;
  const m = { l: 34, r: 34, t: 12, b: 26 };
  const cw = W - m.l - m.r, ch = H - m.t - m.b;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!hours.length) { svg.innerHTML = ""; return; }

  const step = cw / hours.length, bw = Math.max(4, step * 0.7);
  const y = (min) => m.t + ch - (min / 60) * ch;
  let out = "";
  for (const v of [0, 15, 30, 45, 60]) {
    out += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>`;
    out += `<text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  out += `<text x="${W - m.r + 6}" y="${m.t + 4}">100%</text><text x="${W - m.r + 6}" y="${y(0) + 4}">0%</text>`;

  const pts = [];
  hours.forEach((h, i) => {
    const x = m.l + i * step + (step - bw) / 2;
    const tip = `${h.hour} Uhr: ${Math.round(h.expected_min)} von ${h.possible_min} min` +
      (h.probability != null ? ` · Wahrscheinlichkeit ${Math.round(h.probability * 100)} %` : "");
    out += `<g><title>${tip}</title>`;
    out += `<rect class="bar-possible" x="${x}" y="${y(h.possible_min)}" width="${bw}" height="${ch - (y(h.possible_min) - m.t)}" rx="3"/>`;
    out += `<rect class="bar-sun" x="${x}" y="${y(h.expected_min)}" width="${bw}" height="${ch - (y(h.expected_min) - m.t)}" rx="3"/>`;
    out += `</g>`;
    if (step > 24 || i % 2 === 0) out += `<text x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${h.hour.slice(0, 2)}</text>`;
    if (h.probability != null && h.possible_min > 0) pts.push([x + bw / 2, y(h.probability * 60)]);
  });
  if (pts.length > 1) out += `<polyline class="prob-line" points="${pts.map((p) => p.join(",")).join(" ")}"/>`;
  pts.forEach(([px, py]) => (out += `<circle class="prob-dot" cx="${px}" cy="${py}" r="2.5"/>`));
  svg.innerHTML = out;
}

/* ---------------- Einstellungen ---------------- */
function fillSettings(s) {
  const L = s.location;
  $("#loc-name").value = L.name || "";
  $("#loc-lat").value = L.lat ?? "";
  $("#loc-lon").value = L.lon ?? "";
  $("#loc-elev").value = L.elevation ?? "";
  $("#loc-tz").value = L.timezone || "";

  $("#hz-fields").innerHTML = s.horizon.map((p, i) =>
    `<label>${DIRS[i] ?? p.az + "°"} (${p.az}°)<input type="number" step="0.5" min="0" max="89" data-az="${p.az}" value="${p.h}"></label>`).join("");
  $("#hz-fields").querySelectorAll("input").forEach((inp) => inp.addEventListener("input", drawHorizon));
  drawHorizon();

  $("#calc-threshold").value = s.sunny_threshold_min;
  $("#calc-metcontact").value = s.met_contact || "";
  $("#calc-correction").checked = s.correction.enabled;
  const labels = {
    icon: "DWD ICON (Open-Meteo)", ecmwf: "ECMWF IFS (Open-Meteo)", meteofrance: "Météo-France (Open-Meteo)",
    gfs: "NOAA GFS (Open-Meteo)", mosmix: "DWD MOSMIX (Bright Sky)", met: "MET Norway (Bewölkung → Sonne)",
  };
  $("#src-table").innerHTML = `<tr><th>Aktiv</th><th>Quelle</th><th>Gewicht</th></tr>` +
    Object.entries(s.sources).map(([k, v]) => `<tr>
      <td><input type="checkbox" data-src="${k}" ${v.enabled ? "checked" : ""}></td>
      <td>${esc(labels[k] || k)}</td>
      <td><input type="number" step="0.1" min="0" max="3" data-weight="${k}" value="${v.weight}"></td></tr>`).join("");

  const M = s.mail;
  $("#mail-enabled").checked = M.enabled;
  $("#mail-recipients").value = M.recipients;
  $("#mail-time").value = M.send_time;
  $("#mail-target").value = M.target;
  $("#mail-host").value = M.smtp_host;
  $("#mail-port").value = M.smtp_port;
  $("#mail-security").value = M.smtp_security;
  $("#mail-user").value = M.smtp_user;
  $("#mail-pass").value = "";
  $("#mail-pass").placeholder = M.smtp_password_set ? "•••••• (gespeichert)" : "";
  $("#mail-sender").value = M.sender;
  updateMailHint();
}

function collectSettings() {
  const num = (sel) => ($(sel).value === "" ? null : Number($(sel).value));
  const sourcesOut = {};
  document.querySelectorAll("[data-src]").forEach((cb) => {
    sourcesOut[cb.dataset.src] = { enabled: cb.checked, weight: Number($(`[data-weight="${cb.dataset.src}"]`).value || 0) };
  });
  return {
    location: {
      name: $("#loc-name").value.trim(),
      lat: num("#loc-lat"), lon: num("#loc-lon"), elevation: num("#loc-elev"),
      timezone: $("#loc-tz").value.trim() || "Europe/Berlin",
    },
    horizon: readHorizon(),
    sunny_threshold_min: num("#calc-threshold") ?? 30,
    met_contact: $("#calc-metcontact").value.trim(),
    correction: { enabled: $("#calc-correction").checked },
    sources: sourcesOut,
    mail: {
      enabled: $("#mail-enabled").checked,
      recipients: $("#mail-recipients").value.trim(),
      send_time: $("#mail-time").value || "06:30",
      target: $("#mail-target").value,
      smtp_host: $("#mail-host").value.trim(),
      smtp_port: num("#mail-port") ?? 587,
      smtp_security: $("#mail-security").value,
      smtp_user: $("#mail-user").value.trim(),
      smtp_password: $("#mail-pass").value,
      sender: $("#mail-sender").value.trim(),
    },
  };
}

async function save() {
  const btn = $("#save");
  btn.disabled = true;
  try {
    settings = await api("/api/settings", { method: "POST", body: collectSettings() });
    fillSettings(settings);
    $("#save-msg").textContent = "Gespeichert ✓";
  } catch (err) {
    $("#save-msg").textContent = `Fehler: ${err.message}`;
  } finally {
    btn.disabled = false;
    setTimeout(() => ($("#save-msg").textContent = ""), 4000);
  }
}
$("#save").addEventListener("click", save);

/* ---------------- Karte & Standortsuche ---------------- */
function initMap() {
  if (map || typeof L === "undefined") { map?.invalidateSize(); return; }
  const lat = Number($("#loc-lat").value) || 51.16, lon = Number($("#loc-lon").value) || 10.45;
  const hasLoc = $("#loc-lat").value !== "";
  map = L.map("map").setView([lat, lon], hasLoc ? 15 : 6);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "© OpenStreetMap",
  }).addTo(map);
  marker = L.marker([lat, lon], { draggable: true, opacity: hasLoc ? 1 : 0 }).addTo(map);
  marker.on("dragend", () => { const p = marker.getLatLng(); setLocation(p.lat, p.lng); });
  map.on("click", (e) => setLocation(e.latlng.lat, e.latlng.lng));
}

async function setLocation(lat, lon, extra = {}) {
  $("#loc-lat").value = lat.toFixed(5);
  $("#loc-lon").value = lon.toFixed(5);
  if (extra.name) $("#loc-name").value = extra.name;
  if (marker) { marker.setLatLng([lat, lon]).setOpacity(1); }
  try {
    const info = await api(`/api/lookup?lat=${lat}&lon=${lon}`);
    $("#loc-elev").value = info.elevation != null ? Math.round(info.elevation) : "";
    if (info.timezone) $("#loc-tz").value = info.timezone;
  } catch { /* Höhe/Zeitzone bleiben manuell editierbar */ }
  $("#hz-msg").textContent = "Standort geändert – Geländehorizont neu laden?";
}

let searchTimer;
$("#loc-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("#loc-results").hidden = true; return; }
  searchTimer = setTimeout(async () => {
    try {
      const results = await api(`/api/geocode?q=${encodeURIComponent(q)}`);
      $("#loc-results").innerHTML = results.length
        ? results.map((r, i) => `<li data-i="${i}">${esc(r.name)}</li>`).join("")
        : `<li class="muted">Nichts gefunden</li>`;
      $("#loc-results").hidden = false;
      $("#loc-results").onclick = (ev) => {
        const r = results[ev.target.dataset.i];
        if (!r) return;
        $("#loc-results").hidden = true;
        $("#loc-search").value = "";
        map?.setView([r.lat, r.lon], 15);
        setLocation(r.lat, r.lon, { name: r.name.split(",")[0] });
      };
    } catch (err) {
      $("#loc-results").innerHTML = `<li class="err">${esc(err.message)}</li>`;
      $("#loc-results").hidden = false;
    }
  }, 300);
});

$("#loc-gps").addEventListener("click", () => {
  if (!navigator.geolocation) return alert("Dein Browser unterstützt keine Standortabfrage.");
  navigator.geolocation.getCurrentPosition(
    (pos) => { map?.setView([pos.coords.latitude, pos.coords.longitude], 17); setLocation(pos.coords.latitude, pos.coords.longitude); },
    (err) => alert(`Standort nicht verfügbar: ${err.message}\n(Browser erlauben dies meist nur über HTTPS oder localhost.)`),
    { enableHighAccuracy: true, timeout: 15000 },
  );
});
["#loc-lat", "#loc-lon"].forEach((sel) => $(sel).addEventListener("change", () => {
  const lat = Number($("#loc-lat").value), lon = Number($("#loc-lon").value);
  if (Number.isFinite(lat) && Number.isFinite(lon)) { map?.setView([lat, lon]); setLocation(lat, lon); }
}));

/* ---------------- Horizont ---------------- */
function readHorizon() {
  return [...document.querySelectorAll("#hz-fields input")].map((i) => ({ az: Number(i.dataset.az), h: Number(i.value || 0) }));
}

function drawHorizon() {
  const pts = readHorizon();
  const R = 90;                       // Rand = 0°, Mitte = 90° Höhe
  const xy = (az, h) => {
    const r = R * (1 - Math.min(h, 90) / 90), a = (az * Math.PI) / 180;
    return [r * Math.sin(a), -r * Math.cos(a)];
  };
  let out = "";
  for (const h of [0, 30, 60]) out += `<circle class="ring" r="${R * (1 - h / 90)}"/>`;
  const outer = pts.map((p) => xy(p.az, 0).join(",")).join(" ");
  const inner = pts.map((p) => xy(p.az, p.h).join(",")).reverse().join(" ");
  out += `<polygon class="area" points="${outer} ${inner}" fill-rule="evenodd"/>`;
  [["N", 0], ["O", 90], ["S", 180], ["W", 270]].forEach(([t, az]) => {
    const [x, y] = xy(az, -18);
    out += `<text x="${x}" y="${y}">${t}</text>`;
  });
  $("#hz-plot").innerHTML = out;
}

$("#hz-terrain").addEventListener("click", async () => {
  const lat = $("#loc-lat").value, lon = $("#loc-lon").value;
  if (!lat || !lon) { $("#hz-msg").textContent = "Bitte zuerst einen Standort wählen."; return; }
  $("#hz-msg").textContent = "Lade Geländedaten (PVGIS) …";
  try {
    const hz = await api(`/api/horizon?lat=${lat}&lon=${lon}`);
    document.querySelectorAll("#hz-fields input").forEach((inp) => {
      const p = hz.find((x) => x.az === Number(inp.dataset.az));
      if (p) inp.value = p.h;
    });
    drawHorizon();
    $("#hz-msg").textContent = "Geländehorizont übernommen – Speichern nicht vergessen.";
  } catch (err) {
    $("#hz-msg").textContent = `Geländedaten nicht verfügbar: ${err.message}`;
  }
});
$("#hz-reset").addEventListener("click", () => {
  document.querySelectorAll("#hz-fields input").forEach((inp) => (inp.value = 0));
  drawHorizon();
});

/* ---------------- Mail ---------------- */
const isGmail = (host) => /^smtp\.(gmail|googlemail)\.com$/i.test(host.trim());
function updateMailHint() { $("#mail-hint").hidden = !isGmail($("#mail-host").value); }

$("#mail-preset-gmail").addEventListener("click", () => {
  $("#mail-host").value = "smtp.gmail.com";
  $("#mail-port").value = 587;
  $("#mail-security").value = "starttls";
  const user = $("#mail-user").value.trim();
  const first = $("#mail-recipients").value.split(/[,;]/)[0].trim();
  if (!user.includes("@")) $("#mail-user").value = user ? `${user}@gmail.com` : first;
  updateMailHint();
  $("#mail-pass").focus();
});
$("#mail-host").addEventListener("input", updateMailHint);
// Port und Verschlüsselung gehören zusammen: 465 = SSL/TLS, 587 = STARTTLS
$("#mail-port").addEventListener("change", () => {
  const port = Number($("#mail-port").value);
  if (port === 465) $("#mail-security").value = "ssl";
  if (port === 587) $("#mail-security").value = "starttls";
});
$("#mail-security").addEventListener("change", () => {
  const sec = $("#mail-security").value, port = Number($("#mail-port").value);
  if (sec === "ssl" && port === 587) $("#mail-port").value = 465;
  if (sec === "starttls" && port === 465) $("#mail-port").value = 587;
});
$("#mail-test").addEventListener("click", async () => {
  const btn = $("#mail-test");
  btn.disabled = true;
  $("#mail-status").textContent = "Speichere und sende …";
  try {
    settings = await api("/api/settings", { method: "POST", body: collectSettings() });
    fillSettings(settings);
    const r = await api("/api/mail/test", { method: "POST" });
    $("#mail-status").textContent = `Gesendet ✓ (${fmt1(r.total_hours)} h für ${r.date})`;
  } catch (err) {
    $("#mail-status").textContent = `Fehler: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});

async function loadStatus() {
  try {
    const st = await api("/api/status");
    const parts = [];
    if (st.last_ok) parts.push(`Letzte Mail: ${st.last_ok.replace("T", " ")}`);
    if (st.last_error) parts.push(`Letzter Fehler: ${st.last_error}`);
    if (parts.length && !$("#mail-status").textContent) $("#mail-status").textContent = parts.join(" · ");
  } catch { /* Status ist optional */ }
}

/* ---------------- Start ---------------- */
(async function init() {
  settings = await api("/api/settings");
  fillSettings(settings);
  loadStatus();
  if (settings.location.lat == null) showView("settings");
  else loadForecast();
})();
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => lastResult && renderChart(lastResult.hours), 150);
});
