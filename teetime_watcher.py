#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tee-Time-Watcher fuer PC Caddie://online
=========================================

Zweck
-----
Prueft fuer eine konfigurierte Liste von Golfplaetzen den kommenden Samstag
auf freie Startzeiten zwischen 08.00 und 09.30 Uhr und meldet Treffer per
Telegram (oder Konsole). Nur Lesen, kein Auto-Buchen. Du buchst danach
manuell in der PC-Caddie-App ueber den mitgesendeten Link.

Einrichtung (einmalig)
----------------------
1. pip install requests beautifulsoup4
2. Pro Platz den echten Timetable-Endpunkt erfassen:
   - Club-Buchungsseite im Browser oeffnen (meist auf *.pccaddie.net).
   - DevTools (F12) -> Tab "Network" -> Datum/Woche umschalten.
   - Den XHR/Fetch finden, der die Startzeiten laedt (JSON oder HTML).
   - Request-URL + Parameter + ggf. Cookies/Header kopieren und unten
     in COURSES bzw. in fetch_timetable_raw() eintragen.
   PC Caddie ist zentralisiert: hast du das Format fuer einen Club, laesst
   es sich meist nur ueber die Club-ID auf weitere Clubs uebertragen.
3. Telegram-Bot anlegen (BotFather) -> TELEGRAM_TOKEN und TELEGRAM_CHAT_ID setzen.
4. Optional: GOOGLE_MAPS_API_KEY setzen, damit die 75-Minuten-Pruefung
   echte Fahrzeiten statt der kuratierten Liste verwendet.
5. Per Cron planen, z.B. taeglich Di-Fr um 07:00:
   0 7 * * 2-5 /usr/bin/python3 /pfad/teetime_watcher.py

WICHTIG: fetch_timetable_raw() und parse_slots() sind clubspezifisch und
mit TODO markiert. Ohne den echten Endpunkt liefert das Skript nichts.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

ORIGIN = "Baar, Schweiz"          # Startpunkt fuer die Fahrzeitberechnung
MAX_DRIVE_MIN = 75                # Fahrzeit-Obergrenze
TEE_WINDOW = (dt.time(8, 0), dt.time(9, 30))   # gewuenschtes Abschlagfenster
FLIGHT_SIZE = 4                   # benoetigte freie Plaetze im Flight
TARGET_WEEKDAY = 5               # 0=Mo ... 5=Sa, 6=So

# Manche Clubs (z.B. Engelberg) reservieren Wochenend-Vormittage fuer
# Mitglieder (+ deren Gaeste). Solche Slots sind als Migros-Greenfee-Gast
# online meist nicht buchbar. True = solche Zeiten herausfiltern.
SKIP_MEMBER_RESERVED = True

PCCO_BASE = "https://mobile.pccaddie.net"   # fuer absolute Buchungslinks

# Anlagenauswahl: listet alle Clubs der pcco-Plattform samt club=ID auf.
CLUBSELECT_URL = "https://mobile.pccaddie.net/clubs/pcco/app.php?cat=clubselect"

# Laendercode in der Anlagenauswahl: 041=Schweiz, 049=Deutschland, 043=Oesterreich.
DISCOVER_COUNTRY = "041"

# Laenderkennung fuer die Discovery: 041=Schweiz, 049=Deutschland,
# 043=Oesterreich, 035=Luxemburg. None = alle Laender.
COUNTRY_FILTER = "041"

# Benachrichtigung
TELEGRAM_TOKEN = ""              # von BotFather
TELEGRAM_CHAT_ID = ""           # deine Chat-ID
GOOGLE_MAPS_API_KEY = ""        # optional, sonst wird die kuratierte Liste genutzt


