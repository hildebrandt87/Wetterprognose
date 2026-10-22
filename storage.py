"""Einstellungen und Laufzeitstatus in SQLite (data/wetterprognose.db)."""
import copy
import json
import sqlite3
import threading
from pathlib import Path

import sources

DB_PATH = Path(__file__).resolve().parent / "data" / "wetterprognose.db"
_lock = threading.Lock()

DEFAULT_SETTINGS = {
    "location": {"name": "", "lat": None, "lon": None, "elevation": None, "timezone": "Europe/Berlin"},
    # Horizontprofil: Höhe (Grad) je Himmelsrichtung, 0=N, 90=O, 180=S, 270=W
    "horizon": [{"az": i * 22.5, "h": 0.0} for i in range(16)],
    "sunny_threshold_min": 30,
    "sources": {k: {"enabled": True, "weight": v["weight"]} for k, v in sources.SOURCES.items()},
    "met_contact": "",
    # Lernende Monatskorrektur aus dem Archiv (Prognose vs. Messung)
    "correction": {"enabled": True},
    "mail": {
        "enabled": False,
        "recipients": "",
        "send_time": "06:30",
        "target": "tomorrow",          # "tomorrow" oder "today"
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_security": "starttls",   # "starttls", "ssl" oder "none"
        "smtp_user": "",
        "smtp_password": "",
        "sender": "",
    },
}


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return c


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get(key, default=None):
    with _lock, _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(key, value):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def load_settings():
    return _merge(DEFAULT_SETTINGS, get("settings", {}))


def save_settings(incoming):
    current = load_settings()
    # Leeres Passwortfeld im Formular bedeutet: bisheriges Passwort behalten
    mail = incoming.get("mail") or {}
    if not mail.get("smtp_password"):
        mail.pop("smtp_password", None)
    merged = _merge(current, incoming)
    if "horizon" in incoming:
        merged["horizon"] = incoming["horizon"]
    put("settings", merged)
    return merged


def public_settings(settings):
    """Einstellungen ohne SMTP-Passwort für das Frontend."""
    s = copy.deepcopy(settings)
    s["mail"]["smtp_password_set"] = bool(s["mail"].pop("smtp_password", ""))
    return s
