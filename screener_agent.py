"""
screener_agent.py -- Agente di Screening Autonomo
Orchestratore multi-agente finanziario

Scopre automaticamente i candidati migliori partendo da più universi azionari:
  - S&P 500 (USA large cap)
  - FTSE MIB (Italia)
  - EuroStoxx 50 (Europa)
  - Russell 2000 (USA small cap)

Flusso: lista ticker per mercato -> pre-filtro qualitativo -> top N per mercato

Dipendenze: pip install yfinance pandas requests python-dotenv
"""

import json
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_DIR = Path("screener_reports")
OUTPUT_DIR.mkdir(exist_ok=True)

# ---- Configurazione per-mercato ----------------------------------------------

MARKET_CONFIG = {
    "sp500": {
        "label":             "S&P 500 (USA)",
        "min_market_cap":    10_000_000_000,   # 10B USD
        "min_avg_volume":     1_000_000,
        "max_pe_forward":     80,
        "min_revenue_growth": -0.10,
        "max_per_market":     7,               # compatibile con sector-based (3+2+2)
    },
    "italy": {
        "label":             "FTSE MIB (Italia)",
        "min_market_cap":     1_000_000_000,   # 1B
        "min_avg_volume":       100_000,
        "max_pe_forward":        80,
        "min_revenue_growth":   -0.10,
        "max_per_market":         5,
    },
    "europe": {
        "label":             "EuroStoxx 50 (Europa)",
        "min_market_cap":     5_000_000_000,   # 5B
        "min_avg_volume":       200_000,
        "max_pe_forward":        80,
        "min_revenue_growth":   -0.10,
        "max_per_market":         5,
    },
    "russell2000": {
        "label":             "Russell 2000 (USA Small Cap)",
        "min_market_cap":       200_000_000,   # 200M
        "min_avg_volume":       100_000,
        "max_pe_forward":        80,
        "min_revenue_growth":   -0.10,
        "max_per_market":         5,
    },
}

# ---- Settori target per S&P 500 ---------------------------------------------

TARGET_SECTORS = {
    "Technology":  {"yf_names": ["Technology"],                        "max_picks": 3},
    "Financials":  {"yf_names": ["Financial Services", "Banking"],     "max_picks": 2},
    "Industrials": {"yf_names": ["Industrials"],                       "max_picks": 2},
}

# ---- Liste ticker statiche ---------------------------------------------------

FTSE_MIB_TICKERS = [
    "ENI.MI", "ENEL.MI", "ISP.MI", "UCG.MI", "STLAM.MI", "RACE.MI",
    "TRN.MI", "SRG.MI", "G.MI", "LDO.MI", "PRY.MI", "MONC.MI",
    "MB.MI", "BAMI.MI", "STM.MI", "A2A.MI", "NEXI.MI", "AMP.MI",
    "EXO.MI", "BC.MI", "SPM.MI", "ERG.MI", "IP.MI", "FBK.MI",
    "REC.MI", "PIRC.MI", "TIT.MI", "CPR.MI", "BMED.MI", "PST.MI",
    "INWIT.MI", "HERA.MI", "DIA.MI", "SFER.MI",
]

EUROSTOXX50_TICKERS = [
    # Francia
    "AI.PA", "AIR.PA", "AXA.PA", "BN.PA", "BNP.PA", "DG.PA",
    "GLE.PA", "MC.PA", "OR.PA", "RI.PA", "SAN.PA", "SGO.PA",
    "TTE.PA", "EL.PA", "KER.PA", "SU.PA", "VIE.PA", "ML.PA",
    # Germania
    "ADS.DE", "ALV.DE", "BAYN.DE", "BMW.DE", "BAS.DE", "DB1.DE",
    "DTE.DE", "IFX.DE", "MBG.DE", "MUV2.DE", "RWE.DE", "SAP.DE", "SIE.DE",
    # Paesi Bassi
    "AD.AS", "ASML.AS", "HEIA.AS", "INGA.AS", "PHIA.AS", "WKL.AS",
    # Spagna
    "ACS.MC", "BBVA.MC", "IBE.MC", "ITX.MC", "REP.MC", "SAN.MC",
    # Belgio
    "ABI.BR",
    # Finlandia
    "NOKIA.HE",
    # Italia (presenti in EuroStoxx50)
    "ENI.MI", "ENEL.MI", "ISP.MI", "UCG.MI",
]

