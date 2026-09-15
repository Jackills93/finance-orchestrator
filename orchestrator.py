"""
orchestrator.py -- Orchestratore Multi-Agente Finanziario
Coordina in sequenza: macro_agent -> filter_agent -> technical_agent
Produce la raccomandazione finale con score composito.

Dipendenze: pip install anthropic requests pandas python-dotenv
Variabili d'ambiente: ANTHROPIC_API_KEY, FMP_API_KEY, ALPHA_VANTAGE_API_KEY
"""

import os
import json
from datetime import datetime
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()
client     = Anthropic()
OUTPUT_DIR = Path("orchestrator_output")
OUTPUT_DIR.mkdir(exist_ok=True)

# Importa gli agenti
from macro_agent     import run_macro_agent, get_macro_context_for_filter
from filter_agent    import run_filter_agent
from technical_agent import run_technical_agent
from screener_agent  import run_screener


# --- Aggiustamento macro sullo score fondamentale -----------------------------

MACRO_ADJUSTMENTS = {
    (1, 3):   +5,
    (4, 6):    0,
    (7, 8):   -5,
    (9, 10): -10,
}

def apply_macro_adjustment(filter_results: list, sector_risk: dict) -> list:
    """
    Aggiusta lo score fondamentale in base al rischio macro per settore.
    Un settore con risk_score 2 riceve +5 punti, uno con 9 riceve -10.
    """
    for company in filter_results:
        sector     = company.get("sector", "Technology")
        risk_score = sector_risk.get(sector, {}).get("risk_score", 5)
        adj        = next(
            (v for (lo, hi), v in MACRO_ADJUSTMENTS.items() if lo <= risk_score <= hi),
            0
        )
        company["total_score"]       = max(0, min(100, company["total_score"] + adj))
        company["macro_adjustment"]  = adj
        company["macro_risk_score"]  = risk_score
    return filter_results


# --- Score composito finale ---------------------------------------------------

def build_final_ranking(technical_results: list) -> list:
    """
    Costruisce il ranking finale combinando score tecnico e fondamentale.
    Pesi finali: fondamentale 60% . tecnico 40% (gia calcolato in technical_agent).
    Aggiunge il segnale operativo definitivo.
    """
    ranked = []
    for r in technical_results:
        composite = r.get("composite_score") or r.get("total_score") or 0
        signal    = r.get("signal", "HOLD")

        # Dati tecnici insufficienti (es. rate limit Alpha Vantage): escludi dal ranking
        if signal == "INSUFFICIENT_DATA":
            print(f"  [SKIP] {r['ticker']} escluso -- dati tecnici insufficienti")
            continue
        entry     = r.get("daily_entry", {}).get("entry_signal", "n/a")

        # Segnale operativo finale
        if composite >= 70 and signal == "BUY":
            final_signal = "STRONG_BUY"
        elif composite >= 60 and signal == "BUY":
            final_signal = "BUY"
        elif composite <= 30 or signal == "SELL":
            final_signal = "SELL"
        else:
            final_signal = "HOLD"

        ranked.append({
            "ticker":           r["ticker"],
            "composite_score":  composite,
            "final_signal":     final_signal,
            "technical_score":  r.get("total_score"),
            "fundamental_score":r.get("fundamental_score"),
            "macro_adjustment": r.get("macro_adjustment", 0),
            "confidence":       r.get("confidence", "medium"),
            "price":            r.get("price"),
            "ma20":             r.get("ma20"),
            "ma50":             r.get("ma50"),
            "rsi":              r.get("rsi"),
            "entry_signal":     entry,
            "entry_advice":     r.get("entry_advice", ""),
            "key_signals":      r.get("key_signals", []),
            "risk_note":        r.get("risk_note", ""),
            "sector":           r.get("sector", ""),
            "suggested_sl":     r.get("suggested_sl"),
            "suggested_tp":     r.get("suggested_tp"),
            "suggested_qty":    r.get("suggested_qty"),
            "risk_amount":      r.get("risk_amount"),
            "position_value":   r.get("position_value"),
            "sizing_note":      r.get("sizing_note", ""),
            "sector_etf":       r.get("sector_etf"),
            "benchmark":        r.get("benchmark"),
        })

    ranked.sort(key=lambda x: x["composite_score"], reverse=True)
    return ranked


# --- Report finale ------------------------------------------------------------

FINAL_REPORT_SYSTEM = """Sei un portfolio manager senior. Ricevi il ranking finale di asset
dopo analisi macro, fondamentale e tecnica. Produci:
1. "recommendation": la tua raccomandazione operativa sull'asset top (2-3 righe, italiano).
2. "macro_context_applied": come il contesto macro ha influenzato la selezione (1-2 righe).
3. "key_risks": lista di 2-3 rischi principali da monitorare.
4. "alternative": secondo asset in lista e perche e alternativa valida (1 riga).

Rispondi SOLO con JSON valido senza markdown:
{"recommendation":"...","macro_context_applied":"...","key_risks":["..."],"alternative":"..."}"""


