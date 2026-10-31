@echo off
cd /d "%~dp0"
python win_music_bridge.py %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Ha ocurrido un error o falta instalar dependencias.
    echo Intentando instalar dependencias...
    pip install -r requirements.txt
    echo.
    echo Reintentando ejecutar el bridge...
    python win_music_bridge.py %*
)
pause
