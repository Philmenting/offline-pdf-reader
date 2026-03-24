$ErrorActionPreference = "Stop"

$exePath = "dist/offline-pdf-reader/offline-pdf-reader.exe"
$tesseractPath = "dist/offline-pdf-reader/tesseract/tesseract.exe"
$zipPath = "offline-pdf-reader-windows-portable.zip"

if (!(Test-Path $exePath)) {
  throw "Smoke check failed: missing EXE at '$exePath'"
}
if (!(Test-Path $tesseractPath)) {
  throw "Smoke check failed: missing bundled Tesseract at '$tesseractPath'"
}
if (!(Test-Path $zipPath)) {
  throw "Smoke check failed: missing portable ZIP at '$zipPath'"
}

Write-Host "Smoke checks passed:"
Write-Host "- EXE exists: $exePath"
Write-Host "- Bundled OCR exists: $tesseractPath"
Write-Host "- ZIP exists: $zipPath"

if ($env:GITHUB_STEP_SUMMARY) {
  @"
## Windows smoke checks
- ✅ EXE vorhanden (`$exePath`)
- ✅ Bundled OCR vorhanden (`$tesseractPath`)
- ✅ ZIP vorhanden (`$zipPath`)
"@ | Out-File -FilePath $env:GITHUB_STEP_SUMMARY -Append -Encoding utf8
}
