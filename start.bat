@echo off
setlocal
cd /d "%~dp0"

rem Port par defaut : 4040. Surcharge possible : start.bat 8000
set PORT=%1
if "%PORT%"=="" set PORT=4040

set PYTHON=.venv\Scripts\python.exe

echo.
echo === AI Recruitment Assistant ===
echo.

if not exist "%PYTHON%" (
    echo [1/4] Creation de l'environnement virtuel...
    py -3.11 -m venv .venv
    if errorlevel 1 (
        echo ERREUR : Python 3.11 introuvable. Installe-le puis relance.
        pause
        exit /b 1
    )
    "%PYTHON%" -m pip install --upgrade pip --quiet
    "%PYTHON%" -m pip install -e ".[dev]" --quiet
    if errorlevel 1 (
        echo ERREUR : installation des dependances echouee.
        pause
        exit /b 1
    )
) else (
    echo [1/4] Environnement virtuel present.
)

if not exist ".env" (
    echo [2/4] Creation du fichier .env depuis .env.example...
    copy /y ".env.example" ".env" >nul
) else (
    echo [2/4] Fichier .env present.
)

echo [3/4] Application des migrations...
"%PYTHON%" manage.py migrate --noinput
if errorlevel 1 (
    echo ERREUR : migrations en echec.
    pause
    exit /b 1
)

echo [4/4] Demarrage du serveur sur le port %PORT%...
echo.
echo     http://127.0.0.1:%PORT%/
echo     Compte de demonstration : recruteur / demo-recrutement-2026
echo     (creer les donnees : %PYTHON% manage.py seed_demo)
echo.
echo     Ctrl+C pour arreter.
echo.

"%PYTHON%" manage.py runserver %PORT%

endlocal
