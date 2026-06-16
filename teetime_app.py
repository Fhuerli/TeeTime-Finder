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

import datetime as dt
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
    return [
        {"name": c.name, "club_id": c.pccaddie_club_id, "drive": c.drive_min_est}
        for c in courses
    ]


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


@st.cache_data(show_spinner=False, ttl=300)
def fetch_slots(club_id: int, name: str, date: dt.date):
    course = tw.Course(name=name, lat=0.0, lon=0.0, pccaddie_club_id=club_id)
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


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Tee-Time-Finder", page_icon="\u26f3", layout="wide")
st.markdown(
    """
    <style>
      section[data-testid="stSidebar"] { width: 440px !important; }
      section[data-testid="stSidebar"] > div { width: 440px !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("\u26f3 Tee-Time-Finder")
st.caption("Zeigt nur Startzeiten, die buchbar und für euch möglich sind.")

all_courses = load_courses()
migros_data = load_migros()
migros_clubs = (migros_data or {}).get("clubs", {})

with st.sidebar:
    st.header("Eingaben")

    # Annahme: alle nutzen die Migros GolfCard. Plätze, auf denen damit nicht
    # gespielt werden kann (nicht in der Migros-Liste, oder am Wochenende nur
    # mit Mitglied), werden nicht angezeigt.
    if migros_data:
        st.caption(f"Migros-Liste Stand {migros_data.get('stand','?')}  ·  "
                   f"{len(migros_clubs)} Plätze")
    else:
        st.warning("Keine Migros-Liste geladen. Bitte unten die offizielle "
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

    # Auswahl bleibt erhalten: beim ersten Mal alles auswählen, danach nur
    # noch auf gültige Optionen beschränken (neue nicht automatisch dazu).
    if "sel_places" not in st.session_state:
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

    st.divider()
    go = st.button("Suchen", type="primary", use_container_width=True)

    # --- Ganz unten: Migros-Liste aktualisieren (nur 1x pro Jahr nötig) ---
    st.divider()
    with st.expander("Migros-Liste aktualisieren (1x pro Jahr)"):
        st.caption("Lade hier die offizielle PDF 'Greenfee-Tarife für Migros "
                   "GolfCard' hoch (z.B. fürs nächste Jahr). Die App "
                   "übernimmt sie automatisch.")
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


if go:
    if t_from > t_to:
        st.error("Das Zeitfenster ist ungültig (Von ist später als Bis).")
        st.stop()

    selected = [c for c in pool if c["name"] in chosen]
    if not selected:
        st.warning("Keine Plätze ausgewählt. Bitte links Plätze auswählen "
                   "oder die Fahrzeit erhöhen.")
        st.stop()

    rows: list[dict] = []
    checked: list[dict] = []

    progress = st.progress(0.0, text="Suche läuft ...")
    for i, course in enumerate(selected, start=1):
        progress.progress(i / len(selected), text=f"Prüfe {course['name']} ...")

        mins = course["drive"]
        if mins is not None and mins > max_drive:
            checked.append({"Platz": course["name"],
                            "Status": f"zu weit (ca. {mins} Min.)"})
            continue

        slots = fetch_slots(course["club_id"], course["name"], date)
        hits = [s for s in slots
                if slot_possible(s, t_from, t_to, flight, only_available)]

        info = course.get("migros") or {}
        drive_txt = f"ca. {mins}" if mins is not None else "?"
        if not hits:
            checked.append({"Platz": course["name"],
                            "Status": f"keine freie Zeit ({drive_txt} Min.)"})
            continue

        for s in hits:
            rows.append({
                "Platz": course["name"],
                "Zeit": s["time"],
                "Frei": s["free"],
                "Fahrzeit (Min.)": mins if mins is not None else "?",
                "Greenfee Mo-Fr": info.get("mofr18", "") or "",
                "Greenfee Sa/So": info.get("saso18", "") or "",
                "Bedingung": condition_text(info),
                "Buchen": s["link"],
            })
    progress.empty()

    st.subheader(f"Buchbare Startzeiten am {date_de(date)}")

    if rows:
        df = pd.DataFrame(rows)
        # Standard-Sortierung: nach Platz gruppiert (alle Startzeiten eines
        # Platzes zusammen), Plätze nach Fahrzeit, dann nach Zeit. In der
        # Tabelle lässt sich jede Spalte per Klick auf den Titel umsortieren.
        df["_drive"] = df["Fahrzeit (Min.)"].apply(
            lambda x: x if isinstance(x, int) else 9999)
        df = (df.sort_values(["_drive", "Platz", "Zeit"])
                .drop(columns="_drive"))
        st.dataframe(
            df, use_container_width=True, hide_index=True,
            column_config={
                "Buchen": st.column_config.LinkColumn("Buchen", display_text="Jetzt buchen"),
                "Greenfee Mo-Fr": st.column_config.TextColumn(
                    "Greenfee Mo-Fr", help="18-Loch-Greenfee Mo-Fr für "
                    "Migros-GolfCard-Mitglieder (CHF)."),
                "Greenfee Sa/So": st.column_config.TextColumn(
                    "Greenfee Sa/So", help="18-Loch-Greenfee am Wochenende "
                    "für Migros-GolfCard-Mitglieder (CHF)."),
            },
        )
        st.success(f"{len(rows)} buchbare Startzeit(en) gefunden.")
        st.caption("Tipp: Auf einen Spaltentitel klicken sortiert die Tabelle "
                   "nach dieser Spalte. Greenfee-Angaben aus der "
                   "Migros-Tarifliste, ohne Gewähr.")
    else:
        st.info("Keine buchbaren Startzeiten im gewählten Rahmen gefunden.")

    with st.expander("Geprüft, aber nicht angezeigt"):
        if checked:
            st.dataframe(pd.DataFrame(checked), use_container_width=True,
                         hide_index=True)
        else:
            st.write("Alle gewählten Plätze hatten Treffer.")
else:
    st.info("Eingaben links festlegen und auf **Suchen** klicken.")
