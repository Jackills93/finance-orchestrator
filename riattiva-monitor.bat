@echo off
title Riattiva Monitor Finance

REM --- Auto-elevazione: modificare un task pianificato richiede i privilegi admin ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Richiesta privilegi di amministratore...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Riattivazione monitor automatico SL/TP...
schtasks /Change /TN "FinanceOrchestratorMonitor" /ENABLE >nul 2>&1
if %errorlevel% equ 0 (
    echo    -^> Monitor riattivato: tornera' a girare ogni 5 minuti.
) else (
    echo    -^> Errore: task "FinanceOrchestratorMonitor" non trovato.
)
echo.
pause
