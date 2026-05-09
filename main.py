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
    global trade_history

    for trade in trade_history:
        if trade["id"] == trade_id_value:
            trade["result"] = result
            trade["profit_points"] = profit_points
            return


def check_trade(price: float):
    global open_trade

    if open_trade is None:
        return

    side = open_trade["side"]
    tp1 = open_trade["tp1"]
    tp2 = open_trade["tp2"]
    sl = open_trade["sl"]

    if side == "BUY":
        if not open_trade["tp1_hit"] and price >= tp1:
            open_trade["tp1_hit"] = True
            update_trade_history(open_trade["id"], "TP1", 80)

            send_telegram(
                f"✅ TP1 HIT\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: BUY\n"
                f"Entry: {open_trade['entry']}\n"
                f"TP1: {tp1}"
            )

        if price >= tp2:
            update_trade_history(open_trade["id"], "TP2", 160)

            send_telegram(
                f"✅ TP2 HIT - Trade Closed\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: BUY\n"
                f"Entry: {open_trade['entry']}\n"
                f"TP2: {tp2}"
            )

            open_trade = None
            return

        if price <= sl:
            update_trade_history(open_trade["id"], "SL", -80)

            send_telegram(
                f"❌ SL HIT - Trade Closed\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: BUY\n"
                f"Entry: {open_trade['entry']}\n"
                f"SL: {sl}"
            )

            open_trade = None
            return

    if side == "SELL":
        if not open_trade["tp1_hit"] and price <= tp1:
            open_trade["tp1_hit"] = True
            update_trade_history(open_trade["id"], "TP1", 80)

            send_telegram(
                f"✅ TP1 HIT\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: SELL\n"
                f"Entry: {open_trade['entry']}\n"
                f"TP1: {tp1}"
            )

        if price <= tp2:
            update_trade_history(open_trade["id"], "TP2", 160)

            send_telegram(
                f"✅ TP2 HIT - Trade Closed\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: SELL\n"
                f"Entry: {open_trade['entry']}\n"
                f"TP2: {tp2}"
            )

            open_trade = None
            return

        if price >= sl:
            update_trade_history(open_trade["id"], "SL", -80)

            send_telegram(
                f"❌ SL HIT - Trade Closed\n\n"
                f"Symbol: {open_trade['symbol']}\n"
                f"Side: SELL\n"
                f"Entry: {open_trade['entry']}\n"
                f"SL: {sl}"
            )

            open_trade = None
            return


