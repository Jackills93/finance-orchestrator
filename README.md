# Finance Orchestrator

Sistema multi-agente per la selezione di asset finanziari.
Combina screening, analisi macroeconomica, filtro fondamentale e analisi tecnica
in un pipeline sequenziale orchestrato da Claude, con dashboard web,
journal dei trade e alert automatici su Stop Loss / Take Profit.

## Architettura

```
Screener (opz.)  →  candidati da S&P 500, FTSE MIB, EuroStoxx 50, Russell 2000 (yfinance)
Agente Macro     →  notizie Fed/BCE/inflazione via web search di Claude + VIX
Agente Filtro    →  P/E fwd, P/S, ROE, D/E, EV/EBITDA, P/B, Revenue Growth (yfinance)
Agente Tecnico   →  MA 20/50 sett., MACD, RSI, RS vs indice, sector rotation, ATR
                    (Alpha Vantage per titoli USA, yfinance per Europa e come fallback)
Orchestratore    →  aggiustamento macro, ranking finale e raccomandazione di Claude
```

| File | Ruolo |
|---|---|
| `orchestrator.py` | Pipeline completo da riga di comando |
| `screener_agent.py` | Selezione automatica dei ticker |
| `macro_agent.py` | Analisi macro con score di rischio per settore |
| `filter_agent.py` | Scoring fondamentale e knockout |
| `technical_agent.py` | Scoring tecnico, SL/TP su ATR e position sizing |
| `app.py` | Dashboard Flask (`http://localhost:5000`) |
| `monitor.py` | Controllo SL/TP ogni 5 minuti e alert Telegram |
| `weekly_report.py` | Email riepilogativa del sabato |
| `avvia.bat` / `ferma.bat` / `riattiva-monitor.bat` | Avvio e arresto su Windows |

## Setup

### 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2. Crea il file .env

```
# Obbligatorie
ANTHROPIC_API_KEY=xxxx
ALPHA_VANTAGE_API_KEY=xxxx

# Opzionali: alert Telegram (monitor.py, app.py)
TELEGRAM_BOT_TOKEN=xxxx
TELEGRAM_CHAT_ID=xxxx

# Opzionali: email (report settimanale e fallback alert)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=xxxx
SMTP_PASS=xxxx
NOTIFY_EMAIL=xxxx

# Opzionale: calendario trimestrali nel report settimanale (fallback: yfinance)
FMP_API_KEY=xxxx

# Opzionali: position sizing (capitale vuoto o 0 = sizing disattivato)
PORTFOLIO_CAPITAL=10000
RISK_PER_TRADE_PCT=1
MAX_POSITION_PCT=20
```

Chiavi, preferenze (lingua, valuta), parametri di sizing e password delle
impostazioni si possono modificare anche dal tab **Impostazioni** della dashboard.

Chiavi API gratuite:
- Anthropic: https://console.anthropic.com
- Alpha Vantage: https://www.alphavantage.co (piano free: 25 req/giorno)
- FMP: https://financialmodelingprep.com (piano free: 250 req/giorno)

## Avvio

### Dashboard (modo raccomandato)

```bash
python app.py
```

Su Windows basta `avvia.bat`, che apre anche il browser.

### Sicurezza della dashboard

Di default la dashboard ascolta solo su `127.0.0.1`, quindi è raggiungibile
solo dal PC su cui gira. Inoltre:

- le richieste con un header `Host` diverso da `localhost` / `127.0.0.1` vengono
  rifiutate (protezione dal DNS rebinding);
- le richieste che modificano dati (POST/DELETE) provenienti da pagine di altri
  siti aperte nel browser vengono bloccate (protezione CSRF);
- la password del tab Impostazioni è salvata con hash PBKDF2 e salt (`werkzeug`);
  un vecchio hash SHA-256 viene aggiornato al primo salvataggio delle impostazioni.

Per aprire la dashboard da altri dispositivi della rete locale:

```
APP_HOST=0.0.0.0
APP_ALLOWED_HOSTS=192.168.1.50,nome-pc
APP_PORT=5000
```

In questo caso chiunque sia sulla stessa rete può usare la dashboard,
comprese le funzioni che cancellano trade e storico: imposta una password
nelle Impostazioni e usala solo su reti fidate.

### Da riga di comando

```bash
# Ticker scelti a mano
python orchestrator.py ASML NVDA JPM CAT ENI.MI

# Screening automatico su uno o più mercati
python orchestrator.py --auto-screen --markets sp500 italy europe russell2000

# Screening S&P 500 limitato ad alcuni settori
python orchestrator.py --auto-screen --sectors Technology Industrials --max-per-sector 2

# Refresh macro forzato
python orchestrator.py ASML NVDA JPM --refresh-macro
```

### Singoli agenti (per test)

```bash
python screener_agent.py sp500 italy
python macro_agent.py
python filter_agent.py
python technical_agent.py
```

## Logica di scoring

### Aggiustamento macro — orchestrator.py

Lo score fondamentale viene corretto in base al `risk_score` (1-10) del settore:

```python
MACRO_ADJUSTMENTS = {(1, 3): +5, (4, 6): 0, (7, 8): -5, (9, 10): -10}
```

