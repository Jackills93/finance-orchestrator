"""
filter_agent.py -- Agente Filtro Fondamentale
Orchestratore multi-agente finanziario

Metriche: P/E forward, P/S, ROE, D/E, EV/EBITDA, Revenue Growth
Fonte dati: Yahoo Finance (yfinance) -- gratuito, nessuna API key richiesta
Knockout automatici per settore + scoring ponderato LLM-assisted

Dipendenze: pip install anthropic yfinance python-dotenv
Variabili d'ambiente: ANTHROPIC_API_KEY
"""

import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
import pandas as pd
import yfinance as yf

from sectors import TARGET_SECTORS, normalize_sector

load_dotenv()
client   = Anthropic()
OUTPUT_DIR = Path("filter_reports")
OUTPUT_DIR.mkdir(exist_ok=True)

# --- Pesi per settore ---------------------------------------------------------

SECTOR_WEIGHTS = {
    "Technology": {
        "pe_forward":      0.12,
        "ps_ratio":        0.22,
        "roe":             0.20,
        "debt_equity":     0.14,
        "ev_ebitda":       0.14,
        "revenue_growth":  0.18,
    },
    "Financials": {
        "pb_ratio":        0.32,
        "roe":             0.28,
        "debt_equity":     0.10,
        "revenue_growth":  0.20,
        "ev_ebitda":       0.10,
    },
    "Industrials": {
        "pe_forward":      0.18,
        "debt_equity":     0.22,
        "ev_ebitda":       0.14,
        "roe":             0.20,
        "revenue_growth":  0.26,
    },
}

# Fallback per settori non mappati
DEFAULT_WEIGHTS = {
    "pe_forward":     0.20,
    "ps_ratio":       0.15,
    "roe":            0.20,
    "debt_equity":    0.15,
    "ev_ebitda":      0.15,
    "revenue_growth": 0.15,
}

# --- Regole knockout ----------------------------------------------------------

KNOCKOUT_RULES = {
    "Technology":  {"debt_equity_max": 3.0, "revenue_growth_min": -0.15},
    "Financials":  {"revenue_growth_min": -0.15, "pe_forward_max": 25.0},
    "Industrials": {"debt_equity_max": 3.0, "revenue_growth_min": -0.15, "pe_forward_max": 40.0},
}

# --- Earnings proximity check -------------------------------------------------

def check_earnings_proximity(ticker: str, days: int = 7) -> tuple[bool, str]:
    """
    Controlla se ci sono earnings previsti nei prossimi N giorni.
    Se si, il titolo viene saltato per evitare di entrare prima di una trimestrale.
    Restituisce (is_near, messaggio).
    """
    try:
        t   = yf.Ticker(ticker)
        cal = t.calendar
        today   = datetime.now().date()
        cutoff  = today + timedelta(days=days)

        dates = []
        if cal is None:
            return False, ""

        # yfinance >= 0.2: calendar e un DataFrame con date come colonne
        if hasattr(cal, "columns"):
            for col in cal.columns:
                try:
                    d = pd.Timestamp(col).date()
                    dates.append(d)
                except Exception:
                    pass
        # formato dict (versioni precedenti)
        elif isinstance(cal, dict):
            for key in ("Earnings Date", "earningsDate"):
                val = cal.get(key, [])
                if not isinstance(val, list):
                    val = [val]
                for v in val:
                    try:
                        dates.append(pd.Timestamp(v).date())
                    except Exception:
                        pass

        for d in dates:
            if today <= d <= cutoff:
                return True, f"Earnings previsti il {d.strftime('%Y-%m-%d')} -- skip per ridurre rischio evento"

        return False, ""
    except Exception:
        return False, ""


# --- Fetch Yahoo Finance ------------------------------------------------------