# PC-Caddie-Timetable-Endpunkt (verifiziert am Beispiel Engelberg, club=188).
# Pfad bleibt gleich, nur die Club-ID aendert sich pro Platz.
# date-Parameter ist DAY|YYYY-MM-DD, der Pipe ist URL-codiert als %7C.
PCCADDIE_URL = ("https://mobile.pccaddie.net/clubs/pcco/app.php"
                "?club={club}&cat={cat}&date=DAY%7C{date}")

# Browser-aehnlicher User-Agent, damit der Abruf nicht sofort geblockt wird.
DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                   "Version/17.0 Mobile/15E148 Safari/604.1"),
}


# Gemeinsame HTTP-Session: alle Abrufe gehen auf denselben Host
# (mobile.pccaddie.net). Mit Keep-Alive und Connection-Pool entfaellt pro Platz
# der erneute TCP-/TLS-Handshake -> spuerbar schneller, wenn viele Plaetze
# parallel geprueft werden. pool_maxsize deckt die parallelen Worker ab.
# requests.Session ist fuer parallele GETs aus mehreren Threads geeignet.
_SESSION = requests.Session()
_SESSION.headers.update(DEFAULT_HEADERS)
_adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=24)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)


# HTML-Parser fuer BeautifulSoup. lxml ist deutlich schneller als der
# eingebaute html.parser; faellt automatisch zurueck, falls lxml fehlt.
_BS_PARSER: Optional[str] = None


def _bs_parser() -> str:
    global _BS_PARSER
    if _BS_PARSER is None:
        try:
            import lxml  # noqa: F401
            _BS_PARSER = "lxml"
        except Exception:  # noqa: BLE001
            _BS_PARSER = "html.parser"
    return _BS_PARSER


@dataclass
class Course:
    name: str
    lat: float
    lon: float
    # Numerische PC-Caddie-Club-ID (aus der Timetable-URL des Clubs).
    pccaddie_club_id: Optional[int] = None
    # Alternativ vollstaendige URL, falls ein Club abweicht ({date}-Platzhalter).
    timetable_url: str = ""
    # Optionale Header/Cookies, falls Login noetig (Members-Bereiche).
    headers: dict = field(default_factory=dict)
    # Direkter Buchungslink fuer die Meldung (in der App zum Buchen).
    booking_link: str = ""
    # Kuratierte Fahrzeit in Min (Fallback, wenn kein Maps-Key gesetzt ist).
    drive_min_est: Optional[int] = None
    enabled: bool = True
    # Optionaler Bereich (Anlage) innerhalb eines Clubs, z.B. "H18N" fuer den
    # 18-Loch-Platz. Wird als &alias=ALIAS|<code> an die Timetable-URL gehaengt.
    alias: str = ""
    # Alternativer Bereichsschluessel mancher Anlagen (z.B. Sempachersee):
    # wird als &als_id=<zahl> an die Timetable-URL gehaengt.
    als_id: str = ""
    # Timetable-Kategorie. Standard ist die normale Liste; manche Anlagen
    # (mehrere Plaetze) liefern ihre Zeiten nur ueber die Spalten-Uebersicht
    # "tt_timetable_course_alias".
    cat: str = "tt_timetable_course"


