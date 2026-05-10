import os
import json
import sqlite3
import requests
from datetime import datetime
from contextlib import contextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ─────────────────────────────────────────
# DATABASE SETUP — SQLite
# ─────────────────────────────────────────
DB_PATH = "trades.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT,
                side          TEXT,
                setup         TEXT,
                entry         REAL,
                sl            REAL,
                tp1           REAL,
                tp2           REAL,
                risk_points   REAL,
                reward_points REAL,
                zone_key      TEXT,
                result        TEXT DEFAULT 'OPEN',
                profit_points REAL DEFAULT 0,
                tp1_hit       INTEGER DEFAULT 0,
                reason        TEXT,
                created_at    TEXT,
                closed_at     TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS used_zones (
                zone_key TEXT PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_state (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                symbol       TEXT,
                timeframe    TEXT,
                trend        TEXT,
                close_price  REAL,
                updated_at   TEXT
            )
        """)
        conn.commit()

init_db()

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ─────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────
def db_get_open_trade():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE result = 'OPEN' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

def db_insert_trade(trade: dict) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO trades
              (symbol, side, setup, entry, sl, tp1, tp2,
               risk_points, reward_points, zone_key, result,
               profit_points, tp1_hit, reason, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade["symbol"], trade["side"], trade["setup"],
            trade["entry"], trade["sl"], trade["tp1"], trade["tp2"],
            trade["risk_points"], trade["reward_points"],
            trade["zone_key"], "OPEN", 0, 0,
            trade["reason"],
            datetime.utcnow().isoformat()
        ))
        return cur.lastrowid

def db_update_trade(trade_id: int, result: str, profit: float, tp1_hit=None):
    with get_db() as conn:
        if tp1_hit is not None:
            conn.execute(
                "UPDATE trades SET tp1_hit=? WHERE id=?",
                (1 if tp1_hit else 0, trade_id)
            )
        if result != "TP1_HIT":
            conn.execute(
                "UPDATE trades SET result=?, profit_points=?, closed_at=? WHERE id=?",
                (result, profit, datetime.utcnow().isoformat(), trade_id)
            )

def db_zone_used(zone_key: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM used_zones WHERE zone_key=?", (zone_key,)
        ).fetchone()
        return row is not None

def db_add_zone(zone_key: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO used_zones (zone_key) VALUES (?)", (zone_key,)
        )

def db_get_all_trades():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

def db_get_used_zones():
    with get_db() as conn:
        rows = conn.execute("SELECT zone_key FROM used_zones").fetchall()
        return [r["zone_key"] for r in rows]

def db_update_market(data: dict):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO market_state (id, symbol, timeframe, trend, close_price, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              symbol=excluded.symbol,
              timeframe=excluded.timeframe,
              trend=excluded.trend,
              close_price=excluded.close_price,
              updated_at=excluded.updated_at
        """, (
            data.get("symbol"),
            data.get("timeframe"),
            data.get("trend"),
            float(data.get("close", 0)),
            datetime.utcnow().isoformat()
        ))

def db_get_market():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM market_state WHERE id=1").fetchone()
        return dict(row) if row else None

def db_get_stats():
    with get_db() as conn:
        rows = conn.execute("SELECT result, profit_points FROM trades").fetchall()
    trades = [dict(r) for r in rows]
    wins        = sum(1 for t in trades if t["result"] in ("TP1", "TP2"))
    losses      = sum(1 for t in trades if t["result"] == "SL")
    open_count  = sum(1 for t in trades if t["result"] == "OPEN")
    total_pnl   = sum(t["profit_points"] or 0 for t in trades)
    total_closed = wins + losses
    win_rate    = round((wins / total_closed * 100), 1) if total_closed > 0 else 0
    return {
        "total":      len(trades),
        "wins":       wins,
        "losses":     losses,
        "open":       open_count,
        "win_rate":   win_rate,
        "total_pnl":  round(total_pnl, 2)
    }

# ─────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        })
        print("Telegram:", r.status_code)
    except Exception as e:
        print("Telegram Error:", e)

