@echo off
title Ferma Finance Orchestrator

REM --- Auto-elevazione: disattivare un task pianificato richiede i privilegi admin ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Richiesta privilegi di amministratore...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ==========================================
echo    Arresto Finance Orchestrator
echo ==========================================
echo.

REM --- 1. Chiude la dashboard (server Flask app.py, porta 5000) ---
echo [1/2] Chiusura dashboard (app.py)...
powershell -NoProfile -Command "$p = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'app\.py' }); if ($p.Count -gt 0) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Host ('    -> Dashboard chiusa (' + $p.Count + ' processo/i).') } else { Write-Host '    -> Nessuna dashboard in esecuzione.' }"
echo.

REM --- 2. Disattiva il monitor automatico (task pianificato ogni 5 minuti) ---
echo [2/2] Disattivazione monitor automatico...
schtasks /End    /TN "FinanceOrchestratorMonitor" >nul 2>&1
schtasks /Change /TN "FinanceOrchestratorMonitor" /DISABLE >nul 2>&1
if %errorlevel% equ 0 (
    echo     -^> Monitor disattivato: non partira' piu' all'avvio del PC.
) else (
    echo     -^> Monitor gia' disattivato o non trovato.
)
echo.

echo ==========================================
echo    Sistema fermato.
echo ==========================================
echo.
echo Gli alert SL/TP su Telegram sono ora SOSPESI.
echo Per riaccenderli:  esegui  riattiva-monitor.bat
echo.
pause
