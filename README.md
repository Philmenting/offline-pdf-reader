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

Optional – Office↔PDF conversion (Datei → „Office-Dokument öffnen" / „Herunterladen als Word"):
- Requires **LibreOffice** (`soffice`/`libreoffice`) installed or bundled.
  - Ubuntu/Debian: `sudo apt install libreoffice`
  - macOS (brew): `brew install --cask libreoffice`
  - Windows: install LibreOffice (the app auto-detects the default install path).
- For higher-quality PDF→DOCX text flow, optionally `pip install pdf2docx`.

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
- Batch-Rename für Ordner mit Dry-Run-Tabellenvorschau (Status/Altname/Neuer Name/Confidence/Quelle/Grund), farblicher Confidence-Markierung, standardmäßiger Confidence-Sortierung, sortierbaren Spalten, Konflikt-Policy (`(1)`, `(2)` oder überspringen), optional „nur sichere Vorschläge“ sowie Lauf-Statistik (Preview/Umbenannt/Unverändert/Skip-Gründe) in Ergebnis + Log
- Export (TXT/JSON/CSV) pro Datei und als Ordner-Aggregat auf gemeinsamem Datenmodell
- Remove empty/blank pages into a new PDF

## Windows-Test (portable, OCR ohne Extra-Installation)
1. Öffne: `Actions` Tab im Repo
2. Wähle Workflow **Build Windows EXE**
3. Klicke **Run workflow** (führt vor dem Build auch `validate_helpers.py` + `validate_parser_regression.py` aus)
4. Prüfe im Workflow-Run die **Validation checks** + **Windows smoke checks** im *Step Summary* (inkl. `tessdata` mit `deu`/`eng`)
5. Lade unter **Artifacts** (falls Quota verfügbar):
   - `offline-pdf-reader-windows-portable` (ZIP)
   - `offline-pdf-reader-windows-smoketest-report` (vorausgefüllter Testreport)
   
   Falls Artifact-Quota erreicht ist, bleibt der Workflow trotzdem nutzbar (Nightly-Release-Asset wird weiterhin veröffentlicht).
6. ZIP entpacken und `offline-pdf-reader.exe` im Ordner `offline-pdf-reader/` starten

Hinweis: Tesseract wird im Windows-Build mitgebündelt. Fehlende Sprachdaten (`deu`/`eng`) werden im CI-Build automatisch ergänzt. Für OCR ist daher keine separate Installation nötig.

### 5-Punkte Smoke-Testprotokoll (Windows)
1. **Start**: App startet ohne Fehlermeldung; ein PDF lässt sich öffnen.
2. **Viewer**: Seitenwechsel + Zoom + Suche funktionieren (mind. 1 Treffer anspringen).
3. **OCR**: "OCR alle" für ein Mehrseiten-PDF ausführen; Fortschritt + Abschlussmeldung prüfen.
4. **Rename**: Batch-Rename-Vorschau zeigt Tabelle mit Confidence/Quelle/Grund; Dry-Run abbrechen und einmal bestätigen.
5. **Export**: TXT/JSON/CSV jeweils einmal erzeugen (Einzeldatei oder Ordner-Aggregat).

Für echte Run-Dokumentation: siehe `WINDOWS_SMOKETEST.md` (ausfüllbares Report-Template).

## Lightweight validation
```bash
./scripts/run_local_checks.sh
```

Optional (strict wie CI):
```bash
VALIDATION_STRICT=1 ./scripts/run_local_checks.sh
```

Die Einzelchecks funktionieren weiterhin auch direkt:
```bash
python3 validate_helpers.py
python3 validate_parser_regression.py
```

Hinweis: `validate_helpers.py` prüft bewusst robust auf Kernsignale (Datum/Nummer/Filename-Struktur), damit kleine Parser-Normalisierungen keinen False-Alarm auslösen.

Hinweis: Falls lokale Runtime-Abhängigkeiten fehlen (z. B. `fitz`/PyMuPDF), melden `validate_helpers.py` und `validate_parser_regression.py` lokal jeweils `SKIPPED` statt hart zu fehlschlagen. In CI laufen die Checks im Strict-Mode (`VALIDATION_STRICT=1`) und schlagen bei fehlenden Abhängigkeiten fehl.

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
