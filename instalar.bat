@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto erro
".venv\Scripts\python.exe" -m pip install --no-cache-dir --no-compile -r requirements-ai.txt
if errorlevel 1 goto erro
echo.
echo Instalacao concluida. Abra iniciar.bat.
pause
exit /b 0
:erro
echo Falha na instalacao. Confira a mensagem acima. Python 3.11 a 3.13 recomendado.
pause
exit /b 1
