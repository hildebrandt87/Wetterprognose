# Wetterprognose – Sonnenstunden

Web-App, die die Sonnenstunden für einen exakten Standort vorhersagt – gesamt und je Stunde –
und die Prognose jeden Morgen per E-Mail verschickt. Reines Python (Standardbibliothek), keine Installation nötig.

## Start

```bash
python3 app.py                 # http://<server>:8080
python3 app.py --port 9000
python3 app.py --show          # Prognose für morgen im Terminal
python3 app.py --show today
WP_PASSWORD=geheim python3 app.py   # Webfrontend mit Passwort (Basic Auth)
```

Dauerhaft als Dienst (nötig für die tägliche Mail):

```bash
cp wetterprognose.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now wetterprognose
journalctl -u wetterprognose -f
```

## Einrichtung im Browser

1. **Standort** – Ort suchen, auf die Karte klicken oder die Markierung genau platzieren. Höhe und Zeitzone werden automatisch ermittelt.
2. **Horizont** – „Geländehorizont laden“ holt Berge/Täler aus PVGIS (EU JRC). Nahe Hindernisse (Haus, Bäume) je Himmelsrichtung in Grad erhöhen.
3. **Quellen** – Gewichte anpassen oder Quellen abschalten.
4. **E-Mail** – SMTP-Daten, Empfänger, Sendezeit, „Prognose für morgen/heute“. Mit „Testmail“ prüfen.

Bei Gmail/GMX/Web.de ein App-Passwort verwenden (Gmail: `smtp.gmail.com`, Port 587, STARTTLS).

## Quellen

| Quelle | Daten | Standardgewicht |
|---|---|---|
| DWD ICON (Open-Meteo) | Sonnenscheindauer je Stunde | 1,0 |
| ECMWF IFS (Open-Meteo) | Sonnenscheindauer je Stunde | 0,9 |
| Météo-France (Open-Meteo) | Sonnenscheindauer je Stunde | 0,6 |
| NOAA GFS (Open-Meteo) | Sonnenscheindauer je Stunde | 0,5 |
| DWD MOSMIX (Bright Sky) | statistische Stationsprognose, Sonnenminuten | 1,0 × Entfernungsfaktor |
| MET Norway | Bewölkung → geschätzter Sonnenanteil | 0,5 |

## Algorithmus

1. **Sonnenbahn** minutengenau (NOAA-Algorithmus inkl. Refraktion) → je Stunde die Minuten, in denen die
   Sonne über flachem Horizont bzw. über *deinem* Horizontprofil steht.
2. **Normalisieren:** Jede Quelle wird zu einem Sonnenanteil `f` (0–1) je Stunde:
   Modell-Sonnenminuten ÷ astronomisch mögliche Minuten (flach), bzw. `1 − (Bewölkung/100)^1,3`.
3. **Ensemble:** gewichteter Mittelwert von `f` über alle Quellen.
4. **Lokalisieren:** erwartete Minuten = `f` × Minuten mit Sonne über deinem Horizont.
5. **Auswertung:** Summe = Sonnenstunden; Stunden ≥ Schwelle (Standard 30 min) ergeben die sonnigen Zeitfenster;
   Streuung der Quellen → Zuverlässigkeit; Anteil der Quellen mit `f ≥ 0,5` → Sonnenwahrscheinlichkeit.

## Photovoltaik

Unter Einstellungen → Photovoltaik beliebig viele Modulfelder (Anzahl, Wp je Modul, Neigung, Ausrichtung) sowie
Systemverluste, Wechselrichtergrenze und Temperaturkoeffizient eintragen. Berechnung je Stunde: gewichtetes Mittel von
Global- und Diffusstrahlung (4 Modelle + MOSMIX), Transposition auf die Modulebene (isotropes Himmelsmodell, Direktanteil
entfällt hinter dem Horizontprofil), Zelltemperatur nach NOCT, Verluste, Wechselrichterbegrenzung. Zum Vergleich der Ertrag
bei wolkenlosem Himmel (Haurwitz).

Im Archiv werden Strahlungsprognose und gemessene Globalstrahlung **stündlich** gespeichert. Daraus wird der
Solarertrag je Tag zweimal berechnet: aus der Prognose und aus der Messung („was die Anlage bei der tatsächlichen
Einstrahlung ungefähr erzeugt hätte“ – kein Zählerwert). Ändern sich Anlage, Horizont oder Standort, rechnet die App
die gesamte Historie automatisch lokal neu (Kennung `pv_config`). Die Archiv-Ansicht zeigt beides als Diagramm.

## Lernende Monatskorrektur

Die Wettermodelle überschätzen die Sonne lokal systematisch (z. B. Nebel/Hochnebel im Winter). Aus dem Archiv wird je
Kalendermonat ein Faktor gelernt: gemessene ÷ unkorrigiert prognostizierte Sonnenstunden (Nachbarmonate halb gewichtet,
bei < 15 Tagen Jahresmittel, Schrumpfung Richtung 1 bei wenigen Daten, begrenzt auf 0,3–1,3). Der Faktor wird stündlich
auf jede Quelle angewendet. Im Archiv wird zusätzlich die unkorrigierte Prognose (`raw_h`) gespeichert, damit die Korrektur
nie aus ihren eigenen Ergebnissen lernt. Die Güte zeigt die Archiv-Ansicht per Leave-one-out-Kreuzvalidierung.
Abschaltbar unter Einstellungen → Berechnung.

Für den Solarertrag gibt es eine zweite, gleich aufgebaute Korrektur der **Globalstrahlung**: Prognose (gewichtetes
Modellmittel, `ghi_raw`) gegen die gemessene Globalstrahlung der nächsten DWD-Station (`ghi_actual`). Der Faktor
skaliert Global- und Diffusstrahlung vor der Umrechnung auf die Module.

## Archiv

- Täglich zur Sendezeit wird die **Prognose für morgen** gespeichert (falls für heute keine existiert, auch die heutige).
- **Gemessene Sonnenstunden** der nächsten DWD-Station (Bright Sky) werden bis 10 Tage rückwirkend nachgetragen.
- **Komprimierung:** 3 Tage nach Monatsende werden die Stundendetails des Monats als ein zlib-Block
  (`archive_months`) gespeichert, Tagessummen bleiben in `archive_days` direkt abfragbar.
- **Nachladen:** Vergangene Zeiträume (ab 2022) lassen sich in der Archiv-Ansicht füllen – Prognose rekonstruiert aus
  dem Open-Meteo-Modellarchiv (ohne MOSMIX/MET), plus DWD-Messwerte. Live gespeicherte Prognosen werden nie überschrieben.
- Ansicht: Jahr → Monat → Tag sowie Kalenderwochen, mit Prognosefehler (Ø Abweichung und Tendenz).

## Dateien

- `app.py` – Webserver, API, Mail-Zeitplan
- `forecast.py` – Kombinations-Algorithmus
- `sources.py` – Abruf der Wetterquellen, Geocoding, Geländehorizont
- `sun.py` – Sonnenstand und mögliche Sonnenminuten
- `archive.py` – Archiv: Tagesjob, Messwerte, Komprimierung, Nachladen, Abfragen
- `correction.py` – lernende Monatskorrektur und deren Kreuzvalidierung
- `pv.py` – Photovoltaik-Ertragsprognose
- `mailer.py` – Mail-Inhalt (Text + HTML) und SMTP-Versand
- `storage.py` – Einstellungen in `data/wetterprognose.db` (SQLite; enthält das SMTP-Passwort im Klartext)
- `static/` – Webfrontend