# Verifizierte Koordinaten (Google Places) und PC-Caddie-Club-IDs.
# Reihenfolge grob nach Fahrzeit ab Zug. Club-IDs aus den Timetable-URLs.
COURSES: list[Course] = [
    Course("Golfpark Holzhäusern", 47.148489, 8.443651, pccaddie_club_id=174, drive_min_est=15),
    Course("Golf Küssnacht am Rigi", 47.088937, 8.431819, pccaddie_club_id=187, drive_min_est=25),
    Course("Golf Meggen",           47.049079, 8.357162, pccaddie_club_id=1253, drive_min_est=30),
    Course("Golf Sempachersee",     47.154110, 8.212372, pccaddie_club_id=171, drive_min_est=32),
    Course("Golfpark Oberkirch",    47.157222, 8.106389, pccaddie_club_id=225, drive_min_est=40),
    Course("Golfpark Zürichsee",   47.198056, 8.897500, pccaddie_club_id=192, drive_min_est=42),
    Course("Golfpark Otelfingen",   47.454887, 8.401812, pccaddie_club_id=208, drive_min_est=45),
    Course("Golf Entfelden",        47.347870, 8.048213, pccaddie_club_id=173, drive_min_est=50),
    Course("Golf Kyburg",           47.463142, 8.713126, pccaddie_club_id=218, drive_min_est=50),
    Course("Golf Engelberg-Titlis", 46.821000, 8.405000, pccaddie_club_id=188, drive_min_est=55),
    # Members-Clubs: Wochenend-Gaststatus offen, daher vorerst deaktiviert.
    Course("Lucerne Golf Club",     47.064941, 8.338832, drive_min_est=30, enabled=False),
    Course("Golf CC Schönenberg",  47.205081, 8.627538, drive_min_est=25, enabled=False),
    Course("Golf CC Zürich",       47.335318, 8.626083, drive_min_est=35, enabled=False),
    Course("Golfclub Breitenloo",   47.471316, 8.630375, drive_min_est=45, enabled=False),
    Course("Andermatt Swiss Alps",  46.634142, 8.583858, drive_min_est=70, enabled=False),
]


# Fahrzeit-Schaetzwerte (Minuten ab Baar) je Club-ID. Wird genutzt, wenn die
# Plaetze automatisch aus discover_clubs geladen werden: bekannte Plaetze
# bekommen ihren Schaetzwert, alle uebrigen haben keine Schaetzung (None).
DRIVE_EST: dict[int, int] = {
    c.pccaddie_club_id: c.drive_min_est
    for c in COURSES
    if c.pccaddie_club_id is not None and c.drive_min_est is not None
}


# Migros-GolfCard-Ausschlussliste: Club-IDs, auf denen man mit der Migros
# GolfCard NICHT buchen/spielen kann. Bewusst leer gehalten ("im Zweifel
# anzeigen"). Hier bei Bedarf einzelne IDs eintragen, z.B. {169, 143}.
# Hintergrund: Mit der Migros GolfCard ist man auf nahezu allen Schweizer
# Plaetzen buchungsberechtigt; eine kleine manuelle Ausschlussliste ist
# robuster als der Versuch, die jaehrlich wechselnde Migros-PDF-Liste
# automatisch auszulesen (PDF, Bot-Sperre, wechselnder Dateiname).
EXCLUDE_MIGROS: set[int] = set()


# Namensnormalisierung (fuer den Fahrzeit-Abgleich nach Platzname). Nutzt
# bevorzugt migros.normalize; faellt sonst auf eine lokale Variante zurueck,
# damit teetime_watcher auch ohne migros.py lauffaehig bleibt.
try:
    from migros import normalize as _normalize
except Exception:  # noqa: BLE001
    import unicodedata as _ud

    _GENERIC_FALLBACK = {
        "golf", "golfpark", "golfclub", "gc", "club", "country", "and", "cc",
        "the", "de", "du", "le", "la", "am", "swiss", "alps", "course",
    }

    def _normalize(name: str) -> str:
        s = _ud.normalize("NFKD", name)
        s = "".join(c for c in s if not _ud.combining(c)).lower()
        s = re.sub(r"[^a-z0-9 ]", " ", s)
        return " ".join(t for t in s.split()
                        if t and t not in _GENERIC_FALLBACK)


