"""
weekly_report.py — Report settimanale Finance Screener
Eseguito ogni sabato alle 09:00 dal Task Scheduler di Windows.

Registra il task (una tantum, da eseguire come amministratore):
  schtasks /Create /TN "FinanceScreenerWeeklyReport" /SC WEEKLY /D SAT /ST 09:00 ^
    /TR "\"<percorso python.exe>\" \"<percorso cartella progetto>\\weekly_report.py\"" /F
"""

import os
import json
import sqlite3
import smtplib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

LOG_FILE = BASE_DIR / "weekly_report.log"
logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---- FOMC schedule (aggiorna ogni anno) --------------------------------------
FOMC_DATES = [
    # 2025
    datetime(2025,  1, 29), datetime(2025,  3, 19), datetime(2025,  5,  7),
    datetime(2025,  6, 18), datetime(2025,  7, 30), datetime(2025,  9, 17),
    datetime(2025, 10, 29), datetime(2025, 12, 10),
    # 2026
    datetime(2026,  1, 28), datetime(2026,  3, 18), datetime(2026,  4, 29),
    datetime(2026,  6, 10), datetime(2026,  7, 29), datetime(2026,  9, 16),
    datetime(2026, 11,  4), datetime(2026, 12, 16),
]

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CHF": "Fr"}


# ---- Helpers -----------------------------------------------------------------

def _trim_log():
    try:
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > 2000:
                LOG_FILE.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def _get_active_trades() -> list[dict]:
    db_path = BASE_DIR / "trades.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY date DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_price(ticker: str) -> float | None:
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        p = None
        try:
            p = t.fast_info.last_price
        except Exception:
            pass
        if not p:
            info = t.info
            p = info.get("currentPrice") or info.get("regularMarketPrice")
        if not p:
            hist = t.history(period="1d", interval="5m")
            if not hist.empty:
                p = float(hist["Close"].iloc[-1])
        return float(p) if p else None
    except Exception as e:
        log.warning(f"Prezzo {ticker} non disponibile: {e}")
        return None


def _get_earnings_fmp(tickers: list[str]) -> list[dict]:
    """Earnings dai prossimi 14 giorni via FMP API."""
    import requests
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        return []
    today = datetime.today()
    to_   = today + timedelta(days=14)
    url = (
        f"https://financialmodelingprep.com/api/v3/earning_calendar"
        f"?from={today.strftime('%Y-%m-%d')}&to={to_.strftime('%Y-%m-%d')}&apikey={api_key}"
    )
    try:
        resp = requests.get(url, timeout=10)
        if not resp.ok:
            return []
        data = resp.json()
        upper = {t.upper() for t in tickers}
        return [
            {"ticker": e["symbol"], "date": e["date"]}
            for e in data
            if e.get("symbol", "").upper() in upper
        ]
    except Exception as e:
        log.warning(f"FMP earnings error: {e}")
        return []


def _get_earnings_yf(tickers: list[str]) -> list[dict]:
    """Fallback: earnings via yfinance calendar."""
    import yfinance as yf
    results = []
    today = datetime.today()
    cutoff = today + timedelta(days=14)
    for ticker in tickers:
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                continue
            # yfinance >= 0.2 returns a dict
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date", [])
                if not isinstance(dates, list):
                    dates = [dates]
            else:
                # older versions: DataFrame
                dates = cal.loc["Earnings Date"].tolist() if "Earnings Date" in cal.index else []
            for d in dates:
                try:
                    dt = datetime(d.year, d.month, d.day)
                    if today <= dt <= cutoff:
                        results.append({"ticker": ticker, "date": dt.strftime("%d/%m/%Y")})
                        break
                except Exception:
                    pass
        except Exception:
            pass
    return results


def _get_earnings(tickers: list[str]) -> list[dict]:
    result = _get_earnings_fmp(tickers)
    if not result:
        result = _get_earnings_yf(tickers)
    # Format dates: YYYY-MM-DD → DD/MM/YYYY
    formatted = []
    for e in result:
        d = e["date"]
        try:
            parsed = datetime.strptime(d, "%Y-%m-%d")
            d = parsed.strftime("%d/%m/%Y")
        except Exception:
            pass
        formatted.append({"ticker": e["ticker"], "date": d})
    return formatted


