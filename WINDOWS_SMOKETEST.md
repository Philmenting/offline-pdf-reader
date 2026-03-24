# Windows Smoke-Test Report (Template)

Use this file after each **Build Windows EXE** workflow run to document real test evidence.

## Run metadata
- Date/time (Europe/Berlin):
- Commit SHA:
- Workflow run URL:
- Artifact downloaded: `offline-pdf-reader-windows-portable`

## CI checks (from Step Summary)
- [ ] Validation checks passed (`validate_helpers.py`, `validate_parser_regression.py`)
- [ ] Windows smoke checks passed (EXE + bundled Tesseract + ZIP)
- [ ] Artifact summary present

## Manual 5-point smoke test (Windows)
1. Start
   - [ ] App starts without error
   - Notes:
2. Viewer
   - [ ] Page navigation + zoom + search hit jump works
   - Notes:
3. OCR
   - [ ] Multi-page OCR run completed with progress + result dialog
   - Notes:
4. Rename
   - [ ] Batch-rename preview shows Status/Confidence/Source/Reason
   - [ ] Dry-run cancel + confirm path verified
   - Notes:
5. Export
   - [ ] TXT/JSON/CSV export generated
   - Notes:

## Result
- Overall: [ ] PASS / [ ] FAIL
- Blocking issues:
- Follow-up actions:
