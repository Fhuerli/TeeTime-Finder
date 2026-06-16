# -*- coding: utf-8 -*-
"""
Migros GolfCard: PDF-Parser und Namensabgleich
==============================================

Liest die offizielle PDF "Greenfee-Tarife für Migros GolfCard Mitglieder" ein
und liefert je Golfclub die Tarife und Bedingungen. Damit weiss die App, auf
welchen Plätzen man mit der Migros GolfCard spielen kann und was es kostet.

Die App nutzt standardmässig die mitgelieferte Datei migros_data.json (Stand
2026). Lädt man im App-Fenster eine neue Jahres-PDF hoch, wird sie mit diesem
Parser eingelesen und migros_data.json überschrieben; die App ist damit ohne
Code-Änderung aktualisiert.

Zum Einlesen einer PDF wird pdfplumber benötigt (pip install pdfplumber).
Für den reinen Betrieb mit der mitgelieferten JSON ist pdfplumber nicht nötig.
"""

from __future__ import annotations

import json
import re
import unicodedata


# ---------------------------------------------------------------------------
# Namensnormalisierung (für den Abgleich PC-Caddie <-> Migros-Liste)
# ---------------------------------------------------------------------------

# Generische Wörter, die in PC-Caddie-Namen vorkommen, aber nicht in der
# Migros-Liste (oder umgekehrt). Werden für den Abgleich entfernt.
_GENERIC = {
    "golf", "golfpark", "golfclub", "gc", "club", "country", "and", "cc",
    "the", "de", "du", "le", "la", "am", "swiss", "alps", "course",
}


