# Finance Orchestrator

Sistema multi-agente per la selezione di asset finanziari.
Combina analisi macroeconomica, filtro fondamentale e analisi tecnica
in un pipeline sequenziale orchestrato da Claude.

## Architettura

```
Agente Macro     →  notizie Fed/BCE/inflazione via web search
Agente Filtro    →  P/E, P/S, ROE, D/E, EV/EBITDA, Revenue Growth (FMP)
Agente Tecnico   →  MA 50/200, RSI 14, MACD 12/26/9, ATR 14 (Alpha Vantage)
Orchestratore    →  coordina il flusso e produce la raccomandazione finale
```

## Setup

### 1. Struttura directory

```
finance-orchestrator/
├── orchestrator.py
├── macro_agent.py
├── filter_agent.py
├── technical_agent.py
├── .env
├── macro_reports/
├── technical_reports/
└── orchestrator_output/
```

### 2. Installa le dipendenze

```bash
pip install anthropic requests pandas python-dotenv
```

### 3. Crea il file .env

```
ANTHROPIC_API_KEY=xxxx
FMP_API_KEY=xxxx
ALPHA_VANTAGE_API_KEY=xxxx
```

Chiavi API gratuite:
- Anthropic: https://console.anthropic.com
- FMP: https://financialmodelingprep.com (piano free: 250 req/giorno)
- Alpha Vantage: https://www.alphavantage.co (piano free: 25 req/giorno)

## Avvio

### Analisi completa (modo raccomandato)

```bash
python orchestrator.py ASML NVDA JPM CAT ENI
```

### Con refresh macro forzato

```bash
python orchestrator.py ASML NVDA JPM --refresh-macro
```

### Singoli agenti (per test)

```bash
python macro_agent.py
python filter_agent.py
python technical_agent.py
```

## Parametri configurabili

### Pesi fondamentali per settore — filter_agent.py

```python
SECTOR_WEIGHTS = {
    "Technology":  {"pe_forward": 0.12, "ps_ratio": 0.22, ...},
    "Financials":  {"pb_ratio": 0.32, "cet1_ratio": 0.20, ...},
    "Industrials": {"debt_equity": 0.22, "ev_ebitda": 0.14, ...},
}
```

### Pesi indicatori tecnici — technical_agent.py

```python
INDICATOR_WEIGHTS = {
    "ma_crossover": 0.40,
    "macd":         0.35,
    "rsi":          0.25,
}
```

### Pesi score composito finale — technical_agent.py

```python
# Composite score = tecnico 40% + fondamentale 60%
scored_w["composite_score"] = round(ts * 0.40 + fs * 0.60)
```

### Soglie knock-out — filter_agent.py

```python
KNOCKOUT_RULES = {
    "Technology":  {"debt_equity_max": 3.0, "revenue_growth_min": -0.15},
    "Financials":  {"cet1_min": 0.10,       "revenue_growth_min": -0.15},
    "Industrials": {"debt_equity_max": 3.0, "revenue_growth_min": -0.15},
}
```

## Output

Ogni esecuzione produce tre file JSON:

```
orchestrator_output/run_YYYYMMDD_HHMM.json   ← output completo con timestamp
orchestrator_output/run_latest.json           ← sempre l'ultimo run
macro_reports/macro_report_latest.json        ← analisi macro (cache giornaliera)
technical_reports/technical_report_latest.json
```

## Note operative

**Rate limit Alpha Vantage (piano free):** 25 req/giorno, 5/minuto.
L'agente tecnico inserisce pause automatiche di 12s tra le chiamate.
Con più di 5 ticker considera il piano a pagamento (~50$/mese).

**Rate limit FMP (piano free):** 250 req/giorno.
Sufficiente per 15-20 ticker al giorno.

**Cache macro:** il report macro viene riusato per tutta la giornata.
Usa `--refresh-macro` solo se ci sono eventi significativi intraday (Fed meeting, CPI).
