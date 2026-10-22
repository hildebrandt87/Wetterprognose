"""Historisches Archiv: Prognose und gemessene Sonnenstunden je Tag.

Tabellen (in data/wetterprognose.db):
  archive_days     – eine Zeile je Tag mit Tagessummen (bleibt immer unkomprimiert abfragbar)
  archive_details  – Stundendetails je Tag als JSON (nur für laufende/unfertige Monate)
  archive_months   – Stundendetails abgeschlossener Monate, zlib-komprimiert in einem Block
"""
import calendar
import json
import logging
import threading
import zlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import forecast
import pv
import sources
import storage
from sun import solar_position

log = logging.getLogger("wetterprognose.archive")

# Messwerte treffen mit Verzögerung ein: so viele Tage nach Monatsende wird erst komprimiert
PACK_DELAY_DAYS = 3
# So weit rückwirkend werden fehlende Messwerte täglich nachgeholt
ACTUAL_LOOKBACK_DAYS = 10

KIND_LABELS = {"vortag": "Vortagesprognose", "gleicher_tag": "Prognose vom selben Tag",
               "rekonstruiert": "rekonstruiert (Modellarchiv)"}

_job_lock = threading.Lock()
backfill_state = {"running": False, "done": 0, "total": 0, "message": ""}
_month_cache = {}


