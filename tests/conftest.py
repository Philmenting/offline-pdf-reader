"""Gemeinsame Test-Konfiguration.

Setzt eine Offscreen-Qt-Plattform, damit GUI-abhängige Tests (sofern
PySide6 installiert ist) auch ohne Display laufen. Reine Logik-Tests
(z. B. ``test_pdf_text_utils``) benötigen das nicht.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
