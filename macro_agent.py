"""
macro_agent.py -- Agente Analisi Macroeconomica
Orchestratore multi-agente finanziario

Monitora: politica monetaria (Fed/BCE) + inflazione USA/EU
Output: JSON strutturato con score di rischio per settore + testo narrativo
Modalita: on-demand (chiamato dall'orchestratore) + salva sempre su file

Dipendenze: pip install anthropic requests python-dotenv
Variabili d'ambiente: ANTHROPIC_API_KEY
"""

import os
import json
from datetime import datetime
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
import yfinance as yf

load_dotenv()
client = Anthropic()

# --- Directory output ---------------------------------------------------------
OUTPUT_DIR = Path("macro_reports")
OUTPUT_DIR.mkdir(exist_ok=True)

# --- Query di ricerca per area tematica ---------------------------------------
# Anno calcolato a runtime: un anno fisso nelle query fa tornare notizie vecchie
_YEAR = datetime.now().year

SEARCH_QUERIES = {
    "fed_policy": [
        f"Federal Reserve interest rate decision {_YEAR}",
        "Fed FOMC meeting minutes latest",
        "Jerome Powell speech rates outlook",
    ],
    "ecb_policy": [
        f"ECB European Central Bank rate decision {_YEAR}",
        "Christine Lagarde speech monetary policy",
        "ECB deposit rate cut outlook",
    ],
    "inflation_usa": [
        f"US CPI inflation data latest {_YEAR}",
        "US PCE personal consumption expenditures",
        "US core inflation trend",
    ],
    "inflation_eu": [
        f"Eurozone HICP inflation data {_YEAR}",
        "EU inflation ECB target",
        "Germany France inflation latest",
    ],
}

# --- System prompt dell'agente ------------------------------------------------
MACRO_SYSTEM_PROMPT = """Sei un economista senior specializzato in analisi macroeconomica
per la selezione di asset finanziari. Il tuo compito e:

1. Analizzare le notizie piu recenti su politica monetaria (Fed e BCE) e inflazione
   (USA e Eurozona) fornite come contesto.
2. Produrre un JSON strutturato con:
   - Un riassunto narrativo del contesto macro attuale.
   - Uno score di rischio per ciascuno dei 3 settori target: Technology, Financials, Industrials.
   - I driver principali (positivi e negativi) per ciascun settore.
   - Un outlook a 3-6 mesi per ciascun settore.

Regole:
- Lo score di rischio e da 1 (rischio minimo, contesto favorevole) a 10 (rischio massimo).
- Sii conciso e basati SOLO sulle notizie fornite, non inventare dati.
- Se le notizie sono insufficienti per un settore, assegna score 5 e segnalalo.
- Restituisci SOLO JSON valido, senza markdown, senza testo fuori dal JSON.

Schema output obbligatorio:
{
  "analysis_date": "YYYY-MM-DD",
  "macro_narrative": "stringa con il contesto macro generale (max 300 parole)",
  "fed_stance": "hawkish|neutral|dovish",
  "ecb_stance": "hawkish|neutral|dovish",
  "us_inflation_trend": "rising|stable|falling",
  "eu_inflation_trend": "rising|stable|falling",
  "sector_risk": {
    "Technology": {
      "risk_score": 1-10,
      "drivers_positive": ["...", "..."],
      "drivers_negative": ["...", "..."],
      "outlook_3_6m": "stringa breve"
    },
    "Financials": { ... },
    "Industrials": { ... }
  },
  "overall_market_bias": "risk_on|neutral|risk_off",
  "key_events_watch": ["evento 1", "evento 2"],
  "data_quality": "high|medium|low",
  "warnings": []
}
"""

# --- VIX fetch ---------------------------------------------------------------

def fetch_vix() -> dict:
    """
    Recupera il VIX corrente via yfinance.
    Restituisce valore, livello interpretato e suggerimento bias.
    """
    try:
        info  = yf.Ticker("^VIX").info
        value = info.get("regularMarketPrice") or info.get("currentPrice")
        if value is None:
            hist  = yf.Ticker("^VIX").history(period="1d")
            value = float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception:
        value = None

    if value is None:
        return {"value": None, "level": "unknown", "bias_hint": "neutral", "description": "Dati VIX non disponibili"}

    value = round(float(value), 2)

    if value < 15:
        level = "low"; bias_hint = "risk_on"
        description = f"VIX {value} -- Mercato tranquillo, bassa volatilita implicita. Clima favorevole al rischio."
    elif value < 20:
        level = "moderate"; bias_hint = "risk_on"
        description = f"VIX {value} -- Volatilita moderata, sentiment positivo ma con qualche incertezza."
    elif value < 25:
        level = "elevated"; bias_hint = "neutral"
        description = f"VIX {value} -- Volatilita elevata, incertezza crescente. Prudenza consigliata."
    elif value < 35:
        level = "high"; bias_hint = "risk_off"
        description = f"VIX {value} -- Paura diffusa nel mercato. Sentiment risk-off marcato."
    else:
        level = "extreme"; bias_hint = "risk_off"
        description = f"VIX {value} -- Livello di panico. Risk-off estremo, storicamente zona di capitolazione."

    return {
        "value":       value,
        "level":       level,
        "bias_hint":   bias_hint,
        "description": description,
    }


