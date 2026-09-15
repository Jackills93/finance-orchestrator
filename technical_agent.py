"""
technical_agent.py -- Agente Analisi Tecnica
Orchestratore multi-agente finanziario

Indicatori: MA 20/50 settimanali (Golden/Death Cross), RSI 14, MACD 12/26/9, ATR 14,
            Volume (conferma crossover), RS vs benchmark di mercato, Sector Rotation
Benchmark RS: S&P500 (USA), FTSE MIB (.MI), EuroStoxx 50 (altre borse europee)
Timeframe principale: settimanale | Entrata: giornaliero
Fonte dati: Alpha Vantage (USA) + yfinance (Europa e fallback)

Dipendenze: pip install anthropic requests pandas python-dotenv
Variabili d'ambiente: ALPHA_VANTAGE_API_KEY, ANTHROPIC_API_KEY
"""

import os
import json
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()
client    = Anthropic()
AV_KEY    = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
AV_BASE   = "https://www.alphavantage.co/query"
OUTPUT_DIR = Path("technical_reports")
OUTPUT_DIR.mkdir(exist_ok=True)

# --- Pesi indicatori ----------------------------------------------------------
INDICATOR_WEIGHTS = {
    "ma_crossover":    0.28,
    "macd":            0.24,
    "rsi":             0.18,
    "rs_vs_market":    0.15,
    "sector_rotation": 0.15,
}

# Peso composito: 55% tecnico + 45% fondamentale
COMPOSITE_TECH_WEIGHT = 0.55
COMPOSITE_FUND_WEIGHT = 0.45

# Benchmark per mercato: la Relative Strength confronta il titolo con l'indice
# della sua borsa (stessa valuta), non sempre con l'S&P500.
BENCHMARKS = {
    "us":     ("^GSPC",      "S&P500"),
    "italy":  ("FTSEMIB.MI", "FTSE MIB"),
    "europe": ("^STOXX50E",  "EuroStoxx 50"),
}


def benchmark_for_ticker(ticker: str) -> tuple[str, str]:
    """Restituisce (symbol, label) del benchmark coerente con la borsa del titolo."""
    if ticker.endswith(".MI"):
        return BENCHMARKS["italy"]
    if "." in ticker:  # altri suffissi europei: .PA .DE .AS .MC .BR .HE ...
        return BENCHMARKS["europe"]
    return BENCHMARKS["us"]


# Mappa settore -> ETF settoriale SPDR (titoli USA)
SECTOR_ETFS = {
    "Technology":             "XLK",
    "Financials":             "XLF",
    "Industrials":            "XLI",
    "Healthcare":             "XLV",
    "Energy":                 "XLE",
    "Consumer Discretionary": "XLY",
    "Consumer Staples":       "XLP",
    "Real Estate":            "XLRE",
    "Materials":              "XLB",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}

# Mappa settore -> ETF iShares STOXX Europe 600 (titoli europei, quotati su Xetra)
SECTOR_ETFS_EU = {
    "Technology":             "EXV3.DE",
    "Financials":             "EXV1.DE",   # Banks (proxy del settore finanziario)
    "Industrials":            "EXH4.DE",
    "Healthcare":             "EXV4.DE",
    "Energy":                 "EXH1.DE",
    "Consumer Discretionary": "EXH7.DE",
    "Consumer Staples":       "EXH3.DE",
    "Materials":              "EXV7.DE",
    "Utilities":              "EXH9.DE",
    "Communication Services": "EXV2.DE",
}

# Cache benchmark e ETF settoriali per evitare fetch multipli nella stessa sessione
_benchmark_cache:   dict = {}
_sector_etf_cache:  dict = {}

# --- Fetch dati da Alpha Vantage ----------------------------------------------

def _av_get(params: dict, pause: float = 12.0) -> dict:
    """
    Chiamata generica ad Alpha Vantage con retry e rate-limit.
    Il piano gratuito permette 5 chiamate/minuto -> pause di 12s tra le chiamate.
    """
    try:
        r = requests.get(AV_BASE, params={**params, "apikey": AV_KEY}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "Note" in data or "Information" in data:
            print(f"  [AV LIMIT] Rate limit raggiunto -- attendo 60s...")
            time.sleep(60)
            r = requests.get(AV_BASE, params={**params, "apikey": AV_KEY}, timeout=15)
            data = r.json()
        time.sleep(pause)
        return data
    except Exception as e:
        print(f"  [AV ERROR] {e}")
        return {}


def _is_european(ticker: str) -> bool:
    """Restituisce True se il ticker ha un suffisso di borsa europea (.MI, .PA, ecc.)."""
    return "." in ticker


def _fetch_weekly_yfinance(ticker: str) -> pd.DataFrame:
    """Fetch settimanale via yfinance -- usato per ticker europei e come fallback."""
    try:
        raw = yf.Ticker(ticker).history(period="5y", interval="1wk",
                                        auto_adjust=True)
        if raw.empty:
            return pd.DataFrame()
        raw.index = pd.to_datetime(raw.index).tz_localize(None)
        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df = df.sort_index().reset_index()
        df = df.rename(columns={"Date": "date", "Datetime": "date", "index": "date"})
        if "date" not in df.columns:
            df.insert(0, "date", df.index)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        # Scarta la candela della settimana corrente se ancora senza dati
        # (yfinance la emette con close NaN e avvelena RS/MA/MACD sull'ultima riga)
        df = df.dropna(subset=["close"])
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"  [YF] Errore fetch weekly {ticker}: {e}")
        return pd.DataFrame()


