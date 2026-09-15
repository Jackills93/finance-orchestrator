# Agente Filtro Fondamentale — Specifica Completa

## Ruolo nell'orchestratore

Questo agente è il **terzo step** del pipeline:
1. Agente Macro → produce contesto macroeconomico
2. Agente Report → analizza bilanci e earnings di una lista di aziende
3. **→ Agente Filtro** (questo) → punteggia e filtra la lista
4. Agente Tecnico → analisi grafica sulle aziende promosse

Input ricevuto: lista di ticker + settore di appartenenza + dati fondamentali già estratti
Output prodotto: lista ordinata per score con motivazione per ogni azienda

---

## System Prompt (da incollare in Claude Code)

```
Sei un analista fondamentale quantitativo specializzato nella selezione di azioni.
Ricevi una lista di aziende con i loro dati fondamentali e il settore di appartenenza.
Il tuo compito è assegnare uno score ponderato a ciascuna azienda in base ai parametri
definiti nella configurazione, confrontando ogni metrica con la media del settore di riferimento.

Regole operative:
- Confronta sempre ogni metrica con la media di settore, non con valori assoluti.
- Applica i pesi corretti in base al settore dell'azienda (Tech, Banche, Industriali/Energia).
- Restituisci sempre un JSON strutturato come da schema output.
- Se un dato mancante supera il 30% dei parametri richiesti, segnala l'azienda come "DATA_INCOMPLETE".
- Non inventare dati. Se un valore non è disponibile, usa null e segnalalo nel campo "warnings".
- Lo score finale è in scala 0-100. Sotto 40 = esclusa. Tra 40-59 = watch list. Da 60 = promossa.
```

---

## Parametri e Pesi per Settore

### Logica di scoring per singolo parametro
Ogni parametro restituisce un sub-score da 0 a 100:
- **100** = supera la soglia con ampio margine (>20% meglio della media settore)
- **75** = supera la soglia (tra 0% e 20% meglio)
- **40** = non supera ma è vicino (entro il 10% peggio della media)
- **0** = fuori soglia in modo significativo (>10% peggio della media)

### Tabella pesi per settore

| Parametro | Condizione | Tech/Growth | Banche/Fin EU | Industriali/Energia |
|---|---|---|---|---|
| P/E forward | Azienda < Media settore | 12% | 8% | 20% |
| P/S ratio | Azienda < Media settore × 0.8 | 22% | 4% | 8% |
| ROE | Azienda > Media settore | 18% | 18% | 18% |
| Debt/Equity | Azienda < Media settore | 8% | 4% | 22% |
| P/B (Price/Book) | Azienda < Media settore | 5% | 32% | 10% |
| EV/EBITDA | Azienda < Media settore | 15% | 4% | 14% |
| Revenue Growth YoY | Azienda > Media settore | 20% | 10% | 8% |
| **Totale** | | **100%** | **80%*** | **100%** |

*Per Banche: il 20% residuo va assegnato a CET1 Ratio (solidità patrimoniale) > 13%.

### Parametri knock-out (esclusione automatica indipendente dallo score)
- Debt/Equity > 3.0 per Tech e Consumer → esclusa automaticamente
- CET1 < 10% per Banche → esclusa automaticamente
- Revenue Growth < -15% YoY → esclusa automaticamente (azienda in contrazione grave)
- P/E negativo (perdita netta) → penalità di 20 punti sullo score finale

---

## Fonte Dati: Financial Modeling Prep (FMP)

### Endpoint da utilizzare

```python
BASE_URL = "https://financialmodelingprep.com/api/v3"

# Dati fondamentali azienda
GET /key-metrics/{ticker}?period=annual&apikey={FMP_KEY}
# → P/E, P/S, P/B, EV/EBITDA, ROE, Debt/Equity

# Crescita ricavi
GET /income-statement-growth/{ticker}?period=annual&apikey={FMP_KEY}
# → revenueGrowth (YoY)

# Media di settore (confronto)
GET /sector_price_earning_ratio?date={today}&exchange=NYSE&apikey={FMP_KEY}
# → da usare come proxy per P/E medio settore

# Profilo azienda (settore di appartenenza)
GET /profile/{ticker}?apikey={FMP_KEY}
# → sector, industry, exchange
```

### Nota su medie di settore
FMP fornisce il P/E medio per settore ma non tutte le medie. Per le metriche mancanti
(P/S medio, ROE medio, D/E medio) usa questo approccio:

```python
# Strategia fallback per medie settore
# 1. Prima prova: FMP /sector-metrics (endpoint premium)
# 2. Fallback: calcola la media dagli altri ticker della lista in analisi
#    che appartengono allo stesso settore
# 3. Fallback finale: usa benchmark hardcoded da config/sector_benchmarks.py
```