def ask_openai(data: dict):
    prompt = f"""
You are a strict trading decision engine.

Use ONLY the provided JSON market data.
Do NOT invent zones.
Do NOT assume missing data.
Return NO_TRADE if any required condition is missing.

Very important:
- Only one trade is allowed.
- Trend is the most important filter.
- FVG or OB is valid ONLY if it overlaps the SAME support/resistance zone.
- Do not trade if price is far from the relevant support/resistance zone.
- Near means price is within 80 points maximum from the zone.
- Ignore broken FVG/OB unless still active.
- If liquidity is already broken, treat it as sweep evidence only if direction matches the setup.
- If data is unclear, return NO_TRADE.

BUY SETUP 1:
1. bullish trend
2. price near support
3. support overlaps bullish FVG or bullish OB
4. same support area

BUY SETUP 2:
1. bullish trend
2. sellside liquidity sweep
3. break nearest resistance

SELL SETUP 1:
1. bearish trend
2. price near resistance
3. resistance overlaps bearish FVG or bearish OB
4. same resistance area

SELL SETUP 2:
1. bearish trend
2. buyside liquidity sweep
3. break nearest support

Risk:
BUY:
SL = entry - 80
TP1 = entry + 80
TP2 = entry + 160

SELL:
SL = entry + 80
TP1 = entry - 80
TP2 = entry - 160

Return JSON only.

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
                        "reason": {"type": "string"}
                    },
                    "required": [
                        "signal",
                        "setup",
                        "entry",
                        "sl",
                        "tp1",
                        "tp2",
                        "reason"
                    ],
                    "additionalProperties": False
                }
            }
        }
    )

    return json.loads(response.output_text)


@app.get("/")
def home():
    return {
        "status": "backend is running"
    }


@app.get("/dashboard")
def dashboard():
    return {
        "status": "ok",
        "open_trade": open_trade,
        "latest_market": latest_market,
        "trades": trade_history,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    }

def check_trade_by_candle(high: float, low: float, close: float):
    global open_trade

    if open_trade is None:
        return

    print("CHECKING OPEN TRADE:", open_trade)
    print("CANDLE high:", high, "low:", low, "close:", close)

    side = open_trade["side"]
    tp1 = open_trade["tp1"]
    tp2 = open_trade["tp2"]
    sl = open_trade["sl"]

    if side == "BUY":
        if low <= sl:
            update_trade_history(open_trade["id"], "SL", -80)
            send_telegram(f"❌ SL HIT\n\nSymbol: {open_trade['symbol']}\nSide: BUY\nEntry: {open_trade['entry']}\nSL: {sl}")
            open_trade = None
            return

        if high >= tp2:
            update_trade_history(open_trade["id"], "TP2", 160)
            send_telegram(f"✅ TP2 HIT\n\nSymbol: {open_trade['symbol']}\nSide: BUY\nEntry: {open_trade['entry']}\nTP2: {tp2}")
            open_trade = None
            return

        if not open_trade["tp1_hit"] and high >= tp1:
            open_trade["tp1_hit"] = True
            update_trade_history(open_trade["id"], "TP1", 80)
            send_telegram(f"✅ TP1 HIT\n\nSymbol: {open_trade['symbol']}\nSide: BUY\nEntry: {open_trade['entry']}\nTP1: {tp1}")

    if side == "SELL":
        if high >= sl:
            update_trade_history(open_trade["id"], "SL", -80)
            send_telegram(f"❌ SL HIT\n\nSymbol: {open_trade['symbol']}\nSide: SELL\nEntry: {open_trade['entry']}\nSL: {sl}")
            open_trade = None
            return

        if low <= tp2:
            update_trade_history(open_trade["id"], "TP2", 160)
            send_telegram(f"✅ TP2 HIT\n\nSymbol: {open_trade['symbol']}\nSide: SELL\nEntry: {open_trade['entry']}\nTP2: {tp2}")
            open_trade = None
            return

        if not open_trade["tp1_hit"] and low <= tp1:
            open_trade["tp1_hit"] = True
            update_trade_history(open_trade["id"], "TP1", 80)
            send_telegram(f"✅ TP1 HIT\n\nSymbol: {open_trade['symbol']}\nSide: SELL\nEntry: {open_trade['entry']}\nTP1: {tp1}")
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
            "reason": decision["reason"]
        }

        trade_history.insert(0, {
            "id": trade_id,
            "symbol": data.get("symbol"),
            "side": decision["signal"],
            "setup": decision["setup"],
            "entry": decision["entry"],
            "sl": decision["sl"],
            "tp1": decision["tp1"],
            "tp2": decision["tp2"],
            "result": "OPEN",
            "profit_points": 0,
            "created_at": str(data.get("time")),
            "reason": decision["reason"]
        })

        trade_id += 1

        msg = (
            f"🚨 NEW TRADE\n\n"
            f"Symbol: {data.get('symbol')}\n"
            f"Side: {decision['signal']}\n\n"
            f"Setup: {decision['setup']}\n\n"
            f"Entry: {decision['entry']}\n"
            f"SL: {decision['sl']}\n"
            f"TP1: {decision['tp1']}\n"
            f"TP2: {decision['tp2']}\n\n"
            f"Reason:\n"
            f"{decision['reason']}"
        )

        send_telegram(msg)

    return {
        "status": "ok",
        "decision": decision,
        "open_trade": open_trade,
        "trades": trade_history
    }