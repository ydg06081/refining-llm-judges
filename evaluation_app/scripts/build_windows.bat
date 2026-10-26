@echo off
setlocal

cd /d "%~dp0\.."

:: --- Pre-flight: fail in seconds, not after a full PyInstaller run ---
:: The evaluation dataset must be in data\ before building. The repository ships
:: a synthetic demo set (data\sample_dataset.csv); replace it with your own CSV
:: (same columns) for a real study.
if not exist "data\sample_dataset.csv" (
  echo.
  echo ERROR: data\sample_dataset.csv not found.
  echo.
  echo   Place the evaluation dataset at:
  echo.
  echo     %cd%\data\sample_dataset.csv
  echo.
  echo   or regenerate the synthetic demo set with:
  echo     python scripts\make_sample_dataset.py
  echo.
  exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: 'python' is not on PATH.
  echo   Install Python 3.10 or newer from python.org and tick
  echo   "Add python.exe to PATH" during setup, then reopen this window.
  echo.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)

.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe -m PyInstaller doctor-eval.spec --clean --noconfirm

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

if not exist "dist\doctor-eval\storage" mkdir "dist\doctor-eval\storage"
if not exist "dist\doctor-eval\exports" mkdir "dist\doctor-eval\exports"
if not exist "dist\doctor-eval\data" mkdir "dist\doctor-eval\data"

:: Ship ONLY the three columns the app reads. A source CSV may carry extra
:: columns (identifiers, demographics, dates) the app never uses; copying the
:: whole data\ folder would put all of that on every evaluator's laptop.
:: make_dist_csv trims it (and fails the build if a required column is missing,
:: so we never ship a broken or demo-only dataset).
.venv\Scripts\python.exe scripts\make_dist_csv.py ^
  "data\sample_dataset.csv" ^
  "dist\doctor-eval\data\sample_dataset.csv"
if errorlevel 1 (
  echo Dataset preparation failed.
  exit /b 1
)

:: Guard against shipping a demo-only build: the dataset MUST be present, or
:: evaluators silently get 4 placeholder samples.
if not exist "dist\doctor-eval\data\sample_dataset.csv" (
  echo ERROR: evaluation dataset missing from dist. Aborting.
  exit /b 1
)

:: Never ship a database: a post-build test launch would leave test evaluations
:: and the tester's admin account inside it. A fresh folder starts with no
:: accounts and asks for the admin ID/password on first launch.
if exist "dist\doctor-eval\storage\doctor_eval.db" del /q "dist\doctor-eval\storage\doctor_eval.db"

:: --- Optional Authenticode code signing ---
:: To sign, set ONE of these before running this script:
::   set "SIGN_PFX=C:\path\to\codesign.pfx"  &  set "SIGN_PFX_PASS=your-pfx-password"
::   set "SIGN_THUMBPRINT=<SHA1 thumbprint of a cert in your Windows cert store>"
:: Optionally override the RFC3161 timestamp server:
::   set "SIGN_TIMESTAMP=http://timestamp.sectigo.com"
:: signtool.exe must be on PATH (run from a Developer Command Prompt / Windows SDK).
if "%SIGN_TIMESTAMP%"=="" set "SIGN_TIMESTAMP=http://timestamp.digicert.com"
set "SIGN_TARGET=dist\doctor-eval\doctor-eval.exe"

if defined SIGN_PFX (
  echo Signing %SIGN_TARGET% with PFX...
  signtool sign /fd SHA256 /tr "%SIGN_TIMESTAMP%" /td SHA256 /f "%SIGN_PFX%" /p "%SIGN_PFX_PASS%" "%SIGN_TARGET%"
  if errorlevel 1 (
    echo Signing failed.
    exit /b 1
  )
  signtool verify /pa "%SIGN_TARGET%"
) else if defined SIGN_THUMBPRINT (
  echo Signing %SIGN_TARGET% with cert-store thumbprint...
  signtool sign /fd SHA256 /tr "%SIGN_TIMESTAMP%" /td SHA256 /sha1 "%SIGN_THUMBPRINT%" "%SIGN_TARGET%"
  if errorlevel 1 (
    echo Signing failed.
    exit /b 1
  )
  signtool verify /pa "%SIGN_TARGET%"
) else (
  echo No signing certificate set ^(SIGN_PFX or SIGN_THUMBPRINT^); skipping code signing.
)

echo.
echo Portable folder ready:
echo %cd%\dist\doctor-eval
echo.
echo Give the whole dist\doctor-eval folder to evaluators.
echo.
echo ACCOUNTS: the folder ships with no accounts. On first launch the app asks
echo   for an admin ID and password; the admin then creates evaluator accounts
echo   ^(ID, name, password^) from the dashboard. To hand out a ready-to-use
echo   folder, launch it once yourself, create the accounts, then copy it.
echo.
echo UPGRADING an evaluator who already has evaluations:
echo   copy EVERYTHING from dist\doctor-eval EXCEPT storage\ over their
echo   existing folder, overwriting. Their storage\ holds every saved
echo   evaluation -- replacing it deletes their work.