---

## Schema Input dell'Agente

```json
{
  "context": {
    "macro_summary": "stringa dal Agente Macro",
    "analysis_date": "2025-06-01"
  },
  "companies": [
    {
      "ticker": "ASML",
      "name": "ASML Holding NV",
      "sector": "Technology",
      "exchange": "NASDAQ",
      "fundamentals": {
        "pe_forward": 32.4,
        "ps_ratio": 8.1,
        "pb_ratio": 18.2,
        "roe": 0.72,
        "debt_equity": 0.28,
        "ev_ebitda": 22.1,
        "revenue_growth_yoy": 0.18
      },
      "sector_averages": {
        "pe_forward": 38.0,
        "ps_ratio": 11.0,
        "pb_ratio": 22.0,
        "roe": 0.55,
        "debt_equity": 0.40,
        "ev_ebitda": 26.0,
        "revenue_growth_yoy": 0.12
      }
    }
  ]
}
```

---

## Schema Output dell'Agente

```json
{
  "filter_results": [
    {
      "ticker": "ASML",
      "name": "ASML Holding NV",
      "sector": "Technology",
      "total_score": 87,
      "status": "PROMOTED",
      "knockout_triggered": false,
      "parameter_scores": {
        "pe_forward":     { "value": 32.4, "sector_avg": 38.0, "sub_score": 75, "weight": 0.12 },
        "ps_ratio":       { "value": 8.1,  "sector_avg": 11.0, "sub_score": 100,"weight": 0.22 },
        "roe":            { "value": 0.72, "sector_avg": 0.55, "sub_score": 100,"weight": 0.18 },
        "debt_equity":    { "value": 0.28, "sector_avg": 0.40, "sub_score": 100,"weight": 0.08 },
        "pb_ratio":       { "value": 18.2, "sector_avg": 22.0, "sub_score": 75, "weight": 0.05 },
        "ev_ebitda":      { "value": 22.1, "sector_avg": 26.0, "sub_score": 75, "weight": 0.15 },
        "revenue_growth": { "value": 0.18, "sector_avg": 0.12, "sub_score": 100,"weight": 0.20 }
      },
      "strengths": [
        "P/S ratio 26% sotto la media settore — sottovalutata sui ricavi",
        "ROE 31% sopra la media settore — alta efficienza del capitale",
        "Revenue growth +18% YoY vs +12% media settore"
      ],
      "warnings": [],
      "pass_to_technical": true
    }
  ],
  "summary": {
    "total_analyzed": 15,
    "promoted": 4,
    "watchlist": 3,
    "excluded": 7,
    "data_incomplete": 1
  }
}
```

---

## Codice Python — filter_agent.py

