"""Photovoltaik-Ertragsprognose.

Je Stunde:
  1. Global- (GHI) und Diffusstrahlung (DHI) als gewichtetes Mittel der Quellen
     (MOSMIX liefert nur GHI – der Diffusanteil kommt dort aus dem Modellmittel).
  2. Transposition auf die Modulebene (isotropes Himmelsmodell):
       Direkt:  (GHI − DHI) · Rb,  Rb = ∫cos(Einfallswinkel)·sichtbar / ∫cos(Zenit) über die Stunde
                sichtbar = Sonne über dem eigenen Horizontprofil
       Diffus:  DHI · (1 + cos β) / 2
       Boden:   GHI · Albedo · (1 − cos β) / 2
  3. Zelltemperatur T = T_Luft + POA · (NOCT − 20) / 800, Leistung · (1 + γ · (T − 25))
  4. Systemverluste, Begrenzung auf die Wechselrichterleistung.
Die Strahlung wird vorher mit der gelernten Monatskorrektur (Messung DWD vs. Modelle) skaliert.
Zum Vergleich: Ertrag bei wolkenlosem Himmel (Haurwitz-Modell, Diffusanteil 15 %).
"""
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import correction
import sources
from sun import horizon_at, solar_position

NOCT = 45
SAMPLES_PER_HOUR = 6
CLEAR_DIFFUSE_SHARE = 0.15


def total_kwp(pv):
    return sum(a["count"] * a["wp"] for a in pv["arrays"]) / 1000


def _geometry(day, loc, horizon, arrays):
    """Je lokaler Stunde: cos(Zenit)-Integral, Rb je Modulfeld, Clear-Sky-GHI (Wh/m²)."""
    tz = ZoneInfo(loc["timezone"])
    start = datetime.combine(day, time(0), tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tz).astimezone(timezone.utc)
    step = timedelta(minutes=60 / SAMPLES_PER_HOUR)
    planes = []
    for a in arrays:
        b, g = math.radians(a["tilt"]), math.radians(a["azimuth"])
        planes.append((math.sin(b) * math.sin(g), math.sin(b) * math.cos(g), math.cos(b)))

    hours = {}
    t = start
    while t < end:
        mid = t + step / 2
        key = t.astimezone(tz).strftime("%Y-%m-%dT%H:00")
        h = hours.setdefault(key, {"cosz": 0.0, "beam": [0.0] * len(arrays), "clear_ghi": 0.0})
        elev, az = solar_position(mid, loc["lat"], loc["lon"])
        if elev > 0:
            el, a = math.radians(elev), math.radians(az)
            sun = (math.cos(el) * math.sin(a), math.cos(el) * math.cos(a), math.sin(el))
            cosz = sun[2]
            h["cosz"] += cosz
            h["clear_ghi"] += 1098 * cosz * math.exp(-0.057 / cosz) / SAMPLES_PER_HOUR
            if elev > horizon_at(horizon, az):
                for i, n in enumerate(planes):
                    h["beam"][i] += max(0.0, sun[0] * n[0] + sun[1] * n[1] + sun[2] * n[2])
        t += step
    return hours


def _irradiance(raw, weights):
    """Gewichtetes Mittel von GHI, DHI und Temperatur je Stunde."""
    out = {}
    keys = set().union(*(r["values"].keys() for r in raw.values())) if raw else set()
    for key in keys:
        ghi_w = dhi_w = t_w = ghi = dhi = temp = 0.0
        for k, r in raw.items():
            v = r["values"].get(key)
            if not v or v.get("ghi") is None:
                continue
            w = weights[k]
            ghi += w * v["ghi"]
            ghi_w += w
            if v.get("dhi") is not None:
                dhi += w * min(v["dhi"], v["ghi"])
                dhi_w += w * v["ghi"]
            if v.get("temp") is not None:
                temp += w * v["temp"]
                t_w += w
        if ghi_w:
            g = ghi / ghi_w
            share = dhi / dhi_w if dhi_w else 1.0  # Diffusanteil aus den Quellen, die ihn liefern
            out[key] = {"ghi": g, "dhi": g * min(1.0, share), "temp": temp / t_w if t_w else 15.0}
    return out


def _power(poa, temp_air, kwp, pv):
    cell = temp_air + poa * (NOCT - 20) / 800
    p = kwp * poa / 1000 * (1 + pv["temp_coeff"] / 100 * (cell - 25))
    return max(0.0, p * (1 - pv["losses_pct"] / 100))


def source_weights(settings, raw):
    weights = {}
    for k, r in raw.items():
        w = settings["sources"].get(k, {}).get("weight", sources.SOURCES[k]["weight"]) * r.get("weight_factor", 1.0)
        if not r.get("error") and r["values"] and w > 0:
            weights[k] = w
    return weights


def daily_ghi(irr, day: date):
    """Tagessumme der Globalstrahlung in kWh/m² aus Stundenmitteln (W/m²)."""
    prefix = day.isoformat()
    return round(sum(v["ghi"] for k, v in irr.items() if k.startswith(prefix)) / 1000, 2)


