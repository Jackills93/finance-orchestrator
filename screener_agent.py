"""
screener_agent.py -- Agente di Screening Autonomo
Orchestratore multi-agente finanziario

Scopre automaticamente i candidati migliori partendo da più universi azionari:
  - S&P 500           (USA large cap)   -> lista da GitHub (datasets/s-and-p-500-companies)
  - S&P SmallCap 600  (USA small cap)   -> lista da Wikipedia
  - FTSE MIB          (Italia)          -> lista da Wikipedia
  - EURO STOXX 50     (Europa)          -> lista da Wikipedia

Flusso per ogni mercato:
  1. composizione dell'indice (scaricata una volta al giorno, con copia salvata
     e lista di riserva interna se il download fallisce)
  2. solo i settori della pipeline (Technology / Financials / Industrials, vedi sectors.py)
  3. liquidità: controvalore medio giornaliero, calcolato in blocco dai prezzi
  4. filtri fondamentali minimi (market cap, P/E forward, crescita ricavi)
  5. punteggio preliminare relativo al settore (percentili) e top N per settore

Dipendenze: pip install yfinance pandas requests lxml python-dotenv
"""

import io
import json
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from sectors import TARGET_SECTORS, normalize_sector

OUTPUT_DIR = Path("screener_reports")
OUTPUT_DIR.mkdir(exist_ok=True)
UNIVERSE_DIR = OUTPUT_DIR / "universe"
UNIVERSE_DIR.mkdir(exist_ok=True)

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (finance-orchestrator screener)"}

# ---- Configurazione per-mercato ----------------------------------------------
# min_avg_value: controvalore medio giornaliero (prezzo x volume, ultimi 60 giorni)
# nella valuta di quotazione. Sostituisce il vecchio filtro sul numero di azioni,
# che penalizzava i titoli con prezzo alto e favoriva quelli con prezzo basso.

MARKET_CONFIG = {
    "sp500": {
        "label":              "S&P 500 (USA)",
        "min_market_cap":     10_000_000_000,
        "min_avg_value":      50_000_000,
        "max_pe_forward":     80,
        "min_revenue_growth": -0.10,
        "max_picks":          {"Technology": 3, "Financials": 2, "Industrials": 2},
        "max_total":          None,
        "min_universe":       400,
    },
    "sp600": {
        "label":              "S&P SmallCap 600 (USA)",
        "min_market_cap":     300_000_000,
        "min_avg_value":      3_000_000,
        "max_pe_forward":     80,
        "min_revenue_growth": -0.10,
        "max_picks":          {"Technology": 2, "Financials": 2, "Industrials": 2},
        "max_total":          5,
        "min_universe":       500,
    },
    "italy": {
        "label":              "FTSE MIB (Italia)",
        "min_market_cap":     1_000_000_000,
        "min_avg_value":      5_000_000,
        "max_pe_forward":     80,
        "min_revenue_growth": -0.10,
        "max_picks":          {"Technology": 2, "Financials": 2, "Industrials": 2},
        "max_total":          5,
        "min_universe":       30,
    },
    "europe": {
        "label":              "EURO STOXX 50 (Europa)",
        "min_market_cap":     5_000_000_000,
        "min_avg_value":      20_000_000,
        "max_pe_forward":     80,
        "min_revenue_growth": -0.10,
        "max_picks":          {"Technology": 2, "Financials": 2, "Industrials": 2},
        "max_total":          5,
        "min_universe":       40,
    },
}

# Nome storico del mercato small cap (la lista fissa "Russell 2000" non e' piu' usata)
MARKET_ALIASES = {"russell2000": "sp600"}

# Pesi del punteggio preliminare (percentili dentro il settore)
PRE_SCORE_WEIGHTS = {
    "revenue_growth": 0.30,
    "roe":            0.25,
    "pe_forward":     0.25,   # piu' basso = meglio
    "pos_52w":        0.20,   # posizione nel range di 52 settimane
}

# ---- Liste di riserva --------------------------------------------------------
# Usate solo se il download fallisce e non esiste una copia salvata.
# Composizione Wikipedia del 2026-09-19, gia' ristretta ai tre settori.