def _conn():
    c = storage._conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS archive_days (
            date TEXT PRIMARY KEY,
            place TEXT, lat REAL, lon REAL,
            forecast_h REAL, possible_h REAL, daylength_h REAL, confidence REAL,
            sunrise TEXT, sunset TEXT,
            kind TEXT, forecast_at TEXT,
            actual_h REAL, actual_station TEXT,
            packed INTEGER NOT NULL DEFAULT 0,
            raw_h REAL,
            pv_kwh REAL,
            ghi_raw REAL, ghi_actual REAL, ghi_station TEXT,
            pv_actual_kwh REAL, pv_config TEXT,
            rad_f INTEGER NOT NULL DEFAULT 0, rad_m INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS archive_details (date TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS archive_months (
            month TEXT PRIMARY KEY, data BLOB NOT NULL,
            raw_bytes INTEGER, packed_bytes INTEGER, days INTEGER, packed_at TEXT
        );
    """)
    cols = {r[1] for r in c.execute("PRAGMA table_info(archive_days)")}
    if "raw_h" not in cols:
        # Migration: bisherige Prognosen waren unkorrigiert
        c.execute("ALTER TABLE archive_days ADD COLUMN raw_h REAL")
        c.execute("UPDATE archive_days SET raw_h = forecast_h")
        c.commit()
    # Spätere Spalten nachrüsten (bestehende Datenbanken)
    for col, typ in (("pv_kwh", "REAL"), ("ghi_raw", "REAL"), ("ghi_actual", "REAL"), ("ghi_station", "TEXT"),
                     ("pv_actual_kwh", "REAL"), ("pv_config", "TEXT"),
                     ("rad_f", "INTEGER NOT NULL DEFAULT 0"), ("rad_m", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols and "raw_h" in cols:
            c.execute(f"ALTER TABLE archive_days ADD COLUMN {col} {typ}")
            c.commit()
    c.row_factory = _dict_row
    return c


def _dict_row(cursor, row):
    return {d[0]: v for d, v in zip(cursor.description, row)}


# ------------------------------------------------------------------ Schreiben

def _details(day_iso):
    with _conn() as c:
        row = c.execute("SELECT data FROM archive_details WHERE date = ?", (day_iso,)).fetchone()
    return json.loads(row["data"]) if row else {}


def store_forecast(result, kind, overwrite=False):
    """Speichert eine Tagesprognose. Eine vorhandene Vortagesprognose wird nicht überschrieben."""
    day = result["date"]
    loc = result["location"]
    with _conn() as c:
        existing = c.execute("SELECT kind, packed FROM archive_days WHERE date = ?", (day,)).fetchone()
        if existing and (existing["packed"] or (existing["kind"] and not overwrite)):
            return False
        c.execute("""
            INSERT INTO archive_days (date, place, lat, lon, forecast_h, possible_h, daylength_h, confidence,
                                      sunrise, sunset, kind, forecast_at, raw_h, pv_kwh, ghi_raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
              place=excluded.place, lat=excluded.lat, lon=excluded.lon, forecast_h=excluded.forecast_h,
              possible_h=excluded.possible_h, daylength_h=excluded.daylength_h, confidence=excluded.confidence,
              sunrise=excluded.sunrise, sunset=excluded.sunset, kind=excluded.kind, forecast_at=excluded.forecast_at,
              raw_h=excluded.raw_h, pv_kwh=excluded.pv_kwh, ghi_raw=excluded.ghi_raw
        """, (day, loc.get("name"), loc["lat"], loc["lon"], result["total_hours"], result["possible_hours"],
              result["daylength_hours"], result["confidence"], result["sunrise"], result["sunset"],
              kind, result["generated"], result.get("raw_total_hours", result["total_hours"]),
              (result.get("pv") or {}).get("kwh"), (result.get("radiation") or {}).get("ghi_raw")))
        details = {}
        row = c.execute("SELECT data FROM archive_details WHERE date = ?", (day,)).fetchone()
        if row:
            details = json.loads(row["data"])
        details["forecast"] = {
            "hours": [{k: h[k] for k in ("hour", "possible_min", "expected_min", "probability")}
                      for h in result["hours"]],
            "windows": result["windows"],
            "sources": [{"label": s["label"], "total_h": s.get("total_h")} for s in result["sources"]
                        if s.get("total_h") is not None],
            "confidence_label": result["confidence_label"],
            "correction": result.get("correction"),
        }
        rad = result.get("radiation") or {}
        if rad.get("hourly_raw"):
            corr = rad.get("correction")
            details.setdefault("rad", {}).update(f=rad["hourly_raw"], cf=corr["factor"] if corr else 1.0)
            c.execute("UPDATE archive_days SET rad_f = 1, pv_config = NULL WHERE date = ?", (day,))
        c.execute("INSERT OR REPLACE INTO archive_details (date, data) VALUES (?, ?)",
                  (day, json.dumps(details, separators=(",", ":"))))
    return True


def _update_details(c, iso, fn):
    """Ändert die Details eines Tages; ein komprimierter Monat wird dafür entpackt (später neu gepackt)."""
    row = c.execute("SELECT packed FROM archive_days WHERE date = ?", (iso,)).fetchone()
    if row and row["packed"]:
        _unpack_month(c, iso[:7])
    det = c.execute("SELECT data FROM archive_details WHERE date = ?", (iso,)).fetchone()
    details = json.loads(det["data"]) if det else {}
    fn(details)
    c.execute("INSERT OR REPLACE INTO archive_details (date, data) VALUES (?, ?)",
              (iso, json.dumps(details, separators=(",", ":"))))


def store_radiation_forecast(c, irr, days, loc):
    """Stündliche Strahlungsprognose (unkorrigiert, Modellarchiv) und Tagessumme für vergangene Tage."""
    for d in days:
        iso = d.isoformat()
        hourly = {k[11:13]: [round(v["ghi"]), round(v["dhi"]), round(v["temp"], 1)]
                  for k, v in irr.items() if k.startswith(iso)}
        if not hourly:
            continue
        c.execute("""
            INSERT INTO archive_days (date, place, lat, lon, ghi_raw, rad_f) VALUES (?,?,?,?,?,1)
            ON CONFLICT(date) DO UPDATE SET ghi_raw=COALESCE(archive_days.ghi_raw, excluded.ghi_raw),
                                            rad_f=1, pv_config=NULL
        """, (iso, loc.get("name"), loc["lat"], loc["lon"], pv.daily_ghi(irr, d)))
        _update_details(c, iso, lambda det: det.setdefault("rad", {}).setdefault("f", hourly))


def store_actuals(observed, days, loc):
    """Trägt gemessene Sonnenminuten und Globalstrahlung ein – nur fehlende Werte, nur vollständige Tage."""
    stored = 0
    with _conn() as c:
        for d in days:
            iso = d.isoformat()
            row = c.execute("SELECT packed, actual_h, ghi_actual, rad_m FROM archive_days WHERE date = ?",
                            (iso,)).fetchone()
            daylight = _daylight_hours(d, loc)
            # Nachts melden Stationen oft keinen Wert – vollständig heißt: jede Stunde mit Sonne ist gemessen
            ghi = {k[11:16]: v for k, v in observed.get("ghi", {}).items() if k.startswith(iso)}
            if ghi and daylight <= ghi.keys() and (not row or not row["rad_m"]):
                hourly = {h[:2]: round(v) for h, v in sorted(ghi.items()) if v}
                c.execute("INSERT INTO archive_days (date, place, lat, lon) VALUES (?,?,?,?) "
                          "ON CONFLICT(date) DO NOTHING", (iso, loc.get("name"), loc["lat"], loc["lon"]))
                _update_details(c, iso, lambda det: det.setdefault("rad", {}).update(m=hourly))
                c.execute("UPDATE archive_days SET rad_m = 1, pv_config = NULL WHERE date = ?", (iso,))
                row = c.execute("SELECT packed, actual_h, ghi_actual, rad_m FROM archive_days WHERE date = ?",
                                (iso,)).fetchone()
            if ghi and daylight <= ghi.keys() and (not row or row["ghi_actual"] is None):
                c.execute("""
                    INSERT INTO archive_days (date, place, lat, lon, ghi_actual, ghi_station) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(date) DO UPDATE SET ghi_actual=excluded.ghi_actual, ghi_station=excluded.ghi_station
                """, (iso, loc.get("name"), loc["lat"], loc["lon"], round(sum(ghi.values()) / 1000, 2),
                      observed.get("ghi_station")))
            hours = {k[11:16]: v for k, v in observed["values"].items() if k.startswith(iso)}
            if not hours or not daylight <= hours.keys() or (row and row["actual_h"] is not None):
                continue
            total_h = round(sum(hours.values()) / 60, 1)
            if row and row["packed"]:
                _unpack_month(c, iso[:7])
            c.execute("""
                INSERT INTO archive_days (date, place, lat, lon, actual_h, actual_station) VALUES (?,?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET actual_h=excluded.actual_h, actual_station=excluded.actual_station
            """, (iso, loc.get("name"), loc["lat"], loc["lon"], total_h,
                  f"{observed['station']} ({observed['distance_km']} km)"))
            det = c.execute("SELECT data FROM archive_details WHERE date = ?", (iso,)).fetchone()
            details = json.loads(det["data"]) if det else {}
            details["actual"] = {h: round(m) for h, m in sorted(hours.items()) if m}
            c.execute("INSERT OR REPLACE INTO archive_details (date, data) VALUES (?, ?)",
                      (iso, json.dumps(details, separators=(",", ":"))))
            stored += 1
    return stored


def _daylight_hours(day, loc):
    """Lokale Stunden ('HH:00'), in denen die Sonne zeitweise über dem flachen Horizont steht."""
    tz = ZoneInfo(loc["timezone"])
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(ZoneInfo("UTC"))
    hours = set()
    for q in range(0, 24 * 60, 15):
        t = start + timedelta(minutes=q)
        local = t.astimezone(tz)
        if local.date() == day and solar_position(t, loc["lat"], loc["lon"])[0] > 0:
            hours.add(local.strftime("%H:00"))
    return hours


def _unpack_month(c, month):
    """Holt einen komprimierten Monat zurück in archive_details (wird beim nächsten pack_months neu gepackt)."""
    row = c.execute("SELECT data FROM archive_months WHERE month = ?", (month,)).fetchone()
    if row:
        for day, details in json.loads(zlib.decompress(row["data"])).items():
            c.execute("INSERT OR REPLACE INTO archive_details (date, data) VALUES (?, ?)",
                      (day, json.dumps(details, separators=(",", ":"))))
        c.execute("DELETE FROM archive_months WHERE month = ?", (month,))
    c.execute("UPDATE archive_days SET packed = 0 WHERE date LIKE ?", (f"{month}-%",))
    _month_cache.pop(month, None)


def fill_actuals(settings, today):
    loc = settings["location"]
    start = today - timedelta(days=ACTUAL_LOOKBACK_DAYS)
    with _conn() as c:
        missing = [date.fromisoformat(r["date"]) for r in c.execute(
            "SELECT date FROM archive_days WHERE (actual_h IS NULL OR ghi_actual IS NULL OR rad_m = 0) "
            "AND date >= ? AND date < ?",
            (start.isoformat(), today.isoformat()))]
    if not missing:
        return 0
    observed = sources.fetch_observed(min(missing), max(missing), loc)
    return store_actuals(observed, missing, loc)


# ------------------------------------------------------------------ Solarertrag

pv_state = {"running": False, "message": ""}
_settings_for_queries = {}


def _irr_from_details(day, rad):
    """Stündliche Strahlung aus den Archivdetails: Prognose (mit damaliger Korrektur) und Messung.

    Die Messung liefert nur Globalstrahlung; der Diffusanteil kommt aus der Prognose derselben Stunde.
    """
    iso = day.isoformat()
    f, m, cf = rad.get("f") or {}, rad.get("m") or {}, rad.get("cf", 1.0)
    fc = {f"{iso}T{h}:00": {"ghi": v[0] * cf, "dhi": v[1] * cf, "temp": v[2]} for h, v in f.items()}
    meas = {}
    for h, g in m.items():
        fv = f.get(h)
        share = min(1.0, fv[1] / fv[0]) if fv and fv[0] > 0 else 1.0
        meas[f"{iso}T{h}:00"] = {"ghi": g, "dhi": g * share, "temp": fv[2] if fv else 15.0}
    return fc if f else None, meas if m else None


def recalc_pv(settings):
    """Berechnet den Solarertrag aller Archivtage neu, deren Anlagen-/Standortkennung veraltet ist."""
    if not settings["pv"]["enabled"] or not pv.active_arrays(settings["pv"]):
        return 0
    key = pv.config_key(settings)
    with _conn() as c:
        days = [r["date"] for r in c.execute(
            "SELECT date FROM archive_days WHERE (rad_f = 1 OR rad_m = 1) "
            "AND (pv_config IS NULL OR pv_config != ?) ORDER BY date", (key,))]
    pv_state.update(running=True, message=f"Berechne Solarertrag für {len(days)} Tage …")
    try:
        updates = []
        for iso in days:
            d = date.fromisoformat(iso)
            fc, meas = _irr_from_details(d, day_details(iso).get("rad", {}))
            pv_fc = pv.day_yield(settings, d, fc)[0] if fc else None
            pv_act = pv.day_yield(settings, d, meas)[0] if meas else None
            updates.append((pv_fc, pv_act, key, iso))
        with _conn() as c:
            c.executemany("UPDATE archive_days SET pv_kwh = COALESCE(?, pv_kwh), pv_actual_kwh = ?, pv_config = ? "
                          "WHERE date = ?", updates)
        pv_state["message"] = f"Solarertrag für {len(days)} Tage berechnet."
        return len(days)
    finally:
        pv_state["running"] = False


def start_recalc_pv(settings):
    if not pv_state["running"]:
        threading.Thread(target=recalc_pv, args=(settings,), daemon=True).start()


def _pv_hours(day):
    settings = storage.load_settings()
    if not settings["pv"]["enabled"] or not pv.active_arrays(settings["pv"]):
        return None
    fc, meas = _irr_from_details(day, day_details(day.isoformat()).get("rad", {}))
    return {"forecast": pv.day_yield(settings, day, fc)[1] if fc else {},
            "measured": pv.day_yield(settings, day, meas)[1] if meas else {}}


# ------------------------------------------------------------------ Komprimierung

def pack_months(today):
    """Packt alle abgeschlossenen Monate: Details -> ein zlib-Block, Tageszeilen bleiben erhalten."""
    cutoff = (today - timedelta(days=PACK_DELAY_DAYS)).replace(day=1)
    packed = []
    with _conn() as c:
        months = [r["m"] for r in c.execute(
            "SELECT DISTINCT substr(date, 1, 7) AS m FROM archive_details WHERE date < ? ORDER BY m",
            (cutoff.isoformat(),))]
        for month in months:
            rows = c.execute("SELECT date, data FROM archive_details WHERE date LIKE ? ORDER BY date",
                             (f"{month}-%",)).fetchall()
            existing = c.execute("SELECT data FROM archive_months WHERE month = ?", (month,)).fetchone()
            bundle = json.loads(zlib.decompress(existing["data"])) if existing else {}
            bundle.update({r["date"]: json.loads(r["data"]) for r in rows})
            raw = json.dumps(bundle, separators=(",", ":")).encode()
            blob = zlib.compress(raw, 9)
            c.execute("INSERT OR REPLACE INTO archive_months VALUES (?,?,?,?,?,?)",
                      (month, blob, len(raw), len(blob), len(bundle), datetime.now().isoformat(timespec="minutes")))
            c.execute("DELETE FROM archive_details WHERE date LIKE ?", (f"{month}-%",))
            c.execute("UPDATE archive_days SET packed = 1 WHERE date LIKE ?", (f"{month}-%",))
            _month_cache.pop(month, None)
            packed.append(month)
    if packed:
        with storage._conn() as c:
            c.execute("VACUUM")
        log.info("Archiv komprimiert: %s", ", ".join(packed))
    return packed


def _month_bundle(month):
    if month not in _month_cache:
        with _conn() as c:
            row = c.execute("SELECT data FROM archive_months WHERE month = ?", (month,)).fetchone()
        _month_cache.clear()  # nur einen entpackten Monat im Speicher halten
        _month_cache[month] = json.loads(zlib.decompress(row["data"])) if row else {}
    return _month_cache[month]


def day_details(day_iso):
    d = _details(day_iso)
    return d if d else _month_bundle(day_iso[:7]).get(day_iso, {})


# ------------------------------------------------------------------ Tagesjob & Nachladen

def daily_job(settings, mail_result=None):
    """Prognose für morgen sichern, fehlende Messwerte holen, abgeschlossene Monate packen."""
    if settings["location"].get("lat") is None:
        return
    with _job_lock:
        tz = ZoneInfo(settings["location"]["timezone"])
        today = datetime.now(tz).date()
        tomorrow = (today + timedelta(days=1)).isoformat()
        result = mail_result if mail_result and mail_result["date"] == tomorrow else forecast.compute(settings, "tomorrow")
        store_forecast(result, "vortag")
        with _conn() as c:
            has_today = c.execute("SELECT 1 FROM archive_days WHERE date = ? AND kind IS NOT NULL",
                                  (today.isoformat(),)).fetchone()
        if not has_today:
            store_forecast(forecast.compute(settings, "today"), "gleicher_tag")
        try:
            fill_actuals(settings, today)
        except Exception as e:  # noqa: BLE001
            log.warning("Messwerte nicht verfügbar: %s", e)
        pack_months(today)
        recalc_pv(settings)
        storage.put("archive_state", {"last_run": today.isoformat()})


def _month_chunks(start, end):
    cur = start
    while cur <= end:
        last = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        yield cur, min(last, end)
        cur = last + timedelta(days=1)


def backfill(settings, start, end):
    """Füllt vergangene Tage: rekonstruierte Prognose (Open-Meteo-Modellarchiv) + Messwerte (DWD)."""
    loc = settings["location"]
    om_keys = [k for k, v in sources.SOURCES.items()
               if "model" in v and settings["sources"].get(k, {}).get("enabled", True)]
    backfill_state.update(running=True, done=0, total=(end - start).days + 1, message="Starte …")
    failed = []
    try:
        for chunk_start, chunk_end in _month_chunks(start, end):
            backfill_state["message"] = f"Lade {chunk_start:%m/%Y} …"
            days = [chunk_start + timedelta(days=i) for i in range((chunk_end - chunk_start).days + 1)]
            with _conn() as c:
                rows = c.execute("SELECT date, kind, actual_h, ghi_raw, ghi_actual, rad_f, rad_m FROM archive_days "
                                 "WHERE date >= ? AND date <= ?",
                                 (chunk_start.isoformat(), chunk_end.isoformat())).fetchall()
            have = sum(r["kind"] is not None for r in rows)
            have_ghi = {r["date"] for r in rows if r["ghi_raw"] is not None and r["rad_f"]}
            measured = {r["date"] for r in rows
                        if r["actual_h"] is not None and r["ghi_actual"] is not None and r["rad_m"]}
            need_actual = [d for d in days if d.isoformat() not in measured]
            raw = None
            if have < len(days):  # Modellarchiv nur abfragen, wenn Prognosen fehlen
                try:
                    raw = sources.fetch_open_meteo(om_keys, chunk_start, chunk_end, loc, historical=True)
                except Exception as e:  # noqa: BLE001
                    log.warning("Modellarchiv %s nicht verfügbar: %s", chunk_start, e)
                    failed.append(f"{chunk_start:%m/%Y}")
            need_ghi = [d for d in days if d.isoformat() not in have_ghi]
            if need_ghi:  # Strahlungsprognose aus dem Modellarchiv für die Strahlungskorrektur
                try:
                    rad = sources.fetch_radiation(min(need_ghi), max(need_ghi), loc, settings["sources"], historical=True)
                    weights = pv.source_weights(settings, rad)
                    irr = pv._irradiance({k: rad[k] for k in weights}, weights)
                    with _job_lock, _conn() as c:
                        store_radiation_forecast(c, irr, need_ghi, loc)
                except Exception as e:  # noqa: BLE001
                    log.warning("Strahlungsarchiv %s nicht verfügbar: %s", chunk_start, e)
                    failed.append(f"{chunk_start:%m/%Y} (Strahlung)")
            observed = {"values": {}}
            if need_actual:
                try:
                    observed = sources.fetch_observed(min(need_actual), max(need_actual), loc)
                except Exception as e:  # noqa: BLE001
                    log.warning("Messwerte %s nicht verfügbar: %s", chunk_start, e)
            for d in days:
                if raw:
                    with _job_lock:
                        store_forecast(forecast.evaluate(settings, d, raw, correct=False), "rekonstruiert")
                backfill_state["done"] += 1
            if observed["values"] or observed.get("ghi"):
                with _job_lock:
                    store_actuals(observed, need_actual, loc)
        with _job_lock:
            tz = ZoneInfo(loc["timezone"])
            packed = pack_months(datetime.now(tz).date())
        backfill_state["message"] = "Berechne Solarertrag …"
        recalc_pv(settings)
        backfill_state["message"] = (f"Fertig: {backfill_state['total']} Tage verarbeitet"
                                     + (f", {len(packed)} Monate komprimiert" if packed else "")
                                     + (f". Fehlgeschlagen: {', '.join(failed)} – bitte erneut nachladen."
                                        if failed else "."))
    except Exception as e:  # noqa: BLE001
        log.exception("Nachladen fehlgeschlagen")
        backfill_state["message"] = f"Fehler: {e}"
    finally:
        backfill_state["running"] = False


def start_backfill(settings, start, end):
    if backfill_state["running"]:
        raise ValueError("Es läuft bereits ein Nachladevorgang.")
    if settings["location"].get("lat") is None:
        raise ValueError("Kein Standort gesetzt.")
    today = datetime.now(ZoneInfo(settings["location"]["timezone"])).date()
    end = min(end, today - timedelta(days=1))
    if start > end:
        raise ValueError("Zeitraum muss in der Vergangenheit liegen.")
    if start < date(2022, 1, 1):
        raise ValueError("Das Modellarchiv reicht nur bis 2022 zurück.")
    threading.Thread(target=backfill, args=(settings, start, end), daemon=True).start()


# ------------------------------------------------------------------ Abfragen

def _days_between(start, end):
    with _conn() as c:
        return c.execute("SELECT * FROM archive_days WHERE date >= ? AND date <= ? ORDER BY date",
                         (start.isoformat(), end.isoformat())).fetchall()


def _aggregate(rows):
    def total(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals), 1) if vals else None
    pairs = [(r["forecast_h"], r["actual_h"]) for r in rows
             if r["forecast_h"] is not None and r["actual_h"] is not None]
    return {
        "days": len(rows),
        "forecast_h": total("forecast_h"),
        "actual_h": total("actual_h"),
        "possible_h": total("possible_h"),
        "pv_kwh": total("pv_kwh"),
        "ghi_actual": total("ghi_actual"),
        "pv_actual_kwh": total("pv_actual_kwh"),
        "actual_days": sum(r["actual_h"] is not None for r in rows),
        "forecast_days": sum(r["forecast_h"] is not None for r in rows),
        "mae_h": round(sum(abs(f - a) for f, a in pairs) / len(pairs), 2) if pairs else None,
        "bias_h": round(sum(f - a for f, a in pairs) / len(pairs), 2) if pairs else None,
    }


def _day_row(r):
    d = date.fromisoformat(r["date"])
    return {"key": r["date"], "label": f"{['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][d.weekday()]} {d:%d.%m.}",
            "forecast_h": r["forecast_h"], "raw_h": r["raw_h"], "actual_h": r["actual_h"],
            "possible_h": r["possible_h"], "pv_kwh": r["pv_kwh"], "pv_actual_kwh": r["pv_actual_kwh"],
            "ghi_actual": r["ghi_actual"],
            "kind": r["kind"], "packed": bool(r["packed"])}


MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


def query(mode, year=None, month=None, week=None, day=None):
    if mode == "year":
        rows = _days_between(date(year, 1, 1), date(year, 12, 31))
        out = []
        for m in range(1, 13):
            sub = [r for r in rows if int(r["date"][5:7]) == m]
            out.append({"key": f"{year}-{m:02d}", "label": MONTHS[m - 1][:3], **_aggregate(sub)})
        return {"title": str(year), "drill": "month", "rows": out, "summary": _aggregate(rows),
                "best": _best(rows)}
    if mode == "month":
        last = calendar.monthrange(year, month)[1]
        rows = _days_between(date(year, month, 1), date(year, month, last))
        return {"title": f"{MONTHS[month - 1]} {year}", "drill": "day",
                "rows": [_day_row(r) for r in rows], "summary": _aggregate(rows), "best": _best(rows)}
    if mode == "week":
        start = date.fromisocalendar(year, week, 1)
        rows = _days_between(start, start + timedelta(days=6))
        return {"title": f"KW {week}/{year} ({start:%d.%m.} – {start + timedelta(days=6):%d.%m.})",
                "drill": "day", "rows": [_day_row(r) for r in rows], "summary": _aggregate(rows),
                "best": _best(rows)}
    if mode == "day":
        rows = _days_between(day, day)
        r = rows[0] if rows else None
        return {"title": f"{['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'][day.weekday()]}, "
                         f"{day:%d.%m.%Y}",
                "day": r, "kind_label": KIND_LABELS.get(r["kind"]) if r else None,
                "details": day_details(day.isoformat()) if r else {},
                "pv_hours": _pv_hours(day) if r else None,
                "summary": _aggregate(rows)}
    raise ValueError("Unbekannte Ansicht")


def _best(rows):
    vals = [r for r in rows if (r["actual_h"] if r["actual_h"] is not None else r["forecast_h"]) is not None]
    if not vals:
        return None
    best = max(vals, key=lambda r: r["actual_h"] if r["actual_h"] is not None else r["forecast_h"])
    return {"date": best["date"],
            "hours": best["actual_h"] if best["actual_h"] is not None else best["forecast_h"],
            "measured": best["actual_h"] is not None}


def meta():
    with _conn() as c:
        span = c.execute("SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS days, "
                         "SUM(packed) AS packed_days FROM archive_days").fetchone()
        years = [int(r["y"]) for r in c.execute(
            "SELECT DISTINCT substr(date, 1, 4) AS y FROM archive_days ORDER BY y")]
        packs = c.execute("SELECT COUNT(*) AS months, SUM(raw_bytes) AS raw, SUM(packed_bytes) AS packed "
                          "FROM archive_months").fetchone()
        open_bytes = c.execute("SELECT COALESCE(SUM(LENGTH(data)), 0) AS b FROM archive_details").fetchone()["b"]
    size = storage.DB_PATH.stat().st_size if storage.DB_PATH.exists() else 0
    return {**span, "packed_days": span["packed_days"] or 0, "years": years,
            "packed_months": packs["months"], "raw_bytes": packs["raw"] or 0, "packed_bytes": packs["packed"] or 0,
            "open_detail_bytes": open_bytes, "db_bytes": size,
            "backfill": backfill_state, "pv_recalc": pv_state, "last_run": storage.get("archive_state", {}).get("last_run")}