def fetch_fundamentals(ticker: str) -> dict:
    """
    Raccoglie i dati fondamentali da Yahoo Finance tramite yfinance.
    Nessuna API key richiesta, completamente gratuito.
    """
    print(f"  [YF] Fetching fundamentals -- {ticker}")
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        # Settore normalizzato (None = fuori dai tre settori della pipeline)
        raw_sector = info.get("sector") or ""
        sector = normalize_sector(raw_sector)

        # Revenue growth YoY da income statement
        rev_growth = None
        try:
            inc = t.income_stmt
            if inc is not None and not inc.empty and "Total Revenue" in inc.index:
                rev = inc.loc["Total Revenue"].dropna()
                if len(rev) >= 2:
                    curr, prev = float(rev.iloc[0]), float(rev.iloc[1])
                    if prev != 0:
                        rev_growth = (curr - prev) / abs(prev)
        except Exception:
            pass

        # Normalizza D/E: yfinance lo restituisce come rapporto decimale
        de_raw = info.get("debtToEquity")
        debt_equity = de_raw / 100 if de_raw is not None else None

        # Normalizza ROE: yfinance lo restituisce gia in decimale (es. 0.85)
        roe_raw = info.get("returnOnEquity")

        return {
            "ticker":         ticker,
            "name":           info.get("longName", ticker),
            "sector":         sector,
            "raw_sector":     raw_sector,
            "price":          info.get("currentPrice") or info.get("regularMarketPrice"),
            "pe_forward":     info.get("forwardPE"),
            "ps_ratio":       info.get("priceToSalesTrailing12Months"),
            "roe":            roe_raw,
            "debt_equity":    debt_equity,
            "ev_ebitda":      info.get("enterpriseToEbitda"),
            "pb_ratio":       info.get("priceToBook"),
            "revenue_growth": rev_growth,
        }
    except Exception as e:
        print(f"  [YF ERROR] {ticker}: {e}")
        return {
            "ticker": ticker, "name": ticker, "sector": None, "raw_sector": "",
            "fetch_error": str(e),
            "price": None, "pe_forward": None, "ps_ratio": None,
            "roe": None, "debt_equity": None, "ev_ebitda": None,
            "pb_ratio": None, "revenue_growth": None,
        }


# --- Scoring fondamentale -----------------------------------------------------

def _score_metric(metric: str, value: float | None, sector: str) -> tuple[float, str]:
    """
    Trasforma il valore grezzo di una metrica in uno score 0-100.
    Restituisce (score, note).
    """
    if value is None:
        return 50.0, "dato non disponibile"

    if metric == "pe_forward":
        if value < 0:   return 20.0, f"P/E negativo ({value:.1f}) -- utili negativi"
        if value < 15:  return 90.0, f"P/E {value:.1f} -- sottovalutato"
        if value < 25:  return 75.0, f"P/E {value:.1f} -- valutazione ragionevole"
        if value < 35:  return 55.0, f"P/E {value:.1f} -- sopra media settore"
        if value < 50:  return 35.0, f"P/E {value:.1f} -- elevato"
        return          15.0, f"P/E {value:.1f} -- molto elevato"

    if metric == "ps_ratio":
        if value < 2:   return 90.0, f"P/S {value:.1f} -- molto attraente"
        if value < 5:   return 75.0, f"P/S {value:.1f} -- ragionevole"
        if value < 10:  return 50.0, f"P/S {value:.1f} -- nella norma tech"
        if value < 20:  return 30.0, f"P/S {value:.1f} -- elevato"
        return          10.0, f"P/S {value:.1f} -- molto elevato"

    if metric == "roe":
        v = value * 100 if abs(value) < 5 else value  # normalizza % vs decimale
        if v < 0:    return 20.0, f"ROE {v:.1f}% -- negativo"
        if v < 5:    return 35.0, f"ROE {v:.1f}% -- basso"
        if v < 15:   return 60.0, f"ROE {v:.1f}% -- nella norma"
        if v < 30:   return 80.0, f"ROE {v:.1f}% -- buono"
        return       95.0, f"ROE {v:.1f}% -- eccellente"

    if metric == "debt_equity":
        if value < 0:   return 50.0, f"D/E {value:.2f} -- dato anomalo"
        if value < 0.3: return 90.0, f"D/E {value:.2f} -- struttura finanziaria solida"
        if value < 0.8: return 75.0, f"D/E {value:.2f} -- leva moderata"
        if value < 1.5: return 55.0, f"D/E {value:.2f} -- leva nella norma"
        if value < 2.5: return 35.0, f"D/E {value:.2f} -- leva elevata"
        return          15.0, f"D/E {value:.2f} -- leva molto elevata"

    if metric == "ev_ebitda":
        if value < 0:   return 20.0, f"EV/EBITDA {value:.1f} -- EBITDA negativo"
        if value < 8:   return 90.0, f"EV/EBITDA {value:.1f} -- economico"
        if value < 15:  return 70.0, f"EV/EBITDA {value:.1f} -- ragionevole"
        if value < 25:  return 50.0, f"EV/EBITDA {value:.1f} -- nella norma"
        if value < 40:  return 30.0, f"EV/EBITDA {value:.1f} -- elevato"
        return          10.0, f"EV/EBITDA {value:.1f} -- molto elevato"

    if metric == "revenue_growth":
        v = value * 100
        if v > 30:   return 100.0, f"Revenue growth {v:.1f}% -- eccellente"
        if v > 15:   return 85.0,  f"Revenue growth {v:.1f}% -- forte"
        if v > 5:    return 70.0,  f"Revenue growth {v:.1f}% -- buono"
        if v > 0:    return 55.0,  f"Revenue growth {v:.1f}% -- positivo ma moderato"
        if v > -10:  return 30.0,  f"Revenue growth {v:.1f}% -- calo lieve"
        return       10.0, f"Revenue growth {v:.1f}% -- calo significativo"

    if metric == "pb_ratio":
        if value < 1:   return 90.0, f"P/B {value:.2f} -- sotto fair value contabile"
        if value < 2:   return 75.0, f"P/B {value:.2f} -- ragionevole"
        if value < 3:   return 55.0, f"P/B {value:.2f} -- nella norma"
        if value < 5:   return 35.0, f"P/B {value:.2f} -- elevato"
        return          15.0, f"P/B {value:.2f} -- molto elevato"

    return 50.0, f"{metric}={value} -- metrica non mappata"


