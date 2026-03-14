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
- Suggest filename from date/vendor/doc type/number
- Save-as with suggested name
- PDF merge, page extraction, and page reordering

## Next steps
- OCR confidence highlighting + correction suggestions
- Local learning rules from user corrections
- Batch rename folder mode
- One-click OCR to new searchable PDF
- Windows test build (.exe) workflow
