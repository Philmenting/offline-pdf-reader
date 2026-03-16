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
- Extract text (native PDF text, fallback OCR)
- Extract all pages with progress + partial OCR-failure handling
- Force OCR for all pages via dedicated button
- Suggest filename from date/vendor/doc type/number
- Save-as with suggested name
- PDF merge, page extraction, page reordering
- Remove empty/blank pages into a new PDF

## Windows .exe testen (GitHub Actions)
1. Öffne: `Actions` Tab im Repo
2. Wähle Workflow **Build Windows EXE**
3. Klicke **Run workflow**
4. Nach Abschluss findest du unter **Artifacts** die Datei `offline-pdf-reader.exe`

Hinweis: Für OCR muss auf Windows zusätzlich Tesseract installiert sein.

## Next steps
- OCR confidence highlighting direkt in der Seitenansicht (Overlay)
- Verbesserte Korrekturvorschläge für Datum/Betrag/Nummer
- Batch rename folder mode
- One-click OCR to new searchable PDF