# Angenommene Fahrzeiten ab Baar (ZG) in Minuten, je Platzname. Bewusst grobe
# Schaetzwerte nach Lage (keine echte Routenberechnung). Greift fuer alle
# Plaetze, egal ob aus discover oder Migros-Liste. Werte sind leicht
# anzupassen: Zahl beim jeweiligen Platz aendern.
_DRIVE_RAW: dict[str, int] = {
    # Zentralschweiz / Zug / Luzern / Schwyz
    "Holzhäusern": 15, "Ennetsee": 15, "Zug": 15,
    "Küssnacht am Rigi": 25, "Axenstein": 35, "Meggen": 30,
    "Lucerne": 30, "Luzern": 30, "Sempachersee": 32, "Sempach": 32,
    "Rastenmoos": 35, "Oberkirch": 40, "Ybrig": 35, "Brunnen": 35,
    "Engelberg": 55, "Engelberg-Titlis": 55,
    "Andermatt Golf Course": 75, "Andermatt": 75, "Andermatt Realp": 80,
    # Zürich / Zürichsee
    "Schönenberg": 25, "Zürichsee": 42, "Nuolen": 42,
    "Zürich": 35, "Zürich-Zumikon": 35, "Zumikon": 35, "Dolder": 40,
    "Otelfingen": 45, "Lägern": 45, "Breitenloo": 45, "Kyburg": 50,
    "Augwil": 40, "Bubikon": 45, "Hittnau": 45, "Greifensee": 40,
    "Fällanden": 40, "Sihlwald": 25, "Wallisellen": 40,
    # Aargau / Solothurn
    "Entfelden": 50, "Schinznach Bad": 55, "Schinznach": 55,
    "Fricktal": 60, "Limpachtal": 55, "Wylihof": 60, "Niederlenz": 50,
    "Aaretal": 95,
    # Bern / Berner Oberland / Freiburg
    "Bern": 90, "Moossee": 90, "Münchenbuchsee": 90, "Thunersee": 95,
    "Interlaken": 95, "Interlaken-Unterseen": 95, "Gstaad": 120,
    "Riederalp": 135, "Gruyère": 110, "Wallenried": 100, "Wylerfeld": 90,
    "Blumisberg": 95,
    # Neuenburg / Jura
    "Neuchâtel": 100, "Les Bois": 110, "Saignelégier": 110,
    # Basel / Grenzgebiet
    "Basel": 80, "Markgräflerland": 85, "Rheinblick": 80,
    "Bodensee Weissensberg": 75,
    # Ostschweiz / Thurgau / St. Gallen / Appenzell
    "Lipperswil": 70, "Erlen": 70, "Waldkirch": 75, "Niederbüren": 75,
    "Gams-Werdenberg": 85, "Gonten": 90, "Appenzell": 90, "Heidiland": 85,
    "Bad Ragaz": 85, "Lenzburg": 50, "Ostschweizerischer": 75,
    # Graubünden
    "Domat/Ems": 110, "Domat Ems": 110, "Chur": 110, "Lenzerheide": 120,
    "Arosa": 130, "Davos": 130, "Klosters": 135, "Alvaneu Bad": 120,
    "Vulpera": 150, "Engadin": 150, "Samedan": 150, "Zuoz": 150,
    "Zuoz-Madulain": 150, "Sedrun": 95,
    # Tessin
    "Lugano": 130, "Magliaso": 130, "Patriziale Ascona": 140,
    "Ascona": 140, "Gerre Losone": 140, "Losone": 140,
    # Waadt / Genf / Wallis / Region Genfersee
    "Lausanne": 140, "Lavaux": 140, "Signal de Bougy": 150, "Bougy": 150,
    "Montreux": 130, "Aigle": 130, "Villars": 140, "Crans-sur-Sierre": 140,
    "Crans": 140, "Sion": 130, "Verbier": 150, "Vuissens": 130,
    "Yverdon": 130, "Payerne": 115, "Bonmont": 180, "Bossey": 185,
    "Domaine Impérial": 175, "Gland": 175, "Genève": 185, "Genf": 185,
    "Maison Blanche": 180, "Esery": 185, "Brésil": 185,
    # Nachtrag: zuvor ohne Schaetzwert
    "Bürgenstock": 35, "Flühli Sörenberg": 50, "Unterengstringen": 40,
    "Winterberg": 50, "Goldenberg": 55, "Weid Hauenstein": 55,
    "Rheinfelden": 60, "Emmental": 80, "Heidental": 70, "Laufental": 75,
    "Brigels": 110, "Sagogn": 110, "Golf Goms": 120, "Leuk": 135,
    "Matterhorn": 150, "St. Moritz Kulm": 150,
    # Grenznahe Plaetze (nur relevant, falls Ausland mitgesucht wird)
    "Markgräferland": 85, "La Largue": 90, "Saint Apollinaire": 95,
    "Obere Alp": 70, "Les Coullaux": 140,
}