RUSSELL2000_TICKERS = [
    # Technology / Semiconduttori
    "AMBA", "ACMR", "NVTS", "PRGS", "VIAV", "MGNI", "KLIC", "DIOD",
    # Healthcare / Biotech
    "TMDX", "INSP", "RVMD", "MMSI", "ITCI", "ACCD", "MLAB", "GMED",
    # Finanziari
    "BANC", "BHLB", "FHB", "WABC", "PFBC", "TCBK", "HOPE", "COLB",
    # Industriali
    "UFPI", "PRIM", "POWL", "MYRG", "MATX", "PATK", "KFRC", "ATKR",
    # Consumer Discretionary
    "BOOT", "SHAK", "CAKE", "LGIH", "IPAR", "WRLD", "BRBR",
    # Consumer Staples
    "CALM", "SMPL", "JJSF", "LANC",
    # Energia
    "MTDR", "CVI", "TALO", "CIVI", "MGY", "REX",
    # REITs
    "CTRE", "IIPR", "NXRT", "PLYM", "NHI", "GMRE",
    # Materiali
    "HWKN", "AZTA", "TREC",
]

# ---- Scoring pre-screening ---------------------------------------------------

def _pre_score(info: dict) -> float:
    """Score veloce 0-100 per ordinare i candidati."""
    score = 0.0

    rg = info.get("revenueGrowth")
    if rg is not None:
        if rg > 0.20:   score += 30
        elif rg > 0.10: score += 22
        elif rg > 0.0:  score += 15
        else:           score += 5

    roe = info.get("returnOnEquity")
    if roe is not None:
        if roe > 0.30:   score += 25
        elif roe > 0.15: score += 18
        elif roe > 0.05: score += 10
        else:            score += 3

    pe = info.get("forwardPE")
    if pe is not None and pe > 0:
        if pe < 15:   score += 25
        elif pe < 25: score += 18
        elif pe < 40: score += 10
        elif pe < 60: score += 5

    price  = info.get("currentPrice") or info.get("regularMarketPrice", 0)
    low52  = info.get("fiftyTwoWeekLow",  0)
    high52 = info.get("fiftyTwoWeekHigh", 0)
    if price and high52 and low52 and (high52 - low52) > 0:
        pos = (price - low52) / (high52 - low52)
        if pos > 0.70:   score += 20
        elif pos > 0.50: score += 14
        elif pos > 0.30: score += 8
        else:            score += 2

    return round(score, 1)


def _passes_pre_filters(info: dict, mkt_cfg: dict) -> tuple[bool, str]:
    """Verifica i filtri minimi per il mercato specificato."""
    mc = info.get("marketCap", 0)
    if mc and mc < mkt_cfg["min_market_cap"]:
        return False, f"market cap {mc/1e9:.1f}B < {mkt_cfg['min_market_cap']/1e9:.0f}B"

    vol = info.get("averageVolume", 0)
    if vol and vol < mkt_cfg["min_avg_volume"]:
        return False, f"volume {vol:,} < {mkt_cfg['min_avg_volume']:,}"

    pe = info.get("forwardPE")
    if pe and pe > mkt_cfg["max_pe_forward"]:
        return False, f"P/E forward {pe:.0f} > {mkt_cfg['max_pe_forward']}"

    rg = info.get("revenueGrowth")
    if rg is not None and rg < mkt_cfg["min_revenue_growth"]:
        return False, f"revenue growth {rg*100:.1f}% < {mkt_cfg['min_revenue_growth']*100:.0f}%"

    return True, "ok"


# ---- Fetch lista S&P500 -----------------------------------------------------

def fetch_sp500_list() -> pd.DataFrame:
    """Scarica la lista aggiornata dei componenti S&P500 da GitHub."""
    print("[SCREENER] Download lista S&P500 da GitHub...")
    try:
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
        df = pd.read_csv(url)[["Symbol", "Security", "GICS Sector"]].copy()
        df.columns = ["ticker", "name", "sector"]
        df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
        print(f"  -> {len(df)} componenti caricati")
        return df
    except Exception as e:
        print(f"  [ERR] Download S&P500 fallito: {e}")
        return pd.DataFrame(columns=["ticker", "name", "sector"])


def _map_gics_to_target(gics_sector: str) -> str | None:
    mapping = {
        "Information Technology":  "Technology",
        "Communication Services":  "Technology",
        "Financials":              "Financials",
        "Industrials":             "Industrials",
    }
    return mapping.get(gics_sector)


# ---- Fetch info singolo ticker -----------------------------------------------