def radiation(settings, day: date, correct=True):
    """Strahlungsprognose eines Tages, auf Wunsch mit gelernter Monatskorrektur (Faktor auf GHI und DHI)."""
    raw = sources.fetch_radiation(day, day, settings["location"], settings["sources"])
    weights = source_weights(settings, raw)
    prefix = day.isoformat()
    irr_raw = {k: v for k, v in _irradiance({k: raw[k] for k in weights}, weights).items() if k.startswith(prefix)}
    corr = (correction.factor_for(day, "ghi")
            if correct and settings["correction"].get("radiation", True) else None)
    f = corr["factor"] if corr else 1.0
    irr = {k: {**v, "ghi": v["ghi"] * f, "dhi": v["dhi"] * f} for k, v in irr_raw.items()}
    return {
        "irr": irr, "irr_raw": irr_raw,
        "ghi_raw": daily_ghi(irr_raw, day), "ghi": daily_ghi(irr, day),
        "correction": corr,
        "sources": [{"label": sources.SOURCES[k]["label"], "error": r.get("error"), "used": k in weights}
                    for k, r in raw.items()],
    }


def _hour_power(g, ir, arrays, pv):
    """Leistung je Modulfeld (kW, Stundenmittel) für eine Stunde, nach Wechselrichtergrenze."""
    albedo = pv.get("albedo", 0.2)
    arr_kw = []
    for i, a in enumerate(arrays):
        cos_b = math.cos(math.radians(a["tilt"]))
        rb = min(g["beam"][i] / g["cosz"], 10.0)  # begrenzt Ausreißer bei sehr flacher Sonne
        poa = (max(0.0, ir["ghi"] - ir["dhi"]) * rb + ir["dhi"] * (1 + cos_b) / 2
               + ir["ghi"] * albedo * (1 - cos_b) / 2)
        arr_kw.append(_power(poa, ir["temp"], a["count"] * a["wp"] / 1000, pv))
    total = sum(arr_kw)
    inverter = pv.get("inverter_kw") or None
    if inverter and total > inverter:
        arr_kw = [p * inverter / total for p in arr_kw]
    return arr_kw


def compute(settings, day: date, rad):
    pv = settings["pv"]
    arrays = [a for a in pv["arrays"] if a["count"] > 0 and a["wp"] > 0]
    if not pv["enabled"] or not arrays:
        return None
    geo = _geometry(day, settings["location"], settings.get("horizon"), arrays)
    none = {"ghi": 0.0, "dhi": 0.0, "temp": 15.0}

    hours, per_array, total, raw_total, clear_total, peak = [], [0.0] * len(arrays), 0.0, 0.0, 0.0, (0.0, None)
    for key, g in sorted(geo.items()):
        if g["cosz"] <= 0:
            continue
        ir = rad["irr"].get(key, none)
        arr_kw = _hour_power(g, ir, arrays, pv)
        raw_kw = sum(_hour_power(g, rad["irr_raw"].get(key, none), arrays, pv))
        clear = {"ghi": g["clear_ghi"], "dhi": g["clear_ghi"] * CLEAR_DIFFUSE_SHARE, "temp": ir["temp"]}
        clear_kw = sum(_hour_power(g, clear, arrays, pv))
        hour_kw = sum(arr_kw)
        for i, p in enumerate(arr_kw):
            per_array[i] += p
        total += hour_kw
        raw_total += raw_kw
        clear_total += clear_kw
        if hour_kw > peak[0]:
            peak = (hour_kw, key[11:16])
        hours.append({"hour": key[11:16], "kwh": round(hour_kw, 3), "clear_kwh": round(clear_kw, 3),
                      "ghi": round(ir["ghi"])})

    kwp = total_kwp({"arrays": arrays})
    return {
        "kwh": round(total, 1),
        "raw_kwh": round(raw_total, 1),
        "clear_kwh": round(clear_total, 1),
        "kwp": round(kwp, 2),
        "specific": round(total / kwp, 2),
        "peak_kw": round(peak[0], 2), "peak_hour": peak[1],
        "ghi_kwh_m2": rad["ghi"],
        "correction": rad["correction"],
        "hours": hours,
        "arrays": [{"name": a.get("name") or f"Feld {i + 1}", "kwp": round(a["count"] * a["wp"] / 1000, 2),
                    "kwh": round(per_array[i], 1)} for i, a in enumerate(arrays)],
        "sources": rad["sources"],
    }


def active_arrays(pv):
    return [a for a in pv["arrays"] if a["count"] > 0 and a["wp"] > 0]


def config_key(settings):
    """Kennung aller Angaben, die den Ertrag beeinflussen – ändert sie sich, wird das Archiv neu berechnet."""
    pv = settings["pv"]
    relevant = {k: pv.get(k) for k in ("arrays", "losses_pct", "inverter_kw", "temp_coeff", "albedo")}
    relevant.update(horizon=settings.get("horizon"), lat=settings["location"]["lat"], lon=settings["location"]["lon"])
    return hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:12]


def day_yield(settings, day: date, irr):
    """Tagesertrag (kWh) und Stundenwerte aus Stundenstrahlung {Stundenschlüssel: {ghi, dhi, temp}}."""
    pv = settings["pv"]
    arrays = active_arrays(pv)
    if not arrays:
        return None, {}
    geo = _geometry(day, settings["location"], settings.get("horizon"), arrays)
    none = {"ghi": 0.0, "dhi": 0.0, "temp": 15.0}
    hours, total = {}, 0.0
    for key, g in sorted(geo.items()):
        if g["cosz"] <= 0:
            continue
        kw = sum(_hour_power(g, irr.get(key, none), arrays, pv))
        total += kw
        hours[key[11:16]] = round(kw, 3)
    return round(total, 2), hours
