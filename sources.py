"""Datenquellen. Jede Quelle liefert Werte je lokalem Stundenintervall [H, H+1h).

kind = "sunshine": Sonnenminuten im Intervall (Modell rechnet mit flachem Horizont)
kind = "cloud":    Gesamtbewölkung in % zur Intervallmitte
"""
import json
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

USER_AGENT = "Wetterprognose-Selfhosted/1.0"
TIMEOUT = 45
RETRIES = 3

SOURCES = {
    "icon":        {"label": "DWD ICON (Open-Meteo)",       "kind": "sunshine", "weight": 1.0, "model": "icon_seamless"},
    "ecmwf":       {"label": "ECMWF IFS (Open-Meteo)",      "kind": "sunshine", "weight": 0.9, "model": "ecmwf_ifs025"},
    "meteofrance": {"label": "Météo-France (Open-Meteo)",   "kind": "sunshine", "weight": 0.6, "model": "meteofrance_seamless"},
    "gfs":         {"label": "NOAA GFS (Open-Meteo)",       "kind": "sunshine", "weight": 0.5, "model": "gfs_seamless"},
    "mosmix":      {"label": "DWD MOSMIX (Bright Sky)",     "kind": "sunshine", "weight": 1.0},
    "met":         {"label": "MET Norway (Bewölkung)",      "kind": "cloud",    "weight": 0.5},
}

# MOSMIX ist eine Stationsprognose: Gewicht fällt mit der Entfernung zur Station linear auf 0.
MOSMIX_MAX_DISTANCE_M = 40000

_cache = {}
CACHE_SECONDS = 1800


