@echo off
chcp 65001 >nul
echo ========================================================
echo         Optivmaf - Hajautettu Worker (Peli-PC)
echo ========================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [VIRHE] Pythonia ei loytynyt PATH-polusta!
    echo Asenna Python 3.10+ osoitteesta https://www.python.org/ tai Windows Storesta.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo [VAROITUS] FFmpegiä ei loytynyt PATH-polusta!
    echo Voit asentaa sen komentoriviltä komennolla: winget install Gyan.FFmpeg
    echo.
)

if not exist node_config.json (
    if exist node_config.example.json (
        echo [INFO] Luodaan node_config.json esimerkkipohjasta...
        copy node_config.example.json node_config.json >nul
        echo MUOKKAA node_config.json -tiedostoon oman koneesi verkkolevyjen kirjaimet ennen kayttoa!
        echo.
    )
)

python worker.py
pause
