@echo off
rem Xiaoli Armor - status viewer (ASCII only, see the install launcher).
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "HERE=%~dp0"
set "EXE="
for %%F in ("%HERE%*.exe") do if not defined EXE set "EXE=%%~fF"
if defined EXE if not exist "!EXE!" set "EXE="

if defined EXE (
  "!EXE!" --status %*
  set "CODE=!ERRORLEVEL!"
  goto :done
)

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if defined PY (
  %PY% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul
  if errorlevel 1 set "PY="
)
if not defined PY (
  echo [xiaoli-armor] No bundled .exe and no Python 3.11+ found.
  set "CODE=2"
  goto :done
)
%PY% "%HERE%xiaoli.py" --status %*
set "CODE=!ERRORLEVEL!"
:done
if not defined XIAOLI_NO_PAUSE if "%~1"=="" timeout /t 60 >nul 2>&1
endlocal & exit /b %CODE%