def _fetch_daily_yfinance(ticker: str) -> pd.DataFrame:
    """Fetch giornaliero via yfinance -- usato per ticker europei e come fallback."""
    try:
        raw = yf.Ticker(ticker).history(period="6mo", interval="1d",
                                        auto_adjust=True)
        if raw.empty:
            return pd.DataFrame()
        raw.index = pd.to_datetime(raw.index).tz_localize(None)
        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df = df.sort_index().reset_index()
        df = df.rename(columns={"Date": "date", "Datetime": "date", "index": "date"})
        if "date" not in df.columns:
            df.insert(0, "date", df.index)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.dropna(subset=["close"])  # scarta candele senza dati
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"  [YF] Errore fetch daily {ticker}: {e}")
        return pd.DataFrame()


def fetch_weekly_ohlcv(ticker: str) -> pd.DataFrame:
    """Scarica i prezzi settimanali. Usa yfinance per ticker europei, Alpha Vantage per USA."""
    if _is_european(ticker):
        print(f"  [YF] Weekly OHLCV -- {ticker} (borsa europea)")
        return _fetch_weekly_yfinance(ticker)

    print(f"  [AV] Weekly OHLCV -- {ticker}")
    data = _av_get({
        "function":    "TIME_SERIES_WEEKLY_ADJUSTED",
        "symbol":      ticker,
        "outputsize":  "full",
    })
    series = data.get("Weekly Adjusted Time Series", {})
    if not series:
        # Fallback yfinance se Alpha Vantage non risponde
        print(f"  [YF] Fallback weekly -- {ticker}")
        return _fetch_weekly_yfinance(ticker)

    rows = []
    for date_str, v in series.items():
        rows.append({
            "date":   pd.to_datetime(date_str),
            "open":   float(v["1. open"]),
            "high":   float(v["2. high"]),
            "low":    float(v["3. low"]),
            "close":  float(v["5. adjusted close"]),
            "volume": int(v["6. volume"]),
        })
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return df


def fetch_daily_ohlcv(ticker: str, compact: bool = True) -> pd.DataFrame:
    """Scarica i prezzi giornalieri. Usa yfinance per ticker europei, Alpha Vantage per USA."""
    if _is_european(ticker):
        print(f"  [YF] Daily OHLCV -- {ticker} (borsa europea)")
        return _fetch_daily_yfinance(ticker)

    print(f"  [AV] Daily OHLCV -- {ticker}")
    data = _av_get({
        "function":   "TIME_SERIES_DAILY_ADJUSTED",
        "symbol":     ticker,
        "outputsize": "compact" if compact else "full",
    })
    series = data.get("Time Series (Daily)", {})
    if not series:
        print(f"  [YF] Fallback daily -- {ticker}")
        return _fetch_daily_yfinance(ticker)

    rows = []
    for date_str, v in series.items():
        rows.append({
            "date":   pd.to_datetime(date_str),
            "open":   float(v["1. open"]),
            "high":   float(v["2. high"]),
            "low":    float(v["3. low"]),
            "close":  float(v["5. adjusted close"]),
            "volume": int(v["6. volume"]),
        })
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return df


# --- Calcolo indicatori -------------------------------------------------------

def calc_moving_averages(df: pd.DataFrame, fast: int = 50, slow: int = 200) -> pd.DataFrame:
    """Aggiunge MA50, MA200 e segnale di crossover al DataFrame."""
    df = df.copy()
    df[f"ma{fast}"]  = df["close"].rolling(fast).mean()
    df[f"ma{slow}"]  = df["close"].rolling(slow).mean()
    df["ma_diff"]    = df[f"ma{fast}"] - df[f"ma{slow}"]
    df["ma_signal"]  = df["ma_diff"].apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    df["ma_cross"]   = df["ma_signal"].diff()  # +2 = golden cross, -2 = death cross
    return df