def apply_knockout(fundamentals: dict, sector: str) -> tuple[bool, str]:
    """Verifica le regole knockout per il settore. Restituisce (passed, reason)."""
    rules = KNOCKOUT_RULES.get(sector, {})

    de_max = rules.get("debt_equity_max")
    de_val = fundamentals.get("debt_equity")
    if de_max is not None and de_val is not None and de_val > de_max:
        return False, f"KNOCKOUT: D/E {de_val:.2f} > {de_max} (soglia settore)"

    rg_min = rules.get("revenue_growth_min")
    rg_val = fundamentals.get("revenue_growth")
    if rg_min is not None and rg_val is not None and rg_val < rg_min:
        return False, f"KNOCKOUT: Revenue growth {rg_val*100:.1f}% < {rg_min*100:.0f}%"

    pe_max = rules.get("pe_forward_max")
    pe_val = fundamentals.get("pe_forward")
    if pe_max is not None and pe_val is not None and pe_val > 0 and pe_val > pe_max:
        return False, f"KNOCKOUT: P/E forward {pe_val:.1f} > {pe_max} (soglia settore)"

    return True, "ok"


def score_fundamentals(fundamentals: dict) -> dict:
    """Calcola lo score fondamentale ponderato per settore."""
    sector  = fundamentals.get("sector", "Technology")
    weights = SECTOR_WEIGHTS.get(sector, DEFAULT_WEIGHTS)

    scores  = {}
    notes   = {}
    total   = 0.0
    w_sum   = 0.0

    for metric, weight in weights.items():
        value = fundamentals.get(metric)
        s, note = _score_metric(metric, value, sector)
        scores[metric] = round(s, 1)
        notes[metric]  = note
        total  += s * weight
        w_sum  += weight

    total_score = round(total / w_sum) if w_sum > 0 else 50

    passed_ko, ko_reason = apply_knockout(fundamentals, sector)

    return {
        "ticker":         fundamentals["ticker"],
        "name":           fundamentals.get("name", ""),
        "sector":         sector,
        "price":          fundamentals.get("price"),
        "total_score":    total_score,
        "pass_knockout":  passed_ko,
        "knockout_reason": ko_reason,
        "pass_to_technical": passed_ko and total_score >= 50,
        "metric_scores":  scores,
        "metric_notes":   notes,
        "raw_metrics": {
            k: fundamentals.get(k) for k in
            ["pe_forward", "ps_ratio", "roe", "debt_equity", "ev_ebitda", "pb_ratio", "revenue_growth"]
        },
    }


# --- Narrativa LLM ------------------------------------------------------------

FILTER_SYSTEM_PROMPT = """Sei un analista fondamentale senior. Ricevi i dati di scoring
fondamentale di un'azione e devi produrre:
1. "summary": breve valutazione fondamentale in 2 righe (italiano), specifica sui numeri.
2. "strengths": lista di 2 punti di forza fondamentali (frasi brevi).
3. "weaknesses": lista di 1-2 punti deboli o rischi fondamentali.

Basati SOLO sui dati forniti. Rispondi SOLO con JSON valido senza markdown:
{"summary": "...", "strengths": ["...", "..."], "weaknesses": ["..."]}"""


def generate_fundamental_narrative(scored: dict) -> dict:
    prompt = f"Analizza questo profilo fondamentale:\n{json.dumps(scored, indent=2, default=str)}"
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=FILTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(text)
    except Exception:
        return {"summary": text[:200], "strengths": [], "weaknesses": []}


