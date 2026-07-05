; NSIS installer for the Offline PDF Editor (built cross-platform via makensis).
;
; Expects these defines from the command line (see scripts/build-installer-win.mjs):
;   SRCDIR  — the bundled app directory (dist/Offline-PDF-Editor)
;   OUTFILE — output path for the setup executable
;   VERSION — display version, e.g. 0.4.0
;
; Per-user install (no admin/UAC): %LOCALAPPDATA%\Offline-PDF-Editor with
; Start-menu + desktop shortcuts and a proper uninstaller registered in
; Apps & Features (HKCU).

Unicode true
SetCompressor /SOLID lzma

!define APPNAME  "Offline PDF Editor"
!define APPDIR   "Offline-PDF-Editor"
!define EXENAME  "Offline-PDF-Editor.exe"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPDIR}"

Name "${APPNAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\${APPDIR}"
RequestExecutionLevel user
ShowInstDetails show
ShowUnInstDetails show

; ── Pages ────────────────────────────────────────────────────────────────
Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

; ── Install ──────────────────────────────────────────────────────────────
Section "Install"
  SetOutPath "$INSTDIR"
  File /r "${SRCDIR}\*"

  ; uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; shortcuts
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${EXENAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME} deinstallieren.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXENAME}"

  ; Apps & Features entry (per-user)
  WriteRegStr   HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr   HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr   HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\${EXENAME}"
  WriteRegStr   HKCU "${UNINSTKEY}" "Publisher" "offline-pdf-editor (AGPL-3.0)"
  WriteRegStr   HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
SectionEnd

; ── Uninstall ────────────────────────────────────────────────────────────
Section "Uninstall"
  ; the app writes its diagnostic log next to the exe; remove everything
  RMDir /r "$INSTDIR"

  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME} deinstallieren.lnk"
  RMDir  "$SMPROGRAMS\${APPNAME}"
  Delete "$DESKTOP\${APPNAME}.lnk"

  DeleteRegKey HKCU "${UNINSTKEY}"
SectionEnd
