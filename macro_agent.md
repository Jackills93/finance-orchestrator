# Agente Macro — Specifica Completa

## Ruolo nell'orchestratore

Questo agente è il **primo step** del pipeline:

1. **→ Agente Macro** (questo) → produce contesto macroeconomico strutturato
2. Agente Report → analizza bilanci e earnings
3. Agente Filtro → filtra e punteggia con i parametri fondamentali
4. Agente Tecnico → analisi grafica sulle aziende promosse

Input ricevuto: nessuno (parte autonomamente, fa web search)
Output prodotto: JSON con score di rischio per settore + narrativa macro

---

## Aree monitorate

| Area | Query principali | Fonte |
|---|---|---|
| Politica Fed | Rate decision, FOMC minutes, Powell speech | Web search |
| Politica BCE | Rate decision, Lagarde speech, deposit rate | Web search |
| Inflazione USA | CPI, PCE, core inflation | Web search |
| Inflazione EU | HICP Eurozona, Germania, Francia | Web search |

---

## Schema Output JSON

```json
{
  "analysis_date": "2025-06-01",
  "macro_narrative": "La Fed mantiene i tassi invariati nella riunione di maggio...",
  "fed_stance": "neutral",
  "ecb_stance": "dovish",
  "us_inflation_trend": "falling",
  "eu_inflation_trend": "stable",
  "sector_risk": {
    "Technology": {
      "risk_score": 3,
      "drivers_positive": [
        "Tassi USA stabili favoriscono i multipli delle growth stock",
        "Inflazione in calo riduce il costo del capitale"
      ],
      "drivers_negative": [
        "Incertezza geopolitica sui semiconduttori"
      ],
      "outlook_3_6m": "Favorevole, ciclo AI in espansione"
    },
    "Financials": {
      "risk_score": 5,
      "drivers_positive": ["BCE in taglio graduale sostiene NIM nel medio termine"],
      "drivers_negative": ["Compressione margini nel breve con tassi in discesa"],
      "outlook_3_6m": "Neutro, dipende dalla velocità dei tagli BCE"
    },
    "Industrials": {
      "risk_score": 6,
      "drivers_positive": ["Reshoring USA favorisce capex industriale"],
      "drivers_negative": ["Domanda EU debole, PMI manifatturiero sotto 50"],
      "outlook_3_6m": "Cauto, attesa ripresa H2 2025"
    }
  },
  "overall_market_bias": "risk_on",
  "key_events_watch": [
    "FOMC meeting 18 giugno",
    "CPI USA 11 giugno",
    "BCE meeting 6 giugno"
  ],
  "data_quality": "high",
  "warnings": []
}
```

---

## Logica score di rischio (1–10)

| Score | Significato | Impatto sul filtro |
|---|---|---|
| 1–3 | Contesto molto favorevole | Bonus +5 punti sullo score settore |
| 4–6 | Contesto neutro | Nessun aggiustamento |
| 7–8 | Contesto avverso | Penalità –5 punti sullo score settore |
| 9–10 | Contesto molto avverso | Penalità –10 punti + flag nell'output |

Questi aggiustamenti vengono applicati dall'orchestratore quando combina
l'output del macro_agent con quello del filter_agent.

---

## Gestione cache (comportamento on-demand + file)

```
Prima esecuzione del giorno → fa web search → salva su file
Seconda esecuzione stessa giornata → carica da file → skip web search
force_refresh=True → ignora cache, rilancia sempre la ricerca
```

File salvati in `macro_reports/`:
- `macro_report_YYYYMMDD_HHMM.json` — storico con timestamp
- `macro_report_latest.json` — sempre l'ultimo, usato dall'orchestratore

---

## Integrazione con gli altri agenti

### Come lo chiama l'orchestratore

```python
from macro_agent import run_macro_agent, get_macro_context_for_filter

# Step 1: esegui agente macro
macro_analysis = run_macro_agent()

# Step 2: estrai stringa sintetica per il filter_agent
macro_context = get_macro_context_for_filter(macro_analysis)

# Step 3: passa macro_context al filter_agent
from filter_agent import run_filter_agent
filter_output = run_filter_agent(tickers=[...], macro_context=macro_context)
```

### Aggiustamento score nel filter_agent (da implementare in orchestrator.py)

```python
def apply_macro_adjustment(filter_results: list, sector_risk: dict) -> list:
    adjustments = {
        (1, 3):  +5,
        (4, 6):  0,
        (7, 8):  -5,
        (9, 10): -10,
    }
    for company in filter_results:
        sector = company["sector"]
        risk   = sector_risk.get(sector, {}).get("risk_score", 5)
        adj    = next((v for (lo, hi), v in adjustments.items() if lo <= risk <= hi), 0)
        company["total_score"]    = max(0, min(100, company["total_score"] + adj))
        company["macro_adjustment"] = adj
    return filter_results
```

---

## Dipendenze

```
anthropic>=0.25.0
python-dotenv>=1.0.0
```

## Variabili d'ambiente

```bash
ANTHROPIC_API_KEY=xxxx   # obbligatoria
# FMP_API_KEY non serve per questo agente
```

## Struttura directory

```
finance-orchestrator/
├── macro_agent.py
├── filter_agent.py
├── macro_reports/
│   ├── macro_report_20250601_0830.json
│   └── macro_report_latest.json
└── .env
```