def get_json(url, params=None, headers=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    hit = _cache.get(url)
    if hit and _time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429 or attempt == RETRIES - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            if attempt == RETRIES - 1:
                raise
        _time.sleep(2 ** attempt * 5)
    _cache[url] = (_time.time(), data)
    return data


def _hour_key(dt):
    return dt.strftime("%Y-%m-%dT%H:00")


def fetch_open_meteo(keys, start: date, end: date, loc, historical=False):
    """Ein Request für alle aktivierten Open-Meteo-Modelle (historical: archivierte Modellläufe)."""
    models = [SOURCES[k]["model"] for k in keys]
    host = "historical-forecast-api.open-meteo.com" if historical else "api.open-meteo.com"
    data = get_json(f"https://{host}/v1/forecast", {
        "latitude": loc["lat"], "longitude": loc["lon"],
        "hourly": "sunshine_duration",
        "models": ",".join(models),
        "timezone": loc["timezone"],
        "start_date": start.isoformat(),
        "end_date": (end + timedelta(days=1)).isoformat(),
    })
    hourly = data["hourly"]
    results = {}
    for key, model in zip(keys, models):
        series = hourly.get(f"sunshine_duration_{model}")
        if series is None and len(models) == 1:
            series = hourly.get("sunshine_duration")
        values = {}
        for ts, sec in zip(hourly["time"], series or []):
            if sec is None:
                continue
            # Open-Meteo: Summe der vorangegangenen Stunde -> Intervallbeginn = ts - 1h
            start = datetime.fromisoformat(ts) - timedelta(hours=1)
            values[_hour_key(start)] = sec / 60
        results[key] = {"values": values, "meta": {}}
    return results


def fetch_mosmix(day: date, loc):
    tz = ZoneInfo(loc["timezone"])
    data = get_json("https://api.brightsky.dev/weather", {
        "lat": loc["lat"], "lon": loc["lon"],
        "date": f"{day.isoformat()}T01:00",
        "last_date": f"{(day + timedelta(days=1)).isoformat()}T01:00",
        "tz": loc["timezone"],
    })
    stations = {s["id"]: s for s in data.get("sources", [])}
    values, used = {}, set()
    for rec in data.get("weather", []):
        if rec.get("sunshine") is None:
            continue
        # Bright Sky: Sonnenminuten der vorangegangenen Stunde
        start = datetime.fromisoformat(rec["timestamp"]).astimezone(tz) - timedelta(hours=1)
        values[_hour_key(start)] = float(rec["sunshine"])
        used.add(rec["source_id"])
    station = next((stations[i] for i in used if i in stations), None)
    distance = station["distance"] if station else None
    factor = 0.0 if distance is None else max(0.0, 1 - distance / MOSMIX_MAX_DISTANCE_M)
    return {"values": values, "weight_factor": factor, "meta": {
        "station": station["station_name"] if station else None,
        "distance_km": round(distance / 1000, 1) if distance is not None else None,
    }}


def fetch_observed(start: date, end: date, loc, max_distance_m=50000):
    """Gemessene Sonnenminuten je lokaler Stunde von der nächsten DWD-Station mit Sonnenscheinsensor."""
    tz = ZoneInfo(loc["timezone"])
    data = get_json("https://api.brightsky.dev/weather", {
        "lat": loc["lat"], "lon": loc["lon"],
        "date": f"{start.isoformat()}T01:00",
        "last_date": f"{(end + timedelta(days=1)).isoformat()}T01:00",
        "tz": loc["timezone"],
    })
    stations = {s["id"]: s for s in data.get("sources", [])}
    values, station = {}, None
    for rec in data.get("weather", []):
        if rec.get("sunshine") is None:
            continue
        sid = (rec.get("fallback_source_ids") or {}).get("sunshine", rec["source_id"])
        st = stations.get(sid)
        if not st or st["observation_type"] == "forecast" or st["distance"] > max_distance_m:
            continue
        station = station or st
        # Messwert gilt für die vorangegangene Stunde
        begin = datetime.fromisoformat(rec["timestamp"]).astimezone(tz) - timedelta(hours=1)
        values[_hour_key(begin)] = float(rec["sunshine"])
    return {"values": values, "station": station["station_name"] if station else None,
            "distance_km": round(station["distance"] / 1000, 1) if station else None}


def fetch_met(day: date, loc, contact=""):
    tz = ZoneInfo(loc["timezone"])
    ua = f"{USER_AGENT} {contact}".strip()
    data = get_json("https://api.met.no/weatherapi/locationforecast/2.0/compact", {
        "lat": round(loc["lat"], 4), "lon": round(loc["lon"], 4),
    }, headers={"User-Agent": ua})
    points = []
    for entry in data["properties"]["timeseries"]:
        cc = entry["data"]["instant"]["details"].get("cloud_area_fraction")
        if cc is not None:
            points.append((datetime.fromisoformat(entry["time"].replace("Z", "+00:00")), cc))
    points.sort()

    def cloud_at(t):
        for (t0, c0), (t1, c1) in zip(points, points[1:]):
            if t0 <= t <= t1:
                return c0 + (c1 - c0) * (t - t0) / (t1 - t0)
        return None

    values = {}
    start = datetime.combine(day, datetime.min.time(), tz)
    for h in range(25):
        local = (start.astimezone(timezone.utc) + timedelta(hours=h)).astimezone(tz)
        if local.date() != day:
            continue
        cc = cloud_at((local + timedelta(minutes=30)).astimezone(timezone.utc))
        if cc is not None:
            values[_hour_key(local)] = cc
    return {"values": values, "meta": {}}


def fetch_all(day: date, loc, source_settings, met_contact=""):
    """Holt alle aktivierten Quellen. Fehler einer Quelle stoppen die anderen nicht."""
    enabled = [k for k in SOURCES if source_settings.get(k, {}).get("enabled", True)]
    out = {}
    om_keys = [k for k in enabled if "model" in SOURCES[k]]
    if om_keys:
        try:
            out.update(fetch_open_meteo(om_keys, day, day, loc))
        except Exception as e:  # noqa: BLE001
            for k in om_keys:
                out[k] = {"error": str(e)}
    for key, fn in (("mosmix", lambda: fetch_mosmix(day, loc)),
                    ("met", lambda: fetch_met(day, loc, met_contact))):
        if key in enabled:
            try:
                out[key] = fn()
            except Exception as e:  # noqa: BLE001
                out[key] = {"error": str(e)}
    return out


def geocode(query):
    data = get_json("https://geocoding-api.open-meteo.com/v1/search",
                    {"name": query, "count": 8, "language": "de"})
    return [{
        "name": ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x),
        "lat": r["latitude"], "lon": r["longitude"],
        "elevation": r.get("elevation"), "timezone": r.get("timezone"),
    } for r in data.get("results", [])]


def lookup_point(lat, lon):
    """Höhe (DEM) und Zeitzone eines Punkts."""
    data = get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "hourly": "cloud_cover", "forecast_days": 1,
    })
    return {"elevation": data.get("elevation"), "timezone": data.get("timezone")}


def terrain_horizon(lat, lon, step=22.5):
    """Geländehorizont aus PVGIS (EU JRC). PVGIS-Azimut: 0=Süd, -90=Ost -> umrechnen auf 0=Nord."""
    data = get_json("https://re.jrc.ec.europa.eu/api/v5_2/printhorizon",
                    {"lat": lat, "lon": lon, "outputformat": "json"})
    raw = {round((p["A"] + 180) % 360, 1): p["H_hor"] for p in data["outputs"]["horizon_profile"]}
    result = []
    n = int(round(360 / step))
    for i in range(n):
        az = round(i * step, 1)
        result.append({"az": az, "h": round(raw.get(az, 0.0), 1)})
    return result