def _get_fomc_upcoming(days: int = 30) -> list[str]:
    today = datetime.today()
    limit = today + timedelta(days=days)
    return [
        d.strftime("%d/%m/%Y")
        for d in sorted(FOMC_DATES)
        if today <= d <= limit
    ]


def _get_macro_data() -> dict:
    """Legge l'ultimo run dell'orchestratore per estrarre macro bias + narrativa + VIX."""
    out_dir = BASE_DIR / "orchestrator_output"
    if not out_dir.exists():
        return {}
    # run_latest.json per primo, poi i run storici dal piu recente
    files = [out_dir / "run_latest.json"] + sorted(out_dir.glob("run_2*.json"), reverse=True)[:3]
    for f in files:
        try:
            if not f.exists():
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            macro = data.get("macro_analysis") or {}
            if macro:
                return {
                    "bias":      macro.get("overall_market_bias", "—"),
                    "narrative": macro.get("macro_narrative", ""),
                    "vix":       (macro.get("vix") or {}).get("value"),
                    "ts":        data.get("run_timestamp", ""),
                }
        except Exception as e:
            log.warning(f"Lettura {f.name} fallita: {e}")
    return {}


# ---- HTML builder ------------------------------------------------------------

def _build_html(trades: list[dict], macro: dict, earnings: list[dict],
                fomc: list[str], sym: str) -> str:

    today_str   = datetime.today().strftime("%d/%m/%Y")
    next_sat    = (datetime.today() + timedelta(days=7)).strftime("%d/%m/%Y")
    macro_ts    = macro.get("ts", "")[:16].replace("T", " ") if macro.get("ts") else "—"

    bias        = (macro.get("bias") or "—").replace("_", " ")
    bias_color  = ("#bb653b" if "OFF" in bias.upper()
                   else "#6daa45" if "ON" in bias.upper()
                   else "#c9943a")
    vix_val     = macro.get("vix")
    vix_str     = f"{vix_val:.1f}" if vix_val else "—"
    narrative   = macro.get("narrative") or "Avvia un'analisi per aggiornare il riassunto macroeconomico."

    # ---- Trade rows ----------------------------------------------------------
    trade_rows_html = ""
    for tr in trades:
        entry   = float(tr["entry_price"])
        qty     = float(tr["qty"])
        invested = entry * qty
        cur     = _get_price(tr["ticker"])
        if cur:
            gain = (cur - entry) / entry * 100
            gain_color = "#6daa45" if gain >= 0 else "#bb653b"
            cur_str  = f"{sym}{cur:.2f}"
            gain_str = f'<span style="color:{gain_color};font-weight:700">{gain:+.2f}%</span>'
        else:
            cur_str  = "—"
            gain_str = "—"
        sl_str = (f'<span style="color:#bb653b">{sym}{float(tr["stop_loss"]):.2f}</span>'
                  if tr.get("stop_loss") else "—")
        tp_str = (f'<span style="color:#6daa45">{sym}{float(tr["take_profit"]):.2f}</span>'
                  if tr.get("take_profit") else "—")
        trade_rows_html += f"""
          <tr>
            <td style="padding:9px 12px;border-bottom:1px solid #2a2520">{tr['date']}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #2a2520;font-weight:800;color:#5fb3bf">{tr['ticker']}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #2a2520">{sym}{entry:.2f}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #2a2520;color:#8a7d6b">{sym}{invested:.2f}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #2a2520">{cur_str}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #2a2520">{gain_str}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #2a2520">{sl_str}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #2a2520">{tp_str}</td>
          </tr>"""

    if not trade_rows_html:
        trade_rows_html = (
            '<tr><td colspan="8" style="padding:20px;text-align:center;color:#8a7d6b">'
            "Nessun trade aperto questa settimana.</td></tr>"
        )

    # ---- Earnings ------------------------------------------------------------
    if earnings:
        earn_items = "".join(
            f'<li style="margin-bottom:6px"><b style="color:#5fb3bf">{e["ticker"]}</b>'
            f' &mdash; trimestrale prevista il <b>{e["date"]}</b></li>'
            for e in earnings
        )
        earn_html = f'<ul style="margin:0;padding-left:20px">{earn_items}</ul>'
    else:
        earn_html = '<p style="color:#8a7d6b;margin:0">Nessuna trimestrale rilevante nelle prossime 2 settimane per i trade attivi.</p>'

    # ---- FOMC ----------------------------------------------------------------
    if fomc:
        fomc_items = "".join(
            f'<li style="margin-bottom:4px">Riunione FOMC: <b>{d}</b></li>'
            for d in fomc
        )
        fomc_html = f'<ul style="margin:0;padding-left:20px">{fomc_items}</ul>'
    else:
        fomc_html = '<p style="color:#8a7d6b;margin:0">Nessuna riunione FED / FOMC nei prossimi 30 giorni.</p>'

    # ---- Full HTML -----------------------------------------------------------
    return f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Finance Screener — Report settimanale {today_str}</title>
