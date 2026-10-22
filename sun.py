"""Sonnenstand (NOAA-Algorithmus) und astronomisch mögliche Sonnenminuten je Stunde."""
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Sonnenmittelpunkt gilt als sichtbar, sobald der obere Sonnenrand über dem Horizont steht.
SUN_SEMI_DIAMETER = 0.27


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def solar_position(dt_utc, lat, lon):
    """Scheinbare Elevation (inkl. Refraktion) und Azimut (0=N, 90=O) in Grad."""
    jd = dt_utc.timestamp() / 86400 + 2440587.5
    jc = (jd - 2451545) / 36525

    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    mean_anom = math.radians(357.52911 + jc * (35999.05029 - 0.0001537 * jc))
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    eq_ctr = (math.sin(mean_anom) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
              + math.sin(2 * mean_anom) * (0.019993 - 0.000101 * jc)
              + math.sin(3 * mean_anom) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = math.radians(mean_long + eq_ctr - 0.00569 - 0.00478 * math.sin(omega))
    mean_obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    obliq = math.radians(mean_obliq + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(app_long))

    y = math.tan(obliq / 2) ** 2
    l0 = math.radians(mean_long)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * l0) - 2 * ecc * math.sin(mean_anom)
        + 4 * ecc * y * math.sin(mean_anom) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0) - 1.25 * ecc * ecc * math.sin(2 * mean_anom))

    minutes = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60
    true_solar = (minutes + eq_time + 4 * lon) % 1440
    hour_angle = math.radians(true_solar / 4 - 180)

    lat_r = math.radians(lat)
    cos_zen = _clamp(math.sin(lat_r) * math.sin(decl)
                     + math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle))
    zenith = math.acos(cos_zen)
    elevation = 90 - math.degrees(zenith)

    # Atmosphärische Refraktion (NOAA-Näherung)
    if elevation <= 85:
        te = math.tan(math.radians(elevation))
        if elevation > 5:
            refr = 58.1 / te - 0.07 / te ** 3 + 0.000086 / te ** 5
        elif elevation > -0.575:
            refr = 1735 + elevation * (-518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711)))
        else:
            refr = -20.772 / te
        elevation += refr / 3600

    sin_zen = math.sin(zenith)
    if sin_zen < 1e-9:
        azimuth = 180.0
    else:
        a = math.degrees(math.acos(_clamp(
            (math.sin(lat_r) * cos_zen - math.sin(decl)) / (math.cos(lat_r) * sin_zen))))
        azimuth = (a + 180) % 360 if hour_angle > 0 else (540 - a) % 360
    return elevation, azimuth


def horizon_at(horizon, azimuth):
    """Horizonthöhe für einen Azimut, linear interpoliert aus [{az, h}, ...]."""
    if not horizon:
        return 0.0
    pts = sorted((p["az"] % 360, p["h"]) for p in horizon)
    pts = [(pts[-1][0] - 360, pts[-1][1])] + pts + [(pts[0][0] + 360, pts[0][1])]
    for (a0, h0), (a1, h1) in zip(pts, pts[1:]):
        if a0 <= azimuth <= a1:
            return h0 if a1 == a0 else h0 + (h1 - h0) * (azimuth - a0) / (a1 - a0)
    return 0.0


def day_profile(day: date, lat, lon, tz_name, horizon=None):
    """Minutengenaue Sonnenbahn eines lokalen Kalendertags.

    Liefert je lokaler Stunde (Schlüssel 'YYYY-MM-DDTHH:00'):
      flat_min   – Sonnenminuten bei freiem, flachem Horizont (Referenz der Wettermodelle)
      local_min  – Sonnenminuten über dem eigenen Horizontprofil
      first/last – erste/letzte Minute mit Sonne über dem eigenen Horizont
    """
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, time(0), tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tz).astimezone(timezone.utc)

    hours = {}
    sunrise = sunset = None
    visible_from = visible_to = None
    max_elev = -90.0
    t = start
    while t < end:
        elev, az = solar_position(t + timedelta(seconds=30), lat, lon)
        local = t.astimezone(tz)
        key = local.strftime("%Y-%m-%dT%H:00")
        h = hours.setdefault(key, {"flat_min": 0, "local_min": 0, "first": None, "last": None})
        max_elev = max(max_elev, elev)
        if elev > -SUN_SEMI_DIAMETER:
            h["flat_min"] += 1
            sunrise = sunrise or local
            sunset = local + timedelta(minutes=1)
        if elev > horizon_at(horizon, az) - SUN_SEMI_DIAMETER:
            h["local_min"] += 1
            h["first"] = h["first"] or local.strftime("%H:%M")
            h["last"] = (local + timedelta(minutes=1)).strftime("%H:%M")
            visible_from = visible_from or local
            visible_to = local + timedelta(minutes=1)
        t += timedelta(minutes=1)

    fmt = lambda d: d.strftime("%H:%M") if d else None
    return {
        "hours": hours,
        "sunrise": fmt(sunrise),
        "sunset": fmt(sunset),
        "visible_from": fmt(visible_from),
        "visible_to": fmt(visible_to),
        "max_elevation": round(max_elev, 1),
        "flat_total_min": sum(h["flat_min"] for h in hours.values()),
        "local_total_min": sum(h["local_min"] for h in hours.values()),
    }
