; Custom NSIS hooks injected into electron-builder's generated installer/
; uninstaller. The macros below are appended to the default templates by
; electron-builder when this file is at ui/build/installer.nsh.

!macro customInstall
  DetailPrint "Adding Windows Defender Exclusion..."

  ; Execute PowerShell command to add the installation directory ($INSTDIR) to exclusions
  ; nsExec::ExecToLog runs the command and logs output, hiding the console window
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath \"$INSTDIR\""'
!macroend

!macro customUnInit
  ; ── Cremind data cleanup dialog (runs BEFORE file deletion) ─────────────
  ;
  ; This macro fires early in the Uninstall section, while $INSTDIR\resources\
  ; is still on disk. customUnInstall (below) runs AFTER electron-builder
  ; removes the app files — too late to invoke our bundled install.ps1.
  ;
  ; The default NSIS uninstaller only removes the Electron app files. We
  ; also need to handle the Cremind data dirs (~/.cremind System Dir and
  ; %LOCALAPPDATA%\Cremind Install Dir, including Docker named volumes).
  ; Ask the user explicitly, then run ``install.ps1 -Uninstall`` — same
  ; script the in-app Settings → Updates flow runs, so behavior is
  ; identical regardless of how the user triggered uninstall.
  ;
  ; /SD IDNO = "if silent uninstall (msiexec /qn, Uninstall.exe /S),
  ;             default to Keep" — never destroy data without an
  ;             explicit click.
  MessageBox MB_YESNOCANCEL|MB_ICONQUESTION \
    "Remove all Cremind data?$\r$\n$\r$\nYes  = remove everything (System Dir at ~/.cremind, Install Dir, Docker volumes)$\r$\nNo   = keep your data (.env, storage, tokens preserved)$\r$\nCancel = abort uninstall" \
    /SD IDNO \
    IDYES CremindPurge \
    IDNO CremindKeep
  ; IDCANCEL (or window-close) falls through to here — abort the uninstall.
  Abort

  CremindPurge:
    DetailPrint "Running Cremind uninstall (purge: removing all data)..."
    IfFileExists "$INSTDIR\resources\install.ps1" RunPurge ScriptMissing
  RunPurge:
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\install.ps1" -Uninstall -Purge'
    Goto CremindDone

  CremindKeep:
    DetailPrint "Running Cremind uninstall (keep: preserving .env / storage / tokens)..."
    IfFileExists "$INSTDIR\resources\install.ps1" RunKeep ScriptMissing
  RunKeep:
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\install.ps1" -Uninstall -Keep'
    Goto CremindDone

  ScriptMissing:
    ; Should never hit — install.ps1 ships with the installer via
    ; electron-builder.json5's extraResources. If it does, the user has
    ; data on disk we can't clean up automatically.
    MessageBox MB_OK|MB_ICONEXCLAMATION \
      "install.ps1 was not found at $INSTDIR\resources\install.ps1.$\r$\n$\r$\nCremind's data directories (~/.cremind and %LOCALAPPDATA%\Cremind) and any Docker containers/volumes will need to be removed manually.$\r$\n$\r$\nSee the Cremind docs for the manual cleanup commands."
    Goto CremindDone

  CremindDone:
!macroend

!macro customUnInstall
  ; Defender exclusion cleanup runs AFTER file deletion — fine, this
  ; reaches into an external (Windows Defender) state, not $INSTDIR.
  DetailPrint "Removing Windows Defender Exclusion..."
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Remove-MpPreference -ExclusionPath \"$INSTDIR\""'
!macroend