def _fetch_ticker_info(ticker: str) -> dict | None:
    """Recupera info da Yahoo Finance per un singolo ticker."""
    try:
        info = yf.Ticker(ticker).info
        if not info or (info.get("regularMarketPrice") is None and info.get("currentPrice") is None):
            return None
        return info
    except Exception:
        return None


# ---- Screening S&P500 per settore -------------------------------------------

def screen_sector(
    candidates: list[str],
    sector_label: str,
    max_picks: int,
    mkt_cfg: dict,
    workers: int = 5,
) -> list[dict]:
    """Screening per un settore dell'S&P500."""
    print(f"\n[SCREENER] Settore {sector_label} -- {len(candidates)} candidati")
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_ticker_info, t): t for t in candidates}
        done = 0
        for fut in as_completed(futures):
            t    = futures[fut]
            info = fut.result()
            done += 1
            if done % 20 == 0:
                print(f"  ... {done}/{len(candidates)} analizzati")
            if info is None:
                continue
            passed, _ = _passes_pre_filters(info, mkt_cfg)
            if not passed:
                continue
            score = _pre_score(info)
            results.append({
                "ticker":         t,
                "name":           info.get("longName", t),
                "sector":         sector_label,
                "market":         "sp500",
                "market_label":   "S&P 500",
                "pre_score":      score,
                "market_cap_b":   round((info.get("marketCap") or 0) / 1e9, 1),
                "pe_forward":     info.get("forwardPE"),
                "revenue_growth": info.get("revenueGrowth"),
                "roe":            info.get("returnOnEquity"),
                "price":          info.get("currentPrice") or info.get("regularMarketPrice"),
            })

    results.sort(key=lambda x: x["pre_score"], reverse=True)
    top = results[:max_picks]
    print(f"  -> Selezionati: {[r['ticker'] for r in top]}")
    return top


# ---- Screening mercato flat (lista fissa) ------------------------------------

def screen_flat_market(
    tickers: list[str],
    market_key: str,
    mkt_cfg: dict,
    max_picks: int,
    workers: int = 8,
) -> list[dict]:
    """
    Screening per mercati con lista fissa (Italy, Europe, Russell 2000).
    Non filtra per settore: analizza tutti i ticker e prende i top N.
    """
    label = mkt_cfg["label"]
    print(f"\n[SCREENER] {label} -- {len(tickers)} candidati")
    results = []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_ticker_info, t): t for t in tickers}
        done = 0
        for fut in as_completed(futures):
            t    = futures[fut]
            info = fut.result()
            done += 1
            if done % 10 == 0:
                print(f"  ... {done}/{len(tickers)} analizzati")
            if info is None:
                continue
            passed, reason = _passes_pre_filters(info, mkt_cfg)
            if not passed:
                continue
            score = _pre_score(info)
            sector = info.get("sector") or info.get("industryDisp") or "n/a"
            results.append({
                "ticker":         t,
                "name":           info.get("longName", t),
                "sector":         sector,
                "market":         market_key,
                "market_label":   label,
                "pre_score":      score,
                "market_cap_b":   round((info.get("marketCap") or 0) / 1e9, 1),
                "pe_forward":     info.get("forwardPE"),
                "revenue_growth": info.get("revenueGrowth"),
                "roe":            info.get("returnOnEquity"),
                "price":          info.get("currentPrice") or info.get("regularMarketPrice"),
            })

    results.sort(key=lambda x: x["pre_score"], reverse=True)
    top = results[:max_picks]
    print(f"  -> Selezionati: {[r['ticker'] for r in top]}")
    return top


# ---- Entry point -------------------------------------------------------------