DRIVE_EST_BY_NAME: dict[str, int] = {
    _normalize(k): v for k, v in _DRIVE_RAW.items()
}


def drive_estimate(name: str) -> "int | None":
    """Angenommene Fahrzeit (Min.) zu einem Platznamen, tolerant abgeglichen."""
    key = _normalize(name)
    if key in DRIVE_EST_BY_NAME:
        return DRIVE_EST_BY_NAME[key]
    ctoks = set(key.split())
    if not ctoks:
        return None
    # Teilmengen-Abgleich: Platzname enthaelt einen bekannten Ortsnamen.
    best = None
    for nname, mins in DRIVE_EST_BY_NAME.items():
        ntoks = set(nname.split())
        if ntoks and (ntoks <= ctoks or ctoks <= ntoks):
            best = mins
            break
    return best


def courses_from_discovery(country: str = DISCOVER_COUNTRY) -> list["Course"]:
    """Baut die Platzliste automatisch aus der PC-Caddie-Anlagenauswahl.

    Liefert alle Plaetze des Landes (Standard 041 = Schweiz) als Course-Objekte.
    Die Fahrzeit wird ueber den Platznamen aus DRIVE_EST_BY_NAME geschaetzt
    (angenommene Werte ab Baar). Findet sich kein Name-Treffer, wird die
    Club-ID-Tabelle DRIVE_EST geprueft; sonst bleibt die Fahrzeit unbekannt.
    Ohne Netzzugriff liefert discover_clubs eine leere Liste; der Aufrufer
    sollte dann auf die kuratierte COURSES-Liste zurueckfallen.
    """
    clubs = discover_clubs(country)
    out: list[Course] = []
    for cid, name in clubs:
        est = drive_estimate(name)
        if est is None:
            est = DRIVE_EST.get(cid)
        out.append(Course(
            name=name, lat=0.0, lon=0.0,
            pccaddie_club_id=cid,
            drive_min_est=est,
        ))
    return out


@dataclass
class Slot:
    course: str
    date: dt.date
    time: dt.time
    free_spots: int
    booking_link: str = ""
    member_reserved: bool = False   # Info "reserviert: Clubmitglieder"
    guest_min_hcp: bool = False     # Info "Gaeste ... Mindest-Handicap"
    holes: Optional[int] = None     # 9 oder 18, falls aus dem Timetable lesbar


# ---------------------------------------------------------------------------
# Fahrzeit-Filter (optional, sonst kuratierte Liste)
# ---------------------------------------------------------------------------

def within_drive_time(course: Course) -> bool:
    if not GOOGLE_MAPS_API_KEY:
        return (course.drive_min_est or 0) <= MAX_DRIVE_MIN
    try:
        resp = _SESSION.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": ORIGIN,
                "destinations": f"{course.lat},{course.lon}",
                "mode": "driving",
                "departure_time": "now",
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=20,
        )
        data = resp.json()
        secs = data["rows"][0]["elements"][0]["duration"]["value"]
        return secs / 60 <= MAX_DRIVE_MIN
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Fahrzeit fuer {course.name} nicht ermittelbar: {exc}")
        return (course.drive_min_est or 0) <= MAX_DRIVE_MIN


# ---------------------------------------------------------------------------
# PC-Caddie-Abfrage  (CLUBSPEZIFISCH - hier kommt deine Arbeit rein)
# ---------------------------------------------------------------------------

