"""Erstellt und versendet die tägliche Sonnen-Mail."""
import html
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

GMAIL_HOSTS = {"smtp.gmail.com", "smtp.googlemail.com"}
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _de(x):
    return f"{x:.1f}".replace(".", ",")


def _day_label(iso):
    d = date.fromisoformat(iso)
    return f"{WEEKDAYS[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


def _correction_text(result):
    c = result["correction"]
    basis = "Jahresmittel" if c["basis"] == "jahr" else c["month"]
    return (f"{c['factor']:.2f}".replace(".", ",")
            + f" ({basis}, {c['days']} Archivtage) – Modelle allein: {_de(result['raw_total_hours'])} h")


def _windows_text(result):
    if not result["windows"]:
        return "Keine längeren Sonnenphasen erwartet."
    return ", ".join(f"{w['from']}–{w['to']} Uhr" for w in result["windows"])


def build_message(result, settings):
    mail = settings["mail"]
    place = result["location"].get("name") or f"{result['location']['lat']:.4f}, {result['location']['lon']:.4f}"
    day = _day_label(result["date"])
    subject = f"☀ {_de(result['total_hours'])} h Sonne am {day} – {place}"

    lines = [
        f"Sonnenprognose für {place}",
        day,
        "",
        f"Erwartete Sonnenstunden: {_de(result['total_hours'])} h (max. möglich {_de(result['possible_hours'])} h)",
        f"Sonnenaufgang {result['sunrise']} · Sonnenuntergang {result['sunset']}",
        f"Sonne über deinem Horizont: {result['visible_from'] or '–'} – {result['visible_to'] or '–'}",
        f"Sonnige Zeitfenster: {_windows_text(result)}",
        f"Zuverlässigkeit: {result['confidence_label']}",
        *([f"Korrigiert mit Faktor {_correction_text(result)}"] if result.get("correction") else []),
        "",
        "Stunde   Sonne   Wahrscheinlichkeit",
    ]
    for h in result["hours"]:
        if h["possible_min"] == 0:
            continue
        prob = "–" if h["probability"] is None else f"{h['probability'] * 100:.0f} %"
        lines.append(f"{h['hour']}    {h['expected_min']:>3.0f} min  {prob}")

    rows = []
    for h in result["hours"]:
        if h["possible_min"] == 0:
            continue
        pct = 0 if not h["possible_min"] else min(100, round(100 * h["expected_min"] / 60))
        color = "#f59e0b" if h["sunny"] else "#fcd34d"
        prob = "–" if h["probability"] is None else f"{h['probability'] * 100:.0f}&nbsp;%"
        rows.append(
            f"<tr><td style='padding:3px 8px;color:#555;font-family:monospace'>{h['hour']}</td>"
            f"<td style='padding:3px 8px;width:100%'>"
            f"<div style='background:#f1f1f1;border-radius:3px;height:14px'>"
            f"<div style='background:{color};width:{pct}%;height:14px;border-radius:3px'></div></div></td>"
            f"<td style='padding:3px 8px;text-align:right;white-space:nowrap'>{h['expected_min']:.0f}&nbsp;min</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#777;white-space:nowrap'>{prob}</td></tr>")

    windows = "".join(
        f"<li><b>{w['from']}–{w['to']} Uhr</b> · ca. {w['minutes']} min Sonne</li>" for w in result["windows"]
    ) or "<li>Keine längeren Sonnenphasen erwartet.</li>"

    body = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:560px;color:#222">
  <p style="margin:0;color:#777">{html.escape(place)}</p>
  <h2 style="margin:4px 0 12px">{day}</h2>
  <div style="font-size:40px;font-weight:700;color:#d97706">{_de(result['total_hours'])}&nbsp;h</div>
  <p style="margin:0 0 16px;color:#555">Sonnenstunden erwartet · max. möglich {_de(result['possible_hours'])}&nbsp;h ·
     Zuverlässigkeit {html.escape(result['confidence_label'])}</p>
  {f'<p style="margin:-10px 0 16px;color:#999;font-size:12px">Korrigiert mit Faktor {html.escape(_correction_text(result))}</p>'
   if result.get("correction") else ""}
  <p style="margin:0 0 4px">Sonnenaufgang <b>{result['sunrise']}</b> · Sonnenuntergang <b>{result['sunset']}</b><br>
     Sonne über deinem Horizont: {result['visible_from'] or '–'} – {result['visible_to'] or '–'}</p>
  <h3 style="margin:18px 0 6px">Sonnige Zeitfenster</h3>
  <ul style="margin:0;padding-left:20px">{windows}</ul>
  <h3 style="margin:18px 0 6px">Stundenverlauf</h3>
  <table style="border-collapse:collapse;width:100%;font-size:13px">{''.join(rows)}</table>
  <p style="color:#999;font-size:12px;margin-top:18px">Quellen: {html.escape(', '.join(
      s['label'] for s in result['sources'] if not s.get('error')))} · erstellt {result['generated']}</p>