### Pesi fondamentali per settore — filter_agent.py

```python
SECTOR_WEIGHTS = {
    "Technology":  {"pe_forward": 0.12, "ps_ratio": 0.22, "roe": 0.20,
                    "debt_equity": 0.14, "ev_ebitda": 0.14, "revenue_growth": 0.18},
    "Financials":  {"pb_ratio": 0.32, "roe": 0.28, "debt_equity": 0.10,
                    "revenue_growth": 0.20, "ev_ebitda": 0.10},
    "Industrials": {"pe_forward": 0.18, "debt_equity": 0.22, "ev_ebitda": 0.14,
                    "roe": 0.20, "revenue_growth": 0.26},
}
```

I settori non mappati usano `DEFAULT_WEIGHTS`. Passano all'analisi tecnica
solo i titoli con score ≥ 50 che superano i knockout.

### Soglie knock-out — filter_agent.py

```python
KNOCKOUT_RULES = {
    "Technology":  {"debt_equity_max": 3.0, "revenue_growth_min": -0.15},
    "Financials":  {"revenue_growth_min": -0.15, "pe_forward_max": 25.0},
    "Industrials": {"debt_equity_max": 3.0, "revenue_growth_min": -0.15, "pe_forward_max": 40.0},
}
```

Vengono inoltre esclusi i titoli con trimestrale prevista nei prossimi 7 giorni.

### Pesi indicatori tecnici — technical_agent.py

Timeframe settimanale; benchmark RS coerente con la borsa del titolo
(S&P 500, FTSE MIB o EuroStoxx 50); ETF settoriali SPDR per gli USA e
iShares STOXX Europe 600 per l'Europa.

```python
INDICATOR_WEIGHTS = {
    "ma_crossover":    0.28,   # MA20/MA50 con conferma del volume
    "macd":            0.24,
    "rsi":             0.18,
    "rs_vs_market":    0.15,
    "sector_rotation": 0.15,
}
```

Segnale tecnico: BUY ≥ 70, SELL ≤ 30, altrimenti HOLD (HOLD forzato se ATR < 1%).
Sui BUY viene fatta un'analisi giornaliera per il punto di entrata
(`ENTRY_NOW`, `ENTRY_OK`, `WAIT_PULLBACK`).
SL suggerito = prezzo − 2×ATR, TP suggerito = prezzo + 3×ATR.

### Score composito e segnale finale

```python
# technical_agent.py
COMPOSITE_TECH_WEIGHT = 0.55
COMPOSITE_FUND_WEIGHT = 0.45
```

| Segnale finale | Condizione |
|---|---|
| STRONG_BUY | composito ≥ 70 e segnale tecnico BUY |
| BUY | composito ≥ 60 e segnale tecnico BUY |
| SELL | composito ≤ 30 oppure segnale tecnico SELL |
| HOLD | tutti gli altri casi |

## Output

```
orchestrator_output/run_YYYYMMDD_HHMM.json   ← output completo con timestamp
orchestrator_output/run_latest.json           ← sempre l'ultimo run
screener_reports/screener_latest.json
macro_reports/macro_report_latest.json        ← analisi macro (cache giornaliera)
filter_reports/filter_report_latest.json
technical_reports/technical_report_latest.json
trades.db                                     ← journal trade e segnali di backtest
backups/                                      ← ultimi 20 backup JSON dei trade
orchestrator_run.log, monitor.log, weekly_report.log
```

Ogni segnale del ranking viene registrato nel journal di backtest e risolto
dopo 4 settimane (o alla chiusura del trade) come WIN / LOSS.

## Attività pianificate (Windows)

`monitor.py` va eseguito ogni 5 minuti dal Task Scheduler con nome
`FinanceOrchestratorMonitor` (è il nome usato da `ferma.bat` e `riattiva-monitor.bat`):

```bat
schtasks /Create /TN "FinanceOrchestratorMonitor" /SC MINUTE /MO 5 ^
  /TR "\"<percorso python.exe>\" \"<percorso cartella progetto>\monitor.py\"" /F
```

`weekly_report.py` va eseguito ogni sabato alle 09:00:

```bat
schtasks /Create /TN "FinanceScreenerWeeklyReport" /SC WEEKLY /D SAT /ST 09:00 ^
  /TR "\"<percorso python.exe>\" \"<percorso cartella progetto>\weekly_report.py\"" /F
```

## Note operative

**Rate limit Alpha Vantage (piano free):** 25 req/giorno, 5/minuto.
L'agente tecnico fa 1 chiamata per ticker USA (2 se il segnale è BUY) con pause
di 12s; se il limite è raggiunto attende 60s, riprova e in caso di nuovo
fallimento usa yfinance. I titoli europei usano sempre yfinance.

**Cache macro:** il report macro viene riusato per tutta la giornata.
Usa `--refresh-macro` solo se ci sono eventi significativi intraday (Fed meeting, CPI).

**Calendario FOMC:** le date in `weekly_report.py` (`FOMC_DATES`) vanno aggiornate ogni anno.

**Disclaimer:** i segnali sono generati automaticamente a scopo informativo e
non costituiscono consulenza finanziaria.