def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calcola RSI con metodo Wilder (EMA)."""
    df   = df.copy()
    delta = df["close"].diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs        = avg_gain / avg_loss.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Calcola MACD, linea di segnale e istogramma."""
    df           = df.copy()
    ema_fast     = df["close"].ewm(span=fast,   adjust=False).mean()
    ema_slow     = df["close"].ewm(span=slow,   adjust=False).mean()
    df["macd"]   = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]
    df["macd_cross"]  = df["macd_hist"].apply(lambda x: 1 if x > 0 else -1)
    return df


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calcola ATR (Average True Range) come misura di volatilita."""
    df    = df.copy()
    hl    = df["high"] - df["low"]
    hc    = (df["high"] - df["close"].shift()).abs()
    lc    = (df["low"]  - df["close"].shift()).abs()
    tr    = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=period, adjust=False).mean()
    df["atr_pct"] = df["atr"] / df["close"]  # ATR relativo al prezzo
    return df


def calc_volume_confirmation(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """
    Aggiunge conferma del volume al DataFrame.

    Logica:
    - vol_ma20:    media mobile a 20 periodi del volume
    - vol_ratio:   volume corrente / vol_ma20  (>1.0 = sopra media)
    - vol_signal:  "strong" se ratio > 1.2, "normal" se 0.8-1.2, "weak" se < 0.8

    Un Golden/Death Cross con vol_signal="weak" e probabile falso segnale.
    Un Golden/Death Cross con vol_signal="strong" e confermato.
    """
    df = df.copy()
    df["vol_ma20"]   = df["volume"].rolling(period).mean()
    df["vol_ratio"]  = df["volume"] / df["vol_ma20"].replace(0, 1)
    df["vol_signal"] = df["vol_ratio"].apply(
        lambda r: "strong" if r > 1.2 else ("weak" if r < 0.8 else "normal")
    )
    return df


def fetch_benchmark_weekly(symbol: str) -> pd.DataFrame:
    """Scarica i prezzi settimanali di un indice benchmark via yfinance (con cache)."""
    if symbol in _benchmark_cache:
        return _benchmark_cache[symbol]
    try:
        df = yf.Ticker(symbol).history(period="2y", interval="1wk")[["Close"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.columns = ["close"]
        df = df.dropna(subset=["close"])
        df = df.sort_index().reset_index().rename(columns={"Date": "date", "index": "date"})
        _benchmark_cache[symbol] = df
        return df
    except Exception as e:
        print(f"  [RS] Errore fetch benchmark {symbol}: {e}")
        return pd.DataFrame()


def calc_rs_vs_market(df_ticker: pd.DataFrame, df_benchmark: pd.DataFrame,
                      periods: tuple = (12, 26)) -> dict:
    """
    Calcola la Relative Strength del titolo vs il suo benchmark di mercato
    (S&P500 per titoli USA, FTSE MIB per .MI, EuroStoxx 50 per il resto d'Europa:
    stesso mercato e stessa valuta, cosi' il cambio non distorce il confronto).

    RS = rendimento_titolo / rendimento_benchmark sul periodo N settimane.
    RS > 1.0  -> titolo batte il mercato
    RS < 1.0  -> titolo sottoperforma

    Calcola su due periodi (12W e 26W) e fa la media ponderata.
    Restituisce anche se la RS line e in uptrend (ultimi 4 periodi).
    """
    if df_ticker.empty or df_benchmark.empty or len(df_ticker) < max(periods) + 2:
        return {"rs_12w": None, "rs_26w": None, "rs_score": None, "rs_trend": None}

    # Allinea i due DataFrame per data (merge su settimana piu vicina)
    t = df_ticker.set_index("date")["close"].sort_index().dropna()
    s = (df_benchmark.set_index("date")["close"].sort_index() if "date" in df_benchmark.columns
         else df_benchmark.iloc[:, 0].sort_index()).dropna()
    if len(t) < max(periods) + 2 or s.empty:
        return {"rs_12w": None, "rs_26w": None, "rs_score": None, "rs_trend": None}

    results = {}
    for n in periods:
        if len(t) < n + 1:
            results[f"rs_{n}w"] = None
            continue
        ret_ticker = (t.iloc[-1] - t.iloc[-(n+1)]) / t.iloc[-(n+1)]

        # Trova il valore del benchmark al periodo corrispondente
        try:
            s_aligned = s.reindex(t.index, method="nearest")
            ret_bench  = (s_aligned.iloc[-1] - s_aligned.iloc[-(n+1)]) / s_aligned.iloc[-(n+1)]
        except Exception:
            results[f"rs_{n}w"] = None
            continue

        if ret_bench == 0:
            results[f"rs_{n}w"] = None
            continue

        rs = (1 + ret_ticker) / (1 + ret_bench)
        results[f"rs_{n}w"] = round(float(rs), 3)

    # RS trend: confronta RS_12w degli ultimi 4 punti
    rs_values = []
    for i in range(4, 0, -1):
        idx = -(i)
        if len(t) < periods[0] + i + 1:
            continue
        ret_t = (t.iloc[idx] - t.iloc[-(periods[0]+i+1)]) / t.iloc[-(periods[0]+i+1)]
        try:
            s_al  = s.reindex(t.index, method="nearest")
            ret_s = (s_al.iloc[idx] - s_al.iloc[-(periods[0]+i+1)]) / s_al.iloc[-(periods[0]+i+1)]
            if ret_s != 0:
                rs_values.append((1 + ret_t) / (1 + ret_s))
        except Exception:
            pass

    rs_trend = None
    if len(rs_values) >= 3:
        rising = sum(1 for i in range(1, len(rs_values)) if rs_values[i] > rs_values[i-1])
        rs_trend = "up" if rising >= 2 else ("down" if rising == 0 else "flat")

    results["rs_trend"] = rs_trend
    return results


def score_rs(rs_data: dict, benchmark_label: str = "S&P500") -> tuple[int, str]:
    """
    Score Relative Strength (0-100) vs il benchmark di mercato del titolo.

    RS > 1.20  -> outperform forte  -> 100
    RS > 1.10  -> outperform        -> 80
    RS > 1.00  -> lieve outperform  -> 65
    RS > 0.90  -> lieve underperform-> 40
    RS <= 0.90 -> underperform      -> 20

    Bonus +10 se RS trend e "up" (momentum relativo in miglioramento).
    Malus -10 se RS trend e "down".
    """
    rs_12 = rs_data.get("rs_12w")
    rs_26 = rs_data.get("rs_26w")
    trend = rs_data.get("rs_trend")

    if rs_12 is None and rs_26 is None:
        return 50, "RS non disponibile -- dati insufficienti"

    # Media ponderata: 12W conta 60%, 26W conta 40%
    values = [(v, w) for v, w in [(rs_12, 0.6), (rs_26, 0.4)] if v is not None]
    rs_avg = sum(v * w for v, w in values) / sum(w for _, w in values)

    if rs_avg >= 1.20:
        base = 100; label = f"RS {rs_avg:.2f} -- forte outperformance vs {benchmark_label} (+{(rs_avg-1)*100:.0f}%)"
    elif rs_avg >= 1.10:
        base = 80;  label = f"RS {rs_avg:.2f} -- outperformance solida vs {benchmark_label}"
    elif rs_avg >= 1.00:
        base = 65;  label = f"RS {rs_avg:.2f} -- lieve outperformance vs {benchmark_label}"
    elif rs_avg >= 0.90:
        base = 40;  label = f"RS {rs_avg:.2f} -- lieve underperformance vs {benchmark_label}"
    else:
        base = 20;  label = f"RS {rs_avg:.2f} -- underperformance vs {benchmark_label} ({(rs_avg-1)*100:.0f}%)"

    trend_adj  = {"up": +10, "flat": 0, "down": -10}.get(trend, 0)
    trend_note = {"up": " | RS line in uptrend (denaro istituzionale in entrata)",
                  "flat": "",
                  "down": " | RS line in downtrend (deflusso relativo)"}.get(trend, "")

    score = max(0, min(100, base + trend_adj))
    return score, label + trend_note


def fetch_sector_etf_weekly(sector: str, european: bool = False) -> pd.DataFrame:
    """
    Scarica i prezzi settimanali dell'ETF settoriale via yfinance (con cache).
    Per i titoli europei usa gli iShares STOXX Europe 600 invece degli SPDR USA.
    """
    etf_map = SECTOR_ETFS_EU if european else SECTOR_ETFS
    etf = etf_map.get(sector)
    if not etf:
        return pd.DataFrame()
    if etf in _sector_etf_cache:
        return _sector_etf_cache[etf]
    try:
        df = yf.Ticker(etf).history(period="2y", interval="1wk")[["Close"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.columns = ["close"]
        df = df.dropna(subset=["close"])
        df = df.sort_index().reset_index()
        df.columns = ["date", "close"]
        _sector_etf_cache[etf] = df
        return df
    except Exception as e:
        print(f"  [SECTOR] Errore fetch {etf}: {e}")
        return pd.DataFrame()


def score_sector_rotation(df_sector: pd.DataFrame) -> tuple[int, str]:
    """
    Valuta se il settore e in uptrend o downtrend (MA20 vs MA50 sull'ETF settoriale).

    Score:
      prezzo > MA20 > MA50  -> 85 (uptrend forte)
      MA20 > MA50           -> 65 (uptrend moderato)
      MA20 ~ MA50 (flat)    -> 50 (neutro)
      MA20 < MA50           -> 30 (downtrend moderato)
      prezzo < MA20 < MA50  -> 15 (downtrend forte)
    """
    closes = df_sector["close"].dropna() if not df_sector.empty else pd.Series(dtype=float)
    if len(closes) < 52:
        return 50, "dati ETF settoriale non disponibili -- neutro"
    ma20 = closes.rolling(20).mean().iloc[-1]
    ma50 = closes.rolling(50).mean().iloc[-1]

    if pd.isna(ma20) or pd.isna(ma50):
        return 50, "MA settore non calcolabile"

    price = float(closes.iloc[-1])
    ma20  = float(ma20)
    ma50  = float(ma50)
    etf   = closes.name or "ETF"

    if price > ma20 > ma50:
        return 85, f"settore in uptrend forte (ETF {price:.1f} > MA20 {ma20:.1f} > MA50 {ma50:.1f})"
    if ma20 > ma50:
        return 65, f"settore in uptrend moderato (MA20 {ma20:.1f} > MA50 {ma50:.1f})"
    if price < ma20 < ma50:
        return 15, f"settore in downtrend forte (ETF {price:.1f} < MA20 {ma20:.1f} < MA50 {ma50:.1f})"
    if ma20 < ma50:
        return 30, f"settore in downtrend moderato (MA20 {ma20:.1f} < MA50 {ma50:.1f})"
    return 50, f"settore neutro (MA20 {ma20:.1f} ~ MA50 {ma50:.1f})"


def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Pipeline completa: applica tutti gli indicatori al DataFrame."""
    df = calc_moving_averages(df, fast=20, slow=50)
    df = calc_rsi(df, period=14)
    df = calc_macd(df, fast=12, slow=26, signal=9)
    df = calc_atr(df, period=14)
    df = calc_volume_confirmation(df, period=20)
    return df


# --- Scoring tecnico ----------------------------------------------------------

def score_ma(row: pd.Series, prev_row: pd.Series) -> tuple[int, str]:
    """
    Score MA crossover (0-100) con conferma del volume.

    Logica base:
      Golden Cross recente  -> 100 (forte) / 80 (debole se volume basso)
      Death Cross recente   ->   0 (forte) / 20 (debole se volume basso)
      Trend rialzista       ->  75
      Trend ribassista      ->  25

    Aggiustamento volume sul crossover:
      vol_signal = "strong"  -> +0   (segnale gia al massimo / minimo)
      vol_signal = "normal"  -> -10  (crossover accettabile)
      vol_signal = "weak"    -> -20  (probabile falso segnale, penalita)
    """
    ma20       = row.get("ma20")
    ma50       = row.get("ma50")
    cross      = row.get("ma_cross", 0)
    vol_signal = row.get("vol_signal", "normal")
    vol_ratio  = row.get("vol_ratio", 1.0)

    if pd.isna(ma20) or pd.isna(ma50):
        return 50, "dati MA insufficienti"

    vol_penalty = {"strong": 0, "normal": -10, "weak": -20}.get(vol_signal, -10)
    vol_note    = f"volume {vol_ratio:.2f}x media 20 settimane ({vol_signal})"

    if cross >= 2:
        base  = 100
        score = max(0, base + vol_penalty)
        label = "Golden Cross (MA20>MA50) confermato da volume" if vol_signal == "strong" else \
                "Golden Cross (MA20>MA50) -- volume nella norma" if vol_signal == "normal" else \
                "Golden Cross (MA20>MA50) con volume debole -- possibile falso segnale"
        return score, f"{label} | {vol_note}"

    if cross <= -2:
        base  = 0
        score = min(100, base - vol_penalty)
        label = "Death Cross (MA20<MA50) confermato da volume" if vol_signal == "strong" else \
                "Death Cross (MA20<MA50) -- volume nella norma" if vol_signal == "normal" else \
                "Death Cross (MA20<MA50) con volume debole -- segnale meno affidabile"
        return score, f"{label} | {vol_note}"

    if ma20 > ma50:
        return 75, f"Trend rialzista (MA20 {ma20:.2f} > MA50 {ma50:.2f}) | {vol_note}"

    return 25, f"Trend ribassista (MA20 {ma20:.2f} < MA50 {ma50:.2f}) | {vol_note}"


def score_rsi(row: pd.Series, ma_score: int) -> tuple[int, str]:
    """
    Score RSI (0-100).
    Applica la regola: RSI>70 non e SELL se MA50>MA200 (trend forte).
    """
    rsi = row.get("rsi")
    if pd.isna(rsi):
        return 50, "RSI non disponibile"

    rsi = float(rsi)

    if rsi > 70:
        if ma_score >= 75:
            # Trend forte -- ipercomprato = momentum, non inversione
            return 65, f"RSI {rsi:.1f} ipercomprato ma trend MA rialzista -- momentum forte, non SELL"
        return 20, f"RSI {rsi:.1f} ipercomprato -- segnale SELL"

    if rsi < 30:
        return 90, f"RSI {rsi:.1f} ipervenduto -- segnale BUY potenziale"

    if 50 <= rsi <= 65:
        return 75, f"RSI {rsi:.1f} in zona neutra-positiva -- momentum sano"

    if 35 <= rsi < 50:
        return 50, f"RSI {rsi:.1f} zona neutra"

    return 40, f"RSI {rsi:.1f} zona debolezza"


def score_macd(row: pd.Series) -> tuple[int, str]:
    """
    Score MACD (0-100).
    Incrocio sopra/sotto zero pesa sulla forza del segnale.
    """
    macd      = row.get("macd")
    sig       = row.get("macd_signal")
    hist      = row.get("macd_hist")
    cross     = row.get("macd_cross", 0)
    prev_cross = row.get("macd_cross")

    if pd.isna(macd) or pd.isna(sig):
        return 50, "MACD non disponibile"

    macd  = float(macd)
    sig   = float(sig)
    hist  = float(hist) if not pd.isna(hist) else 0

    # Determina se c'e stato un crossover recente (istogramma cambia segno)
    if hist > 0 and macd > 0:
        return 100, f"MACD BUY forte -- incrocio up sopra zero (hist: {hist:.3f})"
    if hist > 0 and macd < 0:
        return 65,  f"MACD BUY debole -- incrocio up sotto zero (hist: {hist:.3f})"
    if hist < 0 and macd < 0:
        return 0,   f"MACD SELL forte -- incrocio dn sotto zero (hist: {hist:.3f})"
    if hist < 0 and macd > 0:
        return 30,  f"MACD SELL debole -- incrocio dn sopra zero (hist: {hist:.3f})"

    return 50, f"MACD neutro (macd: {macd:.3f}, signal: {sig:.3f})"


def atr_confidence(row: pd.Series) -> tuple[str, str]:
    """
    Valuta la volatilita ATR e restituisce un livello di confidenza.
    ATR basso = possibili falsi segnali da MA e MACD.
    """
    atr_pct = row.get("atr_pct")
    if pd.isna(atr_pct):
        return "medium", "ATR non disponibile"

    atr_pct = float(atr_pct)

    if atr_pct < 0.01:
        return "low",    f"ATR {atr_pct:.2%} -- volatilita compressa, rischio falsi segnali"
    if atr_pct < 0.025:
        return "medium", f"ATR {atr_pct:.2%} -- volatilita normale"
    return "high",       f"ATR {atr_pct:.2%} -- volatilita elevata, segnali forti ma rischio ampliato"


def suggest_position_size(price: float | None, stop_loss: float | None) -> dict:
    """
    Position sizing a rischio fisso per trade:

      qty = (capitale x rischio% per trade) / (prezzo - stop loss)

    con tetto sul valore della posizione (max % del capitale) per evitare
    che uno stop molto stretto produca una posizione enorme.

    Parametri da .env (modificabili dal tab Impostazioni della dashboard):
      PORTFOLIO_CAPITAL   capitale complessivo (0 o assente = sizing disattivato)
      RISK_PER_TRADE_PCT  rischio massimo per trade in % del capitale (default 1)
      MAX_POSITION_PCT    valore massimo della posizione in % del capitale (default 20)

    NB: nessuna conversione valutaria -- il capitale e' inteso nella stessa
    valuta del titolo (coerente col resto dell'app).
    """
    empty = {"suggested_qty": None, "risk_amount": None,
             "position_value": None, "sizing_note": ""}
    try:
        capital  = float(os.getenv("PORTFOLIO_CAPITAL", "0") or 0)
        risk_pct = float(os.getenv("RISK_PER_TRADE_PCT", "1") or 1)
        max_pos  = float(os.getenv("MAX_POSITION_PCT", "20") or 20)
    except ValueError:
        return {**empty, "sizing_note": "parametri sizing non validi nel .env"}

    if capital <= 0:
        return empty  # sizing non configurato
    if not price or not stop_loss or price <= stop_loss:
        return {**empty, "sizing_note": "sizing non calcolabile (SL mancante o >= prezzo)"}

    risk_amount = capital * risk_pct / 100
    sl_distance = price - stop_loss
    qty         = int(risk_amount // sl_distance)

    note = f"rischio {risk_pct:g}% = {risk_amount:.0f} su SL a -{sl_distance/price*100:.1f}%"
    max_value = capital * max_pos / 100
    if qty * price > max_value:
        qty  = int(max_value // price)
        note += f" | ridotta al tetto {max_pos:g}% del capitale"
    if qty <= 0:
        return {**empty, "sizing_note":
                f"capitale insufficiente: 1 azione ({price:.2f}) eccede il budget di rischio ({risk_amount:.0f})"}

    return {
        "suggested_qty":  qty,
        "risk_amount":    round(qty * sl_distance, 2),
        "position_value": round(qty * price, 2),
        "sizing_note":    note,
    }


def compute_technical_score(df_weekly: pd.DataFrame,
                            df_benchmark: pd.DataFrame | None = None,
                            benchmark_label: str = "S&P500",
                            sector: str | None = None,
                            european: bool = False) -> dict:
    """
    Calcola lo score tecnico finale su base settimanale.
    Indicatori: MA20/50, MACD, RSI, RS vs benchmark di mercato, Sector Rotation.
    Il benchmark e gli ETF settoriali sono coerenti con la borsa del titolo
    (europei per titoli europei, USA per titoli USA).
    """
    if df_weekly.empty or len(df_weekly) < 60:
        return {
            "total_score": None,
            "signal":      "INSUFFICIENT_DATA",
            "confidence":  "low",
            "error":       f"Dati insufficienti ({len(df_weekly)} candele, servono 60+)"
        }

    df   = compute_all_indicators(df_weekly)
    row  = df.iloc[-1]
    prev = df.iloc[-2]

    ma_score,   ma_reason   = score_ma(row, prev)
    rsi_score,  rsi_reason  = score_rsi(row, ma_score)
    macd_score, macd_reason = score_macd(row)
    confidence, atr_reason  = atr_confidence(row)

    # Relative Strength vs benchmark di mercato (stessa borsa/valuta del titolo)
    bench = df_benchmark if df_benchmark is not None else pd.DataFrame()
    rs_data              = calc_rs_vs_market(df_weekly, bench)
    rs_score, rs_reason  = score_rs(rs_data, benchmark_label)

    # Sector Rotation (ETF settoriali europei per titoli europei)
    df_sector               = fetch_sector_etf_weekly(sector or "", european=european)
    sec_score, sec_reason   = score_sector_rotation(df_sector)

    # Score ponderato (5 indicatori)
    total = (
        ma_score   * INDICATOR_WEIGHTS["ma_crossover"] +
        macd_score * INDICATOR_WEIGHTS["macd"] +
        rsi_score  * INDICATOR_WEIGHTS["rsi"] +
        rs_score   * INDICATOR_WEIGHTS["rs_vs_market"] +
        sec_score  * INDICATOR_WEIGHTS["sector_rotation"]
    )
    total = round(max(0, min(100, total)))

    # Segnale finale
    if confidence == "low":
        signal = "HOLD"  # volatilita troppo bassa, segnali inaffidabili
    elif total >= 70:
        signal = "BUY"
    elif total <= 30:
        signal = "SELL"
    else:
        signal = "HOLD"

    # SL/TP suggeriti basati su ATR (2× ATR per SL, 3× ATR per TP -> R:R 1:1.5)
    price_val = float(row["close"])
    atr_val   = float(row["atr"]) if not pd.isna(row.get("atr", float("nan"))) else None
    suggested_sl = round(price_val - 2.0 * atr_val, 2) if atr_val else None
    suggested_tp = round(price_val + 3.0 * atr_val, 2) if atr_val else None

    # Position sizing basato sul rischio (usa PORTFOLIO_CAPITAL dal .env)
    sizing = suggest_position_size(price_val, suggested_sl)

    return {
        "total_score":   total,
        "signal":        signal,
        "confidence":    confidence,
        "price":         round(price_val, 2),
        "ma20":          round(float(row["ma20"]),  2) if not pd.isna(row["ma20"])  else None,
        "ma50":          round(float(row["ma50"]),  2) if not pd.isna(row["ma50"])  else None,
        "rsi":           round(float(row["rsi"]),   1) if not pd.isna(row["rsi"])   else None,
        "macd":          round(float(row["macd"]),  4) if not pd.isna(row["macd"])  else None,
        "macd_signal":   round(float(row["macd_signal"]), 4) if not pd.isna(row["macd_signal"]) else None,
        "atr_pct":       round(float(row["atr_pct"]), 4) if not pd.isna(row["atr_pct"]) else None,
        "atr_abs":       round(atr_val, 2) if atr_val else None,
        "vol_ratio":     round(float(row["vol_ratio"]), 2) if not pd.isna(row.get("vol_ratio", float("nan"))) else None,
        "vol_signal":    row.get("vol_signal", "normal"),
        "rs_12w":        rs_data.get("rs_12w"),
        "rs_26w":        rs_data.get("rs_26w"),
        "rs_trend":      rs_data.get("rs_trend"),
        "benchmark":     benchmark_label,
        "sector_etf":    (SECTOR_ETFS_EU if european else SECTOR_ETFS).get(sector or "", None),
        "suggested_sl":  suggested_sl,
        "suggested_tp":  suggested_tp,
        "suggested_qty":  sizing["suggested_qty"],
        "risk_amount":    sizing["risk_amount"],
        "position_value": sizing["position_value"],
        "sizing_note":    sizing["sizing_note"],
        "indicator_scores": {
            "ma_crossover":    {"score": ma_score,   "weight": INDICATOR_WEIGHTS["ma_crossover"],    "reason": ma_reason},
            "macd":            {"score": macd_score, "weight": INDICATOR_WEIGHTS["macd"],            "reason": macd_reason},
            "rsi":             {"score": rsi_score,  "weight": INDICATOR_WEIGHTS["rsi"],             "reason": rsi_reason},
            "rs_vs_market":    {"score": rs_score,   "weight": INDICATOR_WEIGHTS["rs_vs_market"],    "reason": rs_reason},
            "sector_rotation": {"score": sec_score,  "weight": INDICATOR_WEIGHTS["sector_rotation"], "reason": sec_reason},
        },
        "atr_note":      atr_reason,
        "analysis_date": datetime.now().strftime("%Y-%m-%d"),
    }


# --- Entry point settimanale --------------------------------------------------

def analyze_daily_entry(df_daily: pd.DataFrame, weekly_signal: str) -> dict:
    """
    Analisi giornaliera per ottimizzare il punto di entrata.
    Viene eseguita solo se il settimanale ha gia dato segnale BUY.
    """
    if weekly_signal != "BUY" or df_daily.empty or len(df_daily) < 30:
        return {"entry_signal": "n/a", "note": "analisi giornaliera non applicabile"}

    df  = compute_all_indicators(df_daily)
    row = df.iloc[-1]

    rsi_daily  = float(row["rsi"])  if not pd.isna(row["rsi"])  else 50
    macd_hist  = float(row["macd_hist"]) if not pd.isna(row["macd_hist"]) else 0

    # Entrata ottimale: pullback su RSI giornaliero senza rompere il trend
    if rsi_daily < 45 and macd_hist > 0:
        return {
            "entry_signal": "ENTRY_NOW",
            "note": f"Pullback sano su giornaliero (RSI {rsi_daily:.1f}) con MACD ancora positivo -- entrata favorevole"
        }
    if rsi_daily > 68:
        return {
            "entry_signal": "WAIT_PULLBACK",
            "note": f"RSI giornaliero {rsi_daily:.1f} esteso -- attendi pullback prima di entrare"
        }
    return {
        "entry_signal": "ENTRY_OK",
        "note": f"Condizioni giornaliere accettabili (RSI {rsi_daily:.1f})"
    }


# --- Narrativa LLM ------------------------------------------------------------

TECHNICAL_SYSTEM_PROMPT = """Sei un analista tecnico senior. Ricevi i dati di scoring
tecnico di un'azione (MA crossover, MACD, RSI, Relative Strength vs il benchmark di mercato
indicato nel campo "benchmark") e devi produrre:
1. Una lista "key_signals" con 2-3 osservazioni tecniche concrete (frasi brevi, in italiano).
   Includi sempre una nota sulla Relative Strength se disponibile (RS>1 = titolo batte il suo indice).
2. Un campo "entry_advice" con il consiglio operativo (max 2 righe).
3. Un campo "risk_note" con il principale rischio tecnico da monitorare (1 riga).

Basati SOLO sui dati forniti. Sii diretto e specifico sui livelli di prezzo.
Rispondi SOLO con JSON valido senza markdown:
{"key_signals": ["...", "..."], "entry_advice": "...", "risk_note": "..."}"""


def generate_technical_narrative(scored: dict) -> dict:
    prompt = f"""Analizza questo scoring tecnico e produci l'output richiesto:
{json.dumps(scored, indent=2, default=str)}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=TECHNICAL_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(text)
    except Exception:
        return {"key_signals": [], "entry_advice": text[:200], "risk_note": ""}


# --- Runner principale --------------------------------------------------------

def run_technical_agent(
    tickers: list[str],
    filter_results: list[dict] | None = None,
) -> dict:
    """
    Esegue l'analisi tecnica per una lista di ticker.

    Args:
        tickers:        Lista di simboli (es. ["ASML", "NVDA"])
        filter_results: Output del filter_agent (opzionale) -- usato per
                        arricchire l'output con il fundamental score.

    Returns:
        Dict con technical_results ordinati per score e summary.
    """
    # Mappa ticker -> {score fondamentale, settore} per arricchire l'output
    fund_map = {}
    if filter_results:
        for r in filter_results:
            fund_map[r["ticker"]] = {
                "score":  r.get("total_score"),
                "sector": r.get("sector", ""),
            }

    results = []

    for ticker in tickers:
        print(f"\n[TECHNICAL] Analisi {ticker}...")

        # 1. Fetch dati settimanali (principale)
        df_w = fetch_weekly_ohlcv(ticker)

        # 2. Benchmark coerente con la borsa del titolo (cache per simbolo)
        bench_symbol, bench_label = benchmark_for_ticker(ticker)
        df_bench = fetch_benchmark_weekly(bench_symbol)
        if df_bench.empty:
            print(f"  [WARN] Benchmark {bench_label} non disponibile -- RS a 50 (neutro)")

        # 3. Calcolo score settimanale (include RS vs benchmark e Sector Rotation)
        ticker_info = fund_map.get(ticker, {})
        sector      = ticker_info.get("sector", "") if isinstance(ticker_info, dict) else ""
        scored_w    = compute_technical_score(
            df_w,
            df_benchmark=df_bench,
            benchmark_label=bench_label,
            sector=sector,
            european=_is_european(ticker),
        )
        scored_w["ticker"] = ticker

        # 4. Se BUY confermato -> analisi giornaliera per l'entrata
        df_d = pd.DataFrame()
        entry = {"entry_signal": "n/a", "note": ""}
        if scored_w.get("signal") == "BUY":
            df_d  = fetch_daily_ohlcv(ticker)
            entry = analyze_daily_entry(df_d, scored_w["signal"])

        scored_w["daily_entry"] = entry

        # 5. Narrativa LLM
        if scored_w.get("total_score") is not None:
            narrative = generate_technical_narrative(scored_w)
            scored_w["key_signals"]  = narrative.get("key_signals", [])
            scored_w["entry_advice"] = narrative.get("entry_advice", "")
            scored_w["risk_note"]    = narrative.get("risk_note", "")
        else:
            scored_w["key_signals"]  = []
            scored_w["entry_advice"] = ""
            scored_w["risk_note"]    = scored_w.get("error", "")

        # 6. Arricchimento con fundamental score e settore
        fund_info = fund_map.get(ticker, {})
        fs = fund_info.get("score") if isinstance(fund_info, dict) else fund_info
        scored_w["fundamental_score"] = fs
        scored_w["sector"] = sector

        # 7. Score composito: 55% tecnico + 45% fondamentale
        ts = scored_w.get("total_score")
        if ts is not None and fs is not None:
            scored_w["composite_score"] = round(ts * COMPOSITE_TECH_WEIGHT + fs * COMPOSITE_FUND_WEIGHT)
        else:
            scored_w["composite_score"] = ts

        results.append(scored_w)
        print(f"  -> score={ts}  signal={scored_w.get('signal')}  confidence={scored_w.get('confidence')}  vol={scored_w.get('vol_signal','?')}")

    # Ordina per composite_score (o technical se fondamentale non disponibile)
    results.sort(key=lambda x: x.get("composite_score") or 0, reverse=True)

    # Salva su file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path  = OUTPUT_DIR / f"technical_report_{timestamp}.json"
    latest    = OUTPUT_DIR / "technical_report_latest.json"

    for path in [out_path, latest]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str, ensure_ascii=False)

    print(f"\n[INFO] Report salvato: {out_path}")

    buy_list  = [r for r in results if r.get("signal") == "BUY"]
    hold_list = [r for r in results if r.get("signal") == "HOLD"]
    sell_list = [r for r in results if r.get("signal") == "SELL"]

    return {
        "technical_results": results,
        "summary": {
            "total_analyzed": len(results),
            "buy":            len(buy_list),
            "hold":           len(hold_list),
            "sell":           len(sell_list),
            "top_pick":       results[0]["ticker"] if results else None,
        }
    }


# --- Test ---------------------------------------------------------------------

if __name__ == "__main__":
    # Simula l'output del filter_agent per il test
    mock_filter = [
        {"ticker": "ASML", "total_score": 87},
        {"ticker": "NVDA", "total_score": 82},
        {"ticker": "JPM",  "total_score": 71},
    ]

    output = run_technical_agent(
        tickers=["ASML", "NVDA", "JPM"],
        filter_results=mock_filter,
    )

    print("\n-- SUMMARY --")
    print(json.dumps(output["summary"], indent=2))

    print("\n-- TOP PICK --")
    top = output["technical_results"][0]
    print(f"Ticker:          {top['ticker']}")
    print(f"Signal:          {top['signal']}")
    print(f"Technical score: {top['total_score']}/100")
    print(f"Composite score: {top['composite_score']}/100")
    print(f"Confidence:      {top['confidence']}")
    print(f"Volume signal:   {top.get('vol_signal','?')}  ({top.get('vol_ratio','?')}x media 20W)")
    print(f"Entry advice:    {top.get('entry_advice','')}")
    if top.get("key_signals"):
        print("Key signals:")
        for s in top["key_signals"]:
            print(f"  * {s}")