</div>"""

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender_address(mail)
    msg["To"] = ", ".join(recipients(settings))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="wetterprognose.local")
    msg.set_content("\n".join(lines))
    msg.add_alternative(body, subtype="html")
    return msg


def recipients(settings):
    raw = settings["mail"]["recipients"].replace(";", ",")
    return [r.strip() for r in raw.split(",") if r.strip()]


def is_gmail(host):
    return host.strip().lower() in GMAIL_HOSTS


def smtp_params(m):
    """Bereinigte Verbindungsdaten: Verschlüsselung passend zum Port, Gmail-Besonderheiten."""
    host = m["smtp_host"].strip()
    port = int(m["smtp_port"])
    security = m["smtp_security"]
    # Port 465 spricht immer direkt TLS, Port 587 immer STARTTLS – falsche Kombination korrigieren
    if port == 465:
        security = "ssl"
    elif port == 587 and security != "starttls":
        security = "starttls"
    user = m["smtp_user"].strip()
    password = m["smtp_password"]
    if is_gmail(host):
        host = "smtp.gmail.com"
        if user and "@" not in user:
            user = f"{user}@gmail.com"
        # Google zeigt App-Passwörter in Vierergruppen mit Leerzeichen an
        password = password.replace(" ", "")
    return host, port, security, user, password


def sender_address(m):
    """From-Adresse: Ein reiner Name im Absenderfeld wird mit dem SMTP-Konto kombiniert."""
    _, _, _, user, _ = smtp_params(m)
    name, addr = parseaddr(m["sender"].strip())
    if "@" not in addr:
        name, addr = m["sender"].strip(), user
    if m["smtp_host"] and is_gmail(m["smtp_host"]) and addr.lower() != user.lower():
        # Gmail ersetzt fremde Absender ohnehin durch das Konto – direkt korrekt setzen
        addr = user
    return formataddr((name, addr)) if name else addr


def send(msg, settings):
    m = settings["mail"]
    if not m["smtp_host"].strip():
        raise ValueError("Kein SMTP-Server eingetragen.")
    if not recipients(settings):
        raise ValueError("Kein Empfänger eingetragen.")
    host, port, security, user, password = smtp_params(m)
    if is_gmail(host) and not (user and password):
        raise ValueError("Gmail benötigt Benutzername (deine Gmail-Adresse) und ein App-Passwort.")
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if user:
                if is_gmail(host):
                    # Nur AUTH PLAIN: Gmail trennt nach einer Ablehnung, ein automatischer
                    # Zweitversuch (smtplib.login) würde den echten Fehler verdecken.
                    server.user, server.password = user, password
                    server.auth("PLAIN", server.auth_plain)
                else:
                    server.login(user, password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        if is_gmail(host):
            detail = ("Gmail verlangt ein App-Passwort statt des normalen Kontopassworts." if e.smtp_code == 534
                      else "Benutzername oder App-Passwort falsch.")
            hint = (" App-Passwort erstellen: https://myaccount.google.com/apppasswords"
                    " (dafür muss die 2-Faktor-Authentifizierung aktiv sein).")
        else:
            detail, hint = "Benutzername oder Passwort falsch.", ""
        raise ValueError(f"Anmeldung als {user} abgelehnt ({e.smtp_code}): {detail}{hint}") from e
    except smtplib.SMTPServerDisconnected as e:
        raise ValueError(f"{host}:{port} hat die Verbindung getrennt – Port und Verschlüsselung prüfen "
                         "(465 = SSL/TLS, 587 = STARTTLS).") from e
    except (OSError, smtplib.SMTPException) as e:
        raise ValueError(f"Versand über {host}:{port} fehlgeschlagen: {e}") from e