# --- Web search con tool use --------------------------------------------------

def search_macro_news(queries: list[str]) -> str:
    """
    Esegue ricerche web usando il tool web_search di Claude.
    Aggrega tutti i risultati in un unico blocco di testo.
    """
    all_results = []

    for query in queries:
        print(f"  [SEARCH] {query}")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": (
                    f"Search for: {query}. "
                    "Return only the key facts found, no commentary."
                )
            }]
        )

        # Estrai testo dalla risposta (sia text che tool_result)
        for block in response.content:
            if hasattr(block, "text") and block.text:
                all_results.append(f"[Query: {query}]\n{block.text}")

    return "\n\n---\n\n".join(all_results)


def fetch_all_macro_context() -> str:
    """Esegue tutte le query per ogni area tematica e aggrega il contesto."""
    print("[INFO] Raccolta notizie macroeconomiche...")
    sections = []

    for area, queries in SEARCH_QUERIES.items():
        print(f"\n[AREA] {area.upper()}")
        text = search_macro_news(queries[:2])  # max 2 query per area per contenere i costi
        sections.append(f"=== {area.upper()} ===\n{text}")

    return "\n\n".join(sections)


# --- Analisi LLM --------------------------------------------------------------

def analyze_macro_context(raw_context: str) -> dict:
    """
    Passa il contesto grezzo a Claude per produrre
    il JSON strutturato con scoring di rischio per settore.
    """
    print("\n[INFO] Analisi contesto macroeconomico con Claude...")

    today = datetime.now().strftime("%Y-%m-%d")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        system=MACRO_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Data di oggi: {today}\n\n"
                f"Notizie raccolte:\n\n{raw_context}\n\n"
                "Produci l'analisi macroeconomica strutturata secondo lo schema richiesto."
            )
        }]
    )

    text = response.content[0].text.strip()

    # Pulizia robusta: rimuovi eventuali backtick markdown
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[WARN] JSON parse error: {e}")
        # Fallback: restituisce struttura minima con il testo grezzo
        return {
            "analysis_date": today,
            "macro_narrative": text[:500],
            "fed_stance": "neutral",
            "ecb_stance": "neutral",
            "us_inflation_trend": "stable",
            "eu_inflation_trend": "stable",
            "sector_risk": {
                "Technology":  {"risk_score": 5, "drivers_positive": [], "drivers_negative": [], "outlook_3_6m": "n/a"},
                "Financials":  {"risk_score": 5, "drivers_positive": [], "drivers_negative": [], "outlook_3_6m": "n/a"},
                "Industrials": {"risk_score": 5, "drivers_positive": [], "drivers_negative": [], "outlook_3_6m": "n/a"},
            },
            "overall_market_bias": "neutral",
            "key_events_watch": [],
            "data_quality": "low",
            "warnings": ["JSON parse fallback -- verifica output manualmente"],
        }


# --- Salvataggio su file ------------------------------------------------------