```python
import os
import json
import requests
from datetime import date
from anthropic import Anthropic

client = Anthropic()

# --- Config pesi per settore ---
SECTOR_WEIGHTS = {
    "Technology": {
        "pe_forward": 0.12, "ps_ratio": 0.22, "roe": 0.18,
        "debt_equity": 0.08, "pb_ratio": 0.05, "ev_ebitda": 0.15,
        "revenue_growth": 0.20
    },
    "Financials": {
        "pe_forward": 0.08, "ps_ratio": 0.04, "roe": 0.18,
        "debt_equity": 0.04, "pb_ratio": 0.32, "ev_ebitda": 0.04,
        "revenue_growth": 0.10, "cet1_ratio": 0.20
    },
    "Industrials": {
        "pe_forward": 0.20, "ps_ratio": 0.08, "roe": 0.18,
        "debt_equity": 0.22, "pb_ratio": 0.10, "ev_ebitda": 0.14,
        "revenue_growth": 0.08
    }
}

KNOCKOUT_RULES = {
    "Technology":   {"debt_equity_max": 3.0, "revenue_growth_min": -0.15},
    "Financials":   {"cet1_min": 0.10,       "revenue_growth_min": -0.15},
    "Industrials":  {"debt_equity_max": 3.0, "revenue_growth_min": -0.15},
}

FMP_KEY = os.environ.get("FMP_API_KEY")
BASE_URL = "https://financialmodelingprep.com/api/v3"

# --- Fetch dati FMP ---
def fetch_fundamentals(ticker: str) -> dict:
    metrics = requests.get(
        f"{BASE_URL}/key-metrics/{ticker}",
        params={"period": "annual", "apikey": FMP_KEY, "limit": 1}
    ).json()
    growth = requests.get(
        f"{BASE_URL}/income-statement-growth/{ticker}",
        params={"period": "annual", "apikey": FMP_KEY, "limit": 1}
    ).json()
    profile = requests.get(
        f"{BASE_URL}/profile/{ticker}",
        params={"apikey": FMP_KEY}
    ).json()

    m = metrics[0] if metrics else {}
    g = growth[0] if growth else {}
    p = profile[0] if profile else {}

    return {
        "ticker": ticker,
        "name": p.get("companyName"),
        "sector": p.get("sector", "Technology"),
        "exchange": p.get("exchangeShortName"),
        "fundamentals": {
            "pe_forward":      m.get("peRatio"),
            "ps_ratio":        m.get("priceToSalesRatio"),
            "pb_ratio":        m.get("pbRatio"),
            "roe":             m.get("roe"),
            "debt_equity":     m.get("debtToEquity"),
            "ev_ebitda":       m.get("enterpriseValueOverEBITDA"),
            "revenue_growth":  g.get("revenueGrowth"),
        }
    }

# --- Calcolo sub-score per parametro ---
def sub_score(value, sector_avg, higher_is_better: bool) -> int:
    if value is None or sector_avg is None:
        return None
    diff = (value - sector_avg) / sector_avg if sector_avg != 0 else 0
    if not higher_is_better:
        diff = -diff  # inverti: vogliamo che sia sotto la media
    if diff > 0.20:   return 100
    if diff > 0.0:    return 75
    if diff > -0.10:  return 40
    return 0

# --- Check knockout ---
def check_knockout(company: dict) -> tuple[bool, str]:
    sector = company["sector"]
    f = company["fundamentals"]
    rules = KNOCKOUT_RULES.get(sector, {})
    if f.get("debt_equity") and f["debt_equity"] > rules.get("debt_equity_max", 999):
        return True, f"Debt/Equity {f['debt_equity']:.1f} supera il limite di {rules['debt_equity_max']}"
    if f.get("revenue_growth") and f["revenue_growth"] < rules.get("revenue_growth_min", -1):
        return True, f"Revenue growth {f['revenue_growth']:.1%} sotto soglia knock-out"
    if sector == "Financials":
        if f.get("cet1_ratio") and f["cet1_ratio"] < rules.get("cet1_min", 0):
            return True, f"CET1 {f['cet1_ratio']:.1%} sotto il minimo regolamentare"
    return False, ""

# --- Scoring principale ---
def score_company(company: dict, sector_avgs: dict) -> dict:
    sector = company["sector"]
    weights = SECTOR_WEIGHTS.get(sector, SECTOR_WEIGHTS["Technology"])
    f = company["fundamentals"]

    param_config = {
        "pe_forward":      (f.get("pe_forward"),     sector_avgs.get("pe_forward"),     False),
        "ps_ratio":        (f.get("ps_ratio"),        sector_avgs.get("ps_ratio"),        False),
        "roe":             (f.get("roe"),             sector_avgs.get("roe"),             True),
        "debt_equity":     (f.get("debt_equity"),     sector_avgs.get("debt_equity"),     False),
        "pb_ratio":        (f.get("pb_ratio"),        sector_avgs.get("pb_ratio"),        False),
        "ev_ebitda":       (f.get("ev_ebitda"),       sector_avgs.get("ev_ebitda"),       False),
        "revenue_growth":  (f.get("revenue_growth"),  sector_avgs.get("revenue_growth"),  True),
    }

    ko_triggered, ko_reason = check_knockout(company)
    parameter_scores = {}
    total = 0.0
    warnings = []

    for param, (val, avg, higher) in param_config.items():
        w = weights.get(param, 0)
        ss = sub_score(val, avg, higher)
        if ss is None:
            warnings.append(f"{param}: dato non disponibile")
            ss = 40  # valore neutro per dati mancanti
        parameter_scores[param] = {
            "value": val, "sector_avg": avg,
            "sub_score": ss, "weight": w
        }
        total += ss * w

    # Penalità P/E negativo
    if f.get("pe_forward") and f["pe_forward"] < 0:
        total -= 20
        warnings.append("P/E negativo: azienda in perdita, penalità -20 punti")

    final_score = round(max(0, min(100, total)))

    if ko_triggered:
        status = "EXCLUDED"
        pass_to_technical = False
    elif final_score >= 60:
        status = "PROMOTED"
        pass_to_technical = True
    elif final_score >= 40:
        status = "WATCHLIST"
        pass_to_technical = False
    else:
        status = "EXCLUDED"
        pass_to_technical = False

    return {
        "ticker": company["ticker"],
        "name": company["name"],
        "sector": sector,
        "total_score": final_score,
        "status": status,
        "knockout_triggered": ko_triggered,
        "knockout_reason": ko_reason if ko_triggered else None,
        "parameter_scores": parameter_scores,
        "warnings": warnings,
        "pass_to_technical": pass_to_technical
    }

# --- Agente LLM: genera strengths e sintesi narrativa ---
def generate_narrative(scored_company: dict, macro_context: str) -> dict:
    prompt = f"""
Sei un analista fondamentale. Analizza questo scoring e genera:
1. Una lista di 2-3 "strengths" (punti di forza) in italiano, frasi brevi e concrete.
2. Una "summary" di massimo 2 righe che spiega perché l'azienda è promossa/esclusa,
   tenendo conto del contesto macro: {macro_context[:300]}

Dati scoring:
{json.dumps(scored_company, indent=2, default=str)}

Rispondi SOLO con JSON valido nel formato:
{{"strengths": ["...", "..."], "summary": "..."}}
"""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    try:
        return json.loads(text)
    except Exception:
        return {"strengths": [], "summary": text}

# --- Entry point agente ---
def run_filter_agent(tickers: list[str], macro_context: str = "") -> dict:
    companies_data = [fetch_fundamentals(t) for t in tickers]

    # Calcola medie di settore dalla lista stessa (fallback)
    from collections import defaultdict
    sector_vals = defaultdict(lambda: defaultdict(list))
    for c in companies_data:
        for param, val in c["fundamentals"].items():
            if val is not None:
                sector_vals[c["sector"]][param].append(val)

    sector_avgs = {
        sector: {param: sum(vals)/len(vals) for param, vals in params.items()}
        for sector, params in sector_vals.items()
    }

    results = []
    for company in companies_data:
        avgs = sector_avgs.get(company["sector"], {})
        scored = score_company(company, avgs)
        narrative = generate_narrative(scored, macro_context)
        scored["strengths"] = narrative.get("strengths", [])
        scored["summary"] = narrative.get("summary", "")
        results.append(scored)

    results.sort(key=lambda x: x["total_score"], reverse=True)

    promoted   = [r for r in results if r["status"] == "PROMOTED"]
    watchlist  = [r for r in results if r["status"] == "WATCHLIST"]
    excluded   = [r for r in results if r["status"] == "EXCLUDED"]

    return {
        "filter_results": results,
        "summary": {
            "total_analyzed":  len(results),
            "promoted":        len(promoted),
            "watchlist":       len(watchlist),
            "excluded":        len(excluded),
            "data_incomplete": sum(1 for r in results if len(r["warnings"]) > 2)
        }
    }

# --- Test rapido ---
if __name__ == "__main__":
    output = run_filter_agent(
        tickers=["ASML", "NVDA", "JPM", "CAT"],
        macro_context="Fed in pausa, inflazione in calo, ciclo AI in espansione."
    )
    print(json.dumps(output, indent=2, default=str))
```

