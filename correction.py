"""Lernende Monatskorrektur: Faktor = gemessene / unkorrigiert prognostizierte Sonnenstunden.

- Grundlage sind alle Archivtage mit Prognose (raw_h) und Messwert (actual_h).
- Nachbarmonate fließen mit halbem Gewicht ein (glättet, solange nur wenige Jahre vorliegen).
- Bei wenigen Tagen wird der Faktor Richtung 1 gezogen (Schrumpfung mit SHRINK_DAYS Pseudo-Tagen).
"""
from datetime import date

NEIGHBOR_WEIGHT = 0.5
SHRINK_DAYS = 10
MIN_DAYS = 15
FACTOR_MIN, FACTOR_MAX = 0.3, 1.3

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


def _pairs():
    import archive  # spät importieren: archive -> forecast -> correction
    with archive._conn() as c:
        return c.execute("SELECT date, raw_h, actual_h, possible_h FROM archive_days "
                         "WHERE raw_h IS NOT NULL AND actual_h IS NOT NULL").fetchall()


def _weights(month):
    return {month: 1.0, (month - 2) % 12 + 1: NEIGHBOR_WEIGHT, month % 12 + 1: NEIGHBOR_WEIGHT}


def _sums(rows):
    sums = {m: [0.0, 0.0, 0] for m in range(1, 13)}  # raw, actual, Tage
    for r in rows:
        s = sums[int(r["date"][5:7])]
        s[0] += r["raw_h"]
        s[1] += r["actual_h"]
        s[2] += 1
    return sums


def _factor(sums, month, exclude=None):
    """Faktor, gewichtete Tage, Basis ("monat"|"jahr"|"keine").

    exclude = (Monat, raw, actual) eines Tages, der für die Kreuzvalidierung herausgenommen wird.
    Reichen die Tage um den Monat nicht, gilt der Faktor über alle Monate.
    """
    def combine(weights):
        raw = act = n = 0.0
        for m, w in weights.items():
            r, a, k = sums[m]
            if exclude and exclude[0] == m:
                r, a, k = r - exclude[1], a - exclude[2], k - 1
            raw += w * r
            act += w * a
            n += w * k
        return raw, act, n

    raw, act, n = combine(_weights(month))
    basis = "monat"
    if n < MIN_DAYS or raw <= 0:
        raw, act, n = combine({m: 1.0 for m in range(1, 13)})
        basis = "jahr"
        if n < MIN_DAYS or raw <= 0:
            return 1.0, n, "keine"
    f = act / raw
    f = (n * f + SHRINK_DAYS) / (n + SHRINK_DAYS)
    return max(FACTOR_MIN, min(FACTOR_MAX, f)), n, basis


def factor_for(day: date):
    month = day.month
    sums = _sums(_pairs())
    factor, n, basis = _factor(sums, month)
    if basis == "keine":
        return None
    return {"factor": round(factor, 3), "days": round(n), "month": MONTHS[month - 1], "basis": basis}


def report():
    """Faktoren je Monat und Güte per Leave-one-out (jeder Tag mit einem Faktor ohne ihn selbst)."""
    rows = _pairs()
    sums = _sums(rows)
    stats = {m: {"n": 0, "err_raw": 0.0, "err_corr": 0.0, "bias_raw": 0.0, "bias_corr": 0.0} for m in range(1, 13)}
    for r in rows:
        m = int(r["date"][5:7])
        f, _, _ = _factor(sums, m, exclude=(m, r["raw_h"], r["actual_h"]))
        corrected = min(r["raw_h"] * f, r["possible_h"] or r["raw_h"] * f)
        s = stats[m]
        s["n"] += 1
        s["err_raw"] += abs(r["raw_h"] - r["actual_h"])
        s["err_corr"] += abs(corrected - r["actual_h"])
        s["bias_raw"] += r["raw_h"] - r["actual_h"]
        s["bias_corr"] += corrected - r["actual_h"]

    months = []
    for m in range(1, 13):
        factor, n, basis = _factor(sums, m)
        s = stats[m]
        k = s["n"]
        months.append({
            "month": m, "label": MONTHS[m - 1], "factor": round(factor, 3), "weighted_days": round(n, 1), "basis": basis,
            "days": k, "raw_h": round(sums[m][0], 1), "actual_h": round(sums[m][1], 1),
            "mae_raw": round(s["err_raw"] / k, 2) if k else None,
            "mae_corr": round(s["err_corr"] / k, 2) if k else None,
            "bias_raw": round(s["bias_raw"] / k, 2) if k else None,
            "bias_corr": round(s["bias_corr"] / k, 2) if k else None,
        })
    total = sum(s["n"] for s in stats.values())
    return {
        "months": months,
        "days": total,
        "mae_raw": round(sum(s["err_raw"] for s in stats.values()) / total, 2) if total else None,
        "mae_corr": round(sum(s["err_corr"] for s in stats.values()) / total, 2) if total else None,
        "bias_raw": round(sum(s["bias_raw"] for s in stats.values()) / total, 2) if total else None,
        "bias_corr": round(sum(s["bias_corr"] for s in stats.values()) / total, 2) if total else None,
        "min_days": MIN_DAYS,
    }