def _extract_holes(text: str) -> Optional[int]:
    """Liest aus einem Text eine Lochzahl wie '9 Loch' oder '18-Loch' heraus.

    PC Caddie benennt den Bereich teils als '9 Loch Platz' oder '18 Loch'.
    Wir erkennen genau 9 oder 18; andere Zahlen ignorieren wir bewusst, weil
    sie meist Anlagengroessen (27/36) und keine Runden bezeichnen.
    """
    if not text:
        return None
    m = re.search(r"\b(9|18)\s*[-\u2013 ]?\s*(?:Loch|Hole)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _strip_session_params(url: str) -> str:
    """Entfernt fremde Sitzungs-IDs aus einem PC-Caddie-Link.

    Beim Auslesen hängt PC Caddie die Server-Sitzung (z.B. __Host-PHPSESSID)
    an jeden Buchungslink. Diese gehört nicht zum Browser des Nutzers und führt
    beim Klick zur Fehlermeldung. Wir behalten nur die inhaltlichen Parameter
    (club, cat, way, als_id, date, time, ...) und werfen die Sitzung weg.
    """
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001
        return url
    if not parts.query:
        return url

    def is_session(key: str) -> bool:
        k = key.lower()
        return ("sessid" in k or "session" in k or k == "sid"
                or key.startswith("__Host-") or key.startswith("__Secure-"))

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not is_session(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), parts.fragment))


def build_url(course: Course, date: dt.date) -> Optional[str]:
    """Baut die Timetable-URL aus Club-ID oder expliziter URL."""
    url = None
    if course.pccaddie_club_id is not None:
        url = PCCADDIE_URL.format(club=course.pccaddie_club_id,
                                  cat=course.cat or "tt_timetable_course",
                                  date=date.isoformat())
    elif course.timetable_url:
        url = course.timetable_url.format(date=date.isoformat())
    if url and course.alias:
        url += f"&alias=ALIAS%7C{course.alias}"
    elif url and course.als_id:
        url += f"&als_id={course.als_id}"
    return url


# Anlagen, deren Zeiten nur ueber die Spalten-Uebersicht kommen statt ueber die
# normale Startzeitenliste (z.B. Sempachersee, dessen tt_timetable_course-Seite
# nicht existiert). parse_slots liest beide Seitenarten.
CLUB_CAT: dict[int, str] = {
    171: "tt_timetable_course_alias",  # Golf Sempachersee
}

# Anlagen, deren Zeiten sich nicht maschinell auslesen lassen -> Direktlink.
# (Derzeit keine.)
CLUB_LINK_ONLY: set = set()


# Anlagen (Bereiche) innerhalb eines Clubs, die PC Caddie ueber das Feld
# "Bereich" getrennt fuehrt. Pro Eintrag ein Anzeigename, der Bereichsschluessel
# (alias ODER als_id, je nach Anlage) und die Lochzahl. Damit erscheinen z.B.
# die Plaetze von Holzhaeusern und Sempachersee einzeln zur Auswahl.
CLUB_AREAS: dict[int, list[dict]] = {
    174: [  # Golfpark Holzhaeusern (Bereichswahl ueber alias)
        {"label": "Holzhäusern, 18-Loch Zugersee", "alias": "H18N", "holes": 18},
        {"label": "Holzhäusern, 9-Loch Rigi", "alias": "H9LN", "holes": 9},
        {"label": "Holzhäusern, 9-Loch Par 3 Pilatus", "alias": "H6L", "holes": 9},
    ],
    205: [  # Golfpark Lipperswil (Bereichswahl ueber alias)
        {"suffix": "18-Loch", "alias": "18L", "holes": 18},
        {"suffix": "9-Loch", "alias": "0901", "holes": 9},
    ],
    208: [  # Golfpark Otelfingen
        {"suffix": "18-Loch", "alias": "018L", "holes": 18},
    ],
}