</head>
<body style="margin:0;padding:0;background:#14130f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e8ddc8">
<div style="max-width:660px;margin:0 auto;padding:32px 16px">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#4f98a3 0%,#5a8f38 100%);border-radius:14px;padding:26px 28px;margin-bottom:22px">
    <div style="font-size:22px;font-weight:800;letter-spacing:-.02em">Finance Screener</div>
    <div style="font-size:14px;opacity:.85;margin-top:5px">Report settimanale &mdash; settimana del {next_sat}</div>
    <div style="font-size:12px;opacity:.65;margin-top:2px">Generato il {today_str}</div>
  </div>

  <!-- Reminder -->
  <div style="background:#1c1a15;border:1px solid #2a2520;border-left:4px solid #5fb3bf;border-radius:12px;padding:20px 22px;margin-bottom:18px">
    <div style="font-size:11px;font-weight:700;color:#5fb3bf;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px">&#128270; Promemoria analisi settimanale</div>
    <div style="font-size:14px;line-height:1.65">
      Buon sabato! &Egrave; il momento di aggiornare la tua watchlist. Avvia <b>Finance Screener</b>, seleziona i mercati di interesse e clicca <b>Avvia analisi</b> per ricevere i segnali aggiornati.
    </div>
    <div style="margin-top:14px">
      <a href="http://localhost:5000" style="display:inline-block;background:linear-gradient(135deg,#4f98a3,#5a8f38);color:#fff;text-decoration:none;padding:10px 22px;border-radius:8px;font-size:13px;font-weight:700">Apri Finance Screener &rarr;</a>
    </div>
  </div>

  <!-- Macro -->
  <div style="background:#1c1a15;border:1px solid #2a2520;border-radius:12px;padding:20px 22px;margin-bottom:18px">
    <div style="font-size:11px;font-weight:700;color:#8a7d6b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:14px">&#127758; Situazione Macroeconomica</div>
    <div style="display:flex;gap:14px;margin-bottom:14px;flex-wrap:wrap">
      <div style="background:#242019;border-radius:8px;padding:12px 16px;flex:1;min-width:110px">
        <div style="font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Bias di mercato</div>
        <div style="font-size:20px;font-weight:800;color:{bias_color}">{bias}</div>
      </div>
      <div style="background:#242019;border-radius:8px;padding:12px 16px;flex:1;min-width:110px">
        <div style="font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">VIX</div>
        <div style="font-size:20px;font-weight:800;color:#c9943a">{vix_str}</div>
      </div>
      <div style="background:#242019;border-radius:8px;padding:12px 16px;flex:1;min-width:110px">
        <div style="font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">Ultima analisi</div>
        <div style="font-size:13px;font-weight:700;color:#e8ddc8">{macro_ts}</div>
      </div>
    </div>
    <div style="font-size:13px;color:#8a7d6b;line-height:1.65;background:#242019;border-radius:8px;padding:13px 15px">
      {narrative}
    </div>
  </div>

  <!-- Active Trades -->
  <div style="background:#1c1a15;border:1px solid #2a2520;border-radius:12px;padding:20px 22px;margin-bottom:18px">
    <div style="font-size:11px;font-weight:700;color:#8a7d6b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:14px">&#128200; Trade Attivi</div>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:13px;min-width:500px">
        <thead>
          <tr style="background:#242019">
            <th style="padding:8px 12px;text-align:left;font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Data</th>
            <th style="padding:8px 12px;text-align:left;font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Ticker</th>
            <th style="padding:8px 12px;text-align:left;font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Ingresso</th>
            <th style="padding:8px 12px;text-align:left;font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Investito</th>
            <th style="padding:8px 12px;text-align:left;font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Prezzo att.</th>
            <th style="padding:8px 12px;text-align:left;font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;font-weight:600">Guad. %</th>
            <th style="padding:8px 12px;text-align:left;font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;font-weight:600">SL</th>
            <th style="padding:8px 12px;text-align:left;font-size:9px;color:#5a5048;text-transform:uppercase;letter-spacing:.06em;font-weight:600">TP</th>
          </tr>
        </thead>
        <tbody>{trade_rows_html}</tbody>
      </table>
    </div>
  </div>

  <!-- Upcoming Events -->
  <div style="background:#1c1a15;border:1px solid #2a2520;border-radius:12px;padding:20px 22px;margin-bottom:22px">
    <div style="font-size:11px;font-weight:700;color:#8a7d6b;text-transform:uppercase;letter-spacing:.07em;margin-bottom:16px">&#128197; Notizie della prossima settimana</div>

    <div style="margin-bottom:18px">
      <div style="font-size:12px;font-weight:700;color:#c9943a;margin-bottom:10px">&#128202; Trimestrali (prossime 2 settimane)</div>
      {earn_html}
    </div>

    <div>
      <div style="font-size:12px;font-weight:700;color:#5fb3bf;margin-bottom:10px">&#127981; Riunioni FED / FOMC (prossimi 30 giorni)</div>
      {fomc_html}
    </div>
  </div>

  <!-- Disclaimer -->
  <div style="border-top:1px solid #2a2520;padding-top:18px;font-size:11px;color:#5a5048;line-height:1.7">
    <b style="color:#8a7d6b">Disclaimer:</b> Questo report &egrave; generato automaticamente da Finance Screener a scopo
    informativo e di promemoria personale. <b>Non costituisce consulenza finanziaria</b>,
    raccomandazione di investimento o sollecitazione all'acquisto/vendita di strumenti finanziari ai sensi
    del D.Lgs. 58/1998 (TUF) e della Direttiva MiFID II. I segnali prodotti dall'intelligenza artificiale
    hanno carattere puramente indicativo. Le performance passate non sono indicative di quelle future.
    Investi sempre con consapevolezza e, in caso di dubbi, consulta un consulente finanziario
    indipendente abilitato.
  </div>

