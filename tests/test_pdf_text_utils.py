"""Tests für die reinen Hilfsfunktionen aus ``pdf_text_utils``.

Diese Tests benötigen weder PySide6 noch PyMuPDF und laufen daher überall.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdf_text_utils import base14_fontcode, detected_fontcode, star_points


def test_base14_fontcode_styles():
    assert base14_fontcode("Helvetica", False, False) == "helv"
    assert base14_fontcode("Helvetica", True, False) == "hebo"
    assert base14_fontcode("Helvetica", False, True) == "heit"
    assert base14_fontcode("Helvetica", True, True) == "hebi"
    assert base14_fontcode("Times", True, False) == "tibo"
    assert base14_fontcode("Courier", False, True) == "coit"


def test_base14_fontcode_unknown_family_falls_back_to_helvetica():
    assert base14_fontcode("Comic Sans", False, False) == "helv"
    assert base14_fontcode("", True, True) == "hebi"


def test_detected_fontcode_from_flags():
    # Bit 4 = bold, Bit 1 = italic.
    assert detected_fontcode("ArialMT", 0) == "helv"
    assert detected_fontcode("ArialMT", 16) == "hebo"
    assert detected_fontcode("ArialMT", 2) == "heit"
    assert detected_fontcode("ArialMT", 18) == "hebi"


def test_detected_fontcode_from_name():
    assert detected_fontcode("Times New Roman", 0) == "tiro"
    assert detected_fontcode("Times-Bold", 0) == "tibo"
    assert detected_fontcode("CourierNew", 0) == "cour"
    assert detected_fontcode("Helvetica-Oblique", 0) == "heit"


def test_star_points_count_and_bounds():
    pts = star_points(0, 0, 100, 100)
    assert len(pts) == 10
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    # Alle Punkte liegen innerhalb des Begrenzungsrechtecks.
    assert min(xs) >= -0.001 and max(xs) <= 100.001
    assert min(ys) >= -0.001 and max(ys) <= 100.001


def test_star_points_first_tip_is_top_center():
    pts = star_points(0, 0, 100, 100)
    # Erster Punkt ist die obere Spitze: x mittig, y am oberen Rand.
    assert math.isclose(pts[0][0], 50.0, abs_tol=0.001)
    assert math.isclose(pts[0][1], 0.0, abs_tol=0.001)