def discover_clubs(country: str = DISCOVER_COUNTRY) -> list[tuple[int, str]]:
    """Liest die Clubliste eines Landes aus der Anlagenauswahl (cat=clubselect).

    Die Seite enthaelt ALLE Laender gleichzeitig; jeder Club steht als
        <div class="pcco-club" data-club="188" data-country="041" data-name="...">
    Die Laenderauswahl im Browser blendet nur per JavaScript aus. Wir filtern
    deshalb selbst auf data-country (Standard 041 = Schweiz) und lesen ID und
    Name direkt aus den data-Attributen.
    """
    try:
        resp = _SESSION.get(CLUBSELECT_URL, timeout=20)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Anlagenauswahl nicht abrufbar: {exc}")
        return []

    found: dict[int, str] = {}
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, _bs_parser())
        for div in soup.select("div.pcco-club"):
            if div.get("data-country") != country:
                continue
            cid = div.get("data-club")
            if not cid or not cid.isdigit():
                continue
            name = (div.get("data-name") or div.get_text() or "").strip()
            found[int(cid)] = " ".join(name.split())
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Clubliste nicht parsebar: {exc}")
        return []

    return sorted(found.items(), key=lambda kv: kv[1].lower())


def fetch_timetable_raw(course: Course, date: dt.date) -> Optional[str]:
    """Holt die Roh-Antwort des Timetables fuer ein Datum.

    Liefert i.d.R. ein HTML-Fragment der mobilen Ansicht zurueck.
    """
    url = build_url(course, date)
    if not url:
        print(f"[SKIP] {course.name}: keine Club-ID/URL hinterlegt.")
        return None
    headers = course.headers or None
    try:
        # Getrennter Connect-/Read-Timeout: bei einem nicht erreichbaren Platz
        # blockiert die Suche so hoechstens kurz (Connect), statt 10 s zu warten.
        resp = _SESSION.get(url, headers=headers, timeout=(4, 10))
        resp.raise_for_status()
        return resp.text
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Abruf {course.name} fehlgeschlagen: {exc}")
        return None


