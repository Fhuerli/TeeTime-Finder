# -*- coding: utf-8 -*-
"""
Tee-Time-Finder (Eingabemaske)
==============================

Streamlit-Oberfläche für den PC-Caddie Tee-Time-Watcher.

Logik
-----
Die Platzliste richtet sich nach der Fahrzeit (Schieber): angezeigt werden
alle Schweizer Plätze innerhalb der eingestellten Fahrzeit. Es wird
angenommen, dass alle eine Migros GolfCard haben; Plätze, auf denen damit
nicht gespielt werden kann (nicht in der Migros-Liste, oder am Wochenende
nur mit Mitglied), werden nicht angezeigt.

PDF-Update (jährlich)
---------------------
Ganz unten im Eingabebereich gibt es einen ausklappbaren Punkt
"Migros-Liste aktualisieren". Dort lädst du einmal pro Jahr die neue
offizielle PDF hoch; die App übernimmt sie automatisch (migros_data.json).

Einrichtung (einmalig)
----------------------
1. teetime_app.py, teetime_watcher.py, migros.py und migros_data.json in
   denselben Ordner legen (z.B. C:\\Golf).
2. pip install streamlit requests beautifulsoup4 pandas pdfplumber
3. Starten:  streamlit run teetime_app.py
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import html as html_lib
import os

import pandas as pd
import streamlit as st

import teetime_watcher as tw
import migros

HERE = os.path.dirname(os.path.abspath(__file__))
MIGROS_JSON = os.path.join(HERE, "migros_data.json")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
               "Freitag", "Samstag", "Sonntag"]


def date_de(d: dt.date) -> str:
    return f"{WEEKDAYS_DE[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


@st.cache_data(show_spinner="Lade Schweizer Plätze ...", ttl=86400)
def load_courses() -> list[dict]:
    courses = tw.courses_from_discovery("041")
    if not courses:
        courses = [c for c in tw.COURSES if c.pccaddie_club_id]

    out = []
    for c in courses:
        areas = tw.CLUB_AREAS.get(c.pccaddie_club_id)
        if areas:
            # Anlage mit mehreren Bereichen -> je Bereich ein eigener Eintrag.
            for a in areas:
                out.append({"name": a["label"], "club_id": c.pccaddie_club_id,
                            "drive": c.drive_min_est,
                            "alias": a.get("alias", ""),
                            "als_id": a.get("als_id", ""),
                            "area_holes": a.get("holes")})
        else:
            out.append({"name": c.name, "club_id": c.pccaddie_club_id,
                        "drive": c.drive_min_est, "alias": "", "als_id": "",
                        "area_holes": None})
    return out


def load_migros() -> dict | None:
    if os.path.exists(MIGROS_JSON):
        try:
            return migros.load_json(MIGROS_JSON)
        except Exception:
            pass
    pdf = migros.find_pdf_in_dir(HERE)
    if pdf:
        try:
            data = migros.parse_migros_pdf(pdf)
            migros.save_json(data, MIGROS_JSON)
            return data
        except Exception:
            return None
    return None


@st.cache_data(show_spinner=False, ttl=90)
def fetch_slots(club_id: int, name: str, date: dt.date, alias: str = "",
                als_id: str = ""):
    course = tw.Course(name=name, lat=0.0, lon=0.0, pccaddie_club_id=club_id,
                       alias=alias or "", als_id=als_id or "")
    raw = tw.fetch_timetable_raw(course, date)
    if not raw:
        return []
    return [
        {
            "time": s.time.strftime("%H:%M"),
            "free": s.free_spots,
            "member_reserved": s.member_reserved,
            "guest_min_hcp": s.guest_min_hcp,
            "link": s.booking_link,
            "holes": s.holes,
        }
        for s in tw.parse_slots(course, date, raw)
    ]


def slot_possible(slot: dict, t_from: dt.time, t_to: dt.time,
                  flight: int, only_available: bool) -> bool:
    t = dt.datetime.strptime(slot["time"], "%H:%M").time()
    if not (t_from <= t <= t_to):
        return False
    if slot["free"] < flight:
        return False
    if only_available and slot["member_reserved"] and not slot["guest_min_hcp"]:
        return False
    return True


def condition_text(info: dict | None) -> str:
    if not info:
        return ""
    parts = []
    if info.get("weekend_member_only"):
        parts.append("Wochenende nur mit Mitglied")
    if info.get("varies"):
        parts.append("Preise tagesabhängig")
    return "; ".join(parts)


def build_results_html(results: list[dict]) -> str:
    """Baut die responsive Kartenansicht der Ergebnisse als HTML."""
    cards = []
    for r in results:
        name = html_lib.escape(r["name"])
        meta = []
        if isinstance(r["drive"], int):
            meta.append(f"ca. {r['drive']} Min.")
        holes = str(r.get("holes", "")).strip()
        if holes.isdigit():
            meta.append(f"{holes} Loch")
        if r["mofr"]:
            meta.append(f"Mo-Fr CHF {html_lib.escape(str(r['mofr']))}")
        if r["saso"]:
            meta.append(f"Sa/So CHF {html_lib.escape(str(r['saso']))}")
        meta_html = (f'<div class="tt-meta">{" &middot; ".join(meta)}</div>'
                     if meta else "")
        cond_html = (f'<div class="tt-cond">{html_lib.escape(r["cond"])}</div>'
                     if r["cond"] else "")

        chips = []
        mixed = r.get("mixed_holes")
        for s in r["slots"]:
            free = s["free"]
            free_txt = f"{free} frei" if free else ""
            extra = ""
            if mixed and s.get("holes"):
                extra = f'<span class="tt-free">{s["holes"]} Loch</span>'
            inner = (f'<span class="tt-time">{html_lib.escape(s["time"])}</span>'
                     f'<span class="tt-free">{free_txt}</span>{extra}')
            link = s.get("link")
            if link:
                chips.append(
                    f'<a class="tt-slot" href="{html_lib.escape(link)}" '
                    f'target="_blank" rel="noopener">{inner}</a>')
            else:
                chips.append(f'<span class="tt-slot tt-slot-off">{inner}</span>')

        cards.append(
            '<div class="tt-course">'
            f'<div class="tt-name">{name}</div>'
            f'{meta_html}{cond_html}'
            f'<div class="tt-slots">{"".join(chips)}</div>'
            '</div>')
    return '<div class="tt-wrap">' + "".join(cards) + "</div>"


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Tee-Time-Finder", page_icon="\u26f3",
                   layout="centered", initial_sidebar_state="auto")
st.markdown(
    """
    <style>
      /* Streamlit-Bedienelemente ausblenden für einen aufgeräumten Auftritt. */
      #MainMenu { visibility: hidden; }
      footer { visibility: hidden; }
      [data-testid="stToolbar"] { display: none; }
      [data-testid="stDecoration"] { display: none; }
      [data-testid="stStatusWidget"] { display: none; }

      /* Auf dem Handy alles kompakter und passend zur Bildschirmbreite. */
      @media (max-width: 991px) {
        h1 { font-size: 1.6rem !important; line-height: 1.2 !important; }
        .block-container { padding-top: 2.5rem !important;
                           padding-left: 0.8rem !important;
                           padding-right: 0.8rem !important; }
      }

      /* Ergebnis-Karten: passen sich jeder Bildschirmbreite an. */
      .tt-wrap { display: flex; flex-direction: column; gap: 12px; }
      .tt-course { border: 1px solid #e4e6e4; border-radius: 14px;
                   padding: 14px 16px; background: #ffffff;
                   box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
      .tt-name { font-weight: 700; font-size: 1.02rem; color: #1b1b1b;
                 line-height: 1.25; }
      .tt-meta { color: #5a5f5a; font-size: 0.84rem; margin-top: 3px; }
      .tt-cond { color: #9a6a00; font-size: 0.8rem; margin-top: 3px; }
      .tt-slots { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
      .tt-slot { display: inline-flex; flex-direction: column;
                 align-items: center; justify-content: center;
                 text-decoration: none; background: #2E6B3E; color: #ffffff;
                 border-radius: 11px; padding: 8px 12px; min-width: 62px;
                 transition: background 0.15s; }
      .tt-slot:hover { background: #24572f; }
      .tt-slot-off { background: #9aa19a; }
      .tt-time { font-weight: 700; font-size: 0.96rem; line-height: 1.15;
                 color: #ffffff; }
      .tt-free { font-size: 0.68rem; opacity: 0.92; color: #ffffff; }
      /* Auf grossen Schirmen etwas grosszügigere Knöpfe. */
      @media (min-width: 992px) {
        .tt-slot { min-width: 72px; padding: 9px 14px; }
        .tt-time { font-size: 1.0rem; }
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("\u26f3 Tee-Time-Finder")
st.caption("Zeigt nur Startzeiten, die buchbar und für euch möglich sind.")

all_courses = load_courses()
migros_data = load_migros()
migros_clubs = (migros_data or {}).get("clubs", {})

# Annahme: alle nutzen die Migros GolfCard. Plätze, auf denen damit nicht
# gespielt werden kann (nicht in der Migros-Liste, oder am Wochenende nur mit
# Mitglied), werden nicht angezeigt.
if migros_data:
    st.caption(f"Migros-Liste Stand {migros_data.get('stand','?')}  ·  "
               f"{len(migros_clubs)} Plätze")
else:
    st.warning("Keine Migros-Liste geladen. Bitte zuunterst die offizielle "
               "PDF hochladen.")

date = st.date_input("Datum", value=dt.date.today(),
                     min_value=dt.date.today(), format="DD.MM.YYYY")
is_weekend = date.weekday() >= 5

c1, c2 = st.columns(2)
# Von/Bis koppeln: ändert sich "Von", wird "Bis" automatisch auf 30 Minuten
# später gesetzt. "Bis" lässt sich danach von Hand weiter anpassen.
if "t_from" not in st.session_state:
    st.session_state["t_from"] = dt.time(8, 0)
    st.session_state["t_to"] = dt.time(8, 30)
    st.session_state["_prev_from"] = dt.time(8, 0)
if st.session_state.get("_prev_from") != st.session_state["t_from"]:
    base = dt.datetime.combine(dt.date.today(), st.session_state["t_from"])
    st.session_state["t_to"] = (base + dt.timedelta(minutes=30)).time()
    st.session_state["_prev_from"] = st.session_state["t_from"]
t_from = c1.time_input("Von", key="t_from")
t_to = c2.time_input("Bis", key="t_to")

flight = st.number_input("Spieler im Flight", min_value=1, max_value=4,
                         value=4, step=1)

only_available = st.checkbox(
    "Nur verfügbare Zeiten anzeigen", value=True,
    help="Blendet alle nicht buchbaren Zeiten aus (reserviert, belegt, "
         "gesperrt).",
)

include_9 = st.checkbox(
    "9-Loch-Zeiten einschliessen", value=True,
    help="Eingeschaltet zeigt auch 9-Loch-Startzeiten. Ausschalten blendet "
         "Zeiten aus, die in PC Caddie als 9-Loch gekennzeichnet sind.",
)

# Alle Plätze mit Migros-Info anreichern und auf Migros-spielbare filtern.
enriched = []
for c in all_courses:
    info = migros.find_migros_course(c["name"], migros_clubs) \
        if migros_clubs else None
    enriched.append({**c, "migros": info})


def playable_migros(course: dict) -> bool:
    info = course["migros"]
    if info is None:
        return False  # nicht in Migros-Liste -> hier nicht spielbar
    if is_weekend and info.get("weekend_member_only"):
        return False  # Wochenende nur mit Mitglied -> als Gast nicht spielbar
    return True


# Ohne geladene Migros-Liste kein Filter (sonst wäre alles leer).
if migros_clubs:
    playable = [c for c in enriched if playable_migros(c)]
else:
    playable = enriched

max_drive = st.slider("Max. Fahrzeit (Min.)", min_value=15,
                      max_value=180, value=75, step=5)
include_far = st.checkbox(
    "Auch Plätze ohne Fahrzeit-Schätzung zeigen", value=True,
    help="Zeigt zusätzlich Plätze, für die keine Fahrzeit hinterlegt "
         "ist (erscheinen am Ende der Liste).",
)

known = [c for c in playable
         if c["drive"] is not None and c["drive"] <= max_drive]
unknown = [c for c in playable if c["drive"] is None]

options = [c["name"] for c in known]
if include_far:
    options += [c["name"] for c in unknown]

# Die Auswahl folgt dem Fahrzeit-Schieber: ändert sich der Schieber oder das
# Häkchen, werden alle Plätze im neuen Umkreis ausgewählt. Solange der Schieber
# gleich bleibt, bleibt ein manuelles Abwählen einzelner Plätze erhalten.
drive_sig = (max_drive, include_far)
if st.session_state.get("_drive_sig") != drive_sig:
    st.session_state["sel_places"] = list(options)
    st.session_state["_drive_sig"] = drive_sig
elif "sel_places" not in st.session_state:
    st.session_state["sel_places"] = list(options)
else:
    st.session_state["sel_places"] = [
        n for n in st.session_state["sel_places"] if n in options]

cap = f"{len(options)} Plätze zur Auswahl"
if include_far and unknown:
    cap += f" (davon {len(unknown)} ohne Fahrzeit-Schätzung)"
st.caption(cap)

chosen = st.multiselect("Plätze", options=options, key="sel_places")
pool = playable

go = st.button("Suchen", type="primary", use_container_width=True)
if go:
    st.session_state["searched"] = True


if st.session_state.get("searched"):
    if t_from > t_to:
        st.error("Das Zeitfenster ist ungültig (Von ist später als Bis).")
        st.stop()

    selected = [c for c in pool if c["name"] in chosen]
    if not selected:
        st.warning("Keine Plätze ausgewählt. Bitte links Plätze auswählen "
                   "oder die Fahrzeit erhöhen.")
        st.stop()

    results: list[dict] = []
    checked: list[dict] = []

    # Plaetze innerhalb der Fahrzeit bestimmen; zu weite gleich vermerken.
    to_fetch = []
    for course in selected:
        mins = course["drive"]
        if mins is not None and mins > max_drive:
            checked.append({"Platz": course["name"],
                            "Status": f"zu weit (ca. {mins} Min.)"})
        else:
            to_fetch.append(course)

    # Startzeiten parallel abrufen (mehrere Plaetze gleichzeitig) statt
    # nacheinander. Das verkuerzt die Wartezeit deutlich.
    slots_by_name: dict[str, list] = {}
    if to_fetch:
        progress = st.progress(0.0, text="Suche läuft ...")
        done = 0
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(fetch_slots, c["club_id"], c["name"], date,
                                 c.get("alias", ""), c.get("als_id", "")): c
                       for c in to_fetch}
            for fut in cf.as_completed(futures):
                c = futures[fut]
                try:
                    slots_by_name[c["name"]] = fut.result()
                except Exception:  # noqa: BLE001
                    slots_by_name[c["name"]] = []
                done += 1
                progress.progress(done / len(to_fetch),
                                  text=f"{done}/{len(to_fetch)} Plätze geprüft ...")
        progress.empty()

    # Treffer auswerten (schnell, ohne Netzwerk).
    for course in to_fetch:
        mins = course["drive"]
        area_holes = course.get("area_holes")
        slots = slots_by_name.get(course["name"], [])
        hits = [s for s in slots
                if slot_possible(s, t_from, t_to, flight, only_available)
                and (include_9 or (s.get("holes") or area_holes) != 9)]

        info = course.get("migros") or {}
        drive_txt = f"ca. {mins}" if mins is not None else "?"
        if not hits:
            checked.append({"Platz": course["name"],
                            "Status": f"keine freie Zeit ({drive_txt} Min.)"})
            continue

        hits_sorted = sorted(hits, key=lambda s: s["time"])
        # Lochzahl: bei definiertem Bereich dessen Lochzahl; sonst die pro
        # Startzeit gelesene; sonst die Anlagen-Lochzahl aus der Migros-Liste.
        if area_holes:
            holes_val = str(area_holes)
            mixed_holes = False
        else:
            slot_h = {s.get("holes") for s in hits_sorted if s.get("holes")}
            if len(slot_h) == 1:
                holes_val = str(next(iter(slot_h)))
                mixed_holes = False
            elif len(slot_h) > 1:
                holes_val = ""
                mixed_holes = True
            else:
                holes_val = info.get("holes", "") or ""
                mixed_holes = False
        results.append({
            "name": course["name"],
            "drive": mins,
            "holes": holes_val,
            "mixed_holes": mixed_holes,
            "mofr": info.get("mofr18", "") or "",
            "saso": info.get("saso18", "") or "",
            "cond": condition_text(info),
            "slots": hits_sorted,
        })

    # Nach Fahrzeit gruppiert sortieren (Plätze ohne Schätzung ans Ende).
    results.sort(key=lambda r: (r["drive"] if isinstance(r["drive"], int)
                                else 9999, r["name"]))

    st.subheader(f"Buchbare Startzeiten am {date_de(date)}")

    if results:
        total = sum(len(r["slots"]) for r in results)
        st.markdown(build_results_html(results), unsafe_allow_html=True)
        st.success(f"{total} buchbare Startzeit(en) gefunden.")
        st.caption("Tippe auf eine Zeit, um direkt zur Buchung zu gelangen. "
                   "Greenfee-Angaben aus der Migros-Tarifliste, ohne Gewähr.")
    else:
        st.info("Keine buchbaren Startzeiten im gewählten Rahmen gefunden.")

    with st.expander("Geprüft, aber nicht angezeigt"):
        if checked:
            st.dataframe(pd.DataFrame(checked), use_container_width=True,
                         hide_index=True)
        else:
            st.write("Alle gewählten Plätze hatten Treffer.")

# --- Ganz unten: Migros-Liste aktualisieren (nur 1x pro Jahr nötig) ---
st.divider()
with st.expander("Migros-Liste aktualisieren (1x pro Jahr)"):
    st.caption("Lade hier die offizielle PDF 'Greenfee-Tarife für Migros "
               "GolfCard' hoch (z.B. fürs nächste Jahr). Die App übernimmt "
               "sie automatisch.")
    up = st.file_uploader("Migros-Tarifliste (PDF)", type="pdf")
    if up is not None:
        sig = f"{up.name}:{up.size}"
        if st.session_state.get("migros_upload_sig") != sig:
            try:
                new_data = migros.parse_migros_pdf(up)
                if new_data["clubs"]:
                    migros.save_json(new_data, MIGROS_JSON)
                    st.session_state["migros_upload_sig"] = sig
                    st.success(f"Aktualisiert: {len(new_data['clubs'])} "
                               f"Plätze (Stand {new_data['stand']}).")
                    st.rerun()
                else:
                    st.error("In der PDF wurden keine Plätze erkannt.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"PDF konnte nicht gelesen werden: {exc}")
