@echo off
REM Build Monitor.exe from Monitor.spec.
REM
REM The spec is the single description of the build. This script used to
REM hand-roll the same job with --onefile/--add-data flags, which is how
REM overlay.py and metrics.py came to be copied next to the exe as inert data
REM files while also being collected properly from their imports. Two
REM descriptions of one build drift; this now just invokes the spec.

setlocal

REM No virtualenv is activated: there is no .venv in this checkout and .venv is
REM gitignored, so a fresh clone has never had one. The system interpreter is
REM what the rest of the project already assumes.
python -m PyInstaller Monitor.spec
if errorlevel 1 (
    echo.
    echo Build failed -- see the errors above.
    exit /b 1
)

if not exist "dist\Monitor.exe" (
    echo.
    echo Build reported success but dist\Monitor.exe is not there.
    exit /b 1
)

echo.
echo Build complete: dist\Monitor.exe

REM Pausing only when a person is watching. An unconditional pause hangs a CI
REM or agent shell forever on a script that has already finished.
if not defined CI if not defined MONITOR_NO_PAUSE pause

endlocal
