@echo off
rem Abre o programa direto pelo codigo-fonte (sem compilar), com o console de depuracao.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Rode o build.bat uma vez antes para criar o ambiente.
    pause & exit /b 1
)
".venv\Scripts\python.exe" app.py --debug
