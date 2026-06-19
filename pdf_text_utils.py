"""Reine, GUI- und PyMuPDF-unabhängige Hilfsfunktionen.

Dieses Modul ist bewusst frei von PySide6-/fitz-Importen, damit die Logik
ohne Laufzeitabhängigkeiten getestet werden kann (siehe ``tests/``). Es ist
der erste Schritt, Logik aus dem ``app.py``-Monolithen herauszulösen.
"""

from __future__ import annotations

import math

# PyMuPDF-Base-14-Fontcodes je Familie und Stil (ohne Schriftdatei einbettbar).
_BASE14_TABLES = {
    "helvetica": {(False, False): "helv", (True, False): "hebo", (False, True): "heit", (True, True): "hebi"},
    "times": {(False, False): "tiro", (True, False): "tibo", (False, True): "tiit", (True, True): "tibi"},
    "courier": {(False, False): "cour", (True, False): "cobo", (False, True): "coit", (True, True): "cobi"},
}


def base14_fontcode(family: str, bold: bool, italic: bool) -> str:
    """Liefert den Base-14-Fontcode für eine Familie + Fett/Kursiv.

    Unbekannte Familien fallen auf Helvetica zurück.
    """
    table = _BASE14_TABLES.get((family or "").strip().lower(), _BASE14_TABLES["helvetica"])
    return table[(bool(bold), bool(italic))]


def detected_fontcode(font_name: str, flags: int) -> str:
    """Bildet eine erkannte Schrift (Name + PyMuPDF-Span-Flags) auf einen
    Base-14-Fontcode ab.

    Flags (PyMuPDF): Bit 1 = kursiv, Bit 2 = serif, Bit 3 = monospace,
    Bit 4 = fett.
    """
    name = (font_name or "").lower()
    bold = bool(flags & 16) or any(t in name for t in ("bold", "black", "heavy", "semibold"))
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    mono = bool(flags & 8) or "courier" in name or "mono" in name or "consol" in name
    serif = bool(flags & 4) or any(t in name for t in ("times", "serif", "georgia", "roman", "minion", "garamond"))
    family = "courier" if mono else ("times" if serif else "helvetica")
    return base14_fontcode(family, bold, italic)


def star_points(x0: float, y0: float, x1: float, y1: float, inner_ratio: float = 0.4) -> list[tuple[float, float]]:
    """Berechnet die 10 Eckpunkte eines fünfzackigen Sterns, der in das
    Rechteck (x0, y0, x1, y1) eingepasst ist."""
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    rx = (x1 - x0) / 2.0
    ry = (y1 - y0) / 2.0
    points: list[tuple[float, float]] = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        factor = 1.0 if i % 2 == 0 else inner_ratio
        points.append((cx + rx * factor * math.cos(ang), cy + ry * factor * math.sin(ang)))
    return points
