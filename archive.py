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
            raw_h REAL
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
                                      sunrise, sunset, kind, forecast_at, raw_h)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
              place=excluded.place, lat=excluded.lat, lon=excluded.lon, forecast_h=excluded.forecast_h,
              possible_h=excluded.possible_h, daylength_h=excluded.daylength_h, confidence=excluded.confidence,
              sunrise=excluded.sunrise, sunset=excluded.sunset, kind=excluded.kind, forecast_at=excluded.forecast_at,
              raw_h=excluded.raw_h
        """, (day, loc.get("name"), loc["lat"], loc["lon"], result["total_hours"], result["possible_hours"],
              result["daylength_hours"], result["confidence"], result["sunrise"], result["sunset"],
              kind, result["generated"], result.get("raw_total_hours", result["total_hours"])))
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
        c.execute("INSERT OR REPLACE INTO archive_details (date, data) VALUES (?, ?)",
                  (day, json.dumps(details, separators=(",", ":"))))
    return True


def store_actuals(observed, days, loc):
    """Trägt gemessene Sonnenminuten für die angegebenen Tage ein (sofern vollständig genug)."""
    stored = 0
    with _conn() as c:
        for d in days:
            iso = d.isoformat()
            hours = {k[11:16]: v for k, v in observed["values"].items() if k.startswith(iso)}
            # Nachts melden Stationen oft keinen Wert – vollständig heißt: jede Stunde mit Sonne ist gemessen
            if not hours or not _daylight_hours(d, loc) <= hours.keys():
                continue
            total_h = round(sum(hours.values()) / 60, 1)
            row = c.execute("SELECT packed FROM archive_days WHERE date = ?", (iso,)).fetchone()
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
            "SELECT date FROM archive_days WHERE actual_h IS NULL AND packed = 0 AND date >= ? AND date < ?",
            (start.isoformat(), today.isoformat()))]
    if not missing:
        return 0
    observed = sources.fetch_observed(min(missing), max(missing), loc)
    return store_actuals(observed, missing, loc)


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
                rows = c.execute("SELECT date, kind, actual_h FROM archive_days WHERE date >= ? AND date <= ?",
                                 (chunk_start.isoformat(), chunk_end.isoformat())).fetchall()
            have = sum(r["kind"] is not None for r in rows)
            measured = {r["date"] for r in rows if r["actual_h"] is not None}
            need_actual = [d for d in days if d.isoformat() not in measured]
            raw = None
            if have < len(days):  # Modellarchiv nur abfragen, wenn Prognosen fehlen
                try:
                    raw = sources.fetch_open_meteo(om_keys, chunk_start, chunk_end, loc, historical=True)
                except Exception as e:  # noqa: BLE001
                    log.warning("Modellarchiv %s nicht verfügbar: %s", chunk_start, e)
                    failed.append(f"{chunk_start:%m/%Y}")
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
            if observed["values"]:
                with _job_lock:
                    store_actuals(observed, need_actual, loc)
        with _job_lock:
            tz = ZoneInfo(loc["timezone"])
            packed = pack_months(datetime.now(tz).date())
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
        "actual_days": sum(r["actual_h"] is not None for r in rows),
        "forecast_days": sum(r["forecast_h"] is not None for r in rows),
        "mae_h": round(sum(abs(f - a) for f, a in pairs) / len(pairs), 2) if pairs else None,
        "bias_h": round(sum(f - a for f, a in pairs) / len(pairs), 2) if pairs else None,
    }


def _day_row(r):
    d = date.fromisoformat(r["date"])
    return {"key": r["date"], "label": f"{['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][d.weekday()]} {d:%d.%m.}",
            "forecast_h": r["forecast_h"], "raw_h": r["raw_h"], "actual_h": r["actual_h"],
            "possible_h": r["possible_h"],
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
            "backfill": backfill_state, "last_run": storage.get("archive_state", {}).get("last_run")}
