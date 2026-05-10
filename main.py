import os
import json
import requests

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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

open_trade = None
latest_market = None
trade_history = []
trade_id = 1
used_zones = []


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        })
        print("Telegram status:", response.status_code)
        print("Telegram response:", response.text)

    except Exception as e:
        print("Telegram Error:", e)


def update_trade_history(trade_id_value, result, profit_points):
    for trade in trade_history:
        if trade["id"] == trade_id_value:
            trade["result"] = result
            trade["profit_points"] = profit_points
            return


def check_trade_by_candle(high: float, low: float, close: float):
    global open_trade

    if open_trade is None:
        return

    side = open_trade["side"]
    tp1 = float(open_trade["tp1"])
    tp2 = float(open_trade["tp2"])
    sl = float(open_trade["sl"])

    print("CHECKING OPEN TRADE:", open_trade)
    print("CANDLE:", high, low, close)

    if side == "BUY":
        if low <= sl:
            update_trade_history(open_trade["id"], "SL", -open_trade["risk_points"])
            send_telegram(
                f"❌ SL HIT\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: BUY\n"
                f"Entry: {open_trade['entry']}\n"
                f"SL: {sl}"
            )
            open_trade = None
            return

        if high >= tp2:
            update_trade_history(open_trade["id"], "TP2", open_trade["reward_points"])
            send_telegram(
                f"✅ TP2 HIT\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: BUY\n"
                f"Entry: {open_trade['entry']}\n"
                f"TP2: {tp2}"
            )
            open_trade = None
            return

        if not open_trade["tp1_hit"] and high >= tp1:
            open_trade["tp1_hit"] = True
            update_trade_history(open_trade["id"], "TP1", open_trade["reward_points"] / 2)
            send_telegram(
                f"✅ TP1 HIT\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: BUY\n"
                f"Entry: {open_trade['entry']}\n"
                f"TP1: {tp1}"
            )

    if side == "SELL":
        if high >= sl:
            update_trade_history(open_trade["id"], "SL", -open_trade["risk_points"])
            send_telegram(
                f"❌ SL HIT\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: SELL\n"
                f"Entry: {open_trade['entry']}\n"
                f"SL: {sl}"
            )
            open_trade = None
            return

        if low <= tp2:
            update_trade_history(open_trade["id"], "TP2", open_trade["reward_points"])
            send_telegram(
                f"✅ TP2 HIT\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: SELL\n"
                f"Entry: {open_trade['entry']}\n"
                f"TP2: {tp2}"
            )
            open_trade = None
            return

        if not open_trade["tp1_hit"] and low <= tp1:
            open_trade["tp1_hit"] = True
            update_trade_history(open_trade["id"], "TP1", open_trade["reward_points"] / 2)
            send_telegram(
                f"✅ TP1 HIT\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: SELL\n"
                f"Entry: {open_trade['entry']}\n"
                f"TP1: {tp1}"
            )