# ─────────────────────────────────────────
# Check open trade SL / TP per candle
# ─────────────────────────────────────────
def check_trade_by_candle(high: float, low: float, close: float):
    trade = db_get_open_trade()
    if trade is None:
        return

    side  = trade["side"]
    tp1   = float(trade["tp1"])
    tp2   = float(trade["tp2"])
    sl    = float(trade["sl"])
    tid   = trade["id"]
    tp1_hit = bool(trade["tp1_hit"])

    if side == "BUY":
        if low <= sl:
            db_update_trade(tid, "SL", -trade["risk_points"])
            send_telegram(
                f"❌ <b>SL HIT</b>\n\n"
                f"Symbol: {trade['symbol']}\nSide: BUY\n"
                f"Entry: {trade['entry']}\nSL: {sl}\n"
                f"Loss: -{trade['risk_points']} pts"
            )
            return
        if high >= tp2:
            db_update_trade(tid, "TP2", trade["reward_points"])
            send_telegram(
                f"✅ <b>TP2 HIT</b>\n\n"
                f"Symbol: {trade['symbol']}\nSide: BUY\n"
                f"Entry: {trade['entry']}\nTP2: {tp2}\n"
                f"Profit: +{trade['reward_points']} pts"
            )
            return
        if not tp1_hit and high >= tp1:
            db_update_trade(tid, "TP1_HIT", trade["reward_points"] / 2, tp1_hit=True)
            send_telegram(
                f"🎯 <b>TP1 HIT</b>\n\n"
                f"Symbol: {trade['symbol']}\nSide: BUY\n"
                f"Entry: {trade['entry']}\nTP1: {tp1}\n"
                f"Partial: +{round(trade['reward_points']/2,1)} pts"
            )

    if side == "SELL":
        if high >= sl:
            db_update_trade(tid, "SL", -trade["risk_points"])
            send_telegram(
                f"❌ <b>SL HIT</b>\n\n"
                f"Symbol: {trade['symbol']}\nSide: SELL\n"
                f"Entry: {trade['entry']}\nSL: {sl}\n"
                f"Loss: -{trade['risk_points']} pts"
            )
            return
        if low <= tp2:
            db_update_trade(tid, "TP2", trade["reward_points"])
            send_telegram(
                f"✅ <b>TP2 HIT</b>\n\n"
                f"Symbol: {trade['symbol']}\nSide: SELL\n"
                f"Entry: {trade['entry']}\nTP2: {tp2}\n"
                f"Profit: +{trade['reward_points']} pts"
            )
            return
        if not tp1_hit and low <= tp1:
            db_update_trade(tid, "TP1_HIT", trade["reward_points"] / 2, tp1_hit=True)
            send_telegram(
                f"🎯 <b>TP1 HIT</b>\n\n"
                f"Symbol: {trade['symbol']}\nSide: SELL\n"
                f"Entry: {trade['entry']}\nTP1: {tp1}\n"
                f"Partial: +{round(trade['reward_points']/2,1)} pts"
            )

# ─────────────────────────────────────────
# Pre-filter (Python — free & fast)
# ─────────────────────────────────────────
def zones_overlap(t1, b1, t2, b2) -> bool:
    return b1 <= t2 and b2 <= t1

def pre_filter(data: dict) -> dict:
    trend   = data.get("trend", "neutral")
    high    = float(data.get("high",  0))
    low     = float(data.get("low",   0))
    tf      = str(data.get("timeframe", ""))

    bull_fvg  = [f for f in data.get("bullish_fvg", []) if f.get("active") is True]
    bear_fvg  = [f for f in data.get("bearish_fvg", []) if f.get("active") is True]
    bull_ob   = [o for o in data.get("bullish_ob",  []) if o.get("breaker") is False]
    bear_ob   = [o for o in data.get("bearish_ob",  []) if o.get("breaker") is False]
    buy_liq   = [l for l in data.get("buyside_liquidity",  []) if l.get("broken") is False]
    sell_liq  = [l for l in data.get("sellside_liquidity", []) if l.get("broken") is False]
    supports    = data.get("supports",    [])
    resistances = data.get("resistances", [])
    res_broken  = data.get("resistance_broken", False)
    sup_broken  = data.get("support_broken",    False)

    # BUY SETUP 1
    if trend == "bullish":
        for sup in supports:
            st = float(sup["top"]);  sb = float(sup["bottom"])
            if not (low <= st and low >= sb):
                continue
            for fvg in bull_fvg:
                if zones_overlap(st, sb, float(fvg["top"]), float(fvg["bottom"])):
                    return {"ok": True, "setup": "BUY_SETUP_1",
                            "support": sup, "zone": fvg, "zone_type": "FVG"}
            for ob in bull_ob:
                if zones_overlap(st, sb, float(ob["top"]), float(ob["bottom"])):
                    return {"ok": True, "setup": "BUY_SETUP_1",
                            "support": sup, "zone": ob, "zone_type": "OB"}

    # SELL SETUP 1
    if trend == "bearish":
        for res in resistances:
            rt = float(res["top"]);  rb = float(res["bottom"])
            if not (high >= rb and high <= rt):
                continue
            for fvg in bear_fvg:
                if zones_overlap(rt, rb, float(fvg["top"]), float(fvg["bottom"])):
                    return {"ok": True, "setup": "SELL_SETUP_1",
                            "resistance": res, "zone": fvg, "zone_type": "FVG"}
            for ob in bear_ob:
                if zones_overlap(rt, rb, float(ob["top"]), float(ob["bottom"])):
                    return {"ok": True, "setup": "SELL_SETUP_1",
                            "resistance": res, "zone": ob, "zone_type": "OB"}

    # BUY SETUP 2
    if trend == "bullish" and buy_liq and res_broken:
        return {"ok": True, "setup": "BUY_SETUP_2", "zone": buy_liq[0]}

    # SELL SETUP 2
    if trend == "bearish" and sell_liq and sup_broken:
        return {"ok": True, "setup": "SELL_SETUP_2", "zone": sell_liq[0]}

    # PENDING 15M
    if tf == "15" and trend == "bullish" and bull_ob and supports:
        return {"ok": True, "setup": "PENDING_BUY_15M",
                "zone": bull_ob[0], "support": supports[0]}
    if tf == "15" and trend == "bearish" and bear_ob and resistances:
        return {"ok": True, "setup": "PENDING_SELL_15M",
                "zone": bear_ob[0], "resistance": resistances[0]}

    return {"ok": False, "reason": "No setup conditions met"}

