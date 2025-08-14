# src/main.py
import os, time, math, logging, threading
from dotenv import load_dotenv
load_dotenv()

# libs
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd, numpy as np
import talib as ta
from telegram import Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------- CONFIG ----------
MODE = os.getenv("MODE","paper")    # 'paper' or 'live'
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TG_TOKEN = os.getenv("7786434709:AAE29u264oOFf9qH0oBSmjfTKQLSlUu_TUo")
TG_ADMIN_ID = int(os.getenv("TG_ADMIN_ID","0"))
SYMBOLS = os.getenv("SYMBOLS","BTCUSDT").split(",")
DEFAULT_LEV = int(os.getenv("DEFAULT_LEVERAGE","10"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT","1.0"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("futures-bot")

# ---------- BINANCE CLIENT ----------
client = None
if MODE == "live":
    client = Client(API_KEY, API_SECRET)
else:
    # paper mode: we won't create real client; still you can instantiate testnet client if desired
    client = Client(API_KEY, API_SECRET, testnet=True)

# helper: set leverage
def set_leverage(symbol, lev):
    try:
        client.futures_change_leverage(symbol=symbol, leverage=lev)
        log.info(f"Set leverage {symbol} -> {lev}x")
    except Exception as e:
        log.warning("Set leverage failed: %s", e)

# get balance USDT
def get_usdt_balance():
    try:
        bal = client.futures_account_balance()
        for e in bal:
            if e['asset']=="USDT":
                return float(e['balance'])
    except Exception as e:
        log.warning("get_usdt_balance err: %s", e)
    return 0.0

# rounding helper per symbol (very basic)
def round_qty(symbol, qty):
    # NOTE: production: read exchangeInfo for stepSize/precision per symbol
    return float(round(qty, 3))

# fetch klines
def fetch_klines(symbol, interval='1h', limit=200):
    df = pd.DataFrame(client.futures_klines(symbol=symbol, interval=interval, limit=limit))
    if df.empty: return None
    df = df.iloc[:,0:6]
    df.columns = ['open_time','open','high','low','close','volume']
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    return df

# simple ATR-based stop distance
def compute_atr_distance(symbol):
    df = fetch_klines(symbol, interval='1h', limit=50)
    if df is None: return None
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    atr = ta.ATR(high, low, close, timeperiod=14)
    last_atr = float(atr[-1])
    return last_atr

# position size from risk percent: risk_amount / (stop_distance * price) * leverage (approx)
def calc_qty(symbol, risk_percent, stop_distance_usdt, leverage):
    balance = get_usdt_balance()
    risk_amount = balance * (risk_percent/100.0)
    price = float(client.futures_mark_price(symbol=symbol)['markPrice'])
    # qty in base asset:
    # approximate qty = (risk_amount * leverage) / (stop_distance_usdt * price)
    if stop_distance_usdt <= 0: return 0.0
    qty = (risk_amount * leverage) / (stop_distance_usdt * price)
    return round_qty(symbol, qty)

# open long (market) + place TP & SL market orders (reduceOnly)
def open_long(symbol, qty, tp_price, sl_price):
    if MODE != "live":
        log.info(f"[PAPER] open_long {symbol} qty={qty} tp={tp_price} sl={sl_price}")
        return {"paper": True, "qty": qty}
    try:
        order = client.futures_create_order(symbol=symbol, side='BUY', type='MARKET', quantity=qty)
        # place TP
        tp = client.futures_create_order(
            symbol=symbol,
            side='SELL',
            type='TAKE_PROFIT_MARKET',
            stopPrice=str(tp_price),
            closePosition=True
        )
        sl = client.futures_create_order(
            symbol=symbol,
            side='SELL',
            type='STOP_MARKET',
            stopPrice=str(sl_price),
            closePosition=True
        )
        return {"entry": order, "tp": tp, "sl": sl}
    except BinanceAPIException as e:
        log.error("BinanceAPIException open_long: %s", e)
    except Exception as e:
        log.exception("open_long error")
    return None

# close all positions for symbol
def close_all_positions(symbol):
    try:
        pos = client.futures_position_information(symbol=symbol)
        for p in pos:
            amt = float(p['positionAmt'])
            if amt != 0:
                side = 'SELL' if amt>0 else 'BUY'
                q = abs(amt)
                if MODE != "live":
                    log.info(f"[PAPER] close pos {symbol} side={side} qty={q}")
                else:
                    client.futures_create_order(symbol=symbol, side=side, type='MARKET', quantity=q)
        return True
    except Exception as e:
        log.exception("close_all_positions error")
    return False

# ---------- SAMPLE STRATEGY: EMA crossover + RSI confirmation ----------
SIGNAL_ON = True
def run_strategy_cycle():
    for sym in SYMBOLS:
        try:
            df = fetch_klines(sym, interval='15m', limit=200)
            if df is None: continue
            close = df['close'].values
            ema_fast = ta.EMA(close, timeperiod=18)
            ema_slow = ta.EMA(close, timeperiod=50)
            rsi = ta.RSI(close, timeperiod=14)
            if ema_fast[-2] < ema_slow[-2] and ema_fast[-1] > ema_slow[-1] and rsi[-1] < 55:
                # buy signal
                atr = compute_atr_distance(sym)
                if atr is None: continue
                stop_distance = atr * 1.2   # multiplier
                price = float(client.futures_mark_price(symbol=sym)['markPrice'])
                tp = price + stop_distance * 3
                sl = price - stop_distance
                qty = calc_qty(sym, RISK_PERCENT, stop_distance, DEFAULT_LEV)
                if qty <= 0: continue
                log.info(f"Signal BUY {sym} qty={qty} price={price} tp={tp} sl={sl}")
                res = open_long(sym, qty, tp_price=tp, sl_price=sl)
                log.info("open res: %s", res)
        except Exception as e:
            log.exception("strategy cycle error %s", e)

# ---------- TELEGRAM BOT (simple handlers) ----------
bot_app = None
async def cmd_status(update, context):
    uid = update.effective_user.id
    if uid != TG_ADMIN_ID:
        await update.message.reply_text("Not authorized.")
        return
    bal = get_usdt_balance()
    await update.message.reply_text(f"MODE={MODE}\nUSDT balance: {bal:.2f}\nSymbols: {','.join(SYMBOLS)}")

async def cmd_signal(update, context):
    global SIGNAL_ON
    uid = update.effective_user.id
    if uid != TG_ADMIN_ID:
        await update.message.reply_text("Not authorized.")
        return
    arg = (context.args[0].lower() if context.args else "on")
    if arg == "on":
        SIGNAL_ON = True
        await update.message.reply_text("Auto signals: ON")
    else:
        SIGNAL_ON = False
        await update.message.reply_text("Auto signals: OFF")

async def cmd_buy(update, context):
    uid = update.effective_user.id
    if uid != TG_ADMIN_ID:
        await update.message.reply_text("Not authorized.")
        return
    try:
        sym = context.args[0].upper()
        side = context.args[1].lower() if len(context.args)>1 else "buy"
        risk = float(context.args[2]) if len(context.args)>2 else RISK_PERCENT
        # simple manual buy: use ATR to compute stop & size
        atr = compute_atr_distance(sym)
        if atr is None:
            await update.message.reply_text("Failed ATR")
            return
        price = float(client.futures_mark_price(symbol=sym)['markPrice'])
        stop_distance = atr * 1.2
        tp = price + stop_distance * 3
        sl = price - stop_distance
        qty = calc_qty(sym, risk, stop_distance, DEFAULT_LEV)
        res = open_long(sym, qty, tp, sl)
        await update.message.reply_text(f"Manual buy result: {res}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_closeall(update, context):
    uid = update.effective_user.id
    if uid != TG_ADMIN_ID:
        await update.message.reply_text("Not authorized.")
        return
    sym = context.args[0].upper() if context.args else SYMBOLS[0]
    ok = close_all_positions(sym)
    await update.message.reply_text(f"Close all {sym}: {ok}")

def start_telegram_loop():
    global bot_app
    bot_app = ApplicationBuilder().token(TG_TOKEN).build()
    bot_app.add_handler(CommandHandler("status", cmd_status))
    bot_app.add_handler(CommandHandler("signal", cmd_signal))
    bot_app.add_handler(CommandHandler("buy", cmd_buy))
    bot_app.add_handler(CommandHandler("closeall", cmd_closeall))
    t = threading.Thread(target=bot_app.run_polling, daemon=True)
    t.start()
    log.info("Telegram bot started")

# ---------- main loop ----------
if __name__ == "__main__":
    log.info("Starting bot MODE=%s", MODE)
    # set leverage for symbols
    for s in SYMBOLS:
        try:
            set_leverage(s, DEFAULT_LEV)
        except:
            pass
    start_telegram_loop()
    try:
        while True:
            if SIGNAL_ON:
                run_strategy_cycle()
            time.sleep(15)
    except KeyboardInterrupt:
        log.info("Shutting down")