def ask_openai(data: dict):
    prompt = f"""
You are a strict professional trading decision engine.

RULES:

CRITICAL FINAL DECISION RULE:
If your analysis says any condition failed, zone not touched, no overlap, invalid RR, entry missed, resistance too close, support too close, or setup is incomplete, then signal MUST be "NO_TRADE".

Never return BUY or SELL while the reason says the final conclusion is NO_TRADE.

BUY or SELL is allowed only if every required condition is fully satisfied.

- Use ONLY data from the provided JSON. Do NOT invent or assume any zone.
- Return NO_TRADE immediately if ANY required condition is missing.
- Do NOT reuse a zone that exists in used_zones.
- Price "touching" a zone means: candle low enters the zone top/bottom for BUY, or candle high enters the zone for SELL.
- Bullish FVG is valid only if active=true.
- Bearish FVG is valid only if active=true.
- Bullish OB is valid only if breaker=false.
- Bearish OB is valid only if breaker=false.
- The zone_key must be the ID of the FVG or OB that triggered the signal.
- Explain step by step why each condition is met or not.

=== BUY SETUP 1 — Trend + Bullish Zone ===
ALL of the following must be true:
1. trend = "bullish"
2. At least one active bullish_fvg OR one bullish_ob with breaker=false exists
3. Current candle low has touched or entered that bullish zone: low <= zone top AND low >= zone bottom
4. entry = zone bottom or zone midpoint if zone is wide
5. SL = zone bottom minus buffer
6. TP1 = entry + risk_points, TP2 = entry + (2 x risk_points)
7. risk_points >= 40, reward_points >= 80

=== BUY SETUP 2 — Liquidity Sweep + Breakout ===
ALL of the following must be true:
1. trend = "bullish"
2. buyside_liquidity list is not empty and at least one entry has broken=false
3. resistance_broken = true
4. entry = close of current candle
5. SL = bottom of broken resistance zone
6. TP1 = entry + risk_points, TP2 = entry + (2 x risk_points)
7. risk_points >= 30, reward_points >= 60

=== SELL SETUP 1 — Trend + Bearish Zone ===
ALL of the following must be true:
1. trend = "bearish"
2. At least one active bearish_fvg OR one bearish_ob with breaker=false exists
3. Current candle high has touched or entered that bearish zone: high >= zone bottom AND high <= zone top
4. entry = zone top or zone midpoint if zone is wide
5. SL = zone top plus buffer
6. TP1 = entry - risk_points, TP2 = entry - (2 x risk_points)
7. risk_points >= 40, reward_points >= 80

=== SELL SETUP 2 — Liquidity Sweep + Breakdown ===
ALL of the following must be true:
1. trend = "bearish"
2. sellside_liquidity list is not empty and at least one entry has broken=false
3. support_broken = true
4. entry = close of current candle
5. SL = top of broken support zone
6. TP1 = entry - risk_points, TP2 = entry - (2 x risk_points)
7. risk_points >= 30, reward_points >= 60

=== PENDING BUY — 15M Only ===
ALL of the following must be true:
1. timeframe = "15"
2. trend = "bullish"
3. bullish_ob exists with breaker=false
4. entry = bullish_ob bottom
5. SL = bullish_ob bottom minus buffer
6. TP2 distance >= 120 points

=== PENDING SELL — 15M Only ===
ALL of the following must be true:
1. timeframe = "15"
2. trend = "bearish"
3. bearish_ob exists with breaker=false
4. entry = bearish_ob top
5. SL = bearish_ob top plus buffer
6. TP2 distance >= 120 points

Used zones:
{json.dumps(used_zones, ensure_ascii=False)}

Market data:
{json.dumps(data, ensure_ascii=False)}
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
                        "signal": {
                            "type": "string",
                            "enum": ["BUY", "SELL", "NO_TRADE"]
                        },
                        "setup": {"type": "string"},
                        "entry": {"type": "number"},
                        "sl": {"type": "number"},
                        "tp1": {"type": "number"},
                        "tp2": {"type": "number"},
                        "risk_points": {"type": "number"},
                        "reward_points": {"type": "number"},
                        "zone_key": {"type": "string"},
                        "reason": {"type": "string"}
                    },
                    "required": [
                        "signal", "setup", "entry", "sl",
                        "tp1", "tp2", "risk_points", "reward_points",
                        "zone_key", "reason"
                    ],
                    "additionalProperties": False
                }
            }
        }
    )

    return json.loads(response.output_text)


@app.get("/")
def home():
    return {"status": "backend is running"}


@app.get("/dashboard")
def dashboard():
    return {
        "status": "ok",
        "open_trade": open_trade,
        "latest_market": latest_market,
        "trades": trade_history,
        "used_zones": used_zones,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    }


@app.get("/test-telegram")
def test_telegram():
    send_telegram("✅ Telegram test message from Trading AI backend")
    return {
        "status": "telegram_test_sent",
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    }


@app.post("/webhook")
async def webhook(request: Request):
    global open_trade
    global latest_market
    global trade_history
    global trade_id
    global used_zones

    data = await request.json()
    latest_market = data

    print("TradingView data:", data)

    high = float(data.get("high"))
    low = float(data.get("low"))
    close = float(data.get("close"))

    check_trade_by_candle(high, low, close)

    if open_trade is not None:
        return {
            "status": "open_trade_exists",
            "trade": open_trade
        }

    decision = ask_openai(data)

    print("AI decision:", decision)

    reason_lower = decision["reason"].lower()

    bad_words = [
        "no_trade",
        "no trade",
        "condition fails",
        "condition failed",
        "condition 3 fails",
        "fails",
        "failed",
        "not touched",
        "has not touched",
        "does not touch",
        "not enter",
        "no signal",
        "invalid",
        "incomplete",
        "entry missed",
        "not valid",
        "not satisfied",
        "does not satisfy",
        "fails for",
        "condition is missing"
    ]

    if decision["signal"] in ["BUY", "SELL"]:
        if any(word in reason_lower for word in bad_words):
            decision["signal"] = "NO_TRADE"
            return {
                "status": "blocked_reason_conflict",
                "decision": decision
            }

    if decision["signal"] == "NO_TRADE":
        return {
            "status": "no_trade",
            "decision": decision
        }

    if decision["zone_key"] in used_zones:
        return {
            "status": "zone_already_used",
            "zone_key": decision["zone_key"],
            "decision": decision
        }

    if decision["signal"] in ["BUY", "SELL"]:
        open_trade = {
            "id": trade_id,
            "symbol": data.get("symbol"),
            "side": decision["signal"],
            "setup": decision["setup"],
            "entry": decision["entry"],
            "sl": decision["sl"],
            "tp1": decision["tp1"],
            "tp2": decision["tp2"],
            "tp1_hit": False,
            "risk_points": decision["risk_points"],
            "reward_points": decision["reward_points"],
            "zone_key": decision["zone_key"],
            "reason": decision["reason"]
        }

        used_zones.append(decision["zone_key"])

        trade_history.insert(0, {
            "id": trade_id,
            "symbol": data.get("symbol"),
            "side": decision["signal"],
            "setup": decision["setup"],
            "entry": decision["entry"],
            "sl": decision["sl"],
            "tp1": decision["tp1"],
            "tp2": decision["tp2"],
            "risk_points": decision["risk_points"],
            "reward_points": decision["reward_points"],
            "zone_key": decision["zone_key"],
            "result": "OPEN",
            "profit_points": 0,
            "created_at": str(data.get("time")),
            "reason": decision["reason"]
        })

        trade_id += 1

        msg = (
            f"🚨 NEW TRADE\n\n"
            f"Symbol: {data.get('symbol')}\n"
            f"Side: {decision['signal']}\n"
            f"Setup: {decision['setup']}\n\n"
            f"Entry: {decision['entry']}\n"
            f"SL: {decision['sl']}\n"
            f"TP1: {decision['tp1']}\n"
            f"TP2: {decision['tp2']}\n\n"
            f"Risk Points: {decision['risk_points']}\n"
            f"Reward Points: {decision['reward_points']}\n"
            f"Zone: {decision['zone_key']}\n\n"
            f"Reason:\n{decision['reason']}"
        )

        send_telegram(msg)

    return {
        "status": "ok",
        "decision": decision,
        "open_trade": open_trade,
        "trades": trade_history
    }