def save_report(analysis: dict) -> Path:
    """Salva il report JSON su file con timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename  = OUTPUT_DIR / f"macro_report_{timestamp}.json"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)

    # Aggiorna anche il file "latest" per uso facile dall'orchestratore
    latest = OUTPUT_DIR / "macro_report_latest.json"
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2, ensure_ascii=False)

    print(f"[INFO] Report salvato: {filename}")
    print(f"[INFO] Latest aggiornato: {latest}")
    return filename


# --- Helpers per l'orchestratore ---------------------------------------------

def load_latest_report() -> dict | None:
    """
    Carica l'ultimo report salvato su disco.
    Usato dall'orchestratore per evitare ricerche ridondanti nella stessa giornata.
    """
    latest = OUTPUT_DIR / "macro_report_latest.json"
    if not latest.exists():
        return None

    with open(latest, "r", encoding="utf-8") as f:
        report = json.load(f)

    # Considera il report valido solo se e di oggi
    today = datetime.now().strftime("%Y-%m-%d")
    if report.get("analysis_date") == today:
        print("[INFO] Report macro di oggi gia disponibile -- skip ricerca web.")
        return report

    return None


def get_macro_context_for_filter(analysis: dict) -> str:
    """
    Estrae una stringa sintetica dall'analisi macro
    da passare come `macro_context` al filter_agent.
    """
    sr  = analysis.get("sector_risk", {})
    fed = analysis.get("fed_stance", "neutral")
    ecb = analysis.get("ecb_stance", "neutral")
    bias = analysis.get("overall_market_bias", "neutral")

    lines = [
        analysis.get("macro_narrative", ""),
        f"Fed stance: {fed} | ECB stance: {ecb} | Market bias: {bias}",
    ]

    for sector, data in sr.items():
        score = data.get("risk_score", 5)
        outlook = data.get("outlook_3_6m", "")
        lines.append(f"{sector} -- risk score {score}/10 -- {outlook}")

    events = analysis.get("key_events_watch", [])
    if events:
        lines.append("Eventi da monitorare: " + ", ".join(events))

    return "\n".join(lines)


# --- Entry point -------------------------------------------------------------

def run_macro_agent(force_refresh: bool = False) -> dict:
    """
    Esegue l'agente macro completo.

    Args:
        force_refresh: Se True, ignora il report salvato e rilancia le ricerche.

    Returns:
        Dict con l'analisi macroeconomica strutturata.
    """
    # Se esiste gia un report di oggi e non forziamo il refresh, lo riusiamo
    if not force_refresh:
        cached = load_latest_report()
        if cached:
            return cached

    # 1. Fetch VIX (quantitativo, rapido)
    print("[INFO] Fetch VIX...")
    vix = fetch_vix()
    print(f"  -> VIX {vix['value']} ({vix['level'].upper()}) -- {vix['bias_hint']}")

    # 2. Raccolta notizie via web search
    raw_context = fetch_all_macro_context()

    # Arricchisce il contesto con il dato VIX prima di passarlo a Claude
    vix_note = f"\n\n=== VIX (Fear Index) ===\n{vix['description']}\n"
    raw_context += vix_note

    # 3. Analisi LLM -> JSON strutturato
    analysis = analyze_macro_context(raw_context)

    # 4. Aggiunge il blocco VIX al JSON di output
    analysis["vix"] = vix

    # 5. Salvataggio su file (sempre)
    save_report(analysis)

    return analysis


# --- Stampa report leggibile -------------------------------------------------

def print_report(analysis: dict) -> None:
    """Stampa un riassunto human-readable del report macro."""
    print("\n" + "=" * 60)
    print(f"  MACRO REPORT -- {analysis.get('analysis_date', 'n/a')}")
    print("=" * 60)
    print(f"\n{analysis.get('macro_narrative', '')}\n")
    print(f"  Fed:    {analysis.get('fed_stance','?').upper()}")
    print(f"  BCE:    {analysis.get('ecb_stance','?').upper()}")
    print(f"  Bias:   {analysis.get('overall_market_bias','?').upper()}")
    print(f"  US CPI: {analysis.get('us_inflation_trend','?')}")
    print(f"  EU CPI: {analysis.get('eu_inflation_trend','?')}")
    print("\n-- RISK SCORE PER SETTORE --")

    for sector, data in analysis.get("sector_risk", {}).items():
        score   = data.get("risk_score", "?")
        outlook = data.get("outlook_3_6m", "")
        if isinstance(score, int):
            bar = "#" * score + "-" * (10 - score)
        else:
            bar = "----------"
        print(f"  {sector:<14} [{bar}]  {score}/10  {outlook}")

    events = analysis.get("key_events_watch", [])
    if events:
        print("\n-- EVENTI DA MONITORARE --")
        for e in events:
            print(f"  * {e}")

    warnings = analysis.get("warnings", [])
    if warnings:
        print("\n-- WARNINGS --")
        for w in warnings:
            print(f"  ! {w}")
    print("=" * 60 + "\n")


# --- Test ---------------------------------------------------------------------

if __name__ == "__main__":
    analysis = run_macro_agent(force_refresh=True)
    print_report(analysis)

    # Mostra la stringa sintetica che verra passata al filter_agent
    print("-- MACRO CONTEXT PER FILTER AGENT --")
    print(get_macro_context_for_filter(analysis))
