@echo off
chcp 65001 >nul
title Phael Tech Provision - gerar executavel
cd /d "%~dp0"
set "LOG=%~dp0build.log"
echo Inicio %date% %time% > "%LOG%"

echo.
echo  ===  Phael Tech Provision 2 - gerando o Phael Tech Provision.exe  ===
echo.

set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
rem Procura um Python compativel (3.13 > 3.12 > 3.11; o pythonnet ainda pode nao ter suporte ao 3.14)
set "PY="
for %%V in (3.13 3.12 3.11 3.14) do (
    if not defined PY (
        py -%%V -c "import sys" <nul >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys" <nul >nul 2>&1 && set "PY=python"
)
if not defined PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe""
)
if not defined PY (
    echo Python nao encontrado. Instalando o Python 3.13 pelo winget ^(precisa de internet^)...
    echo INSTALANDO_PYTHON >> "%LOG%"
    winget install --id Python.Python.3.13 -e --scope user --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe""
)
if not defined PY (
    echo [ERRO] Python nao encontrado. Instale o Python 3.13 em https://www.python.org/downloads/
    echo        e marque "Add python.exe to PATH" durante a instalacao.
    echo SEM_PYTHON >> "%LOG%"
    pause & exit /b 1
)
echo Usando: %PY%
%PY% --version
%PY% --version >> "%LOG%" 2>&1

if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente virtual...
    %PY% -m venv .venv >> "%LOG%" 2>&1 || goto erro
)
call ".venv\Scripts\activate.bat"

echo Instalando dependencias (pywebview, psutil, pyinstaller)...
python -m pip install --upgrade pip >> "%LOG%" 2>&1
python -m pip install -r requirements.txt >> "%LOG%" 2>&1 || goto erro

echo Gerando executavel (pode levar alguns minutos)...
python -m PyInstaller --noconfirm --clean PhaelTechProvision.spec >> "%LOG%" 2>&1 || goto erro
copy /y "dist\Phael Tech Provision.exe" "..\Phael Tech Provision.exe" >nul
echo SUCESSO >> "%LOG%"

echo.
echo  Pronto!  O arquivo "Phael Tech Provision.exe" foi copiado para a pasta phael tech provision.
echo.
pause
exit /b 0

:erro
echo FALHOU >> "%LOG%"
echo.
echo [ERRO] A geracao falhou. Detalhes em build.log
echo Se o erro mencionar "pythonnet", instale o Python 3.13 e rode o build.bat de novo.
pause
exit /b 1
