"""
monitor.py -- Monitor standalone SL/TP per Finance Orchestrator
Eseguito ogni 5 minuti dal Task Scheduler di Windows.
Funziona indipendentemente dal server Flask (app.py).
"""

import os
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

LOG_FILE = BASE_DIR / "monitor.log"
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

MAX_LOG_LINES = 2000  # rotazione log semplice


def _trim_log():
    """Mantieni il log sotto MAX_LOG_LINES righe."""
    try:
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > MAX_LOG_LINES:
                LOG_FILE.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def _send_telegram(message: str) -> bool:
    import requests
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("Telegram non configurato (manca TOKEN o CHAT_ID nel .env)")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.ok:
            log.info(f"Telegram OK: {message[:80].replace(chr(10),' ')}")
            return True
        log.error(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"Telegram errore connessione: {e}")
    return False


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


def resolve_backtest_signals(conn) -> None:
    """
    Risolve i segnali di backtest scaduti (target_date raggiunta) con il prezzo reale.
    Spostato qui da app.py: il monitor gira dal Task Scheduler, quindi la risoluzione
    avviene anche senza il server Flask acceso.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        pending = conn.execute(
            "SELECT id, ticker, entry_price FROM backtest_signals "
            "WHERE outcome='PENDING' AND target_date <= ?",
            (today,),
        ).fetchall()
    except sqlite3.OperationalError:
        return  # tabella backtest_signals non ancora creata

    if not pending:
        return

    log.info(f"Backtest: {len(pending)} segnali scaduti da risolvere")
    for row in pending:
        entry = row["entry_price"]
        if not entry:
            continue
        p = _get_price(row["ticker"])
        if p is None:
            continue
        ret_pct = round((p - float(entry)) / float(entry) * 100, 2)
        outcome = "WIN" if ret_pct > 0 else "LOSS"
        conn.execute(
            "UPDATE backtest_signals SET price_at_target=?, return_pct=?, outcome=? WHERE id=?",
            (round(p, 2), ret_pct, outcome, row["id"]),
        )
        conn.commit()
        log.info(f"  {row['ticker']}: {outcome} {ret_pct:+.2f}% (prezzo {p:.2f})")


def run_monitor():
    db_path = BASE_DIR / "trades.db"
    if not db_path.exists():
        log.warning("trades.db non trovato -- nessun trade da monitorare")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Risolvi i segnali di backtest scaduti (indipendente dai trade aperti)
    resolve_backtest_signals(conn)

    rows = conn.execute(
        "SELECT * FROM trades "
        "WHERE exit_price IS NULL "
        "AND (stop_loss IS NOT NULL OR take_profit IS NOT NULL) "
        "AND alert_sent IS NULL"
    ).fetchall()

    if not rows:
        conn.close()
        return  # silenzioso: nessun trade da monitorare

    log.info(f"Trade aperti con SL/TP: {len(rows)}")

    tickers = list({r["ticker"] for r in rows})
    prices  = {}
    for t in tickers:
        p = _get_price(t)
        if p:
            prices[t] = p
            log.info(f"  {t}: ${p:.2f}")

    alerts_sent = 0
    for row in rows:
        p = prices.get(row["ticker"])
        if p is None:
            continue

        hit = None
        if row["stop_loss"]   and p <= float(row["stop_loss"]):
            hit = ("🔴 STOP LOSS",   float(row["stop_loss"]))
        elif row["take_profit"] and p >= float(row["take_profit"]):
            hit = ("🟢 TAKE PROFIT", float(row["take_profit"]))

        if hit:
            label, level = hit
            pnl = round((p - float(row["entry_price"])) * float(row["qty"]), 2)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            message = (
                f"<b>{label} raggiunto: {row['ticker']}</b>\n\n"
                f"Ingresso:    <b>${float(row['entry_price']):.2f}</b>\n"
                f"Livello:     <b>${level:.2f}</b>\n"
                f"Prezzo att.: <b>${p:.2f}</b>\n"
                f"Quantita:    {row['qty']}\n"
                f"P&amp;L:    <b>${pnl:+.2f}</b>"
            )
            if _send_telegram(message):
                # Chiude il trade nel DB con il prezzo corrente
                pnl_final = round((p - float(row["entry_price"])) * float(row["qty"]), 2)
                conn.execute(
                    "UPDATE trades SET exit_price=?, pnl=?, alert_sent=? WHERE id=?",
                    (round(p, 2), pnl_final, now_str, row["id"]),
                )
                # Aggiorna il record backtest corrispondente (se esiste)
                ret_pct = round((p - float(row["entry_price"])) / float(row["entry_price"]) * 100, 2)
                outcome = "WIN" if pnl_final > 0 else "LOSS"
                bt = conn.execute(
                    "SELECT id FROM backtest_signals WHERE ticker=? AND outcome='PENDING' ORDER BY id DESC LIMIT 1",
                    (row["ticker"],)
                ).fetchone()
                if bt:
                    conn.execute(
                        "UPDATE backtest_signals SET price_at_target=?, return_pct=?, outcome=?, target_date=? WHERE id=?",
                        (round(p, 2), ret_pct, outcome, now_str[:10], bt["id"]),
                    )
                conn.commit()
                alerts_sent += 1

    conn.close()
    if alerts_sent:
        log.info(f"Alert inviati: {alerts_sent}")


if __name__ == "__main__":
    _trim_log()
    log.info("--- Monitor avviato ---")
    try:
        run_monitor()
    except Exception as e:
        log.error(f"Errore fatale: {e}", exc_info=True)
    log.info("--- Monitor terminato ---")