FTSE_MIB_FALLBACK = {
    "Technology":  ["INW.MI", "STMMI.MI", "TIT.MI"],
    "Financials":  ["AZM.MI", "BAMI.MI", "BMED.MI", "BMPS.MI", "BPE.MI", "FBK.MI", "G.MI",
                    "ISP.MI", "MB.MI", "PST.MI", "UCG.MI", "UNI.MI"],
    "Industrials": ["AVIO.MI", "BZU.MI", "FCT.MI", "IVG.MI", "LDO.MI", "NEXI.MI", "PRY.MI"],
}

EUROSTOXX50_FALLBACK = {
    "Technology":  ["ASML.AS", "DTE.DE", "IFX.DE", "SAP.DE"],
    "Financials":  ["ADYEN.AS", "ALV.DE", "BBVA.MC", "BNP.PA", "CS.PA", "DB1.DE", "DBK.DE",
                    "INGA.AS", "ISP.MI", "MUV2.DE", "NDA-FI.HE", "SAN.MC", "UCG.MI"],
    "Industrials": ["AIR.PA", "DG.PA", "DHL.DE", "ENR.DE", "RHM.DE", "SAF.PA", "SGO.PA",
                    "SIE.DE", "SU.PA", "WKL.AS"],
}

SP500_FALLBACK = {
    "Technology":  ["NVDA", "MSFT", "AAPL", "GOOGL", "META", "AVGO", "AMD"],
    "Financials":  ["JPM", "BAC", "GS", "MS", "WFC"],
    "Industrials": ["CAT", "HON", "GE", "RTX", "UNP", "LMT"],
}

_FALLBACKS = {"sp500": SP500_FALLBACK, "italy": FTSE_MIB_FALLBACK, "europe": EUROSTOXX50_FALLBACK}


# ---- Composizione degli indici -----------------------------------------------

def _wikipedia_table(url: str, required_cols: list[str]) -> pd.DataFrame:
    html = requests.get(url, headers=HTTP_HEADERS, timeout=20).text
    for table in pd.read_html(io.StringIO(html)):
        if set(required_cols) <= set(table.columns) and len(table) >= 30:
            return table
    raise ValueError(f"tabella con colonne {required_cols} non trovata in {url}")


def _rows(df: pd.DataFrame, ticker_col: str, name_col: str, sector_col: str,
          yahoo_dashes: bool = False) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        ticker = str(r[ticker_col]).strip()
        if yahoo_dashes:
            ticker = ticker.replace(".", "-")   # BRK.B -> BRK-B (formato Yahoo)
        rows.append({"ticker": ticker, "name": str(r[name_col]).strip(),
                     "raw_sector": str(r[sector_col]).strip()})
    return rows


def _download_universe(market: str) -> list[dict]:
    if market == "sp500":
        print("[SCREENER] Download lista S&P500 da GitHub...")
        df = pd.read_csv("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv")
        return _rows(df, "Symbol", "Security", "GICS Sector", yahoo_dashes=True)
    if market == "sp600":
        print("[SCREENER] Download lista S&P SmallCap 600 da Wikipedia...")
        df = _wikipedia_table("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", ["Symbol", "GICS Sector"])
        return _rows(df, "Symbol", "Security", "GICS Sector", yahoo_dashes=True)
    if market == "italy":
        print("[SCREENER] Download composizione FTSE MIB da Wikipedia...")
        df = _wikipedia_table("https://en.wikipedia.org/wiki/FTSE_MIB", ["Ticker", "ICB Sector"])
        return _rows(df, "Ticker", "Company", "ICB Sector")
    if market == "europe":
        print("[SCREENER] Download composizione EURO STOXX 50 da Wikipedia...")
        df = _wikipedia_table("https://en.wikipedia.org/wiki/EURO_STOXX_50", ["Ticker", "Sector"])
        return _rows(df, "Ticker", "Name", "Sector")
    raise ValueError(f"mercato sconosciuto: {market}")