# ─────────────────────────────────────────
# OpenAI
# ─────────────────────────────────────────
BAD_WORDS = [
    "no_trade", "no trade", "condition fails", "condition failed",
    "fails", "failed", "not touched", "has not touched",
    "does not touch", "not enter", "no signal", "invalid",
    "incomplete", "entry missed", "not valid", "not satisfied",
    "does not satisfy", "condition is missing", "no overlap",
    "not overlap", "does not overlap"
]

def reason_conflicts(decision: dict) -> bool:
    return any(w in decision.get("reason", "").lower() for w in BAD_WORDS)

def ask_openai(data: dict, pre: dict):
    setup_hint = pre.get("setup", "UNKNOWN")
    used_zones = db_get_used_zones()

    prompt = f"""
You are a strict professional trading decision engine.

CRITICAL RULE:
- If ANY required condition is not fully satisfied → signal = "NO_TRADE".
- Never return BUY/SELL when reason mentions a failed condition.
- Use ONLY the provided JSON data. Never invent zones.
- Do NOT reuse any zone_key listed in used_zones.
- zone_key = the "id" field of the FVG or OB that triggered the signal.
- Explain each condition step by step.

Pre-filter detected possible setup: {setup_hint}

══════════════════════════════
BUY SETUP 1 — Support + Bullish Zone
══════════════════════════════
ALL required:
1. trend = "bullish"
2. Active bullish_fvg (active=true) OR bullish_ob (breaker=false) exists
3. That zone overlaps a support from "supports" list:
   zone.bottom <= support.top AND support.bottom <= zone.top
4. candle low touched support: low <= support.top AND low >= support.bottom
5. entry = support.bottom
6. SL = support.bottom - buffer (3-5 pts)
7. TP1 = entry + risk_points
8. TP2 = entry + (2 × risk_points)
9. risk_points >= 20, reward_points >= 80

══════════════════════════════
BUY SETUP 2 — Liquidity Breakout
══════════════════════════════
ALL required:
1. trend = "bullish"
2. buyside_liquidity has broken=false entry
3. resistance_broken = true
4. entry = close
5. SL = broken resistance bottom
6. TP1 = entry + risk, TP2 = entry + 2×risk
7. risk >= 20, reward >= 60

══════════════════════════════
SELL SETUP 1 — Resistance + Bearish Zone
══════════════════════════════
ALL required:
1. trend = "bearish"
2. Active bearish_fvg (active=true) OR bearish_ob (breaker=false) exists
3. That zone overlaps a resistance from "resistances" list:
   zone.bottom <= resistance.top AND resistance.bottom <= zone.top
4. candle high touched resistance: high >= resistance.bottom AND high <= resistance.top
5. entry = resistance.top
6. SL = resistance.top + buffer (3-5 pts)
7. TP1 = entry - risk_points
8. TP2 = entry - (2 × risk_points)
9. risk_points >= 20, reward_points >= 80

══════════════════════════════
SELL SETUP 2 — Liquidity Breakdown
══════════════════════════════
ALL required:
1. trend = "bearish"
2. sellside_liquidity has broken=false entry
3. support_broken = true
4. entry = close
5. SL = broken support top
6. TP1 = entry - risk, TP2 = entry - 2×risk
7. risk >= 20, reward >= 60

══════════════════════════════
PENDING BUY 15M
══════════════════════════════
1. timeframe = "15"
2. trend = "bullish"
3. bullish_ob breaker=false
4. entry = nearest support bottom
5. SL = support bottom - buffer
6. TP2 distance >= 120

══════════════════════════════
PENDING SELL 15M
══════════════════════════════
1. timeframe = "15"
2. trend = "bearish"
3. bearish_ob breaker=false
4. entry = nearest resistance top
5. SL = resistance top + buffer
6. TP2 distance >= 120

Used zones (do NOT reuse):
{json.dumps(used_zones)}

Market data:
{json.dumps(data)}
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "trade_decision",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "signal":        {"type": "string", "enum": ["BUY", "SELL", "NO_TRADE"]},
                        "setup":         {"type": "string"},
                        "entry":         {"type": "number"},
                        "sl":            {"type": "number"},
                        "tp1":           {"type": "number"},
                        "tp2":           {"type": "number"},
                        "risk_points":   {"type": "number"},
                        "reward_points": {"type": "number"},
                        "zone_key":      {"type": "string"},
                        "reason":        {"type": "string"}
                    },
                    "required": ["signal","setup","entry","sl","tp1","tp2",
                                 "risk_points","reward_points","zone_key","reason"],
                    "additionalProperties": False
                }
            }
        }
    )
    return json.loads(response.output_text)

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.get("/")
def home():
    return {"status": "Trading AI backend running with SQLite"}

@app.get("/dashboard")
def dashboard():
    trades     = db_get_all_trades()
    stats      = db_get_stats()
    open_trade = db_get_open_trade()
    market     = db_get_market()
    used_zones = db_get_used_zones()
    return {
        "status":              "ok",
        "open_trade":          open_trade,
        "latest_market":       market,
        "trades":              trades,
        "stats":               stats,
        "used_zones":          used_zones,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    }

@app.get("/stats")
def stats():
    return db_get_stats()

@app.get("/trades")
def all_trades():
    return {"trades": db_get_all_trades()}

@app.delete("/reset")
def reset_all():
    """Reset all data — use for testing only"""
    with get_db() as conn:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM used_zones")
        conn.execute("DELETE FROM market_state")
    return {"status": "reset complete"}

@app.get("/test-telegram")
def test_telegram():
    send_telegram("✅ <b>Telegram test</b>\nTrading AI backend is running!")
    return {"status": "sent"}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("TradingView:", json.dumps(data, indent=2))

    db_update_market(data)

    high  = float(data.get("high",  0))
    low   = float(data.get("low",   0))
    close = float(data.get("close", 0))

    # 1. Check existing open trade
    check_trade_by_candle(high, low, close)

    open_trade = db_get_open_trade()
    if open_trade:
        return {"status": "open_trade_exists", "trade": open_trade}

    # 2. Pre-filter
    pre = pre_filter(data)
    print("Pre-filter:", pre)

    if not pre["ok"]:
        return {"status": "pre_filter_rejected", "reason": pre["reason"]}

    # 3. OpenAI
    decision = ask_openai(data, pre)
    print("AI:", decision)

    # 4. Safety check
    if decision["signal"] in ["BUY", "SELL"] and reason_conflicts(decision):
        decision["signal"] = "NO_TRADE"
        return {"status": "blocked_reason_conflict", "decision": decision}

    if decision["signal"] == "NO_TRADE":
        return {"status": "no_trade", "decision": decision}

    if db_zone_used(decision["zone_key"]):
        return {"status": "zone_already_used", "zone_key": decision["zone_key"]}

    # 5. Save trade to DB
    trade_data = {
        "symbol":        data.get("symbol"),
        "side":          decision["signal"],
        "setup":         decision["setup"],
        "entry":         decision["entry"],
        "sl":            decision["sl"],
        "tp1":           decision["tp1"],
        "tp2":           decision["tp2"],
        "risk_points":   decision["risk_points"],
        "reward_points": decision["reward_points"],
        "zone_key":      decision["zone_key"],
        "reason":        decision["reason"]
    }
    new_id = db_insert_trade(trade_data)
    db_add_zone(decision["zone_key"])

    # 6. Telegram notification
    emoji = "🟢" if decision["signal"] == "BUY" else "🔴"
    msg = (
        f"{emoji} <b>NEW TRADE #{new_id}</b>\n\n"
        f"Symbol: <b>{data.get('symbol')}</b>\n"
        f"Side: <b>{decision['signal']}</b>\n"
        f"Setup: {decision['setup']}\n\n"
        f"📍 Entry: <b>{decision['entry']}</b>\n"
        f"🛑 SL: <b>{decision['sl']}</b>  ({decision['risk_points']} pts)\n"
        f"🎯 TP1: <b>{decision['tp1']}</b>\n"
        f"🎯 TP2: <b>{decision['tp2']}</b>  ({decision['reward_points']} pts)\n\n"
        f"Zone: <code>{decision['zone_key']}</code>\n\n"
        f"📝 {decision['reason']}"
    )
    send_telegram(msg)

    return {
        "status":   "trade_opened",
        "trade_id": new_id,
        "decision": decision,
        "stats":    db_get_stats()
    }