def run_screener(
    markets: list[str] | None = None,
    sectors: list[str] | None = None,
    max_per_sector: int | None = None,
    max_per_market: int | None = None,
) -> dict:
    """
    Esegue lo screening autonomo su uno o più mercati.

    Args:
        markets:         Mercati da analizzare: "sp500", "italy", "europe", "russell2000"
                         (default: ["sp500"])
        sectors:         Filtra settori S&P500 (default: tutti i TARGET_SECTORS)
        max_per_sector:  Override max pick per settore S&P500
        max_per_market:  Override max pick per mercato flat

    Returns:
        {"tickers": [...], "details": [...], "summary": {...}}
    """
    if markets is None:
        markets = ["sp500"]

    # Deduplica mantenendo l'ordine
    markets = list(dict.fromkeys(markets))

    all_selected = []
    seen_tickers = set()

    for mkt_key in markets:
        mkt_cfg = MARKET_CONFIG.get(mkt_key)
        if mkt_cfg is None:
            print(f"[SCREENER] Mercato sconosciuto: {mkt_key} -- skip")
            continue

        # ---- S&P 500: screening per settore ----------------------------------
        if mkt_key == "sp500":
            sp500 = fetch_sp500_list()
            if sp500.empty:
                sp500 = pd.DataFrame({
                    "ticker": ["NVDA","MSFT","AAPL","GOOGL","META","AVGO","AMD",
                               "JPM","BAC","GS","MS","WFC",
                               "CAT","HON","GE","RTX","UNP","LMT"],
                    "sector": ["Information Technology"]*7 + ["Financials"]*5 + ["Industrials"]*6,
                })

            target = {k: v for k, v in TARGET_SECTORS.items()
                      if sectors is None or k in sectors}

            for sector_label, cfg in target.items():
                n_picks = max_per_sector or cfg["max_picks"]
                gics_names = [k for k, v in {
                    "Information Technology": "Technology",
                    "Communication Services": "Technology",
                    "Financials":             "Financials",
                    "Industrials":            "Industrials",
                }.items() if v == sector_label]

                candidates = [t for t in sp500[sp500["sector"].isin(gics_names)]["ticker"].tolist()
                              if t not in seen_tickers]
                if not candidates:
                    continue

                selected = screen_sector(candidates, sector_label, n_picks, mkt_cfg)
                for r in selected:
                    if r["ticker"] not in seen_tickers:
                        seen_tickers.add(r["ticker"])
                        all_selected.append(r)

        # ---- Italia (FTSE MIB) -----------------------------------------------
        elif mkt_key == "italy":
            candidates = [t for t in FTSE_MIB_TICKERS if t not in seen_tickers]
            n_picks = max_per_market or mkt_cfg["max_per_market"]
            selected = screen_flat_market(candidates, mkt_key, mkt_cfg, n_picks)
            for r in selected:
                if r["ticker"] not in seen_tickers:
                    seen_tickers.add(r["ticker"])
                    all_selected.append(r)

        # ---- Europa (EuroStoxx 50) -------------------------------------------
        elif mkt_key == "europe":
            candidates = [t for t in EUROSTOXX50_TICKERS if t not in seen_tickers]
            n_picks = max_per_market or mkt_cfg["max_per_market"]
            selected = screen_flat_market(candidates, mkt_key, mkt_cfg, n_picks)
            for r in selected:
                if r["ticker"] not in seen_tickers:
                    seen_tickers.add(r["ticker"])
                    all_selected.append(r)

        # ---- Russell 2000 ---------------------------------------------------
        elif mkt_key == "russell2000":
            candidates = [t for t in RUSSELL2000_TICKERS if t not in seen_tickers]
            n_picks = max_per_market or mkt_cfg["max_per_market"]
            selected = screen_flat_market(candidates, mkt_key, mkt_cfg, n_picks)
            for r in selected:
                if r["ticker"] not in seen_tickers:
                    seen_tickers.add(r["ticker"])
                    all_selected.append(r)

    # Salva report
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path  = OUTPUT_DIR / f"screener_{timestamp}.json"
    latest    = OUTPUT_DIR / "screener_latest.json"
    for path in [out_path, latest]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(all_selected, f, indent=2, default=str, ensure_ascii=False)

    tickers = [r["ticker"] for r in all_selected]
    print(f"\n[SCREENER] Ticker selezionati: {tickers}")

    return {
        "tickers": tickers,
        "details": all_selected,
        "summary": {
            "markets_screened": markets,
            "total_selected":   len(tickers),
            "by_market":        {m: sum(1 for r in all_selected if r.get("market") == m) for m in markets},
            "report_path":      str(out_path),
        }
    }


# ---- Test standalone ---------------------------------------------------------

if __name__ == "__main__":
    import sys
    mkts = sys.argv[1:] if len(sys.argv) > 1 else ["sp500"]
    result = run_screener(markets=mkts)
    print("\n-- CANDIDATI SELEZIONATI --")
    for r in result["details"]:
        pe  = f"P/E {r['pe_forward']:.1f}" if r["pe_forward"] else "P/E n/a"
        rg  = f"RevG {r['revenue_growth']*100:.1f}%" if r["revenue_growth"] else ""
        roe = f"ROE {r['roe']*100:.1f}%" if r["roe"] else ""
        mkt = r.get("market_label", "")
        print(f"  {r['ticker']:<10} [{mkt}]  score={r['pre_score']:5.1f}  {pe}  {rg}  {roe}")