def load_universe(market: str) -> tuple[list[dict], str]:
    """
    Composizione dell'indice con settore normalizzato.
    Ordine: copia di oggi -> download -> ultima copia salvata -> lista di riserva interna.
    Restituisce (righe, descrizione della fonte usata).
    """
    cache = UNIVERSE_DIR / f"{market}.json"
    today = datetime.now().strftime("%Y-%m-%d")
    cached = None
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    if cached and cached.get("date") == today:
        return cached["rows"], f"copia di oggi ({len(cached['rows'])} titoli)"

    try:
        rows = _download_universe(market)
        min_rows = MARKET_CONFIG[market]["min_universe"]
        if len(rows) < min_rows:
            raise ValueError(f"solo {len(rows)} titoli, attesi almeno {min_rows}")
        cache.write_text(json.dumps({"date": today, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        return rows, f"scaricata oggi ({len(rows)} titoli)"
    except Exception as e:
        print(f"  [WARN] Download composizione {market} fallito: {e}")

    if cached:
        print(f"  [WARN] Uso la copia salvata del {cached.get('date')}")
        return cached["rows"], f"copia salvata del {cached.get('date')} (download fallito)"

    fallback = _FALLBACKS.get(market)
    if fallback:
        print("  [WARN] Uso la lista di riserva interna")
        rows = [{"ticker": t, "name": t, "raw_sector": s}
                for s, tickers in fallback.items() for t in tickers]
        return rows, f"lista di riserva interna ({len(rows)} titoli)"
    return [], "nessuna lista disponibile"


# ---- Liquidità (prezzi in blocco) --------------------------------------------

def _liquidity(tickers: list[str], chunk: int = 100) -> dict:
    """
    Controvalore medio giornaliero (ultimi 60 giorni) per ticker, con una sola
    richiesta di prezzi ogni `chunk` titoli invece di una chiamata .info per titolo.
    Restituisce {ticker: avg_value} solo per i ticker con dati.
    """
    out = {}
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        try:
            data = yf.download(batch, period="3mo", interval="1d", auto_adjust=True,
                               progress=False, group_by="ticker", threads=True)
        except Exception as e:
            print(f"  [WARN] Download prezzi fallito per {len(batch)} titoli: {e}")
            continue
        if data is None or data.empty:
            continue
        for t in batch:
            try:
                frame = data[t] if isinstance(data.columns, pd.MultiIndex) else data
                px = frame[["Close", "Volume"]].dropna()
            except KeyError:
                continue
            if px.empty:
                continue
            out[t] = float((px["Close"] * px["Volume"]).tail(60).mean())
    return out


# ---- Dati fondamentali -------------------------------------------------------

def _fetch_ticker_info(ticker: str) -> dict | None:
    """Recupera info da Yahoo Finance per un singolo ticker (un nuovo tentativo in caso di errore)."""
    for _ in range(2):
        try:
            info = yf.Ticker(ticker).info
            if info and (info.get("regularMarketPrice") is not None or info.get("currentPrice") is not None):
                return info
        except Exception:
            pass
    return None


def _passes_pre_filters(info: dict, mkt_cfg: dict) -> tuple[bool, str]:
    """Verifica i filtri fondamentali minimi per il mercato specificato."""
    mc = info.get("marketCap")
    if mc and mc < mkt_cfg["min_market_cap"]:
        return False, f"market cap {mc/1e9:.1f}B < {mkt_cfg['min_market_cap']/1e9:g}B"

    pe = info.get("forwardPE")
    if pe and pe > mkt_cfg["max_pe_forward"]:
        return False, f"P/E forward {pe:.0f} > {mkt_cfg['max_pe_forward']}"

    rg = info.get("revenueGrowth")
    if rg is not None and rg < mkt_cfg["min_revenue_growth"]:
        return False, f"revenue growth {rg*100:.1f}% < {mkt_cfg['min_revenue_growth']*100:.0f}%"

    return True, "ok"


def _pos_52w(info: dict) -> float | None:
    price  = info.get("currentPrice") or info.get("regularMarketPrice")
    low52  = info.get("fiftyTwoWeekLow")
    high52 = info.get("fiftyTwoWeekHigh")
    if price and low52 and high52 and high52 > low52:
        return (price - low52) / (high52 - low52)
    return None


# ---- Punteggio preliminare relativo al settore -------------------------------

def _percentile(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """Percentile 0-1 dentro il gruppo; dato mancante = 0.5 (neutro), gruppo di 1 = 0.5."""
    valid = values.dropna()
    out = pd.Series(0.5, index=values.index)
    if len(valid) > 1:
        ranks = valid.rank(method="average", ascending=higher_is_better)
        out.loc[valid.index] = (ranks - 1) / (len(valid) - 1)
    return out


def _score_candidates(cands: list[dict]) -> None:
    """Aggiunge pre_score (0-100) a ogni candidato, confrontandolo solo con il suo settore."""
    if not cands:
        return
    df = pd.DataFrame(cands)
    for col in ["revenue_growth", "roe", "pe_forward", "pos_52w"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # P/E negativo (utili attesi negativi) = peggiore del gruppo
    df["_pe"] = df["pe_forward"].where(df["pe_forward"].isna() | (df["pe_forward"] > 0), float("inf"))
    scores = pd.Series(0.0, index=df.index)
    for _, idx in df.groupby("sector").groups.items():
        g = df.loc[idx]
        parts = {
            "revenue_growth": _percentile(g["revenue_growth"]),
            "roe":            _percentile(g["roe"]),
            "pe_forward":     _percentile(g["_pe"], higher_is_better=False),
            "pos_52w":        _percentile(g["pos_52w"]),
        }
        scores.loc[idx] = sum(parts[k] * w for k, w in PRE_SCORE_WEIGHTS.items()) * 100
    for i, c in enumerate(cands):
        c["pre_score"] = round(float(scores.iloc[i]), 1)


# ---- Screening di un mercato -------------------------------------------------

def screen_market(
    market: str,
    sectors: list[str] | None = None,
    max_per_sector: int | None = None,
    max_per_market: int | None = None,
    exclude: set | None = None,
    workers: int = 8,
) -> tuple[list[dict], dict]:
    """Screening completo di un mercato. Restituisce (selezionati, statistiche)."""
    cfg   = MARKET_CONFIG[market]
    label = cfg["label"]
    exclude = exclude or set()
    wanted  = [s for s in TARGET_SECTORS if sectors is None or s in sectors]
    print(f"\n[SCREENER] {label}")

    universe, source = load_universe(market)
    for r in universe:  # ricalcolato anche per le copie salvate, se la mappa cambia
        r["sector"] = normalize_sector(r.get("raw_sector"))
    stats = {"label": label, "source": source, "universe": len(universe)}

    in_scope = [r for r in universe if r.get("sector") in wanted and r["ticker"] not in exclude]
    stats["in_sector"] = len(in_scope)
    print(f"  -> {len(universe)} titoli ({source}), {len(in_scope)} nei settori {wanted}")
    if not in_scope:
        return [], stats

    liquidity = _liquidity([r["ticker"] for r in in_scope])
    no_data   = [r["ticker"] for r in in_scope if r["ticker"] not in liquidity]
    liquid    = [r for r in in_scope if liquidity.get(r["ticker"], 0) >= cfg["min_avg_value"]]
    stats["no_price_data"] = no_data
    stats["illiquid"]      = len(in_scope) - len(no_data) - len(liquid)
    stats["liquid"]        = len(liquid)
    if no_data:
        print(f"  [WARN] Nessun prezzo per {len(no_data)} ticker: {no_data}")
    print(f"  -> {len(liquid)} titoli con controvalore medio >= {cfg['min_avg_value']:,.0f}")

    cands, info_failed, excluded = [], [], {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_ticker_info, r["ticker"]): r for r in liquid}
        done = 0
        for fut in as_completed(futures):
            r    = futures[fut]
            info = fut.result()
            done += 1
            if done % 10 == 0 or done == len(liquid):
                print(f"  ... {done}/{len(liquid)} analizzati")
            if info is None:
                info_failed.append(r["ticker"])
                continue
            passed, reason = _passes_pre_filters(info, cfg)
            if not passed:
                excluded[r["ticker"]] = reason
                continue
            cands.append({
                "ticker":         r["ticker"],
                "name":           info.get("longName") or r["name"],
                "sector":         r["sector"],
                "market":         market,
                "market_label":   label,
                "market_cap_b":   round((info.get("marketCap") or 0) / 1e9, 1),
                "avg_value_m":    round(liquidity[r["ticker"]] / 1e6, 1),
                "pe_forward":     info.get("forwardPE"),
                "revenue_growth": info.get("revenueGrowth"),
                "roe":            info.get("returnOnEquity"),
                "pos_52w":        _pos_52w(info),
                "price":          info.get("currentPrice") or info.get("regularMarketPrice"),
            })
    stats["info_failed"]        = info_failed
    stats["excluded_prefilter"] = excluded
    stats["candidates"]         = len(cands)
    if info_failed:
        print(f"  [WARN] Dati fondamentali non disponibili per {len(info_failed)} ticker: {info_failed}")

    _score_candidates(cands)

    # Top N per settore, poi eventuale tetto complessivo per mercato
    selected = []
    for sector in wanted:
        n = max_per_sector or cfg["max_picks"].get(sector, 2)
        pool = sorted((c for c in cands if c["sector"] == sector), key=lambda c: c["pre_score"], reverse=True)
        selected += pool[:n]
    max_total = max_per_market or cfg["max_total"]
    selected.sort(key=lambda c: c["pre_score"], reverse=True)
    if max_total:
        selected = selected[:max_total]

    stats["selected"] = len(selected)
    print(f"  -> Selezionati: {[c['ticker'] for c in selected]}")
    return selected, stats


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
        markets:         "sp500", "sp600", "italy", "europe" (default: ["sp500"];
                         "russell2000" e' accettato come alias di "sp600")
        sectors:         Sottoinsieme di Technology / Financials / Industrials (default: tutti)
        max_per_sector:  Override del numero massimo di pick per settore
        max_per_market:  Override del tetto complessivo per mercato

    Returns:
        {"tickers": [...], "details": [...], "summary": {...}}
    """
    markets = [MARKET_ALIASES.get(m, m) for m in (markets or ["sp500"])]
    markets = list(dict.fromkeys(markets))  # deduplica mantenendo l'ordine

    all_selected, seen, per_market = [], set(), {}
    for mkt in markets:
        if mkt not in MARKET_CONFIG:
            print(f"[SCREENER] Mercato sconosciuto: {mkt} -- skip")
            continue
        selected, stats = screen_market(mkt, sectors, max_per_sector, max_per_market, exclude=seen)
        for c in selected:
            seen.add(c["ticker"])
            all_selected.append(c)
        per_market[mkt] = stats

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path  = OUTPUT_DIR / f"screener_{timestamp}.json"
    report    = {"run_timestamp": datetime.now().isoformat(), "selected": all_selected, "markets": per_market}
    for path in [out_path, OUTPUT_DIR / "screener_latest.json"]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str, ensure_ascii=False)

    tickers = [c["ticker"] for c in all_selected]
    print(f"\n[SCREENER] Ticker selezionati: {tickers}")

    return {
        "tickers": tickers,
        "details": all_selected,
        "summary": {
            "markets_screened": markets,
            "total_selected":   len(tickers),
            "by_market":        {m: sum(1 for c in all_selected if c["market"] == m) for m in markets},
            "coverage":         per_market,
            "report_path":      str(out_path),
        },
    }


# ---- Test standalone ---------------------------------------------------------

if __name__ == "__main__":
    import sys
    mkts = sys.argv[1:] if len(sys.argv) > 1 else ["sp500"]
    result = run_screener(markets=mkts)
    print("\n-- CANDIDATI SELEZIONATI --")
    for c in result["details"]:
        pe  = f"P/E {c['pe_forward']:.1f}" if c["pe_forward"] else "P/E n/a"
        rg  = f"RevG {c['revenue_growth']*100:.1f}%" if c["revenue_growth"] is not None else ""
        roe = f"ROE {c['roe']*100:.1f}%" if c["roe"] is not None else ""
        print(f"  {c['ticker']:<10} [{c['market_label']}] {c['sector']:<12} score={c['pre_score']:5.1f}  {pe}  {rg}  {roe}")
    print("\n-- COPERTURA --")
    for m, s in result["summary"]["coverage"].items():
        print(f"  {s['label']}: fonte {s['source']} | nei settori {s.get('in_sector', 0)} | "
              f"liquidi {s.get('liquid', 0)} | candidati {s.get('candidates', 0)} | selezionati {s.get('selected', 0)}")
