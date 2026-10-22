"""Wetterprognose – Webserver, API und täglicher Mailversand.

Start:  python3 app.py [--host 0.0.0.0] [--port 8080]
CLI:    python3 app.py --show [today|tomorrow|YYYY-MM-DD]
Optionaler Passwortschutz (HTTP Basic Auth): Umgebungsvariable WP_PASSWORD setzen.
"""
import argparse
import base64
import hmac
import json
import logging
import mimetypes
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import archive
import correction
import forecast
import mailer
import sources
import storage

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Wurde der Server erst nach der Sendezeit gestartet, wird höchstens so lange nachgesendet.
SEND_GRACE = timedelta(hours=3)

log = logging.getLogger("wetterprognose")


def send_daily_mail(settings):
    result = forecast.compute(settings, settings["mail"]["target"])
    mailer.send(mailer.build_message(result, settings), settings)
    return result


def parse_archive_query(q):
    mode = q.get("mode", "month")
    if mode == "day":
        return archive.query("day", day=date.fromisoformat(q["day"]))
    year = int(q["year"])
    if mode == "year":
        return archive.query("year", year=year)
    if mode == "week":
        return archive.query("week", year=year, week=int(q["week"]))
    return archive.query("month", year=year, month=int(q["month"]))


def scheduler_loop():
    while True:
        try:
            settings = storage.load_settings()
            mail = settings["mail"]
            result, mail_failed = None, False
            if settings["location"].get("lat") is None:
                time.sleep(30)
                continue
            tz = ZoneInfo(settings["location"]["timezone"])
            now = datetime.now(tz)
            hh, mm = map(int, mail["send_time"].split(":"))
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            today = now.date().isoformat()
            if mail["enabled"]:
                state = storage.get("mail_state", {})
                if due <= now < due + SEND_GRACE and state.get("last_sent") != today:
                    try:
                        result = send_daily_mail(settings)
                        storage.put("mail_state", {"last_sent": today, "last_ok": now.isoformat(timespec="minutes"),
                                                   "last_hours": result["total_hours"], "last_error": None})
                        log.info("Tagesmail versendet (%s h)", result["total_hours"])
                    except Exception as e:  # noqa: BLE001
                        storage.put("mail_state", {**state, "last_error": f"{now:%Y-%m-%d %H:%M}: {e}"})
                        log.error("Mailversand fehlgeschlagen: %s", e)
                        mail_failed = True
            # Archiv einmal täglich zur Sendezeit (oder beim ersten Lauf danach) fortschreiben
            if now >= due and storage.get("archive_state", {}).get("last_run") != today:
                try:
                    archive.daily_job(settings, result)
                    log.info("Archiv aktualisiert")
                except Exception as e:  # noqa: BLE001
                    log.error("Archiv-Job fehlgeschlagen: %s", e)
                    storage.put("archive_state", {"last_run": today, "last_error": str(e)})
            if mail_failed:
                time.sleep(600)  # nach Mailfehler 10 Minuten bis zum nächsten Versuch
        except Exception:  # noqa: BLE001
            log.error("Scheduler-Fehler:\n%s", traceback.format_exc())
        time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    server_version = "Wetterprognose/1.0"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _authorized(self):
        password = os.environ.get("WP_PASSWORD")
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                if hmac.compare_digest(given, password):
                    return True
            except ValueError:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Wetterprognose"')
        self.end_headers()
        return False

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel):
        path = (STATIC_DIR / rel).resolve()
        if not path.is_file() or STATIC_DIR not in path.parents:
            return self._json({"error": "nicht gefunden"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                         + ("; charset=utf-8" if path.suffix in (".html", ".js", ".css") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self._static("index.html")
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            if url.path == "/api/settings":
                return self._json(storage.public_settings(storage.load_settings()))
            if url.path == "/api/forecast":
                return self._json(forecast.compute(storage.load_settings(), q.get("day", "tomorrow")))
            if url.path == "/api/geocode":
                return self._json(sources.geocode(q.get("q", "")))
            if url.path == "/api/lookup":
                return self._json(sources.lookup_point(float(q["lat"]), float(q["lon"])))
            if url.path == "/api/horizon":
                return self._json(sources.terrain_horizon(float(q["lat"]), float(q["lon"])))
            if url.path == "/api/archive":
                return self._json(parse_archive_query(q))
            if url.path == "/api/archive/meta":
                return self._json(archive.meta())
            if url.path == "/api/correction":
                return self._json(correction.report())
            if url.path == "/api/status":
                return self._json(storage.get("mail_state", {}))
            return self._json({"error": "nicht gefunden"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            log.error("Fehler bei %s:\n%s", self.path, traceback.format_exc())
            return self._json({"error": f"{type(e).__name__}: {e}"}, 502)

    def do_POST(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        try:
            if url.path == "/api/settings":
                return self._json(storage.public_settings(storage.save_settings(self._body())))
            if url.path == "/api/mail/test":
                result = send_daily_mail(storage.load_settings())
                return self._json({"ok": True, "total_hours": result["total_hours"], "date": result["date"]})
            if url.path == "/api/archive/backfill":
                body = self._body()
                archive.start_backfill(storage.load_settings(), date.fromisoformat(body["from"]),
                                       date.fromisoformat(body["to"]))
                return self._json({"ok": True})
            if url.path == "/api/archive/run":
                archive.daily_job(storage.load_settings())
                return self._json(archive.meta())
            return self._json({"error": "nicht gefunden"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            log.error("Fehler bei %s:\n%s", self.path, traceback.format_exc())
            return self._json({"error": f"{type(e).__name__}: {e}"}, 502)


def main():
    parser = argparse.ArgumentParser(description="Wetterprognose – Sonnenstunden-Vorhersage")
    parser.add_argument("--host", default=os.environ.get("WP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WP_PORT", "8080")))
    parser.add_argument("--show", nargs="?", const="tomorrow", metavar="TAG",
                        help="Prognose im Terminal ausgeben statt Server zu starten")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.show:
        r = forecast.compute(storage.load_settings(), args.show)
        print(f"{r['date']}: {r['total_hours']} h Sonne (max. {r['possible_hours']} h), "
              f"Zuverlässigkeit {r['confidence_label']}")
        for w in r["windows"]:
            print(f"  sonnig {w['from']}–{w['to']} (~{w['minutes']} min)")
        for s in r["sources"]:
            print(f"  {s['label']:<28} {s.get('total_h', '-'):>5} h  {s.get('error') or ''}")
        return

    threading.Thread(target=scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("Wetterprognose läuft auf http://%s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
