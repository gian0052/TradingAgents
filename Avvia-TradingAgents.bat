@echo off
setlocal

cd /d "%~dp0"
title TradingAgents - APEX/TurboQuant

echo.
echo ==========================================
echo   TradingAgents - avvio locale PowerShell
echo ==========================================
echo.

if not exist ".venv\Scripts\tradingagents.exe" (
    echo ERRORE: non trovo .venv\Scripts\tradingagents.exe
    echo.
    echo Apri PowerShell in questa cartella e reinstalla il progetto:
    echo   .\.venv\Scripts\python.exe -m pip install .
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo ERRORE: file .env non trovato.
    echo Copia .env.example in .env e configura APEX/Binance Testnet.
    echo.
    pause
    exit /b 1
)

:check_apex
echo Controllo endpoint APEX/TurboQuant: http://localhost:8080/v1/models
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-RestMethod -Uri 'http://localhost:8080/v1/models' -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
    echo.
    echo ATTENZIONE: APEX/TurboQuant non risponde su http://localhost:8080/v1
    echo Avvia APEX, carica il modello locale, poi premi un tasto per ricontrollare.
    echo Per annullare usa CTRL+C.
    echo.
    pause
    goto check_apex
)

echo.
echo Avvio TradingAgents...
echo.
".venv\Scripts\tradingagents.exe"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo TradingAgents e' terminato con codice %EXIT_CODE%.
) else (
    echo TradingAgents terminato.
)
echo.
pause
exit /b %EXIT_CODE%
