"""Kombiniert Sonnenbahn und Quellen zu einer Sonnenstunden-Prognose.

Algorithmus je Stunde:
  1. Jede Quelle wird in einen Sonnenschein-Anteil f (0..1) übersetzt:
       Sonnenminuten-Quellen: f = Modell-Sonnenminuten / astronomisch mögliche Minuten (flacher Horizont)
       Bewölkungs-Quellen:    f = 1 - (Bewölkung/100)^1.3   (empirische Näherung)
  2. f wird gewichtet gemittelt (Gewicht aus Einstellungen x Quellenfaktor, z.B. Stationsentfernung).
  3. Erwartete Sonnenminuten = f x Minuten, in denen die Sonne über DEINEM Horizont steht.
  4. Streuung der Quellen -> Zuverlässigkeit; Anteil Quellen mit f >= 0.5 -> Sonnenwahrscheinlichkeit.
"""
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import correction
import sources
from sun import day_profile

CLOUD_EXPONENT = 1.3
MIN_FLAT_MINUTES = 3   # darunter ist der Quotient Modell/möglich zu instabil


def resolve_day(spec, tz_name):
    today = datetime.now(ZoneInfo(tz_name)).date()
    if spec in (None, "", "tomorrow"):
        return today + timedelta(days=1)
    if spec == "today":
        return today
    if spec.startswith("plus") and spec[4:].isdigit():
        return today + timedelta(days=int(spec[4:]))
    return date.fromisoformat(spec)


def _fraction(kind, value, flat_min):
    if kind == "cloud":
        return max(0.0, 1 - (value / 100) ** CLOUD_EXPONENT)
    if flat_min < MIN_FLAT_MINUTES:
        return None
    return min(1.0, value / flat_min)


def compute(settings, day_spec="tomorrow"):
    loc = settings["location"]
    if loc.get("lat") is None or loc.get("lon") is None:
        raise ValueError("Kein Standort gesetzt – bitte in den Einstellungen festlegen.")
    day = resolve_day(day_spec, loc["timezone"])
    raw = sources.fetch_all(day, loc, settings["sources"], settings.get("met_contact", ""))
    return evaluate(settings, day, raw)


def evaluate(settings, day, raw, correct=True):
    """Kombiniert bereits geladene Quelldaten (raw) für einen Tag. raw darf mehrere Tage umfassen.

    correct=False rechnet ohne Monatskorrektur (z. B. beim Rekonstruieren vergangener Tage).
    """
    loc = settings["location"]
    corr = correction.factor_for(day) if correct and settings["correction"]["enabled"] else None
    factor = corr["factor"] if corr else 1.0
    profile = day_profile(day, loc["lat"], loc["lon"], loc["timezone"], settings.get("horizon"))

    source_rows, weights = [], {}
    for key, info in sources.SOURCES.items():
        if key not in raw:
            continue
        r = raw[key]
        row = {"key": key, "label": info["label"], "meta": r.get("meta", {}), "error": r.get("error")}
        if not r.get("error"):
            w = settings["sources"].get(key, {}).get("weight", info["weight"]) * r.get("weight_factor", 1.0)
            if not r["values"]:
                row["error"] = "keine Daten für diesen Tag"
            elif w > 0:
                weights[key] = w
            row["weight"] = round(w, 2)
        source_rows.append(row)

    threshold = settings.get("sunny_threshold_min", 30)
    hours, totals, raw_total_min = [], {k: 0.0 for k in weights}, 0.0
    for key, p in sorted(profile["hours"].items()):
        if p["flat_min"] == 0 and p["local_min"] == 0:
            continue
        fracs, raw_fracs = {}, {}
        for k in weights:
            v = raw[k]["values"].get(key)
            if v is None:
                continue
            f = _fraction(sources.SOURCES[k]["kind"], v, p["flat_min"])
            if f is not None:
                totals[k] += f * p["local_min"]   # Quellenvergleich zeigt die unkorrigierten Werte
                raw_fracs[k] = f
                fracs[k] = min(1.0, f * factor)
        wsum = sum(weights[k] for k in fracs)
        if wsum:
            raw_total_min += p["local_min"] * sum(weights[k] * f for k, f in raw_fracs.items()) / wsum
            mean = sum(weights[k] * f for k, f in fracs.items()) / wsum
            spread = math.sqrt(sum(weights[k] * (f - mean) ** 2 for k, f in fracs.items()) / wsum)
            prob = sum(weights[k] for k, f in fracs.items() if f >= 0.5) / wsum
        else:
            mean, spread, prob = 0.0, None, None
        expected = mean * p["local_min"]
        hours.append({
            "hour": key[11:16],
            "possible_min": p["local_min"],
            "flat_min": p["flat_min"],
            "expected_min": round(expected, 1),
            "fraction": round(mean, 3),
            "spread": None if spread is None else round(spread, 3),
            "probability": None if prob is None else round(prob, 2),
            "sunny": p["local_min"] > 0 and expected >= min(threshold, p["local_min"] * 0.8),
            "first": p["first"], "last": p["last"],
            "sources": {k: round(f, 2) for k, f in fracs.items()},
        })

    for row in source_rows:
        if row["key"] in totals:
            row["total_h"] = round(totals[row["key"]] / 60, 1)

    # Zusammenhängende sonnige Stunden zu Zeitfenstern zusammenfassen
    windows, cur = [], None
    for h in hours:
        if h["sunny"]:
            if cur is None:
                cur = {"from": h["first"], "to": h["last"], "minutes": 0.0, "hours": 0}
                windows.append(cur)
            cur["to"] = h["last"]
            cur["minutes"] += h["expected_min"]
            cur["hours"] += 1
        else:
            cur = None
    for w in windows:
        w["minutes"] = round(w["minutes"])

    weighted = [(h["spread"], h["possible_min"]) for h in hours if h["spread"] is not None and h["possible_min"]]
    pm = sum(m for _, m in weighted)
    avg_spread = sum(s * m for s, m in weighted) / pm if pm else None
    if avg_spread is None or len(weights) < 2:
        confidence, label = None, "unbekannt"
    else:
        confidence = max(0.0, 1 - 2.5 * avg_spread)
        label = "hoch" if confidence >= 0.7 else "mittel" if confidence >= 0.45 else "niedrig"

    total_min = sum(h["expected_min"] for h in hours)
    return {
        "date": day.isoformat(),
        "location": loc,
        "total_hours": round(total_min / 60, 1),
        "possible_hours": round(profile["local_total_min"] / 60, 1),
        "daylength_hours": round(profile["flat_total_min"] / 60, 1),
        "sunrise": profile["sunrise"], "sunset": profile["sunset"],
        "visible_from": profile["visible_from"], "visible_to": profile["visible_to"],
        "max_elevation": profile["max_elevation"],
        "confidence": None if confidence is None else round(confidence, 2),
        "confidence_label": label,
        "windows": windows,
        "hours": hours,
        "sources": source_rows,
        "threshold_min": threshold,
        "raw_total_hours": round(raw_total_min / 60, 1),
        "correction": corr,
        "generated": datetime.now(ZoneInfo(loc["timezone"])).strftime("%Y-%m-%d %H:%M"),
    }
