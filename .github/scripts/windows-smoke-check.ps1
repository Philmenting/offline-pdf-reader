$ErrorActionPreference = "Stop"

$exePath = "dist/offline-pdf-reader/offline-pdf-reader.exe"
$zipPath = "offline-pdf-reader-windows-portable.zip"

if (!(Test-Path $exePath)) {
  throw "Smoke check failed: missing EXE at '$exePath'"
}

# PyInstaller onedir layouts can vary across versions (e.g. root/ or _internal/).
$tesseractCandidates = Get-ChildItem -Path "dist/offline-pdf-reader" -Filter "tesseract.exe" -File -Recurse -ErrorAction SilentlyContinue
if (!$tesseractCandidates -or $tesseractCandidates.Count -lt 1) {
  throw "Smoke check failed: missing bundled Tesseract executable under dist/offline-pdf-reader"
}
$tesseractPath = $tesseractCandidates[0].FullName

if (!(Test-Path $zipPath)) {
  throw "Smoke check failed: missing portable ZIP at '$zipPath'"
}

Write-Host "Smoke checks passed:"
Write-Host "- EXE exists: $exePath"
Write-Host "- Bundled OCR exists: $tesseractPath"
Write-Host "- ZIP exists: $zipPath"

if ($env:GITHUB_STEP_SUMMARY) {
  Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value "## Windows smoke checks"
  Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value "- EXE vorhanden: $exePath"
  Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value "- Bundled OCR gefunden: $tesseractPath"
  Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value "- ZIP vorhanden: $zipPath"
}