def normalize(name: str) -> str:
    """Reduziert einen Platznamen auf vergleichbare Kernwörter.

    Kleinbuchstaben, Umlaute/Akzente entfernt, Sonderzeichen weg, generische
    Golf-Wörter raus. So matchen z.B. "Golfpark Holzhäusern" und "Holzhäusern".
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    tokens = [t for t in s.split() if t and t not in _GENERIC]
    return " ".join(tokens)


def match_key(name: str) -> frozenset:
    """Token-Menge für toleranten Abgleich."""
    return frozenset(normalize(name).split())


# ---------------------------------------------------------------------------
# PDF einlesen
# ---------------------------------------------------------------------------

def _clean_cell(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _weekend_member_only(saso18: str, saso9: str) -> bool:
    """Wochenende nur mit Mitglied: Preisfeld ist ein Punkt ohne Zahl."""
    def member_only(cell: str) -> bool:
        return "\u25cf" in cell and not any(ch.isdigit() for ch in cell)
    # 18-Loch ist der relevante Fall; wenn 18-Loch nur-mit-Mitglied ist.
    return member_only(saso18)


def _has_price(cell: str) -> bool:
    return any(ch.isdigit() for ch in cell)


def parse_migros_pdf(source) -> dict:
    """Liest die Migros-Greenfee-PDF und liefert ein Daten-Dict.

    source: Dateipfad (str) oder ein file-like Objekt (z.B. Streamlit-Upload).
    Rückgabe:
        {
          "stand": "April 2026",
          "clubs": {
             "<normalisierter name>": {
                "name": "Holzhäusern",
                "mofr18": "90", "mofr9": "50",
                "saso18": "110", "saso9": "60",
                "min_hcp": "54 / 45 (18L)", "holes": "36",
                "weekend_member_only": False,
                "varies": False
             }, ...
          }
        }
    """
    import pdfplumber

    clubs: dict[str, dict] = {}
    stand = ""

    # Wertespalten liegen je nach Zeilenschattierung leicht verschoben; wir
    # holen jede Grösse aus einem kleinen Index-Bereich (erste nicht-leere).
    RANGES = {
        "mofr18": (3, 4), "mofr9": (6, 7),
        "saso18": (9, 10), "saso9": (12, 13),
        "hcp": (15, 16), "loch": (18, 19),
    }
    SKIP = ("GOLFCLUB", "MIGROS GOLFCARD", "MIN. HCP", "MIN HCP", "TOTAL",
            "LÖCHER", "MO-FR", "SA-SO", "18 \u2013 LOCH", "9 \u2013 LOCH",
            "9 - LOCH", "NUR MIT MITGLIED")

    def is_skip(nm: str) -> bool:
        up = nm.upper()
        return ("\u25cf" in nm or len(nm) > 34
                or any(up.startswith(s) or up == s for s in SKIP))

    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            if not stand:
                txt = page.extract_text() or ""
                m = re.search(r"Stand\s+([A-Za-zäöü]+\s+\d{4})", txt)
                if m:
                    stand = m.group(1)

            for table in page.extract_tables():
                last_key = None
                for raw in table:
                    cells = [_clean_cell(c) for c in raw]
                    if len(cells) < 20:
                        continue
                    name = " ".join(cells[i] for i in (0, 1, 2)
                                    if i < len(cells) and cells[i])
                    name = name.replace("\n", " ").strip()
                    if not name:
                        continue

                    def pick(rng):
                        for i in rng:
                            if i < len(cells) and cells[i]:
                                return cells[i]
                        return ""

                    vals = {k: pick(r) for k, r in RANGES.items()}
                    has_values = any(ch.isdigit() for ch in vals["loch"])

                    if not has_values:
                        # Fortsetzungszeile eines mehrzeiligen Namens anhängen.
                        if last_key and not is_skip(name):
                            rec = clubs.pop(last_key)
                            rec["name"] = (rec["name"] + " " + name).strip()
                            last_key = normalize(rec["name"])
                            clubs[last_key] = rec
                        continue

                    if is_skip(name):
                        continue

                    varies = any("\u25cf" in v and _has_price(v) for v in
                                 (vals["mofr18"], vals["mofr9"],
                                  vals["saso18"], vals["saso9"]))
                    rec = {
                        "name": name,
                        "mofr18": vals["mofr18"], "mofr9": vals["mofr9"],
                        "saso18": vals["saso18"], "saso9": vals["saso9"],
                        "min_hcp": vals["hcp"], "holes": vals["loch"],
                        "weekend_member_only": _weekend_member_only(
                            vals["saso18"], vals["saso9"]),
                        "varies": varies,
                    }
                    last_key = normalize(name)
                    clubs[last_key] = rec

    return {"stand": stand or "unbekannt", "clubs": clubs}


# ---------------------------------------------------------------------------
# JSON laden/speichern
# ---------------------------------------------------------------------------

def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find_migros_course(course_name: str, clubs: dict) -> dict | None:
    """Sucht zu einem PC-Caddie-Platznamen den passenden Migros-Eintrag."""
    key = normalize(course_name)
    if not key:
        return None
    if key in clubs:
        return clubs[key]
    ctoks = set(key.split())
    # 1) Token-Teilmenge (z.B. "engelberg titlis" enthaelt "engelberg").
    for nname, info in clubs.items():
        mtoks = set(nname.split())
        if mtoks and (ctoks <= mtoks or mtoks <= ctoks):
            return info
    # 2) Praefix-Abgleich (z.B. "sempachersee" <-> "sempach"). Nur fuer
    #    laengere Namen, um kurze Fehltreffer zu vermeiden.
    for nname, info in clubs.items():
        a, b = key.replace(" ", ""), nname.replace(" ", "")
        short = a if len(a) <= len(b) else b
        if len(short) >= 5 and (a.startswith(b) or b.startswith(a)):
            return info
    return None


def find_pdf_in_dir(folder: str) -> str | None:
    """Findet eine Migros-Greenfee-PDF im Ordner (neueste zuerst).

    Sucht nach Dateinamen, die nach der Migros-Tarifliste aussehen
    (enthalten 'greenfee' oder 'migros'), nimmt sonst irgendeine PDF.
    """
    import glob
    import os

    pdfs = glob.glob(os.path.join(folder, "*.pdf"))
    if not pdfs:
        return None
    # Bevorzugt nach Greenfee/Migros benannte, neueste Datei.
    def score(path: str) -> tuple:
        base = os.path.basename(path).lower()
        named = ("greenfee" in base) or ("migros" in base)
        return (named, os.path.getmtime(path))
    pdfs.sort(key=score, reverse=True)
    return pdfs[0]