def parse_slots(course: Course, date: dt.date, raw: str) -> list[Slot]:
    """Liest die Startzeiten aus dem PC-Caddie-Timetable-HTML.

    Struktur (verifiziert an Engelberg, club=188): Jede Startzeit ist eine
    Zeile <tr class="pcco-tt-time-person"> mit den Attributen
        data-time          z.B. "08:10"
        data-status        "bookable" | "occupied" | "block-time"
        data-seat_bookable  Anzahl freier Plaetze, z.B. "4"
    Der Buchungslink steht in der zugehoerigen Detailzeile (data-toggle-Id),
    im Element div.tk-aktiv > a.
    Die td-Klasse der Zeitzelle signalisiert die Buchungskategorie:
        pcco-tt-filter-information  -> reserviert fuer Mitglieder (+ Gaeste)
        pcco-tt-filter-bargain      -> fuer Gaeste buchbar (Mindest-HCP)
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, _bs_parser())
    slots: list[Slot] = []

    # Normale Startzeitenliste: Zeilen mit der bekannten Klasse. Manche Anlagen
    # (z.B. Sempachersee, Spalten-Uebersicht) verwenden dieselben data-Attribute
    # an anderen Elementen -> Fallback auf alles, was eine Startzeit traegt.
    rows = soup.select("tr.pcco-tt-time-person")
    if not rows:
        rows = [el for el in soup.select("[data-time]")
                if el.get("data-seat_bookable") is not None]

    for row in rows:
        time_str = row.get("data-time")
        status = row.get("data-status")
        if not time_str or (status and status != "bookable"):
            continue  # occupied / block-time / Kopfzeilen ueberspringen

        try:
            free = int(row.get("data-seat_bookable", "0"))
        except ValueError:
            free = 0
        if free <= 0:
            continue

        t = dt.datetime.strptime(time_str, "%H:%M").time()

        # Buchungskategorie aus der Klasse der Zeitzelle ablesen.
        time_cell = row.find("td")
        cell_classes = time_cell.get("class", []) if time_cell else []
        member_reserved = "pcco-tt-filter-information" in cell_classes
        guest_min_hcp = "pcco-tt-filter-bargain" in cell_classes

        # Buchungslink aus der Detailzeile holen (data-toggle = "#id").
        booking_link = ""
        holes = _extract_holes(row.get_text(" ", strip=True))
        toggle = (row.get("data-toggle") or "").lstrip("#")
        if toggle:
            action = soup.find(id=toggle)
            if action:
                if holes is None:
                    holes = _extract_holes(action.get_text(" ", strip=True))
                a = action.select_one(".tk-aktiv a[href]")
                if a:
                    href = a["href"]
                    booking_link = href if href.startswith("http") else PCCO_BASE + href
                    booking_link = _strip_session_params(booking_link)
        # Fallback: kein platzgenauer Link -> auf die Startzeiten-Uebersicht des
        # Tages zeigen, damit der Klick immer auf einer gueltigen Buchungsseite
        # landet statt auf einer Fehlermeldung.
        if not booking_link:
            booking_link = build_url(course, date) or ""

        slots.append(Slot(course.name, date, t, free, booking_link,
                          member_reserved, guest_min_hcp, holes))

    return slots


# ---------------------------------------------------------------------------
# Filter & Benachrichtigung
# ---------------------------------------------------------------------------

def keep(slot: Slot) -> bool:
    start, end = TEE_WINDOW
    if not (start <= slot.time <= end and slot.free_spots >= FLIGHT_SIZE):
        return False
    if SKIP_MEMBER_RESERVED and slot.member_reserved and not slot.guest_min_hcp:
        return False  # nur fuer Mitglieder, als Greenfee-Gast nicht buchbar
    return True


def next_target_date(today: Optional[dt.date] = None) -> dt.date:
    today = today or dt.date.today()
    delta = (TARGET_WEEKDAY - today.weekday()) % 7
    delta = delta or 7   # heute Samstag -> naechsten Samstag nehmen
    return today + dt.timedelta(days=delta)


def notify(hits: list[Slot]) -> None:
    if not hits:
        print("Keine passenden freien Startzeiten gefunden.")
        return

    lines = [f"Freie Startzeiten am {hits[0].date.strftime('%a %d.%m.%Y')}:", ""]
    for s in sorted(hits, key=lambda x: (x.course, x.time)):
        note = " (Gaeste: Mindest-HCP)" if s.guest_min_hcp else ""
        link = f"\n  {s.booking_link}" if s.booking_link else ""
        lines.append(f"- {s.course}: {s.time.strftime('%H:%M')} "
                     f"({s.free_spots} frei){note}{link}")
    msg = "\n".join(lines)
    print(msg)

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                      "disable_web_page_preview": True},
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Telegram-Versand fehlgeschlagen: {exc}")


# ---------------------------------------------------------------------------
# Hauptlauf
# ---------------------------------------------------------------------------

def main() -> None:
    target = next_target_date()
    hits: list[Slot] = []

    for course in COURSES:
        if not course.enabled:
            continue
        if not within_drive_time(course):
            continue
        raw = fetch_timetable_raw(course, target)
        if not raw:
            continue
        for slot in parse_slots(course, target, raw):
            if keep(slot):
                hits.append(slot)

    notify(hits)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        # Optionaler Ländercode als zweites Argument, sonst Schweiz.
        country = sys.argv[2] if len(sys.argv) > 2 else DISCOVER_COUNTRY
        clubs = discover_clubs(country)
        print(f"{len(clubs)} Clubs (Land {country}) gefunden:\n")
        for cid, name in clubs:
            print(f"  club={cid:>5}  {name}")
    else:
        main()