</div>
</body>
</html>"""


# ---- Send -------------------------------------------------------------------

def _send_report(html: str, subject: str):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    notify    = os.getenv("NOTIFY_EMAIL", smtp_user).strip()

    if not smtp_user or not smtp_pass or not notify:
        log.warning("Email non configurata — report non inviato")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Finance Screener <{smtp_user}>"
    msg["To"]      = notify
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, notify, msg.as_string())
        log.info(f"Report inviato a {notify}")
    except Exception as e:
        log.error(f"Errore invio email: {e}")


# ---- Main -------------------------------------------------------------------

def run():
    log.info("--- Report settimanale avviato ---")

    currency = os.getenv("APP_CURRENCY", "USD")
    sym      = CURRENCY_SYMBOLS.get(currency, "$")

    trades   = _get_active_trades()
    log.info(f"Trade attivi: {len(trades)}")

    tickers  = list({t["ticker"] for t in trades})
    earnings = _get_earnings(tickers) if tickers else []
    log.info(f"Trimestrali trovate: {len(earnings)}")

    fomc     = _get_fomc_upcoming(days=30)
    log.info(f"Riunioni FOMC prossime: {fomc}")

    macro    = _get_macro_data()
    log.info(f"Macro bias: {macro.get('bias', 'N/D')}")

    html     = _build_html(trades, macro, earnings, fomc, sym)
    today_s  = datetime.today().strftime("%d/%m/%Y")
    subject  = f"📊 Finance Screener — Report settimanale {today_s}"

    _send_report(html, subject)
    log.info("--- Report settimanale terminato ---")


if __name__ == "__main__":
    _trim_log()
    try:
        run()
    except Exception as e:
        log.error(f"Errore fatale: {e}", exc_info=True)
