# Offline PDF Reader (MVP)

MIT-licensed, offline-first PDF tool inspired by workflows from projects like Stirling-PDF.

Open-source desktop PDF reader that works **fully offline** with:

- PDF viewing
- Local OCR extraction
- Automatic filename suggestion from document content

## Tech
- Python 3.11+
- PySide6 (GUI)
- PyMuPDF (PDF rendering + text extraction)
- pytesseract + Pillow (OCR)

## Offline guarantee
This app performs all processing locally and does not require cloud services.

## License
- Project license: MIT (`LICENSE`)
- Third-party attribution policy: `ATTRIBUTION.md`
- Distribution notice: `NOTICE`

## Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install Tesseract (required for OCR):
- Ubuntu/Debian: `sudo apt install tesseract-ocr`
- macOS (brew): `brew install tesseract`
- Windows: install Tesseract and ensure `tesseract.exe` is in PATH.

## Run
```bash
python app.py
```

## Features in this MVP
- Open a PDF
- Multi-page navigation (buttons, keyboard, mouse wheel, Go-to-page)
- Zoom controls (+/−/reset) and page rotation
- Suche über alle Seiten mit Trefferliste, klickbaren Treffern und Trefferzähler (z. B. 3/18)
- Extrakt/OCR mit Fortschritt pro Seite, Abbruchbutton und robustem Fehlerverhalten (Einzelseitenfehler stoppen nicht den gesamten Lauf)
- Ein-Klick-Flow „OCR + Dateinamen vorschlagen“ (Toolbar + Menü)
- One-click: durchsuchbare PDF-Kopie direkt aus OCR erzeugen (lokal, ohne Cloud)
- Extract text (native PDF text, fallback OCR)
- Force OCR for all pages via dedicated button
- Suggest filename from priorisierten Feldern `Datum_Typ_Absender_Nummer` mit konsistenter Normalisierung
- Save-as with suggested name
- PDF merge, page extraction, page reordering
- Seiten im Thumbnail-Bereich per Delete-Taste oder Kontextmenü löschen (inkl. Mehrfach-Confirm)
- Batch-Rename für Ordner mit Dry-Run-Tabellenvorschau (Status/Altname/Neuer Name/Confidence/Quelle/Grund), farblicher Confidence-Markierung, sortierbaren Spalten, Konflikt-Policy (`(1)`, `(2)` oder überspringen) und optional „nur sichere Vorschläge“
- Export (TXT/JSON/CSV) pro Datei und als Ordner-Aggregat auf gemeinsamem Datenmodell
- Remove empty/blank pages into a new PDF

## Windows-Test (portable, OCR ohne Extra-Installation)
1. Öffne: `Actions` Tab im Repo
2. Wähle Workflow **Build Windows EXE**
3. Klicke **Run workflow** (führt vor dem Build auch `validate_helpers.py` + `validate_parser_regression.py` aus)
4. Prüfe im Workflow-Run die **Windows smoke checks** (EXE + bundled Tesseract + ZIP)
5. Nach Abschluss unter **Artifacts** `offline-pdf-reader-windows-portable` laden
6. ZIP entpacken und `offline-pdf-reader.exe` im Ordner `offline-pdf-reader/` starten

Hinweis: Tesseract wird im Windows-Build mitgebündelt. Für OCR ist daher keine separate Installation nötig.

### 5-Punkte Smoke-Testprotokoll (Windows)
1. **Start**: App startet ohne Fehlermeldung; ein PDF lässt sich öffnen.
2. **Viewer**: Seitenwechsel + Zoom + Suche funktionieren (mind. 1 Treffer anspringen).
3. **OCR**: "OCR alle" für ein Mehrseiten-PDF ausführen; Fortschritt + Abschlussmeldung prüfen.
4. **Rename**: Batch-Rename-Vorschau zeigt Tabelle mit Confidence/Quelle/Grund; Dry-Run abbrechen und einmal bestätigen.
5. **Export**: TXT/JSON/CSV jeweils einmal erzeugen (Einzeldatei oder Ordner-Aggregat).

## Lightweight validation
```bash
python3 validate_helpers.py
python3 validate_parser_regression.py
```

## Manuelle Test-Notizen
- Suche: Begriff eingeben, Trefferliste prüfen, mit Treffer ◀/▶ navigieren, Sprung und Hervorhebung prüfen.
- OCR-Lauf: OCR alle starten, Abbrechen klicken, danach erneut starten (Retry-Pfad).
- Thumbnail-Löschen: Mehrfachauswahl im linken Bereich, Delete drücken oder Rechtsklick -> Löschen.
- Batch-Rename: Ordner auswählen, Vorschau prüfen, dann explizit bestätigen.
- Export: Einzeldatei-Export und Ordner-Aggregat jeweils in TXT/JSON/CSV erzeugen.

## Next steps
- OCR confidence highlighting direkt in der Seitenansicht (Overlay)
- Verbesserte Korrekturvorschläge für Datum/Betrag/Nummer
- OCR-Export optional mit Originallayout überlageren (statt reinem OCR-Seitenbild)