# --- Runner principale --------------------------------------------------------

def run_filter_agent(
    tickers: list[str],
    macro_context: str = "",
    min_score: int = 50,
) -> dict:
    """
    Esegue il filtro fondamentale per una lista di ticker.

    Args:
        tickers:       Lista di simboli da analizzare
        macro_context: Stringa di contesto macro (dall'orchestratore)
        min_score:     Soglia minima per passare all'analisi tecnica

    Returns:
        Dict con filter_results (lista) e summary
    """
    print(f"[INFO] Avvio filtro fondamentale per {tickers}...")
    results = []

    for ticker in tickers:
        print(f"\n[FILTER] Analisi {ticker}...")

        # 0. Earnings proximity check: skip se earnings nei prossimi 7 giorni
        near_earnings, earnings_msg = check_earnings_proximity(ticker, days=7)
        if near_earnings:
            print(f"  -> SKIP earnings: {earnings_msg}")
            results.append({
                "ticker": ticker, "name": ticker, "sector": "N/A",
                "price": None, "total_score": 0,
                "pass_knockout": False, "knockout_reason": earnings_msg,
                "pass_to_technical": False,
                "metric_scores": {}, "metric_notes": {},
                "raw_metrics": {},
                "summary": earnings_msg, "strengths": [], "weaknesses": [],
                "earnings_skip": True,
            })
            continue

        # 1. Fetch dati fondamentali
        fund = fetch_fundamentals(ticker)

        # Fuori perimetro o dati Yahoo non disponibili: escluso con motivo, senza narrativa LLM
        if fund.get("sector") is None:
            if fund.get("fetch_error"):
                reason = f"Dati Yahoo Finance non disponibili ({fund['fetch_error'][:80]})"
            elif not fund.get("raw_sector") and fund.get("price") is None:
                reason = "Dati Yahoo Finance non disponibili (ticker errato o non piu' quotato?)"
            else:
                reason = (f"Settore fuori perimetro ({fund.get('raw_sector') or 'non indicato'}): "
                          f"la pipeline analizza solo {', '.join(TARGET_SECTORS)}")
            print(f"  -> ESCLUSO: {reason}")
            results.append({
                "ticker": ticker, "name": fund.get("name", ticker), "sector": "N/A",
                "price": fund.get("price"), "total_score": 0,
                "pass_knockout": False, "knockout_reason": reason,
                "pass_to_technical": False,
                "metric_scores": {}, "metric_notes": {}, "raw_metrics": {},
                "summary": reason, "strengths": [], "weaknesses": [],
                "earnings_skip": False,
            })
            continue

        # 2. Scoring ponderato + knockout
        scored = score_fundamentals(fund)
        scored["earnings_skip"] = False

        # 3. Narrativa LLM
        narrative = generate_fundamental_narrative(scored)
        scored["summary"]    = narrative.get("summary", "")
        scored["strengths"]  = narrative.get("strengths", [])
        scored["weaknesses"] = narrative.get("weaknesses", [])

        results.append(scored)
        status = "V PROMOSSO" if scored["pass_to_technical"] else "X ESCLUSO"
        print(f"  -> score={scored['total_score']}  ko={scored['pass_knockout']}  {status}")

    # Ordina per score
    results.sort(key=lambda x: x["total_score"], reverse=True)

    # Salva su file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path  = OUTPUT_DIR / f"filter_report_{timestamp}.json"
    latest    = OUTPUT_DIR / "filter_report_latest.json"
    for path in [out_path, latest]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str, ensure_ascii=False)

    print(f"\n[INFO] Report salvato: {out_path}")

    promoted = [r for r in results if r.get("pass_to_technical")]

    return {
        "filter_results": results,
        "summary": {
            "total_analyzed": len(results),
            "promoted":       len(promoted),
            "excluded":       len(results) - len(promoted),
            "top_pick":       results[0]["ticker"] if results else None,
        }
    }


# --- Test ---------------------------------------------------------------------

if __name__ == "__main__":
    output = run_filter_agent(
        tickers=["ASML", "NVDA", "JPM"],
        macro_context="Test run",
    )

    print("\n-- SUMMARY --")
    print(json.dumps(output["summary"], indent=2))

    print("\n-- RISULTATI --")
    for r in output["filter_results"]:
        status = "PROMOSSO" if r["pass_to_technical"] else "ESCLUSO"
        print(f"  {r['ticker']:<6} score={r['total_score']:3d}  {r['sector']:<14} [{status}]")
        print(f"         {r.get('summary','')}")
