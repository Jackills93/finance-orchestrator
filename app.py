"""
app.py -- Dashboard server Flask
Finance Screener
"""

import io
import os
import re
import csv
import hmac
import json
import time
import queue
import sqlite3
import smtplib
import hashlib
import subprocess
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlsplit

import yfinance as yf
from flask import Flask, jsonify, render_template, request, send_file
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
BASE_DIR = Path(__file__).parent

# ---- Sicurezza ---------------------------------------------------------------

# Di default la dashboard ascolta solo su questo PC. Per aprirla da altri
# dispositivi della rete locale impostare nel .env:
#   APP_HOST=0.0.0.0
#   APP_ALLOWED_HOSTS=192.168.1.50,nome-pc   (indirizzi con cui la si apre)
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "5000"))
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _allowed_hosts() -> set:
    extra = os.getenv("APP_ALLOWED_HOSTS", "")
    return _LOCAL_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()}


@app.before_request
def _security_checks():
    # Host header: blocca il DNS rebinding (un sito esterno il cui dominio
    # risolve su 127.0.0.1 per leggere o modificare i dati della dashboard)
    hostname = (urlsplit(f"//{request.host}").hostname or "").lower()
    if hostname not in _allowed_hosts():
        return jsonify({"error": "Host non consentito"}), 403

    # CSRF: le richieste che modificano dati devono partire dalla dashboard
    # stessa, non da una pagina di un altro sito aperta nel browser
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        site = request.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return jsonify({"error": "Richiesta da un altro sito bloccata"}), 403
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify({"error": "Richiesta da un altro sito bloccata"}), 403

# ---- Pipeline state ----------------------------------------------------------

_state = {
    "running":  False,
    "progress": 0,
    "step":     "In attesa",
    "error":    None,
}

# Log persistente dell'output di orchestrator.py, per diagnosticare crash/hang
# (il vecchio codice leggeva le righe solo per aggiornare la progress bar e le
# scartava: se il sottoprocesso moriva a meta' non restava traccia del perche').
PIPELINE_LOG_PATH   = BASE_DIR / "orchestrator_run.log"
PIPELINE_LOG_MAXLEN = 5000  # righe

# Se non arriva nessuna riga di output per questo tempo, il sottoprocesso e'
# considerato bloccato (visto succedere su Windows: il processo muore ma la
# pipe di stdout non riceve mai EOF, e la lettura resta sospesa per sempre).
PIPELINE_STALL_TIMEOUT = 180  # secondi

# Evita invii doppi del report se il pulsante viene premuto due volte
_report_lock = threading.Lock()


def _trim_pipeline_log():
    try:
        if PIPELINE_LOG_PATH.exists():
            lines = PIPELINE_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > PIPELINE_LOG_MAXLEN:
                PIPELINE_LOG_PATH.write_text("\n".join(lines[-PIPELINE_LOG_MAXLEN:]) + "\n", encoding="utf-8")
    except Exception:
        pass

# ---- Database ----------------------------------------------------------------

DB_PATH = BASE_DIR / "trades.db"