---

## File di supporto: config/sector_benchmarks.py

```python
# Benchmark hardcoded — usati come fallback se FMP non restituisce medie settore
# Aggiornati manualmente ogni trimestre

SECTOR_BENCHMARKS = {
    "Technology": {
        "pe_forward": 32.0, "ps_ratio": 8.5,  "pb_ratio": 8.0,
        "roe": 0.25,        "debt_equity": 0.5, "ev_ebitda": 22.0,
        "revenue_growth": 0.14
    },
    "Financials": {
        "pe_forward": 12.0, "ps_ratio": 2.5,  "pb_ratio": 1.3,
        "roe": 0.11,        "debt_equity": 1.8, "ev_ebitda": 10.0,
        "revenue_growth": 0.06, "cet1_ratio": 0.135
    },
    "Industrials": {
        "pe_forward": 18.0, "ps_ratio": 1.8,  "pb_ratio": 3.0,
        "roe": 0.14,        "debt_equity": 0.8, "ev_ebitda": 13.0,
        "revenue_growth": 0.07
    },
    "Energy": {
        "pe_forward": 14.0, "ps_ratio": 1.2,  "pb_ratio": 1.8,
        "roe": 0.13,        "debt_equity": 0.7, "ev_ebitda": 7.0,
        "revenue_growth": 0.05
    }
}
```

---

## Variabili d'ambiente richieste

```bash
# .env
FMP_API_KEY=la_tua_chiave_fmp
ANTHROPIC_API_KEY=la_tua_chiave_anthropic
```

## Dipendenze Python

```
anthropic>=0.25.0
requests>=2.31.0
python-dotenv>=1.0.0
```

---

## Prossimi step

1. `filter_agent.py` → pronto da usare in Claude Code
2. Da costruire: `macro_agent.py` (web search FMP news + Alpha Vantage sentiment)
3. Da costruire: `technical_agent.py` (Alpha Vantage OHLCV + pattern logic)
4. Da costruire: `orchestrator.py` (coordina il flusso e chiama gli agenti in sequenza)