def generate_final_report(ranking: list, macro_analysis: dict) -> dict:
    context = json.dumps({
        "top_3_assets":   ranking[:3],
        "macro_narrative": macro_analysis.get("macro_narrative", ""),
        "market_bias":    macro_analysis.get("overall_market_bias", "neutral"),
        "fed_stance":     macro_analysis.get("fed_stance", "neutral"),
        "ecb_stance":     macro_analysis.get("ecb_stance", "neutral"),
    }, indent=2, default=str)

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system=FINAL_REPORT_SYSTEM,
        messages=[{"role": "user", "content": context}]
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text  = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(text)
    except Exception:
        return {"recommendation": text[:300], "key_risks": [], "alternative": "", "macro_context_applied": ""}


# --- Stampa raccomandazione ---------------------------------------------------

def print_final_recommendation(ranking: list, report: dict, macro: dict) -> None:
    top = ranking[0] if ranking else {}
    print("\n" + "=" * 65)
    print(f"  RACCOMANDAZIONE FINALE  --  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)

    if not top:
        print("  Nessun asset promosso.")
        return

    signal_tag = {"STRONG_BUY": "[**]", "BUY": "[+]", "HOLD": "[~]", "SELL": "[-]"}.get(top["final_signal"], "[?]")
    print(f"\n  {signal_tag}  {top['ticker']}  --  {top['final_signal']}")
    print(f"  Prezzo:             {top.get('price', 'n/a')}")
    print(f"  Score composito:    {top['composite_score']}/100")
    print(f"  Score fondamentale: {top.get('fundamental_score', 'n/a')}/100")
    print(f"  Score tecnico:      {top.get('technical_score', 'n/a')}/100")
    print(f"  Macro adjustment:   {top.get('macro_adjustment', 0):+d} pt  (risk score settore)")
    print(f"  Confidenza:         {top['confidence'].upper()}")
    print(f"  MA50/MA200:         {top.get('ma50', 'n/a')} / {top.get('ma200', 'n/a')}")
    print(f"  RSI settimanale:    {top.get('rsi', 'n/a')}")
    print(f"  Entrata giornaliera:{top.get('entry_signal', 'n/a')}")

    print(f"\n  {report.get('recommendation','')}")

    if top.get("key_signals"):
        print("\n  Segnali tecnici:")
        for s in top["key_signals"]:
            print(f"    * {s}")

    print(f"\n  Contesto macro: {report.get('macro_context_applied','')}")

    if report.get("key_risks"):
        print("\n  Rischi da monitorare:")
        for r in report["key_risks"]:
            print(f"    ! {r}")

    if report.get("alternative"):
        print(f"\n  Alternativa:  {report['alternative']}")

    print("\n-- RANKING COMPLETO --")
    for i, r in enumerate(ranking, 1):
        score = r["composite_score"]
        bar = "#" * (score // 10) + "-" * (10 - score // 10)
        print(f"  {i}. {r['ticker']:<6}  [{bar}]  {score:3d}/100  {r['final_signal']}")

    print("=" * 65 + "\n")


# --- Entry point principale ---------------------------------------------------

def run_orchestrator(
    tickers: list[str] | None = None,
    force_macro_refresh: bool = False,
    auto_screen: bool = False,
    screen_sectors: list[str] | None = None,
    max_per_sector: int | None = None,
    markets: list[str] | None = None,
) -> dict:
    """
    Esegue il pipeline completo: (screener ->) macro -> filtro -> tecnico -> raccomandazione.

    Args:
        tickers:              Lista di ticker da analizzare. Se None e auto_screen=True,
                              i ticker vengono scelti automaticamente dallo screener.
        force_macro_refresh:  Se True, rilancia la ricerca macro anche se c'e un report odierno.
        auto_screen:          Se True, usa lo screener per trovare automaticamente i ticker.
        screen_sectors:       Filtro settori per lo screener (default: tutti).
        max_per_sector:       Numero massimo di pick per settore (default: config screener).
        markets:              Mercati da analizzare: "sp500", "italy", "europe", "russell2000"

    Returns:
        Dict con ranking finale, report narrativo e output intermedi di ogni agente
    """
    print("\n" + "-" * 65)
    print("  ORCHESTRATORE FINANZIARIO  -- avvio pipeline")
    print("-" * 65)

    # -- STEP 0 (opzionale): Screener automatico -------------------------------
    _screener_market_map = {}   # ticker -> {market, market_label}
    if auto_screen or tickers is None:
        mkt_list = markets or ["sp500"]
        print(f"\n[STEP 0] Screening autonomo -- mercati: {mkt_list}")
        screen_result = run_screener(
            markets=mkt_list,
            sectors=screen_sectors,
            max_per_sector=max_per_sector,
        )
        tickers = screen_result["tickers"]
        if not tickers:
            print("  Nessun candidato trovato dallo screener.")
            return {}
        _screener_market_map = {
            r["ticker"]: {"market": r.get("market","sp500"), "market_label": r.get("market_label","S&P 500")}
            for r in screen_result.get("details", [])
        }
        print(f"  -> Ticker selezionati: {tickers}")

    # -- STEP 1: Agente Macro --------------------------------------------------
    print("\n[STEP 1/3] Analisi macroeconomica...")
    macro_analysis   = run_macro_agent(force_refresh=force_macro_refresh)
    macro_context    = get_macro_context_for_filter(macro_analysis)
    sector_risk      = macro_analysis.get("sector_risk", {})
    print(f"  -> Market bias: {macro_analysis.get('overall_market_bias','?').upper()}")
    print(f"  -> Fed: {macro_analysis.get('fed_stance','?')} | BCE: {macro_analysis.get('ecb_stance','?')}")

    # -- STEP 2: Agente Filtro Fondamentale ------------------------------------
    print("\n[STEP 2/3] Filtro fondamentale...")
    filter_output    = run_filter_agent(tickers=tickers, macro_context=macro_context)
    filter_results   = filter_output.get("filter_results", [])

    # Applica aggiustamento macro agli score fondamentali
    filter_results   = apply_macro_adjustment(filter_results, sector_risk)

    # Solo le aziende promosse passano all'analisi tecnica
    promoted_tickers = [r["ticker"] for r in filter_results if r.get("pass_to_technical")]
    print(f"  -> Promosse all'analisi tecnica: {promoted_tickers}")

    if not promoted_tickers:
        print("\n  Nessuna azienda ha superato il filtro fondamentale.")
        return {
            "ranking":          [],
            "final_report":     {},
            "macro_analysis":   macro_analysis,
            "filter_output":    filter_output,
            "technical_output": {},
        }

    # -- STEP 3: Agente Tecnico ------------------------------------------------
    print("\n[STEP 3/3] Analisi tecnica...")
    technical_output = run_technical_agent(
        tickers=promoted_tickers,
        filter_results=filter_results,
    )
    technical_results = technical_output.get("technical_results", [])

    # -- RANKING FINALE --------------------------------------------------------
    ranking      = build_final_ranking(technical_results)

    # Aggiungi info mercato dal screener (se disponibile)
    for r in ranking:
        mkt_info = _screener_market_map.get(r["ticker"], {})
        r["market"]       = mkt_info.get("market",       "sp500")
        r["market_label"] = mkt_info.get("market_label", "S&P 500")

    final_report = generate_final_report(ranking, macro_analysis)

    # Stampa a console
    print_final_recommendation(ranking, final_report, macro_analysis)

    # -- SALVATAGGIO -----------------------------------------------------------
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    full_output = {
        "run_timestamp":    datetime.now().isoformat(),
        "tickers_analyzed": tickers,
        "ranking":          ranking,
        "final_report":     final_report,
        "macro_analysis":   macro_analysis,
        "filter_summary":   filter_output.get("summary", {}),
        "technical_summary":technical_output.get("summary", {}),
    }

    out_path = OUTPUT_DIR / f"run_{timestamp}.json"
    latest   = OUTPUT_DIR / "run_latest.json"
    for path in [out_path, latest]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(full_output, f, indent=2, default=str, ensure_ascii=False)

    print(f"[INFO] Output completo salvato: {out_path}")
    return full_output


# --- CLI ----------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Orchestratore multi-agente finanziario")
    parser.add_argument(
        "tickers",
        nargs="*",
        help="Ticker da analizzare (es. ASML NVDA JPM). Ometti con --auto-screen.",
    )
    parser.add_argument(
        "--auto-screen",
        action="store_true",
        help="Scopri automaticamente i ticker migliori dall'S&P500 tramite screener",
    )
    parser.add_argument(
        "--sectors",
        nargs="+",
        choices=["Technology", "Financials", "Industrials"],
        help="Settori da includere nello screener (default: tutti)",
    )
    parser.add_argument(
        "--max-per-sector",
        type=int,
        default=None,
        help="Numero massimo di ticker per settore nello screener (default: 3/2/2)",
    )
    parser.add_argument(
        "--markets",
        nargs="+",
        choices=["sp500", "italy", "europe", "russell2000"],
        default=["sp500"],
        help="Mercati da analizzare (default: sp500)",
    )
    parser.add_argument(
        "--refresh-macro",
        action="store_true",
        help="Forza il refresh dell'analisi macro anche se esiste un report odierno",
    )
    args = parser.parse_args()

    if not args.tickers and not args.auto_screen:
        parser.error("Specifica i ticker oppure usa --auto-screen")

    run_orchestrator(
        tickers=[t.upper() for t in args.tickers] if args.tickers else None,
        force_macro_refresh=args.refresh_macro,
        auto_screen=args.auto_screen,
        screen_sectors=args.sectors,
        max_per_sector=args.max_per_sector,
        markets=args.markets,
    )