def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT    NOT NULL,
                ticker       TEXT    NOT NULL,
                qty          REAL    NOT NULL,
                entry_price  REAL    NOT NULL,
                exit_price   REAL,
                pnl          REAL,
                signal       TEXT,
                stop_loss    REAL,
                take_profit  REAL,
                alert_sent   TEXT
            )
        """)
        conn.commit()
        # Tabella backtest: traccia ogni segnale per misurare l'accuratezza
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_date     TEXT    NOT NULL,
                ticker          TEXT    NOT NULL,
                signal          TEXT    NOT NULL,
                composite_score INTEGER,
                entry_price     REAL,
                suggested_sl    REAL,
                suggested_tp    REAL,
                target_date     TEXT,
                price_at_target REAL,
                return_pct      REAL,
                outcome         TEXT    DEFAULT 'PENDING'
            )
        """)
        conn.commit()
        # Migrate existing DB: add new columns if missing
        for col, typedef in [
            ("stop_loss",   "REAL"),
            ("take_profit", "REAL"),
            ("alert_sent",  "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
                conn.commit()
            except Exception:
                pass

# ---- Email notifications -----------------------------------------------------

def _send_email(subject: str, body: str):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    notify_to = os.getenv("NOTIFY_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        return  # not configured

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = notify_to
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, [notify_to], msg.as_string())
    except Exception as e:
        print(f"[EMAIL] Errore invio: {e}")


def _send_telegram(message: str):
    """Invia notifica via Telegram Bot. Se non configurato, usa email come fallback."""
    import requests as _req
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        try:
            resp = _req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.ok:
                print(f"[TELEGRAM] Notifica inviata.")
                return
            print(f"[TELEGRAM] Errore HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[TELEGRAM] Errore invio: {e}")
    # Fallback email
    plain   = message.replace("<b>","").replace("</b>","").replace("<i>","").replace("</i>","")
    subject = plain.split("\n")[0][:120]
    _send_email(subject, plain)


def _price_monitor():
    """Background thread: controlla SL/TP ogni 5 minuti per i trade aperti."""
    while True:
        time.sleep(300)
        try:
            with _db() as conn:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE exit_price IS NULL "
                    "AND (stop_loss IS NOT NULL OR take_profit IS NOT NULL)"
                ).fetchall()
            if not rows:
                continue

            tickers = list({r["ticker"] for r in rows})
            prices  = {}
            for t in tickers:
                try:
                    yf_ticker = yf.Ticker(t)
                    p = None
                    # fast_info è più affidabile di .info (non fa scraping HTML)
                    try:
                        p = yf_ticker.fast_info.last_price
                    except Exception:
                        pass
                    if not p:
                        info = yf_ticker.info
                        p = info.get("currentPrice") or info.get("regularMarketPrice")
                    if not p:
                        hist = yf_ticker.history(period="1d", interval="5m")
                        if not hist.empty:
                            p = float(hist["Close"].iloc[-1])
                    if p:
                        prices[t] = float(p)
                except Exception:
                    pass

            for row in rows:
                p = prices.get(row["ticker"])
                if p is None:
                    continue
                hit = None
                if row["stop_loss"]   and p <= row["stop_loss"]:
                    hit = ("🔴 STOP LOSS", row["stop_loss"])
                elif row["take_profit"] and p >= row["take_profit"]:
                    hit = ("🟢 TAKE PROFIT", row["take_profit"])

                if hit and not row["alert_sent"]:
                    label, level = hit
                    pnl_est = (p - row["entry_price"]) * row["qty"]
                    message = (
                        f"<b>{label} raggiunto: {row['ticker']}</b>\n\n"
                        f"Ingresso:     <b>${row['entry_price']:.2f}</b>\n"
                        f"Livello:      <b>${level:.2f}</b>\n"
                        f"Prezzo att.:  <b>${p:.2f}</b>\n"
                        f"Quantita:     {row['qty']}\n"
                        f"P&amp;L stimato: <b>${pnl_est:.2f}</b>"
                    )
                    _send_telegram(message)
                    with _db() as conn:
                        conn.execute(
                            "UPDATE trades SET alert_sent=? WHERE id=?",
                            (datetime.now().strftime("%Y-%m-%d %H:%M"), row["id"])
                        )
                        conn.commit()
        except Exception as e:
            print(f"[MONITOR] Errore: {e}")


# ---- Progress tracker --------------------------------------------------------

def _update_progress(line: str):
    rules = [
        ("Download lista S&P500",             3,  "Download lista S&P500..."),
        ("Ticker selezionati per il pipeline", 24, "Screening completato"),
        ("[STEP 1/3]",                         25, "Analisi macroeconomica..."),
        ("[AREA] FED_POLICY",                  28, "Ricerca notizie Fed..."),
        ("[AREA] ECB_POLICY",                  31, "Ricerca notizie BCE..."),
        ("[AREA] INFLATION_USA",               34, "Inflazione USA..."),
        ("[AREA] INFLATION_EU",                37, "Inflazione EU..."),
        ("Analisi contesto macroeconomico",    40, "Claude analizza macro..."),
        ("Report macro di oggi",               42, "Macro da cache"),
        ("Report salvato: macro_reports",      43, "Macro completato"),
        ("[STEP 2/3]",                         45, "Filtro fondamentale..."),
        ("Promosse all",                       58, "Filtro completato"),
        ("[STEP 3/3]",                         60, "Analisi tecnica..."),
        ("RACCOMANDAZIONE FINALE",             92, "Generazione report..."),
        ("Output completo salvato",            98, "Salvataggio output..."),
    ]
    for marker, pct, label in rules:
        if marker in line:
            _state["progress"] = pct
            _state["step"]     = label
            return

    m = re.search(r"(\d+)/(\d+)\s+analizzati", line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        _state["progress"] = 4 + int(done / total * 19)
        _state["step"]     = f"Screening {done}/{total} titoli..."
        return
    if "[FILTER] Analisi" in line:
        _state["progress"] = min(57, _state["progress"] + 2)
        return
    if "[TECHNICAL] Analisi" in line:
        _state["progress"] = min(90, _state["progress"] + 4)
        return


# ---- Backtest journal --------------------------------------------------------

def _record_backtest_signals(ranking: list):
    """Registra i segnali dell'analisi nel journal di backtest (target: 4 settimane)."""
    if not ranking:
        return
    target_date = (datetime.now() + timedelta(weeks=4)).strftime("%Y-%m-%d")
    signal_date = datetime.now().strftime("%Y-%m-%d")
    with _db() as conn:
        for r in ranking:
            ticker = r.get("ticker")
            if not ticker:
                continue
            conn.execute("""
                INSERT INTO backtest_signals
                  (signal_date, ticker, signal, composite_score,
                   entry_price, suggested_sl, suggested_tp, target_date, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                signal_date,
                ticker,
                r.get("final_signal", ""),
                r.get("composite_score"),
                r.get("price"),
                r.get("suggested_sl"),
                r.get("suggested_tp"),
                target_date,
            ))
        conn.commit()
    print(f"[BACKTEST] Registrati {len(ranking)} segnali per il {target_date}")


# NB: la risoluzione dei segnali PENDING scaduti e' in monitor.py
# (resolve_backtest_signals), eseguita dal Task Scheduler ogni 5 minuti:
# cosi' funziona anche a server Flask spento.


# ---- Pipeline runner ---------------------------------------------------------

def _backup_trades():
    """Esporta tutti i trade come JSON nella cartella backups/."""
    backup_dir = BASE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    with _db() as conn:
        rows = conn.execute("SELECT * FROM trades ORDER BY date ASC, id ASC").fetchall()
    data = {
        "backup_timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "trades": [dict(r) for r in rows],
    }
    filename = backup_dir / f"trades_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # Mantieni solo gli ultimi 20 backup
    backups = sorted(backup_dir.glob("trades_backup_*.json"))
    for old in backups[:-20]:
        old.unlink()
    print(f"[BACKUP] Trade esportati: {filename.name}")
    return filename


def _run_pipeline(mode: str, tickers: list, markets: list | None = None):
    _state.update(running=True, progress=0, step="Avvio...", error=None)
    _trim_pipeline_log()
    proc = None
    try:
        if mode == "auto":
            mkt_list = markets if markets else ["sp500"]
            cmd = ["python", "-u", "orchestrator.py", "--auto-screen", "--markets"] + mkt_list
        else:
            cmd = ["python", "-u", "orchestrator.py"] + [t.upper() for t in tickers]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(BASE_DIR), env=env,
        )

        # Le righe vengono lette in un thread separato e passate tramite coda,
        # cosi' il thread principale puo' applicare un timeout: una lettura
        # bloccante diretta (`for line in proc.stdout`) puo' restare sospesa
        # per sempre se il sottoprocesso muore senza chiudere correttamente lo
        # stdout (visto succedere su Windows).
        line_queue: "queue.Queue" = queue.Queue()

        def _reader():
            try:
                for line in proc.stdout:
                    line_queue.put(line)
            finally:
                line_queue.put(None)  # sentinella di fine stream

        threading.Thread(target=_reader, daemon=True).start()

        with open(PIPELINE_LOG_PATH, "a", encoding="utf-8") as log_f:
            log_f.write(f"\n=== Run {datetime.now().isoformat()} -- {' '.join(cmd)} ===\n")
            stalled = False
            while True:
                try:
                    line = line_queue.get(timeout=PIPELINE_STALL_TIMEOUT)
                except queue.Empty:
                    stalled = True
                    break
                if line is None:
                    break
                log_f.write(line)
                _update_progress(line.rstrip())

            if stalled:
                proc.kill()
                log_f.write(f"[WATCHDOG] Nessun output da {PIPELINE_STALL_TIMEOUT}s -- processo terminato forzatamente\n")
                _state["error"] = f"Analisi bloccata (nessuna attivita' da oltre {PIPELINE_STALL_TIMEOUT}s) -- interrotta automaticamente. Dettagli in orchestrator_run.log"
                _state["step"]  = "Errore"
                return

            proc.wait()
            log_f.write(f"=== Fine run (exit code {proc.returncode}) ===\n")

            if proc.returncode != 0:
                _state["error"] = f"orchestrator.py terminato con codice {proc.returncode} -- vedi orchestrator_run.log"
                _state["step"]  = "Errore"
                return

        _state["progress"] = 100
        _state["step"]     = "Completato"
        _backup_trades()
        # Registra segnali nel backtest journal
        try:
            latest = BASE_DIR / "orchestrator_output" / "run_latest.json"
            if latest.exists():
                with open(latest, encoding="utf-8") as f:
                    run_data = json.load(f)
                _record_backtest_signals(run_data.get("ranking", []))
        except Exception as e:
            print(f"[BACKTEST] Errore registrazione: {e}")
    except Exception as exc:
        _state["error"] = str(exc)
        _state["step"]  = "Errore"
        if proc is not None and proc.poll() is None:
            proc.kill()
    finally:
        _state["running"] = False


# ---- Routes: pipeline --------------------------------------------------------

@app.route("/")
def index():
    return render_template("finance_screener.html")


@app.route("/api/run", methods=["POST"])
def api_run():
    if _state["running"]:
        return jsonify({"error": "Analisi gia in corso"}), 400
    body    = request.get_json(force=True)
    mode    = body.get("mode", "auto")
    tickers = body.get("tickers", [])
    markets = body.get("markets", ["sp500"])
    if mode == "manual" and not tickers:
        return jsonify({"error": "Inserisci almeno un ticker"}), 400
    threading.Thread(target=_run_pipeline, args=(mode, tickers, markets), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/progress")
def api_progress():
    return jsonify(_state)


@app.route("/api/results")
def api_results():
    latest = BASE_DIR / "orchestrator_output" / "run_latest.json"
    if not latest.exists():
        return jsonify({"error": "Nessun risultato disponibile"}), 404
    with open(latest, encoding="utf-8") as f:
        return jsonify(json.load(f))


# ---- Routes: history ---------------------------------------------------------

@app.route("/api/history")
def api_history():
    out_dir = BASE_DIR / "orchestrator_output"
    if not out_dir.exists():
        return jsonify([])
    runs = []
    for p in sorted(out_dir.glob("run_2*.json"), reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            ranking = d.get("ranking", [])
            runs.append({
                "filename":     p.name,
                "timestamp":    d.get("run_timestamp", p.stem.replace("run_", "")),
                "n_strong_buy": sum(1 for r in ranking if r.get("final_signal") == "STRONG_BUY"),
                "n_buy":        sum(1 for r in ranking if r.get("final_signal") == "BUY"),
                "macro_bias":   (d.get("macro_analysis") or {}).get("overall_market_bias", ""),
                "tickers":      [r["ticker"] for r in ranking],
            })
        except Exception:
            continue
    return jsonify(runs)


@app.route("/api/history/<filename>")
def api_history_run(filename):
    p = BASE_DIR / "orchestrator_output" / filename
    if not p.exists() or not p.name.startswith("run_"):
        return jsonify({"error": "File non trovato"}), 404
    with open(p, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/history/<filename>", methods=["DELETE"])
def api_history_delete(filename):
    """Elimina un singolo run dallo storico."""
    p = BASE_DIR / "orchestrator_output" / filename
    if not p.exists() or not p.name.startswith("run_"):
        return jsonify({"error": "File non trovato"}), 404
    p.unlink()
    return jsonify({"deleted": filename})


@app.route("/api/history", methods=["DELETE"])
def api_history_clear():
    """Elimina tutti i run storici tranne run_latest.json."""
    out_dir = BASE_DIR / "orchestrator_output"
    deleted = 0
    if out_dir.exists():
        for p in out_dir.glob("run_2*.json"):
            try:
                p.unlink()
                deleted += 1
            except Exception:
                pass
    return jsonify({"deleted": deleted})


# ---- Routes: VIX -------------------------------------------------------------

@app.route("/api/vix")
def api_vix():
    """Restituisce il VIX corrente con interpretazione."""
    try:
        import yfinance as yf
        info  = yf.Ticker("^VIX").info
        value = info.get("regularMarketPrice") or info.get("currentPrice")
        if value is None:
            hist  = yf.Ticker("^VIX").history(period="1d")
            value = float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception:
        value = None

    if value is None:
        return jsonify({"value": None, "level": "unknown", "bias_hint": "neutral",
                        "description": "Dati VIX non disponibili"})

    value = round(float(value), 2)
    if value < 15:
        level="low";      bias_hint="risk_on";  color="#00d98b"
        label="Bassa volatilita"; desc="Mercato tranquillo, clima favorevole al rischio."
    elif value < 20:
        level="moderate"; bias_hint="risk_on";  color="#00d98b"
        label="Volatilita moderata"; desc="Sentiment positivo con leggera incertezza."
    elif value < 25:
        level="elevated"; bias_hint="neutral";  color="#ffb84d"
        label="Volatilita elevata"; desc="Incertezza crescente, prudenza consigliata."
    elif value < 35:
        level="high";     bias_hint="risk_off"; color="#ff5c6a"
        label="Paura diffusa"; desc="Sentiment risk-off marcato, investitori difensivi."
    else:
        level="extreme";  bias_hint="risk_off"; color="#ff5c6a"
        label="Panico"; desc="Livello di capitolazione, risk-off estremo."

    # Anche il precedente close per calcolare la variazione giornaliera
    prev = info.get("regularMarketPreviousClose") if value else None
    change = round(value - prev, 2) if prev else None
    change_pct = round((change / prev) * 100, 2) if prev and prev != 0 else None

    return jsonify({
        "value":      value,
        "prev_close": prev,
        "change":     change,
        "change_pct": change_pct,
        "level":      level,
        "label":      label,
        "bias_hint":  bias_hint,
        "color":      color,
        "description": desc,
        "thresholds": [
            {"range": "< 15",  "label": "Tranquillo",        "bias": "RISK ON",  "color": "#00d98b"},
            {"range": "15–20", "label": "Moderato",           "bias": "RISK ON",  "color": "#00d98b"},
            {"range": "20–25", "label": "Elevato",            "bias": "Neutro",   "color": "#ffb84d"},
            {"range": "25–35", "label": "Paura diffusa",      "bias": "RISK OFF", "color": "#ff5c6a"},
            {"range": "> 35",  "label": "Panico/Capitolazione","bias": "RISK OFF", "color": "#ff5c6a"},
        ]
    })


# ---- Routes: prices ----------------------------------------------------------

@app.route("/api/prices")
def api_prices():
    """Restituisce il prezzo corrente per una lista di ticker (param: t=AAPL,MSFT)."""
    raw     = request.args.get("t", "")
    tickers = [x.strip().upper() for x in raw.split(",") if x.strip()]
    result  = {}
    for ticker in tickers:
        p = None
        t = yf.Ticker(ticker)
        try:
            p = t.fast_info.last_price
        except Exception:
            pass
        if not p:
            try:
                hist = t.history(period="1d", interval="1m")
                if not hist.empty:
                    p = float(hist["Close"].iloc[-1])
            except Exception:
                pass
        if p:
            result[ticker] = round(float(p), 2)
    return jsonify(result)


# ---- Routes: trades ----------------------------------------------------------

@app.route("/api/trades", methods=["GET"])
def api_trades_get():
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to",   "")
    query = "SELECT * FROM trades"
    params = []
    filters = []
    if date_from:
        filters.append("date >= ?"); params.append(date_from)
    if date_to:
        filters.append("date <= ?"); params.append(date_to)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY date DESC, id DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/trades", methods=["POST"])
def api_trades_post():
    d           = request.get_json(force=True)
    ticker      = d.get("ticker", "").upper().strip()
    qty         = float(d.get("qty", 0))
    entry       = float(d.get("entry_price", 0))
    stop_loss   = float(d["stop_loss"])   if d.get("stop_loss")   else None
    take_profit = float(d["take_profit"]) if d.get("take_profit") else None
    signal      = d.get("signal", "")
    date        = d.get("date") or datetime.now().strftime("%Y-%m-%d")

    if not ticker or qty <= 0 or entry <= 0:
        return jsonify({"error": "Dati trade non validi"}), 400

    with _db() as conn:
        conn.execute(
            "INSERT INTO trades (date, ticker, qty, entry_price, signal, stop_loss, take_profit) "
            "VALUES (?,?,?,?,?,?,?)",
            (date, ticker, qty, entry, signal, stop_loss, take_profit),
        )
        conn.commit()
    return jsonify({"status": "ok"})


@app.route("/api/trades/<int:trade_id>/close", methods=["POST"])
def api_trades_close(trade_id):
    """Chiude un trade aperto impostando il prezzo di uscita e calcolando il P&L."""
    d      = request.get_json(force=True)
    exit_p = float(d.get("exit_price", 0))
    if exit_p <= 0:
        return jsonify({"error": "Prezzo di uscita non valido"}), 400

    with _db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return jsonify({"error": "Trade non trovato"}), 404
        pnl = round((exit_p - row["entry_price"]) * row["qty"], 2)
        conn.execute(
            "UPDATE trades SET exit_price=?, pnl=? WHERE id=?",
            (exit_p, pnl, trade_id)
        )
        # Aggiorna il record backtest corrispondente se ancora PENDING
        ret_pct = round((exit_p - row["entry_price"]) / row["entry_price"] * 100, 2)
        outcome = "WIN" if pnl > 0 else "LOSS"
        today   = datetime.now().strftime("%Y-%m-%d")
        bt = conn.execute(
            "SELECT id FROM backtest_signals WHERE ticker=? AND outcome='PENDING' ORDER BY id DESC LIMIT 1",
            (row["ticker"],)
        ).fetchone()
        if bt:
            conn.execute(
                "UPDATE backtest_signals SET price_at_target=?, return_pct=?, outcome=?, target_date=? WHERE id=?",
                (round(exit_p, 2), ret_pct, outcome, today, bt["id"]),
            )
        conn.commit()
    return jsonify({"status": "ok", "pnl": pnl})


@app.route("/api/price/<ticker>")
def api_price(ticker):
    """Restituisce il prezzo corrente di un ticker via yfinance."""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker.upper())
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
        if p:
            return jsonify({"price": round(float(p), 2)})
        return jsonify({"error": "Prezzo non disponibile"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades/<int:trade_id>/sltp", methods=["POST"])
def api_trades_update_sltp(trade_id):
    """Aggiorna Stop Loss e Take Profit di un trade esistente."""
    d           = request.get_json(force=True)
    stop_loss   = float(d["stop_loss"])   if d.get("stop_loss")   not in (None, "") else None
    take_profit = float(d["take_profit"]) if d.get("take_profit") not in (None, "") else None

    with _db() as conn:
        row = conn.execute("SELECT id FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return jsonify({"error": "Trade non trovato"}), 404
        conn.execute(
            "UPDATE trades SET stop_loss=?, take_profit=? WHERE id=?",
            (stop_loss, take_profit, trade_id)
        )
        conn.commit()
    return jsonify({"status": "ok"})


@app.route("/api/trades/export/csv")
def api_trades_export_csv():
    with _db() as conn:
        rows = conn.execute("SELECT * FROM trades ORDER BY date DESC, id DESC").fetchall()
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["id","data","ticker","qty","entry_price","exit_price",
                "pnl","signal","stop_loss","take_profit","alert_sent"])
    for r in rows:
        w.writerow([r["id"],r["date"],r["ticker"],r["qty"],r["entry_price"],
                    r["exit_price"],r["pnl"],r["signal"],
                    r["stop_loss"],r["take_profit"],r["alert_sent"]])
    filename = f"trades_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return send_file(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        mimetype="text/csv", as_attachment=True, download_name=filename,
    )


@app.route("/api/trades/export/json")
def api_trades_export_json():
    with _db() as conn:
        rows = conn.execute("SELECT * FROM trades ORDER BY date DESC, id DESC").fetchall()
    data     = [dict(r) for r in rows]
    filename = f"trades_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    return send_file(
        io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")),
        mimetype="application/json", as_attachment=True, download_name=filename,
    )


# Keep old /api/trades/export pointing to CSV for backwards compat
@app.route("/api/trades/export")
def api_trades_export_legacy():
    return api_trades_export_csv()


@app.route("/api/trades/import", methods=["POST"])
def api_trades_import():
    """Importa trade da JSON (file upload o body JSON)."""
    if request.files.get("file"):
        raw = request.files["file"].read().decode("utf-8")
    else:
        raw = request.get_data(as_text=True)
    try:
        payload = json.loads(raw)
    except Exception:
        return jsonify({"error": "JSON non valido"}), 400

    trades = payload.get("trades") if isinstance(payload, dict) else payload
    if not isinstance(trades, list):
        return jsonify({"error": "Formato non riconosciuto: atteso array 'trades'"}), 400

    imported = 0
    with _db() as conn:
        for t in trades:
            try:
                conn.execute(
                    "INSERT INTO trades (date, ticker, qty, entry_price, exit_price, pnl, "
                    "signal, stop_loss, take_profit, alert_sent) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (t.get("date"), t.get("ticker"), t.get("qty"), t.get("entry_price"),
                     t.get("exit_price"), t.get("pnl"), t.get("signal"),
                     t.get("stop_loss"), t.get("take_profit"), t.get("alert_sent")),
                )
                imported += 1
            except Exception:
                continue
        conn.commit()
    return jsonify({"status": "ok", "imported": imported})


@app.route("/api/trades", methods=["DELETE"])
def api_trades_delete_all():
    """Elimina tutti i trade."""
    with _db() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()
    return jsonify({"status": "ok"})


@app.route("/api/report/send", methods=["POST"])
def api_report_send():
    """Invia manualmente via email il report riepilogativo (non c'e' piu' invio automatico)."""
    if not _report_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "Invio report gia in corso"}), 409
    try:
        import weekly_report
        weekly_report._trim_log()
        result = weekly_report.run()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Errore generazione report: {e}"}), 500
    finally:
        _report_lock.release()
    if not result["ok"]:
        return jsonify({"ok": False, "error": result["message"]}), 400
    return jsonify(result)


@app.route("/api/backups")
def api_backups():
    """Elenca i backup disponibili nella cartella backups/."""
    backup_dir = BASE_DIR / "backups"
    if not backup_dir.exists():
        return jsonify([])
    files = sorted(backup_dir.glob("trades_backup_*.json"), reverse=True)
    result = []
    for f in files[:20]:
        try:
            with open(f, encoding="utf-8") as fp:
                d = json.load(fp)
            result.append({
                "filename":  f.name,
                "timestamp": d.get("backup_timestamp", ""),
                "n_trades":  len(d.get("trades", [])),
            })
        except Exception:
            pass
    return jsonify(result)


@app.route("/api/trades/<int:trade_id>", methods=["DELETE"])
def api_trades_delete(trade_id):
    with _db() as conn:
        row = conn.execute("SELECT ticker FROM trades WHERE id=?", (trade_id,)).fetchone()
        conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        # Se non esistono altri trade chiusi per questo ticker, riporta il backtest a PENDING
        if row:
            still_closed = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE ticker=? AND exit_price IS NOT NULL",
                (row["ticker"],)
            ).fetchone()[0]
            if still_closed == 0:
                conn.execute(
                    """UPDATE backtest_signals
                       SET outcome='PENDING', price_at_target=NULL, return_pct=NULL
                       WHERE ticker=? AND outcome != 'PENDING'""",
                    (row["ticker"],)
                )
        conn.commit()
    return jsonify({"status": "ok"})


# ---- Main --------------------------------------------------------------------

@app.route("/api/backtest")
def api_backtest():
    with _db() as conn:
        today = datetime.now().strftime("%Y-%m-%d")

        # Passo 1: segnali non-PENDING senza trade chiusi → torna a PENDING
        resolved = conn.execute(
            "SELECT id, ticker FROM backtest_signals WHERE outcome != 'PENDING'"
        ).fetchall()
        for bs in resolved:
            n = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE ticker=? AND exit_price IS NOT NULL",
                (bs["ticker"],)
            ).fetchone()[0]
            if n == 0:
                conn.execute(
                    """UPDATE backtest_signals
                       SET outcome='PENDING', price_at_target=NULL, return_pct=NULL
                       WHERE id=?""",
                    (bs["id"],)
                )

        # Passo 2: segnali PENDING con trade chiuso → risolvi con prezzo di uscita reale
        pending = conn.execute(
            "SELECT id, ticker, entry_price FROM backtest_signals WHERE outcome='PENDING'"
        ).fetchall()
        for bs in pending:
            closed_trade = conn.execute(
                """SELECT exit_price FROM trades
                   WHERE ticker=? AND exit_price IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
                (bs["ticker"],)
            ).fetchone()
            if closed_trade:
                ep  = closed_trade["exit_price"]
                ret = round((ep - bs["entry_price"]) / bs["entry_price"] * 100, 2)
                out = "WIN" if ret > 0 else "LOSS"
                conn.execute(
                    """UPDATE backtest_signals
                       SET price_at_target=?, return_pct=?, outcome=?, target_date=?
                       WHERE id=?""",
                    (round(ep, 2), ret, out, today, bs["id"])
                )
        conn.commit()

        rows = conn.execute("""
            SELECT * FROM backtest_signals ORDER BY signal_date DESC, id DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/backtest/<int:sid>", methods=["DELETE"])
def api_backtest_delete(sid):
    with _db() as conn:
        conn.execute("DELETE FROM backtest_signals WHERE id=?", (sid,))
        conn.commit()
    return jsonify({"deleted": sid})


@app.route("/api/backtest/clear", methods=["DELETE"])
def api_backtest_clear():
    with _db() as conn:
        conn.execute("DELETE FROM backtest_signals")
        conn.commit()
    return jsonify({"deleted": "all"})


# ---- Settings ---------------------------------------------------------------
_ENV_PATH = BASE_DIR / ".env"

def _read_env_raw() -> dict:
    """Read .env as key→value dict (no os.environ side effects)."""
    from dotenv import dotenv_values
    return dict(dotenv_values(_ENV_PATH))

def _write_env_key(key: str, value: str):
    """Write/update a single key in the .env file."""
    from dotenv import set_key
    set_key(str(_ENV_PATH), key, value, quote_mode="never")

def _mask(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 8:
        return "••••••••"
    return val[:4] + "••••" + val[-4:]

_LEGACY_SHA256 = re.compile(r"[0-9a-f]{64}")


def _is_legacy_hash(stored: str) -> bool:
    return bool(_LEGACY_SHA256.fullmatch(stored))


def _hash_password(password: str) -> str:
    # pbkdf2 invece del default scrypt: scrypt manca in alcune build di Python
    return generate_password_hash(password, method="pbkdf2:sha256")


def _verify_password(password: str) -> bool:
    env = _read_env_raw()
    stored = env.get("SETTINGS_PASSWORD_HASH", "")
    if not stored:
        return True  # no password set yet → open access
    if _is_legacy_hash(stored):
        # Vecchio formato: SHA-256 senza salt (aggiornato al primo salvataggio)
        return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)
    return check_password_hash(stored, password)


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    env = _read_env_raw()
    return jsonify({
        "anthropic_key":    _mask(env.get("ANTHROPIC_API_KEY", "")),
        "fmp_key":          _mask(env.get("FMP_API_KEY", "")),
        "av_key":           _mask(env.get("ALPHA_VANTAGE_API_KEY", "")),
        "telegram_token":   _mask(env.get("TELEGRAM_BOT_TOKEN", "")),
        "telegram_chat_id": env.get("TELEGRAM_CHAT_ID", ""),
        "smtp_user":        env.get("SMTP_USER", ""),
        "notify_email":     env.get("NOTIFY_EMAIL", ""),
        "language":         env.get("APP_LANGUAGE", "it"),
        "currency":         env.get("APP_CURRENCY", "USD"),
        "portfolio_capital": env.get("PORTFOLIO_CAPITAL", ""),
        "risk_per_trade":    env.get("RISK_PER_TRADE_PCT", "1"),
        "max_position_pct":  env.get("MAX_POSITION_PCT", "20"),
        "password_set":     bool(env.get("SETTINGS_PASSWORD_HASH", "")),
    })


@app.route("/api/settings/verify", methods=["POST"])
def api_settings_verify():
    data = request.json or {}
    if _verify_password(data.get("password", "")):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Password errata"})


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.json or {}
    if not _verify_password(data.get("password", "")):
        return jsonify({"ok": False, "error": "Password errata"}), 403

    # API keys — save only if the value is not a masked placeholder
    key_fields = {
        "anthropic_key":    "ANTHROPIC_API_KEY",
        "fmp_key":          "FMP_API_KEY",
        "av_key":           "ALPHA_VANTAGE_API_KEY",
        "telegram_token":   "TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": "TELEGRAM_CHAT_ID",
        "smtp_user":        "SMTP_USER",
        "notify_email":     "NOTIFY_EMAIL",
    }
    for field, env_key in key_fields.items():
        val = data.get(field, "").strip()
        if val and "••••" not in val:
            _write_env_key(env_key, val)

    # Preferences — always save
    _write_env_key("APP_LANGUAGE", data.get("language", "it"))
    _write_env_key("APP_CURRENCY", data.get("currency", "USD"))

    # Position sizing — save only numeric values (empty capital = sizing off)
    sizing_fields = {
        "portfolio_capital": ("PORTFOLIO_CAPITAL", ""),
        "risk_per_trade":    ("RISK_PER_TRADE_PCT", "1"),
        "max_position_pct":  ("MAX_POSITION_PCT", "20"),
    }
    for field, (env_key, default) in sizing_fields.items():
        val = str(data.get(field, "")).strip()
        if val == "":
            _write_env_key(env_key, default)
            continue
        try:
            num = float(val)
            if num >= 0:
                _write_env_key(env_key, f"{num:g}")
        except ValueError:
            pass  # valore non numerico: ignora, mantieni il precedente

    # New password (hash con salt); una password gia verificata nel vecchio
    # formato SHA-256 viene ri-salvata nel nuovo formato
    new_pwd = data.get("new_password", "").strip()
    old_pwd = data.get("password", "")
    if new_pwd:
        _write_env_key("SETTINGS_PASSWORD_HASH", _hash_password(new_pwd))
    elif old_pwd and _is_legacy_hash(_read_env_raw().get("SETTINGS_PASSWORD_HASH", "")):
        _write_env_key("SETTINGS_PASSWORD_HASH", _hash_password(old_pwd))

    # Reload env in memory so the running process picks up changes
    load_dotenv(str(_ENV_PATH), override=True)
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    threading.Thread(target=_price_monitor, daemon=True).start()
    print("\n  Finance Screener Dashboard")
    print(f"  Apri il browser su: http://localhost:{APP_PORT}")
    if APP_HOST not in _LOCAL_HOSTS:
        print(f"  ATTENZIONE: in ascolto su {APP_HOST} -- la dashboard e' raggiungibile dalla rete,")
        print(f"  host consentiti: {', '.join(sorted(_allowed_hosts()))}")
    print()
    app.run(debug=False, host=APP_HOST, port=APP_PORT)
