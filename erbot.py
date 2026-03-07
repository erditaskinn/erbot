"""
╔══════════════════════════════════════════════════════════════════════════════╗
║      MULTI-SYMBOL FUTURES SCANNER + TRADING BOT  —  v3.9                  ║
║                                                                              ║
║  MAJOR CHANGES IN v3.9 (full architectural overhaul):                       ║
║                                                                              ║
║  [1]  MULTI-POSITION: up to 3 concurrent positions across different symbols ║
║       active_positions dict replaces single in_position bool                ║
║  [2]  WATCHLIST EXPANDED: 8 → 16 symbols (SUI WIF PEPE INJ OP TIA JUP BONK)║
║  [3]  RSI REMOVED from scoring (StochRSI already covers it better)         ║
║  [4]  BTC TREND: hard gate removed → 8th scoring factor (0.04 weight)      ║
║  [5]  JITTER REMOVED: random noise replaced by correlation filter           ║
║  [6]  CORRELATION FILTER: no two same-category positions simultaneously     ║
║  [7]  ASYMMETRIC TP: separate ROI targets for AL vs SAT                    ║
║  [8]  ASYMMETRIC SL: wider ATR multiplier for SAT (shorts get squeezed)    ║
║  [9]  MAX HOLD TIME: positions auto-closed after N hours with no SL/TP     ║
║  [10] PARTIAL TP (SCALE-OUT): close 60% at TP1, ride 40% to TP2           ║
║  [11] DYNAMIC LEVERAGE: ADX-based leverage scaling per trade               ║
║  [12] GRADIENT EMA21 SCORING: replaces binary above/below                  ║
║  [13] FUNDING RATE BONUS: high positive funding rewards SAT positions      ║
║  [14] COOLDOWN: tick-based → time-based (real datetime)                    ║
║  [15] SESSION FILTER: dead code removed entirely                            ║
║  [16] /istatistik: symbol/direction/hour breakdown from trade log           ║
║  [17] /kapat SEMBOL: close specific position (multi-pos aware)             ║
║                                                                              ║
║  Carried from v3.8 (all intact):                                            ║
║  Exchange bracket orders, limit entry, slippage guard, position sync,      ║
║  per-symbol ban, polling shutdown fix, ROI-mode TP, /saglik, /durum        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import ccxt
import pandas as pd
import csv
import os
import sys
import logging
import telebot
import threading
import argparse
from telebot import types
from datetime import datetime, timedelta, timezone
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.volume import OnBalanceVolumeIndicator
from dotenv import load_dotenv
import time

# Suppress TeleBot "Break infinity polling" spam on shutdown
telebot_logger = logging.getLogger('TeleBot')
telebot_logger.setLevel(logging.CRITICAL)

load_dotenv()

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
CONFIG = {
    # ── Credentials ───────────────────────────────────────────────────────────
    'API_KEY':        os.environ.get('BINANCE_API_KEY',    'YOUR_API_KEY_HERE'),
    'SECRET_KEY':     os.environ.get('BINANCE_SECRET_KEY', 'YOUR_SECRET_KEY_HERE'),
    'TELEGRAM_TOKEN': os.environ.get('TELEGRAM_TOKEN',     'YOUR_TELEGRAM_TOKEN_HERE'),
    'CHAT_ID':        os.environ.get('TELEGRAM_CHAT_ID',   'YOUR_CHAT_ID_HERE'),

    # ── Watchlist (16 symbols) [v3.9 expanded] ────────────────────────────────
    'SYMBOL_LIST': [
        'HYPE/USDT', 'SOL/USDT',  'ETH/USDT',  'BNB/USDT',
        'AVAX/USDT', 'DOGE/USDT', 'LINK/USDT',  'ARB/USDT',
        'SUI/USDT',  'WIF/USDT',  'INJ/USDT',   'OP/USDT',
        'TIA/USDT',  'JUP/USDT',
        # PEPE/USDT and BONK/USDT removed — not available on Binance demo futures.
        # Add them back when switching to live trading if needed.
    ],
    # Correlation categories — no two same-category positions allowed
    # simultaneously (prevents doubling exposure on correlated moves)
    'SYMBOL_CATEGORIES': {
        'HYPE/USDT': 'others',
        'SOL/USDT':  'l1',    'AVAX/USDT': 'l1',
        'SUI/USDT':  'l1',    'INJ/USDT':  'l1',  'TIA/USDT': 'l1',
        'ETH/USDT':  'large', 'BNB/USDT':  'large',
        'ARB/USDT':  'l2',    'OP/USDT':   'l2',
        'LINK/USDT': 'defi',  'JUP/USDT':  'defi',
        'DOGE/USDT': 'meme',  'WIF/USDT':  'meme',
        # 'PEPE/USDT': 'meme',  'BONK/USDT': 'meme',  # demo only
    },
    # Categories where multiple concurrent positions ARE allowed
    # (less correlated with each other than same-bucket coins)
    'CORRELATION_FREE': {'large', 'l2', 'defi', 'others'},
    'BTC_SYMBOL': 'BTC/USDT',

    # ── Timeframes ────────────────────────────────────────────────────────────
    # ENTRY_TF : '5m' = 4-12 signals/day | '3m' = more noise | '15m' = swing
    # BIAS_TF  : should be 3× ENTRY_TF
    'ENTRY_TF': '5m',
    'BIAS_TF':  '15m',
    'BTC_TF':   '1h',

    # ── Leverage — Dynamic [v3.9] ─────────────────────────────────────────────
    # LEVERAGE        : base leverage for all trades.
    # DYNAMIC_LEVERAGE: True = add 1x leverage per 10 ADX points above ADX_THRESHOLD.
    #   e.g. ADX=22 → 5x | ADX=32 → 6x | ADX=42 → 7x
    # MAX_LEVERAGE    : hard ceiling regardless of ADX.
    'LEVERAGE':         5,
    'DYNAMIC_LEVERAGE': True,
    'MAX_LEVERAGE':     8,

    # ── Multi-Position [v3.9] ─────────────────────────────────────────────────
    # MAX_CONCURRENT_POSITIONS : max concurrent open positions.
    # MAX_TOTAL_MARGIN_PCT  : max total margin deployed as fraction of balance.
    #   0.06 = 6% of balance across ALL open positions combined.
    'MAX_CONCURRENT_POSITIONS': 3,
    'MAX_TOTAL_MARGIN_PCT':  0.24,   # 24% = $1200 max total margin across all positions

    # ── Risk Management ───────────────────────────────────────────────────────
    # RISK_PCT      : % of account risked PER TRADE.
    # DAILY_MAX_LOSS: daily loss limit — bot stops after this fraction lost.
    # MAX_POSITION_USD: hard notional cap per single position (0 = disabled).
    # MAX_POSITION_HOURS: auto-close position if open longer than N hours.
    'RISK_PCT':              0.08,   # 8% = ~$400 max loss per trade on $5000 balance
    'DAILY_MAX_LOSS':        0.10,   # 10% daily stop = ~$500 max daily loss
    'MAX_POSITION_USD':      2500,   # $2500 notional per position (~$500 margin at 5x)
    'MAX_POSITION_HOURS':  4,

    # ── TP Configuration — Asymmetric [v3.9] ─────────────────────────────────
    # TP_ROI_MODE     : True = ROI-on-margin target | False = raw price move.
    # TP_ROI_TARGET   : ROI target for AL positions. 0.10 = 10% on margin.
    # TP_ROI_TARGET_SHORT: ROI target for SAT positions (tighter — shorts move fast).
    # TP_RATE       : used only when TP_ROI_MODE = False.
    'TP_ROI_MODE':       True,
    'TP_ROI_TARGET':     0.10,
    'TP_ROI_TARGET_SHORT': 0.12,   # raised from 0.07 — needed to keep R:R near 0.9x with 1.6x SL
    'TP_RATE':         0.02,

    # ── Partial TP / Scale-Out [v3.9] ─────────────────────────────────────────
    # PARTIAL_TP_ACTIVE  : True = close PARTIAL_TP_PCT at TP1, ride rest to TP2.
    # PARTIAL_TP_PCT  : fraction closed at TP1. 0.60 = close 60%, ride 40%.
    # TP2_ROI_TARGET   : ROI target for the remaining 40% (applies to both sides).
    'PARTIAL_TP_ACTIVE':   True,
    'PARTIAL_TP_PCT':   0.60,
    'TP2_ROI_TARGET':    0.15,   # lowered from 0.20 — more reachable on 5m timeframe

    # ── SL Configuration — Asymmetric [v3.9] ─────────────────────────────────
    # Shorts get squeezed harder in crypto (structurally bullish market).
    # SAT positions get a wider SL to avoid stop-hunts.
    'ATR_SL_MULT_LONG':  1.2,   # tightened from 1.5 — improves R:R, reduces loss per SL hit
    'ATR_SL_MULT_SHORT': 1.6,   # tightened from 2.0 — pairs with raised SHORT TP below
    'BREAKEVEN_TRIGGER':   0.008,
    'TRAILING_STEP':     0.005,

    # ── Signal Hard Gates ─────────────────────────────────────────────────────
    # Only 3 hard gates remain: ADX, StochRSI, volume.
    # BTC trend and HTF bias are now SCORE FACTORS only.
    # RSI removed entirely — StochRSI is strictly superior.
    'ADX_THRESHOLD':          18,
    'STOCH_OVERSOLD': 0.30,
    'STOCH_OVERBOUGHT':  0.70,
    'VOLUME_MULT':      1.2,
    'MIN_ATR_PCT':     0.002,
    'MAX_FUNDING_RATE':  0.0003,

    # ── Scoring Weights — 8 factors + candle multiplier [v3.9] ────────────────
    # RSI removed. BTC trend added. All weights sum to 1.0.
    'SCORE_STOCH_W': 0.20,   # StochRSI K position + K/D crossover bonus
    'SCORE_MACD_W':  0.20,   # MACD histogram direction (2-candle momentum)
    'SCORE_ADX_W':   0.16,   # trend strength
    'SCORE_EMA21_W': 0.12,   # gradient price distance from EMA21
    'SCORE_OBV_W':   0.12,   # OBV vs its EMA20 (volume quality)
    'SCORE_VOL_W':   0.10,   # volume surge ratio
    'SCORE_BIAS_W':  0.06,   # 15m HTF bias alignment
    'SCORE_BTC_W':   0.04,   # BTC 1h trend alignment
    # Candle multiplier: ×1.10 if signal candle confirms direction, ×0.90 if not.

    # ── Per-Symbol Ban ─────────────────────────────────────────────────────────
    'MAX_CONSECUTIVE_LOSSES': 2,
    'SYMBOL_BAN_MINUTES':     30,

    # ── Cooldown after loss — time-based [v3.9] ───────────────────────────────
    # After any SL, bot waits this many minutes before opening a NEW position.
    # Set to 0 to disable. (Circuit breaker still works independently.)
    'LOSS_COOLDOWN_MINUTES': 5,

    # ── Fees ──────────────────────────────────────────────────────────────────
    'TAKER_FEE': 0.0004,

    # ── Exchange-Side Bracket Orders ──────────────────────────────────────────
    # BRACKET_ORDERS : True = STOP_MARKET + TAKE_PROFIT_MARKET placed on Binance
    #                immediately after entry. Bot crash = position still safe.
    # SL_UPDATE_THRESHOLD: min improvement fraction before updating exchange SL.
    'BRACKET_ORDERS':   True,
    'SL_UPDATE_THRESHOLD': 0.0015,

    # ── Slippage Control ──────────────────────────────────────────────────────
    # LIMIT_ORDER     : True = try limit at best bid/ask first, fallback market.
    # LIMIT_WAIT_SECONDS: seconds to wait for fill before falling back.
    # MAX_SLIPPAGE_PCT  : skip trade if price already moved this far from signal.
    'LIMIT_ORDER':        True,
    'LIMIT_WAIT_SECONDS':  10,
    'MAX_SLIPPAGE_PCT':     0.003,

    # ── System ────────────────────────────────────────────────────────────────
    'LOG_FILE':          'trade_log.csv',
    'HEARTBEAT_MINUTES':  5,
    'LOOP_SLEEP':         10,
    'MAX_RETRY':         5,
    'POSITION_SYNC_INTERVAL':   4,
}

# Validate scoring weights
_wsum = sum(CONFIG[k] for k in CONFIG if k.startswith('SCORE_') and k.endswith('_W'))
assert abs(_wsum - 1.0) < 0.001, f"Scoring weights must sum to 1.0, got {_wsum}"

# ==============================================================================
# 2. GLOBAL STATE
# ==============================================================================
state_lock = threading.Lock()

# Multi-position state [v3.9]
# Key: symbol string e.g. 'HYPE/USDT'
# Value: dict with all position data
active_positions = {}
# Structure of each position:
# {
#   'side':             'LONG' | 'SHORT',
#   'entry_price':      float,
#   'qty':           float,    # current quantity (reduced after partial TP)
#   'qty_full':       float,    # original full quantity at entry
#   'atr':              float,
#   'leverage':         int,
#   'stop_loss':        float,
#   'highest_price':    float,
#   'lowest_price':     float,
#   'breakeven_reached':bool,
#   'partial_tp_done':  bool,     # True after TP1 partial close executed
#   'open_time':        datetime,
#   'sl_order_id':      str|None,
#   'tp_order_id':      str|None,
#   'last_exchange_sl':  float,
# }

bot_active        = True
start_balance    = 0.0
last_report_time    = datetime.now()
last_scan_results = []

# Per-symbol ban
symbol_loss_count = {}   # {symbol: consecutive_sl_count}
symbol_ban_until   = {}   # {symbol: datetime}

# Cooldown after loss — real datetime [v3.9]
loss_cooldown_until = None   # datetime when cooldown expires

# ==============================================================================
# 3. EXCHANGE CONNECTION
# ==============================================================================
exchange = ccxt.binance({
    'apiKey':  CONFIG['API_KEY'],
    'secret':  CONFIG['SECRET_KEY'],
    'enableRateLimit': True,
    'options': {
        'defaultType':             'future',
        'adjustForTimeDifference': True,
        'recvWindow':              10000,
    }
})

try:
    exchange.enable_demo_trading(True)
except AttributeError:
    demo_base = 'https://demo-fapi.binance.com'
    exchange.urls['api']['fapiPublic']    = f'{demo_base}/fapi/v1'
    exchange.urls['api']['fapiPrivate']   = f'{demo_base}/fapi/v1'
    exchange.urls['api']['fapiPrivateV2'] = f'{demo_base}/fapi/v2'
    exchange.urls['api']['fapi']          = f'{demo_base}/fapi'

# ==============================================================================
# 4. API WRAPPER WITH EXPONENTIAL BACKOFF
# ==============================================================================
def api_call(fn, *args, **kwargs):
    bekleme = 10
    for deneme in range(CONFIG['MAX_RETRY']):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            if deneme == CONFIG['MAX_RETRY'] - 1:
                send_telegram(f"API Baglanti Hatasi (5 deneme):\n{e}")
                raise
            print(f"[API Retry {deneme+1}] {e} — {bekleme}s")
            time.sleep(bekleme)
            bekleme *= 2
        except ccxt.RateLimitExceeded:
            time.sleep(60)
        except Exception:
            raise

# ==============================================================================
# 5. TELEGRAM
# ==============================================================================
t_bot = telebot.TeleBot(CONFIG['TELEGRAM_TOKEN'])

def send_telegram(mesaj, reply_markup=None):
    try:
        t_bot.send_message(
            CONFIG['CHAT_ID'], mesaj,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"[Telegram Hatasi] {e}")

def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    kb.add(
        types.KeyboardButton("Durum"),
        types.KeyboardButton("Bakiye"),
        types.KeyboardButton("PnL"),
    )
    kb.add(
        types.KeyboardButton("Tarama"),
        types.KeyboardButton("Son Islemler"),
        types.KeyboardButton("Piyasa"),
    )
    kb.add(
        types.KeyboardButton("Backtest"),
        types.KeyboardButton("Durdur"),
        types.KeyboardButton("Baslat"),
    )
    kb.add(
        types.KeyboardButton("Saglik Kontrol"),
        types.KeyboardButton("Istatistik"),
        types.KeyboardButton("Tum Pozisyon Kapat"),
    )
    return kb

# ── /start ────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['start'])
@t_bot.message_handler(func=lambda m: m.text == "Baslat")
def cmd_start(message):
    global bot_active
    with state_lock:
        bot_active = True
    send_telegram(
        "Bot aktif. Tarama ve islem dongusu calisiyor.\n"
        "/yardim — tum komutlar",
        reply_markup=main_keyboard()
    )

# ── /dur ──────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['dur'])
@t_bot.message_handler(func=lambda m: m.text == "Durdur")
def cmd_stop(message):
    global bot_active
    with state_lock:
        bot_active = False
    send_telegram(
        "Bot duraklatildi. Acik pozisyonlar korunuyor.\n"
        "Devam icin /start",
        reply_markup=main_keyboard()
    )

# ── /durum ────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['durum'])
@t_bot.message_handler(func=lambda m: m.text == "Durum")
def cmd_status(message):
    with state_lock:
        _positions  = dict(active_positions)
        _active   = bot_active
        _bans    = {k: v for k, v in symbol_ban_until.items()
                    if v > datetime.now()}
        _cooldown = loss_cooldown_until

    bal       = get_balance()
    status_str = "AKTIF" if _active else "DURDURULDU"

    mesaj = (
        f"<b>Bot Durum Raporu — v3.9</b>\n"
        f"Durum   : {status_str}\n"
        f"Bakiye  : <b>{bal:.2f} USDT</b>\n"
        f"Pozisyon: {len(_positions)}/{CONFIG['MAX_CONCURRENT_POSITIONS']}\n"
    )

    if _cooldown and datetime.now() < _cooldown:
        remaining = int((_cooldown - datetime.now()).seconds / 60)
        mesaj += f"Cooldown: {remaining}dk kaldi\n"

    if not _positions:
        mesaj += "\nAcik Pozisyon: YOK\n"
    else:
        mesaj += "\n<b>— Acik Pozisyonlar —</b>\n"
        for sym, pos in _positions.items():
            fiyat_son = get_price(sym)
            if fiyat_son > 0:
                raw = ((fiyat_son - pos['entry_price']) / pos['entry_price']
                       if pos['side'] == 'LONG'
                       else (pos['entry_price'] - fiyat_son) / pos['entry_price'])
                roi = raw * pos['leverage'] * 100
                pnl = (pos['qty'] * (fiyat_son - pos['entry_price'])
                       if pos['side'] == 'LONG'
                       else pos['qty'] * (pos['entry_price'] - fiyat_son))
                sure = int((datetime.now() - pos['open_time']).seconds / 60)
                kalan_sure = int(CONFIG['MAX_POSITION_HOURS'] * 60 - sure)
                kisi_str   = " [KISMI TP YAPILDI]" if pos['partial_tp_done'] else ""
                mesaj += (
                    f"\n{sym} {pos['side']} {pos['leverage']}x{kisi_str}\n"
                    f"  Giris: {pos['entry_price']:.4f} | Su an: {fiyat_son:.4f}\n"
                    f"  SL  : {pos['stop_loss']:.4f}\n"
                    f"  ROI : <b>%{roi:.2f}</b> ({pnl:+.2f} USDT)\n"
                    f"  Sure: {sure}dk (max {CONFIG['MAX_POSITION_HOURS']*60}dk, "
                    f"{kalan_sure}dk kaldi)\n"
                )

    if _bans:
        mesaj += "\n<b>— Gecici Banlar —</b>\n"
        for sym, expiry in _bans.items():
            remaining = int((expiry - datetime.now()).seconds / 60)
            mesaj += f"  {sym}: {remaining}dk\n"

    with state_lock:
        _start_bal = start_balance
    profit = bal - _start_bal
    pct    = (profit / _start_bal * 100) if _start_bal > 0 else 0
    mesaj += f"\nNet P/L : <b>{profit:+.2f} USDT (%{pct:+.2f})</b>"
    send_telegram(mesaj)

# ── /balance ───────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['balance'])
@t_bot.message_handler(func=lambda m: m.text == "Bakiye")
def cmd_balance(message):
    bal = get_balance()
    with state_lock:
        _start_bal = start_balance
    profit = bal - _start_bal
    pct    = (profit / _start_bal * 100) if _start_bal > 0 else 0
    send_telegram(
        f"<b>Cuzdan Durumu</b>\n"
        f"Mevcut    : <b>{bal:.2f} USDT</b>\n"
        f"Baslangic : {_start_bal:.2f} USDT\n"
        f"Net       : <b>{profit:+.2f} USDT (%{pct:+.2f})</b>"
    )

# ── /pnl ──────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['pnl'])
@t_bot.message_handler(func=lambda m: m.text == "PnL")
def cmd_pnl(message):
    try:
        if not os.path.isfile(CONFIG['LOG_FILE']):
            send_telegram("Henuz tamamlanmis islem yok.")
            return
        df = pd.read_csv(CONFIG['LOG_FILE'], delimiter=';',
                         on_bad_lines='skip', engine='python')
        df['Time'] = pd.to_datetime(df['Time'])
        closed_trades = df[df['Status'] == 'CLOSED'].copy()
        closed_trades['PnL_USD_net'] = pd.to_numeric(
            closed_trades['PnL_USD_net'], errors='coerce')
        today = closed_trades[closed_trades['Time'] > datetime.now().replace(
            hour=0, minute=0, second=0)]
        week = closed_trades[closed_trades['Time'] >
                        (datetime.now() - timedelta(days=7))]

        def summary(subset):
            if subset.empty: return "Islem yok"
            pnl = subset['PnL_USD_net'].sum()
            cnt = len(subset)
            wins = len(subset[subset['PnL_USD_net'] > 0])
            avg = subset['PnL_USD_net'].mean()
            wr  = wins / cnt * 100 if cnt > 0 else 0
            return (f"<b>{pnl:+.2f} USDT</b> | {cnt} islem | "
                    f"Win: %{wr:.0f} | Ort: {avg:+.2f}")

        symbol_summary = ""
        if not closed_trades.empty and 'Symbol' in closed_trades.columns:
            for sym, grp in closed_trades.groupby('Symbol'):
                sp = grp['PnL_USD_net'].sum()
                symbol_summary += (f"  {sym}: {sp:+.2f} USDT "
                                f"({len(grp)} islem)\n")

        send_telegram(
            f"<b>PnL Ozeti</b>\n"
            f"Bugun : {summary(today)}\n"
            f"7 Gun : {summary(week)}\n"
            f"Tumu  : {summary(closed_trades)}\n"
            f"<b>Sembole Gore:</b>\n{symbol_summary}"
        )
    except Exception as e:
        send_telegram(f"PnL hatasi: {e}")

# ── /istatistik [v3.9] ────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['istatistik'])
@t_bot.message_handler(func=lambda m: m.text == "Istatistik")
def cmd_istatistik(message):
    try:
        if not os.path.isfile(CONFIG['LOG_FILE']):
            send_telegram("Henuz kayitli islem yok.")
            return
        df = pd.read_csv(CONFIG['LOG_FILE'], delimiter=';',
                         on_bad_lines='skip', engine='python')
        df['Time'] = pd.to_datetime(df['Time'])
        closed_trades = df[df['Status'] == 'CLOSED'].copy()
        if closed_trades.empty:
            send_telegram("Tamamlanmis islem bulunamadi.")
            return
        closed_trades['PnL_USD_net'] = pd.to_numeric(
            closed_trades['PnL_USD_net'], errors='coerce')
        closed_trades['Hour'] = closed_trades['Time'].dt.hour

        mesaj = "<b>Detayli Istatistik — v3.9</b>\n"

        # By direction
        mesaj += "\n<b>— Yone Gore —</b>\n"
        for yon in ['LONG', 'SHORT']:
            subset = closed_trades[closed_trades['Side'] == yon]
            if subset.empty:
                mesaj += f"{yon}: Islem yok\n"
                continue
            pnl = subset['PnL_USD_net'].sum()
            wr  = len(subset[subset['PnL_USD_net'] > 0]) / len(subset) * 100
            mesaj += (f"{yon}: {len(subset)} islem | "
                      f"Win %{wr:.0f} | {pnl:+.2f} USDT\n")

        # By symbol (top 8)
        mesaj += "\n<b>— Sembole Gore (en iyi 8) —</b>\n"
        sym_stats = []
        for sym, grp in closed_trades.groupby('Symbol'):
            pnl = grp['PnL_USD_net'].sum()
            wr  = len(grp[grp['PnL_USD_net'] > 0]) / len(grp) * 100
            sym_stats.append((sym, pnl, wr, len(grp)))
        sym_stats.sort(key=lambda x: x[1], reverse=True)
        for sym, pnl, wr, cnt in sym_stats[:8]:
            mesaj += f"  {sym}: {pnl:+.2f} ({cnt} islem, %{wr:.0f} win)\n"

        # By hour of day
        mesaj += "\n<b>— Saate Gore (en iyi 6) —</b>\n"
        hour_stats = []
        for hour, grp in closed_trades.groupby('Hour'):
            pnl = grp['PnL_USD_net'].sum()
            wr  = len(grp[grp['PnL_USD_net'] > 0]) / len(grp) * 100
            hour_stats.append((hour, pnl, wr, len(grp)))
        hour_stats.sort(key=lambda x: x[1], reverse=True)
        for hour, pnl, wr, cnt in hour_stats[:6]:
            mesaj += (f"  {hour:02d}:00 UTC: {pnl:+.2f} "
                      f"({cnt} islem, %{wr:.0f} win)\n")

        # Closing reasons
        mesaj += "\n<b>— Kapanma Sebebi —</b>\n"
        if 'Close_Reason' in closed_trades.columns:
            for reason, grp in closed_trades.groupby('Close_Reason'):
                pnl = grp['PnL_USD_net'].sum()
                mesaj += f"  {reason}: {len(grp)} islem | {pnl:+.2f} USDT\n"

        send_telegram(mesaj)
    except Exception as e:
        send_telegram(f"Istatistik hatasi: {e}")

# ── /son ──────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['son'])
@t_bot.message_handler(func=lambda m: m.text == "Son Islemler")
def cmd_recent(message):
    try:
        if not os.path.isfile(CONFIG['LOG_FILE']):
            send_telegram("Kayitli islem yok.")
            return
        df = pd.read_csv(CONFIG['LOG_FILE'], delimiter=';',
                         on_bad_lines='skip', engine='python')
        closed_trades = df[df['Status'] == 'CLOSED'].tail(5)
        if closed_trades.empty:
            send_telegram("Tamamlanmis islem bulunamadi.")
            return
        mesaj = "<b>Son 5 Kapali Islem</b>\n"
        for _, row in closed_trades.iloc[::-1].iterrows():
            try:
                pnl_net = float(row['PnL_USD_net'])
                pnl_pct = float(row['PnL_Pct_lev'])
                reason   = row.get('Close_Reason', '?')
            except Exception:
                pnl_net, pnl_pct, reason = 0, 0, "?"
            mesaj += (
                f"{row.get('Symbol','?')} {row.get('Side','?')} "
                f"[{reason}] {str(row.get('Time',''))[:16]}\n"
                f"  PnL: <b>{pnl_net:+.2f} USDT (%{pnl_pct:.2f})</b>\n"
            )
        send_telegram(mesaj)
    except Exception as e:
        send_telegram(f"Son islem hatasi: {e}")

# ── /tara ─────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['tara'])
@t_bot.message_handler(func=lambda m: m.text == "Tarama")
def cmd_scan(message):
    with state_lock:
        cache = list(last_scan_results)
        bans  = {k: v for k, v in symbol_ban_until.items()
                 if v > datetime.now()}
    if cache:
        _send_scan_message(cache, bans, "Onceki Tarama (yenileniyor...)")
    else:
        send_telegram("Tarama baslatiliyor...")
    threading.Thread(target=_scan_thread, daemon=True).start()

def _send_scan_message(sonuclar, bans=None, baslik="Tarama Sonuclari"):
    bans  = bans or {}
    mesaj = f"<b>{baslik}</b>\n"
    for r in sonuclar:
        ban_tag = ""
        if r['symbol'] in bans:
            remaining   = int((bans[r['symbol']] - datetime.now()).seconds / 60)
            ban_tag = f" [BAN {remaining}dk]"
        mesaj += (
            f"{r['symbol']}{ban_tag}  Skor:<b>{r['score']:.2f}</b>  {r['signal']}\n"
            f"  ADX:{r['adx']:.1f} StochK:{r['stoch_k']*100:.0f}% "
            f"MACD:{r['macd_dir']} OBV:{r['obv_trend']}\n"
        )
    mesaj += f"\n{datetime.now().strftime('%H:%M:%S')}"
    send_telegram(mesaj)

def _scan_thread():
    global last_scan_results
    try:
        btc_trend = get_btc_trend()
        sonuclar  = scan_symbols(btc_trend)
        with state_lock:
            last_scan_results = sonuclar
            bans = {k: v for k, v in symbol_ban_until.items()
                    if v > datetime.now()}
        _send_scan_message(sonuclar, bans, "Guncel Tarama Sonuclari")
    except Exception as e:
        print(f"[Tarama Thread] {e}")

# ── /piyasa ───────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['piyasa'])
@t_bot.message_handler(func=lambda m: m.text == "Piyasa")
def cmd_market(message):
    with state_lock:
        _positions = dict(active_positions)
        _cache  = list(last_scan_results)

    if _positions:
        target     = list(_positions.keys())[0]
        title_suffix = f"(acik pozisyon: {target})"
    else:
        best = next((r for r in _cache if r['signal'] != 'WAIT'), None)
        if best:
            target     = best['symbol']
            title_suffix = f"(en yuksek score: {best['score']:.2f})"
        else:
            target     = CONFIG['SYMBOL_LIST'][0]
            title_suffix = "(varsayilan)"

    send_telegram(f"{target} analizi cekiliyor {title_suffix}...")
    try:
        r = analyze_symbol(target)
        if not r:
            send_telegram("Analiz basarisiz.")
            return
        def eb(v): return "EVET" if v else "HAYIR"
        mum_str    = "YUKARI" if r['mum_yukari'] else "ASAGI"
        lev        = calc_dynamic_leverage(r['adx'])
        _tp_al     = r['price'] * (1 + calc_tp_rate('LONG', lev))
        _tp_sat    = r['price'] * (1 - calc_tp_rate('SHORT', lev))
        sl_al_c    = CONFIG['ATR_SL_MULT_LONG']
        sl_sat_c   = CONFIG['ATR_SL_MULT_SHORT']
        _sl_al     = r['price'] - r['atr'] * sl_al_c
        _sl_sat    = r['price'] + r['atr'] * sl_sat_c
        stoch_gate = (
            "AL KAPI GECTI"  if r['stoch_k'] < CONFIG['STOCH_OVERSOLD']
            else "SAT KAPI GECTI" if r['stoch_k'] > CONFIG['STOCH_OVERBOUGHT']
            else f"KAPI GECMEDI"
        )
        if CONFIG['TP_ROI_MODE']:
            tp_label = (
                f"AL ROI %{CONFIG['TP_ROI_TARGET']*100:.0f} ({_tp_al:.4f}) | "
                f"SAT ROI %{CONFIG['TP_ROI_TARGET_SHORT']*100:.0f} ({_tp_sat:.4f})"
            )
        else:
            tp_label = (
                f"AL: {_tp_al:.4f} | SAT: {_tp_sat:.4f}"
            )
        send_telegram(
            f"<b>Detayli Analiz — {target}</b>\n"
            f"Fiyat       : {r['price']:.4f}\n"
            f"ATR         : {r['atr']:.4f} (%{r['atr_pct']*100:.2f})\n"
            f"Kaldirac    : {lev}x (ADX bazli dinamik)\n"
            f"\n<b>— Gosterge Degerleri —</b>\n"
            f"ADX         : {r['adx']:.1f}\n"
            f"StochRSI K  : {r['stoch_k']*100:.1f}% → {stoch_gate}\n"
            f"StochRSI D  : {r['stoch_d']*100:.1f}%\n"
            f"MACD Hist.  : {r['macd_dir']} ({r['macd_hist']:+.6f})\n"
            f"EMA21 (5m)  : {r['ema21']:.4f} "
            f"({'Ustu' if r['price'] > r['ema21'] else 'Alti'})\n"
            f"OBV Trend   : {r['obv_trend']}\n"
            f"Mum Yonu    : {mum_str}\n"
            f"\n<b>— Seviyeler —</b>\n"
            f"TP          : {tp_label}\n"
            f"SL AL       : {_sl_al:.4f} ({sl_al_c}×ATR)\n"
            f"SL SAT      : {_sl_sat:.4f} ({sl_sat_c}×ATR)\n"
            f"\n<b>— Filtreler (score katkilisi) —</b>\n"
            f"Bias (15m)  : {r['bias']}\n"
            f"BTC Trendi  : {r['btc_trend']}\n"
            f"Funding     : {r['funding']*100:.4f}%\n"
            f"Vol Oran    : {r['vol_oran']:.2f}x\n"
            f"Hacim onay  : {eb(r['volume_ok'])}\n"
            f"Volatilite  : {eb(r['vol_ok'])}\n"
            f"\n<b>Sinyal Skoru : {r['score']:.3f}</b>\n"
            f"<b>Sinyal       : {r['signal']}</b>"
        )
    except Exception as e:
        send_telegram(f"Piyasa analiz hatasi: {e}")

# ── /backtest ─────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['backtest'])
@t_bot.message_handler(func=lambda m: m.text == "Backtest")
def cmd_backtest(message):
    send_telegram(
        f"Backtest baslatildi — {len(CONFIG['SYMBOL_LIST'])} symbol, "
        f"300 mum. Bekleyin."
    )
    threading.Thread(target=_backtest_thread, daemon=True).start()

def _backtest_thread():
    for symbol in CONFIG['SYMBOL_LIST']:
        try:
            sonuc = run_backtest(symbol, CONFIG['ENTRY_TF'], limit=300)
            send_telegram(sonuc)
            time.sleep(3)
        except Exception as e:
            send_telegram(f"Backtest hatasi ({symbol}): {e}")

# ── /kapat — Close specific or all positions [v3.9] ──────────────────────────
@t_bot.message_handler(commands=['kapat'])
@t_bot.message_handler(func=lambda m: m.text == "Tum Pozisyon Kapat")
def cmd_close(message):
    with state_lock:
        _positions = dict(active_positions)

    if not _positions:
        send_telegram("Kapatilacak acik pozisyon yok.")
        return

    # Check if specific symbol given: /kapat HYPE/USDT
    target_symbol = None
    if message.text and message.text.startswith('/kapat '):
        parts = message.text.split(' ', 1)
        if len(parts) > 1:
            target_symbol = parts[1].strip().upper()
            if '/' not in target_symbol:
                target_symbol += '/USDT'
            if target_symbol not in _positions:
                send_telegram(
                    f"{target_symbol} icin acik pozisyon bulunamadi.\n"
                    f"Acik: {', '.join(_positions.keys())}"
                )
                return

    to_close = ([target_symbol] if target_symbol
                      else list(_positions.keys()))
    send_telegram(
        f"ACIL KAPAT: {', '.join(to_close)}\n"
        f"Islemler gerceklestiriliyor..."
    )
    threading.Thread(
        target=_close_thread,
        args=(to_close,),
        daemon=True
    ).start()

def _close_thread(semboller):
    for symbol in semboller:
        with state_lock:
            pos = active_positions.get(symbol)
        if not pos:
            continue
        try:
            if CONFIG['BRACKET_ORDERS']:
                cancel_bracket_orders(symbol, pos)

            try:
                pos_check = api_call(exchange.fetch_positions, [symbol])
                still_open = any(
                    float(p.get('contracts', 0) or 0) > 0
                    for p in pos_check
                )
            except Exception:
                still_open = True

            if still_open:
                exit_side = 'sell' if pos['side'] == 'LONG' else 'buy'
                api_call(
                    exchange.create_market_order,
                    symbol, exit_side, pos['qty'],
                    params={'reduceOnly': True}
                )

            fiyat_son = get_price(symbol)
            if fiyat_son > 0:
                raw = ((fiyat_son - pos['entry_price']) / pos['entry_price']
                       if pos['side'] == 'LONG'
                       else (pos['entry_price'] - fiyat_son) / pos['entry_price'])
                roi = raw * pos['leverage'] * 100
                pnl = (pos['qty'] * (fiyat_son - pos['entry_price'])
                       if pos['side'] == 'LONG'
                       else pos['qty'] * (pos['entry_price'] - fiyat_son))
                send_telegram(
                    f"<b>POZISYON KAPANDI — MANUEL</b>\n"
                    f"Sembol : <b>{symbol}</b> | {pos['side']}\n"
                    f"Giris  : {pos['entry_price']:.4f}\n"
                    f"Cikis  : {fiyat_son:.4f}\n"
                    f"ROI    : <b>%{roi:.2f}</b> ({pnl:+.2f} USDT)"
                )

            with state_lock:
                active_positions.pop(symbol, None)

        except Exception as e:
            send_telegram(
                f"KAPAT HATASI — {symbol}\n"
                f"Hata: {str(e)[:100]}\n"
                f"Binance'den manuel kapatin!"
            )

# ── /saglik ───────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['saglik'])
@t_bot.message_handler(func=lambda m: m.text == "Saglik Kontrol")
def cmd_health(message):
    send_telegram("Sistem kontrol ediliyor (~20 saniye)...")
    threading.Thread(target=_health_thread, daemon=True).start()

def _health_thread():
    lines    = []
    error_count = 0
    warn_count = 0

    def ok(et, dg=""):
        lines.append("OK    " + et + (f": {dg}" if dg else ""))
    def hata(et, dg=""):
        nonlocal error_count; error_count += 1
        lines.append("HATA  " + et + (f": {dg}" if dg else ""))
    def uyari(et, dg=""):
        nonlocal warn_count; warn_count += 1
        lines.append("UYARI " + et + (f": {dg}" if dg else ""))
    def section(b):
        lines.append(""); lines.append(f"[ {b} ]")

    lines.append("=== SAGLIK RAPORU — v3.9 ===")
    lines.append(f"Zaman: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    section("API")
    try:
        t0 = time.time()
        exchange.fetch_time()
        ms = int((time.time() - t0) * 1000)
        (ok if ms < 500 else uyari)("API", f"{ms}ms")
    except Exception as e:
        hata("API", str(e)[:60])

    section("BAKIYE")
    try:
        bal = get_balance()
        (ok if bal > 0 else uyari)("Bakiye", f"{bal:.2f} USDT")
    except Exception as e:
        hata("Bakiye", str(e)[:60])

    section("INDIKTORLER (ilk symbol)")
    test_sym = CONFIG['SYMBOL_LIST'][0]
    try:
        t0 = time.time()
        r  = analyze_symbol(test_sym)
        ms = int((time.time() - t0) * 1000)
        if r:
            ok("ADX",       f"{r['adx']:.1f}")
            ok("StochRSI",  f"K:{r['stoch_k']*100:.1f}% D:{r['stoch_d']*100:.1f}%")
            stg = ("AL GECTI"  if r['stoch_k'] < CONFIG['STOCH_OVERSOLD']
                   else "SAT GECTI" if r['stoch_k'] > CONFIG['STOCH_OVERBOUGHT']
                   else "GECMEDI")
            ok("Stoch kapi", stg)
            ok("MACD",      f"{r['macd_dir']} ({ms}ms)")
            ok("EMA21",     f"{r['ema21']:.4f}")
            ok("OBV",       r['obv_trend'])
            ok("Sinyal",    f"{r['signal']} score:{r['score']:.3f}")
        else:
            hata("Indiktor", "None dondu")
    except Exception as e:
        hata("Indiktor", str(e)[:60])

    section("TP MODU")
    lev = CONFIG['LEVERAGE']
    if CONFIG['TP_ROI_MODE']:
        ok("TP Modu", "ROI bazli")
        ok("AL target",
           f"ROI %{CONFIG['TP_ROI_TARGET']*100:.0f} "
           f"→ %{calc_tp_rate('LONG',lev)*100:.2f} price hareketi")
        ok("SAT target",
           f"ROI %{CONFIG['TP_ROI_TARGET_SHORT']*100:.0f} "
           f"→ %{calc_tp_rate('SHORT',lev)*100:.2f} price hareketi")
    else:
        ok("TP Modu", "Fiyat hareketi bazli")
    if CONFIG['PARTIAL_TP_ACTIVE']:
        ok("Kismi TP",
           f"TP1 {CONFIG['PARTIAL_TP_PCT']*100:.0f}% kapat, "
           f"TP2 ROI %{CONFIG['TP2_ROI_TARGET']*100:.0f}")
    else:
        uyari("Kismi TP", "Kapali")
    ok("Dinamik leverage",
       f"{'AKTIF' if CONFIG['DYNAMIC_LEVERAGE'] else 'KAPALI'} "
       f"({lev}x baz, max {CONFIG['MAX_LEVERAGE']}x)")

    section("BRACKET EMIRLER")
    if CONFIG['BRACKET_ORDERS']:
        ok("Bracket modu", "AKTIF — exchange SL+TP")
    else:
        uyari("Bracket modu", "KAPALI — yazilim SL/TP (risk yuksek)")

    section("MULTI-POZISYON DURUMU")
    with state_lock:
        _positions = dict(active_positions)
        _bans   = {k: v for k, v in symbol_ban_until.items()
                   if v > datetime.now()}
    ok("Max pozisyon", str(CONFIG['MAX_CONCURRENT_POSITIONS']))
    ok("Acik pozisyon", str(len(_positions)))
    ok("Max marjin", f"%{CONFIG['MAX_TOTAL_MARGIN_PCT']*100:.0f} balance")
    ok("Max sure", f"{CONFIG['MAX_POSITION_HOURS']}h auto-close")
    for sym, pos in _positions.items():
        sure = int((datetime.now() - pos['open_time']).seconds / 60)
        ok(f"  {sym}", f"{pos['side']} {pos['leverage']}x {sure}dk")
    if _bans:
        for sym, expiry in _bans.items():
            remaining = int((expiry - datetime.now()).seconds / 60)
            uyari(f"Ban: {sym}", f"{remaining}dk")
    else:
        ok("Ban durumu", "Banli symbol yok")

    section("IZLEME LISTESI")
    ok("Sembol sayisi", str(len(CONFIG['SYMBOL_LIST'])))

    lines.append("")
    lines.append("=" * 30)
    if error_count == 0 and warn_count == 0:
        lines.append("SONUC: TUM SISTEMLER NORMAL")
    elif error_count == 0:
        lines.append(f"SONUC: {warn_count} UYARI, HATA YOK")
    else:
        lines.append(f"SONUC: {error_count} HATA  {warn_count} UYARI")

    send_telegram("\n".join(lines))

# ── /yardim ───────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['yardim', 'help'])
def cmd_help(message):
    send_telegram(
        "<b>Komut Listesi — v3.9</b>\n"
        "/durum            — Tum acik pozisyonlar + ban durumu\n"
        "/balance           — Cuzdan ve net profit\n"
        "/pnl              — PnL ozeti\n"
        "/istatistik       — Sembol/yon/hour bazli detayli analiz\n"
        "/son              — Son 5 kapali islem\n"
        "/tara             — 16 sembolun scan tablosu\n"
        "/piyasa           — En iyi sembolun tam analizi\n"
        "/backtest         — Tum semboller icin backtest\n"
        "/saglik           — Tam sistem saglik kontrolu\n"
        "/kapat            — Tum acik pozisyonlari kapat (ACIL)\n"
        "/kapat SEMBOL     — Belirli pozisyonu kapat\n"
        "/dur              — Botu duraklatir\n"
        "/start            — Botu baslatir\n"
        "/yardim           — Bu menu",
        reply_markup=main_keyboard()
    )

# ==============================================================================
# 6. CORE HELPERS
# ==============================================================================
def get_price(symbol):
    try:
        ticker = api_call(exchange.fetch_ticker, symbol)
        return float(ticker['last'])
    except Exception as e:
        print(f"[Fiyat Hatasi {symbol}] {e}")
        return 0.0

def get_balance():
    try:
        balance = api_call(exchange.fetch_balance)
        return float(balance['total'].get('USDT', 0))
    except Exception as e:
        print(f"[Bakiye Hatasi] {e}")
        return 0.0

def calc_fee(notional):
    return notional * CONFIG['TAKER_FEE'] * 2

def calc_tp_rate(side='LONG', leverage=None):
    """Return effective TP price-move ratio for given side and leverage."""
    if leverage is None:
        leverage = CONFIG['LEVERAGE']
    if CONFIG['TP_ROI_MODE']:
        roi = (CONFIG['TP_ROI_TARGET'] if side == 'LONG'
               else CONFIG['TP_ROI_TARGET_SHORT'])
        return roi / leverage
    return CONFIG['TP_RATE']

def calc_dynamic_leverage(adx):
    """Return trade leverage scaled by ADX strength."""
    base = CONFIG['LEVERAGE']
    if not CONFIG['DYNAMIC_LEVERAGE']:
        return base
    extra = max(0, round((adx - CONFIG['ADX_THRESHOLD']) / 10))
    return min(base + extra, CONFIG['MAX_LEVERAGE'])

def check_daily_loss():
    global bot_active
    if not os.path.isfile(CONFIG['LOG_FILE']):
        return True
    try:
        df = pd.read_csv(CONFIG['LOG_FILE'], delimiter=';',
                         on_bad_lines='skip', engine='python')
        df['Time'] = pd.to_datetime(df['Time'])
        today = df[
            (df['Time'] > datetime.now().replace(hour=0, minute=0, second=0)) &
            (df['Status'] == 'CLOSED')
        ]
        if today.empty: return True
        daily_pnl = pd.to_numeric(
            today['PnL_USD_net'], errors='coerce').sum()
        bal  = get_balance()
        ref  = bal + abs(daily_pnl) if bal > 0 else 1
        ratio = abs(daily_pnl) / ref if daily_pnl < 0 else 0
        if ratio >= CONFIG['DAILY_MAX_LOSS']:
            with state_lock:
                bot_active = False
            send_telegram(
                f"<b>DEVRE KESICI</b>\n"
                f"Gunluk kayip limiti asildi!\n"
                f"Kayip : {daily_pnl:.2f} USDT (%{ratio*100:.1f})\n"
                f"Limit : %{CONFIG['DAILY_MAX_LOSS']*100:.0f}\n"
                f"Bot DURDURULDU. /start ile yeniden baslatIn."
            )
            return False
        return True
    except Exception as e:
        print(f"[Risk Kontrol] {e}")
        return True

def log_trade(data, current_balance):
    data['Balance_USDT'] = round(current_balance, 2)
    fieldnames = [
        'Time', 'Symbol', 'Status', 'Side', 'Volume_USD', 'Price',
        'PnL_Pct_lev', 'PnL_USD_gross', 'Fee_USD', 'PnL_USD_net',
        'Close_Reason', 'Balance_USDT'
    ]
    file_exists = os.path.isfile(CONFIG['LOG_FILE'])
    try:
        with open(CONFIG['LOG_FILE'], mode='a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=';',
                                    quoting=csv.QUOTE_ALL, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()
            writer.writerow({k: data.get(k, '-') for k in fieldnames})
    except Exception as e:
        print(f"[Log Hatasi] {e}")

# ==============================================================================
# 7. BRACKET ORDER HELPERS
# ==============================================================================
def _cancel_all_stop_orders(symbol, pos):
    """
    Cancel ALL open STOP_MARKET and TAKE_PROFIT_MARKET orders for a symbol
    by fetching live open orders — not by stored IDs.

    Why: Binance auto-cancels the paired order when one fires (e.g. TP hits →
    Binance deletes SL). Our stored ID becomes stale (-2011). Fetching live
    orders is always accurate.
    """
    try:
        open_orders = api_call(exchange.fetch_open_orders, symbol)
        hedef_tipler = {'stop_market', 'take_profit_market',
                        'STOP_MARKET', 'TAKE_PROFIT_MARKET'}
        cancel_count = 0
        for order in open_orders:
            tip = str(order.get('type', '')).upper()
            if tip in {'STOP_MARKET', 'TAKE_PROFIT_MARKET'}:
                try:
                    api_call(exchange.cancel_order, order['id'], symbol)
                    cancel_count += 1
                    print(f"[Bracket Iptal] {symbol} {tip} id={order['id']}")
                except Exception as e:
                    # -2011 = already gone, safe to ignore
                    if '-2011' not in str(e):
                        print(f"[Bracket Iptal Hata] {order['id']}: {e}")
        if cancel_count == 0:
            print(f"[Bracket Iptal] {symbol}: Acik stop emri bulunamadi (zaten kapali)")
    except Exception as e:
        print(f"[Bracket Iptal Genel] {symbol}: {e}")
    finally:
        # Always clear stored IDs regardless of what happened
        if pos is not None:
            pos['sl_order_id'] = None
            pos['tp_order_id'] = None


def cancel_bracket_orders(symbol, pos):
    """Public alias — cancels all live stop orders for this symbol."""
    _cancel_all_stop_orders(symbol, pos)

def place_bracket_orders(symbol, side, sl_fiyat, tp_fiyat, pos):
    """Place STOP_MARKET + TAKE_PROFIT_MARKET. Updates pos dict in place."""
    close_side = 'sell' if side == 'LONG' else 'buy'
    try:
        sl_fiyat = float(exchange.price_to_precision(symbol, sl_fiyat))
        tp_fiyat = float(exchange.price_to_precision(symbol, tp_fiyat))
    except Exception:
        pass

    # STOP_MARKET
    try:
        sl_emir = api_call(
            exchange.create_order,
            symbol, 'STOP_MARKET', close_side, None, None,
            params={
                'stopPrice':     sl_fiyat,
                'closePosition': True,
                'workingType':   'MARK_PRICE',
            }
        )
        pos['sl_order_id']    = sl_emir.get('id')
        pos['last_exchange_sl'] = sl_fiyat
        print(f"[Bracket SL] {symbol} {sl_fiyat} id={pos['sl_order_id']}")
    except Exception as e:
        send_telegram(
            f"UYARI: Exchange SL yerleştirilemedi\n"
            f"Sembol: {symbol}\nHata: {str(e)[:80]}\n"
            f"Yazilim SL aktif."
        )

    # TAKE_PROFIT_MARKET
    try:
        tp_emir = api_call(
            exchange.create_order,
            symbol, 'TAKE_PROFIT_MARKET', close_side, None, None,
            params={
                'stopPrice':     tp_fiyat,
                'closePosition': True,
                'workingType':   'MARK_PRICE',
            }
        )
        pos['tp_order_id'] = tp_emir.get('id')
        print(f"[Bracket TP] {symbol} {tp_fiyat} id={pos['tp_order_id']}")
    except Exception as e:
        send_telegram(
            f"UYARI: Exchange TP yerleştirilemedi\n"
            f"Sembol: {symbol}\nHata: {str(e)[:80]}"
        )

def update_sl_order(symbol, side, new_sl, pos):
    """
    Update exchange-side STOP_MARKET when trailing stop improves enough.

    Strategy:
    1. Check improvement threshold — skip if not worth updating.
    2. Fetch live open orders for this symbol.
    3. Cancel any existing STOP_MARKET orders found (by live ID, not stored ID).
       If none found, the slot is already empty — safe to place new one.
    4. Place new STOP_MARKET only after confirming slot is clear.
    """
    if not CONFIG['BRACKET_ORDERS']:
        return
    ex_sl = pos.get('last_exchange_sl', 0)
    if ex_sl > 0:
        improvement = ((new_sl - ex_sl) / ex_sl if side == 'LONG'
                       else (ex_sl - new_sl) / ex_sl)
        if improvement < CONFIG['SL_UPDATE_THRESHOLD']:
            return
    close_side = 'sell' if side == 'LONG' else 'buy'
    try:
        new_sl_prec = float(exchange.price_to_precision(symbol, new_sl))
    except Exception:
        new_sl_prec = new_sl

    # Step 1: Cancel ALL existing STOP_MARKET orders via live fetch
    slot_clear = True
    try:
        open_orders = api_call(exchange.fetch_open_orders, symbol)
        stop_orders = [e for e in open_orders
                        if str(e.get('type','')).upper() == 'STOP_MARKET']
        for order in stop_orders:
            try:
                api_call(exchange.cancel_order, order['id'], symbol)
                print(f"[SL Guncelle] Eski SL iptal: id={order['id']}")
            except Exception as e:
                if '-2011' in str(e):
                    # Already gone — slot is clear
                    pass
                else:
                    # Unknown error — do not place new order, might double-up
                    print(f"[SL Guncelle] Iptal hatasi, yeni SL yerlestirilmedi: {e}")
                    slot_clear = False
    except Exception as e:
        print(f"[SL Guncelle] Acik emirler alinamadi: {e}")
        slot_clear = False

    if not slot_clear:
        return

    # Step 2: Place new STOP_MARKET
    try:
        order = api_call(
            exchange.create_order,
            symbol, 'STOP_MARKET', close_side, None, None,
            params={
                'stopPrice':     new_sl_prec,
                'closePosition': True,
                'workingType':   'MARK_PRICE',
            }
        )
        pos['sl_order_id']     = order.get('id')
        pos['last_exchange_sl'] = new_sl_prec
        print(f"[SL Guncelle] Yeni SL: {symbol} {new_sl_prec} id={order.get('id')}")
    except Exception as e:
        if '-4130' in str(e):
            # An existing closePosition order is still there — fetch and store its ID
            print(f"[SL Guncelle] -4130: Mevcut order ID aliniyor...")
            try:
                emirler = api_call(exchange.fetch_open_orders, symbol)
                for e2 in emirler:
                    if str(e2.get('type','')).upper() == 'STOP_MARKET':
                        pos['sl_order_id'] = e2['id']
                        print(f"[SL Guncelle] Mevcut SL ID guncellendi: {e2['id']}")
                        break
            except Exception as e3:
                print(f"[SL Guncelle] ID kurtarma hatasi: {e3}")
        else:
            print(f"[SL Guncelle] Yeni SL yerlesirilemedi: {e}")

# ==============================================================================
# 8. LIMIT ORDER ENTRY WITH MARKET FALLBACK + SLIPPAGE GUARD
# ==============================================================================
def place_limit_order(symbol, side_ccxt, qty, referans_fiyat):
    try:
        ticker      = api_call(exchange.fetch_ticker, symbol)
        anlik_fiyat = float(ticker['last'])
        kayma       = abs(anlik_fiyat - referans_fiyat) / referans_fiyat
        if kayma > CONFIG['MAX_SLIPPAGE_PCT']:
            send_telegram(
                f"Giris atildi — kayma fazla\n"
                f"Sembol: {symbol}\n"
                f"Sinyal: {referans_fiyat:.4f} | Su an: {anlik_fiyat:.4f}\n"
                f"Kayma : %{kayma*100:.3f} (max %{CONFIG['MAX_SLIPPAGE_PCT']*100:.2f})"
            )
            return None
        limit_fiyat = float(
            ticker.get('ask' if side_ccxt == 'buy' else 'bid') or anlik_fiyat
        )
        limit_fiyat = float(exchange.price_to_precision(symbol, limit_fiyat))
    except Exception as e:
        print(f"[Limit Ticker Hatasi] {e}")
        return api_call(exchange.create_market_order, symbol, side_ccxt, qty)

    try:
        limit_emir = api_call(
            exchange.create_limit_order,
            symbol, side_ccxt, qty, limit_fiyat,
            params={'timeInForce': 'GTC'}
        )
        emir_id = limit_emir.get('id')
    except Exception as e:
        print(f"[Limit Emir Hata] {e} — market'e geciliyor")
        return api_call(exchange.create_market_order, symbol, side_ccxt, qty)

    expiry = time.time() + CONFIG['LIMIT_WAIT_SECONDS']
    while time.time() < expiry:
        time.sleep(1)
        try:
            durum = api_call(exchange.fetch_order, emir_id, symbol)
            if durum.get('status') == 'closed':
                return durum
            if durum.get('status') in ('canceled', 'rejected', 'expired'):
                break
        except Exception:
            break

    try:
        api_call(exchange.cancel_order, emir_id, symbol)
    except Exception:
        pass
    return api_call(exchange.create_market_order, symbol, side_ccxt, qty)

# ==============================================================================
# 9. MARKET DATA + BASE INDICATORS
# ==============================================================================
def fetch_ohlcv(symbol, tf, limit=200):
    bars = api_call(exchange.fetch_ohlcv, symbol, timeframe=tf, limit=limit)
    df   = pd.DataFrame(bars,
                        columns=['timestamp','open','high','low','close','volume'])
    return df

def get_btc_trend():
    try:
        df = fetch_ohlcv(CONFIG['BTC_SYMBOL'], CONFIG['BTC_TF'], limit=220)
        df['EMA200'] = EMAIndicator(close=df['close'], window=200).ema_indicator()
        df.dropna(inplace=True)
        son = df.iloc[-2]
        if son['close'] > son['EMA200']:   return 'UP'
        elif son['close'] < son['EMA200']: return 'DOWN'
        return 'NEUTRAL'
    except Exception as e:
        print(f"[BTC Trend] {e}")
        return 'NEUTRAL'

def get_htf_bias(symbol):
    try:
        df = fetch_ohlcv(symbol, CONFIG['BIAS_TF'], limit=80)
        df['EMA50'] = EMAIndicator(close=df['close'], window=50).ema_indicator()
        df['ADX']   = ADXIndicator(high=df['high'], low=df['low'],
                                   close=df['close'], window=14).adx()
        df.dropna(inplace=True)
        son = df.iloc[-2]
        if son['ADX'] > CONFIG['ADX_THRESHOLD']:
            if son['close'] > son['EMA50']:   return 'UP'
            elif son['close'] < son['EMA50']: return 'DOWN'
        return 'NEUTRAL'
    except Exception as e:
        print(f"[HTF Bias {symbol}] {e}")
        return 'NEUTRAL'

def get_funding_rate(symbol):
    try:
        fr = api_call(exchange.fetch_funding_rate, symbol)
        return float(fr.get('fundingRate', 0) or 0)
    except Exception as e:
        print(f"[Funding {symbol}] {e}")
        return 0.0

# ==============================================================================
# 10. SINGLE-SYMBOL ANALYSIS ENGINE — 8 FACTORS (no RSI, BTC as factor) [v3.9]
# ==============================================================================
def analyze_symbol(symbol, btc_trend='NEUTRAL'):
    try:
        df = fetch_ohlcv(symbol, CONFIG['ENTRY_TF'], limit=200)

        df['ADX']     = ADXIndicator(high=df['high'], low=df['low'],
                                     close=df['close'], window=14).adx()
        df['ATR']     = AverageTrueRange(high=df['high'], low=df['low'],
                                         close=df['close'], window=14
                                         ).average_true_range()
        df['VOL_MA']  = df['volume'].rolling(20).mean()

        stoch         = StochRSIIndicator(close=df['close'],
                                          window=14, smooth1=3, smooth2=3)
        df['STOCH_K'] = stoch.stochrsi_k()
        df['STOCH_D'] = stoch.stochrsi_d()

        macd_ind        = MACD(close=df['close'],
                               window_slow=26, window_fast=12, window_sign=9)
        df['MACD_HIST'] = macd_ind.macd_diff()

        df['EMA21']   = EMAIndicator(close=df['close'], window=21).ema_indicator()
        df['OBV']     = OnBalanceVolumeIndicator(close=df['close'],
                                                 volume=df['volume']
                                                 ).on_balance_volume()
        df['OBV_EMA'] = df['OBV'].ewm(span=20, adjust=False).mean()

        df.dropna(inplace=True)
        if len(df) < 4:
            return None

        son  = df.iloc[-2]
        son2 = df.iloc[-3]
        son3 = df.iloc[-4]

        price    = float(son['close'])
        atr      = float(son['ATR'])
        adx      = float(son['ADX'])
        vol      = float(son['volume'])
        vol_ma   = float(son['VOL_MA'])
        vol_oran = vol / vol_ma if vol_ma > 0 else 0
        atr_pct  = atr / price if price > 0 else 0

        stoch_k  = float(son['STOCH_K'])
        stoch_d  = float(son['STOCH_D'])
        macd_h0  = float(son['MACD_HIST'])
        macd_h1  = float(son2['MACD_HIST'])
        macd_h2  = float(son3['MACD_HIST'])
        ema21    = float(son['EMA21'])
        obv      = float(son['OBV'])
        obv_ema  = float(son['OBV_EMA'])
        mum_yukari = float(son['close']) > float(son['open'])

        volume_ok = vol_oran >= CONFIG['VOLUME_MULT']
        vol_ok   = atr_pct  >= CONFIG['MIN_ATR_PCT']

        bias    = get_htf_bias(symbol)
        funding = get_funding_rate(symbol)

        # ── Signal gate: StochRSI only. BTC trend is score factor. [v3.9] ─────
        signal = 'WAIT'
        if adx > CONFIG['ADX_THRESHOLD'] and volume_ok and vol_ok:
            if stoch_k < CONFIG['STOCH_OVERSOLD']:
                if funding <= CONFIG['MAX_FUNDING_RATE']:
                    signal = 'LONG'
            elif stoch_k > CONFIG['STOCH_OVERBOUGHT']:
                if funding >= -CONFIG['MAX_FUNDING_RATE']:
                    signal = 'SHORT'

        score      = 0.0
        macd_dir  = 'NEUTRAL'
        obv_trend = 'NEUTRAL'

        if signal != 'WAIT':
            # 1. ADX strength
            adx_norm = min(adx / 50.0, 1.0)

            # 2. Volume surge
            vol_norm = min((vol_oran - 1.0) / 3.0, 1.0) if vol_oran > 1 else 0.0

            # 3. StochRSI position + K/D crossover bonus
            if signal == 'LONG':
                stoch_norm = max(0.0, 1.0 - stoch_k * 2)
                if stoch_k > stoch_d:
                    stoch_norm = min(stoch_norm * 1.2, 1.0)
            else:
                stoch_norm = max(0.0, stoch_k * 2 - 1.0)
                if stoch_k < stoch_d:
                    stoch_norm = min(stoch_norm * 1.2, 1.0)

            # 4. MACD histogram direction (2-candle momentum)
            if signal == 'LONG':
                if macd_h0 > macd_h1 > macd_h2:
                    macd_norm = 1.0; macd_dir = 'UP-UP'
                elif macd_h0 > macd_h1:
                    macd_norm = 0.6; macd_dir = 'UP'
                else:
                    macd_norm = 0.0; macd_dir = 'DOWN'
            else:
                if macd_h0 < macd_h1 < macd_h2:
                    macd_norm = 1.0; macd_dir = 'DOWN-DOWN'
                elif macd_h0 < macd_h1:
                    macd_norm = 0.6; macd_dir = 'DOWN'
                else:
                    macd_norm = 0.0; macd_dir = 'UP'

            # 5. EMA21 gradient distance [v3.9 — replaces binary]
            ema_dist = abs(price - ema21) / (atr * 2) if atr > 0 else 0
            ema_dist = min(ema_dist, 1.0)
            if signal == 'LONG':
                ema21_norm = ema_dist if price > ema21 else 0.0
            else:
                ema21_norm = ema_dist if price < ema21 else 0.0

            # 6. OBV trend
            if signal == 'LONG':
                obv_norm  = 1.0 if obv > obv_ema else 0.0
                obv_trend = 'UP' if obv > obv_ema else 'DOWN'
            else:
                obv_norm  = 1.0 if obv < obv_ema else 0.0
                obv_trend = 'DOWN' if obv < obv_ema else 'UP'

            # 7. HTF 15m bias
            if signal == 'LONG':
                bias_norm = 1.0 if bias=='UP' else 0.5 if bias=='NEUTRAL' else 0.0
            else:
                bias_norm = 1.0 if bias=='DOWN' else 0.5 if bias=='NEUTRAL' else 0.0

            # 8. BTC trend alignment [v3.9 — was hard gate] ──────────────────
            if signal == 'LONG':
                btc_norm = 1.0 if btc_trend=='UP' else 0.5 if btc_trend=='NEUTRAL' else 0.0
            else:
                btc_norm = 1.0 if btc_trend=='DOWN' else 0.5 if btc_trend=='NEUTRAL' else 0.0

            # Funding bonus for SAT [v3.9]: high positive funding = paid to short
            funding_bonus = 0.0
            if signal == 'SHORT' and funding > CONFIG['MAX_FUNDING_RATE']:
                funding_bonus = min(funding / 0.001, 0.05)  # up to +5% score

            # Candle direction multiplier
            mum_mult = (1.10 if (signal == 'LONG' and mum_yukari) or
                        (signal == 'SHORT' and not mum_yukari) else 0.90)

            score = (
                CONFIG['SCORE_ADX_W']   * adx_norm   +
                CONFIG['SCORE_VOL_W']   * vol_norm   +
                CONFIG['SCORE_STOCH_W'] * stoch_norm +
                CONFIG['SCORE_MACD_W']  * macd_norm  +
                CONFIG['SCORE_EMA21_W'] * ema21_norm +
                CONFIG['SCORE_OBV_W']   * obv_norm   +
                CONFIG['SCORE_BIAS_W']  * bias_norm  +
                CONFIG['SCORE_BTC_W']   * btc_norm
            ) * mum_mult + funding_bonus
        else:
            macd_dir  = 'UP' if macd_h0 > macd_h1 else 'DOWN'
            obv_trend = 'UP' if obv > obv_ema else 'DOWN'

        return {
            'symbol':    symbol,   'price':    price,
            'atr':       atr,      'atr_pct':  atr_pct,
            'adx':       adx,      'bias':     bias,
            'btc_trend': btc_trend,'funding':  funding,
            'volume_ok':  volume_ok, 'vol_ok':   vol_ok,
            'vol_oran':  vol_oran,
            'stoch_k':   stoch_k,  'stoch_d':  stoch_d,
            'macd_hist': macd_h0,  'macd_dir': macd_dir,
            'ema21':     ema21,    'obv_trend': obv_trend,
            'mum_yukari':mum_yukari,
            'signal':    signal,   'score':     score,
        }
    except Exception as e:
        print(f"[Analiz Hatasi {symbol}] {e}")
        return None

# ==============================================================================
# 11. MULTI-SYMBOL SCANNER
# ==============================================================================
def scan_symbols(btc_trend):
    sonuclar = []
    for symbol in CONFIG['SYMBOL_LIST']:
        r = analyze_symbol(symbol, btc_trend)
        if r:
            sonuclar.append(r)
        time.sleep(0.3)
    sonuclar.sort(key=lambda x: x['score'], reverse=True)
    return sonuclar

def select_best_signal(sonuclar, open_symbols=None):
    """
    Pick the highest-scoring signal that:
    - Is not BEKLE
    - Is not already in an open position
    - Is not temporarily banned
    - Passes the correlation filter (no same-category if restricted)
    """
    if open_symbols is None:
        open_symbols = []
    simdi  = datetime.now()

    # Build set of currently occupied categories
    acik_kategoriler = set()
    with state_lock:
        for sym in open_symbols:
            kat = CONFIG['SYMBOL_CATEGORIES'].get(sym, 'others')
            if kat not in CONFIG['CORRELATION_FREE']:
                acik_kategoriler.add(kat)

    for r in sonuclar:
        if r['signal'] == 'WAIT' or r['score'] <= 0:
            continue
        sym = r['symbol']
        # Skip already open
        if sym in open_symbols:
            continue
        # Skip banned
        with state_lock:
            ban = symbol_ban_until.get(sym)
        if ban and simdi < ban:
            continue
        # Correlation filter [v3.9]
        kat = CONFIG['SYMBOL_CATEGORIES'].get(sym, 'others')
        if kat not in CONFIG['CORRELATION_FREE'] and kat in acik_kategoriler:
            continue
        return r
    return None

# ==============================================================================
# 12. POSITION SIZING
# ==============================================================================
def calc_position_size(symbol, price, atr, balance, leverage):
    try:
        risk_val  = balance * CONFIG['RISK_PCT']
        sl_dist   = atr * CONFIG['ATR_SL_MULT_LONG'] if atr > 0 else price * 0.01
        qty       = risk_val / sl_dist
        # Cap 1: 95% of available leveraged balance
        max_qty   = (balance * leverage * 0.95) / price
        qty       = min(qty, max_qty)
        # Cap 2: notional USD cap
        if CONFIG.get('MAX_POSITION_USD', 0) > 0:
            max_qty_usd = CONFIG['MAX_POSITION_USD'] / price
            if qty * price > CONFIG['MAX_POSITION_USD']:
                print(f"[Miktar Cap] {symbol} {qty*price:.1f}→{CONFIG['MAX_POSITION_USD']} USD")
            qty = min(qty, max_qty_usd)
        return float(exchange.amount_to_precision(symbol, qty))
    except Exception as e:
        print(f"[Miktar Hatasi] {e}")
        return 0.0

# ==============================================================================
# 13. TRAILING STOP — per-position [v3.9]
# ==============================================================================
def update_trailing_stop(price, pos):
    """Update stop_loss in pos dict. Returns (new_sl, changed)."""
    old_sl = pos['stop_loss']
    side   = pos['side']

    if side == 'LONG':
        pos['highest_price'] = max(pos['highest_price'], price)
        raw_pnl = (price - pos['entry_price']) / pos['entry_price']
        if not pos['breakeven_reached'] and raw_pnl >= CONFIG['BREAKEVEN_TRIGGER']:
            pos['breakeven_reached'] = True
            new_sl = pos['entry_price'] * 1.0002
            if new_sl > pos['stop_loss']:
                pos['stop_loss'] = new_sl
                send_telegram(
                    f"Breakeven SL — {pos.get('symbol_ref','?')}\n"
                    f"SL → {pos['stop_loss']:.4f}"
                )
        trail = pos['highest_price'] * (1 - CONFIG['TRAILING_STEP'])
        if trail > pos['stop_loss']:
            pos['stop_loss'] = trail
    else:
        pos['lowest_price'] = min(pos['lowest_price'], price)
        raw_pnl = (pos['entry_price'] - price) / pos['entry_price']
        if not pos['breakeven_reached'] and raw_pnl >= CONFIG['BREAKEVEN_TRIGGER']:
            pos['breakeven_reached'] = True
            new_sl = pos['entry_price'] * 0.9998
            if new_sl < pos['stop_loss']:
                pos['stop_loss'] = new_sl
                send_telegram(
                    f"Breakeven SL — {pos.get('symbol_ref','?')}\n"
                    f"SL → {pos['stop_loss']:.4f}"
                )
        trail = pos['lowest_price'] * (1 + CONFIG['TRAILING_STEP'])
        if trail < pos['stop_loss']:
            pos['stop_loss'] = trail

    return pos['stop_loss'], pos['stop_loss'] != old_sl

# ==============================================================================
# 14. STARTUP POSITION RECONCILIATION
# ==============================================================================
def reconcile_open_positions():
    try:
        positions = api_call(exchange.fetch_positions, CONFIG['SYMBOL_LIST'])
        for pos in positions:
            amt = float(pos.get('contracts', 0) or 0)
            if amt <= 0:
                continue
            raw_sym = pos.get('symbol', '')
            matched = next(
                (s for s in CONFIG['SYMBOL_LIST']
                 if s.replace('/', '') in raw_sym or raw_sym in s),
                raw_sym
            )
            entry = float(pos.get('entryPrice', 0) or 0)
            side  = 'LONG' if pos.get('side') == 'long' else 'SHORT'
            atr_e = entry * 0.01
            sl_mult  = (CONFIG['ATR_SL_MULT_LONG'] if side == 'LONG'
                     else CONFIG['ATR_SL_MULT_SHORT'])
            sl    = (entry - atr_e * sl_mult if side == 'LONG'
                     else entry + atr_e * sl_mult)
            lev   = int(pos.get('leverage', CONFIG['LEVERAGE']) or CONFIG['LEVERAGE'])
            pos   = {
                'side':             side,
                'entry_price':      entry,
                'qty':           amt,
                'qty_full':       amt,
                'atr':              atr_e,
                'leverage':         lev,
                'stop_loss':        sl,
                'highest_price':    entry,
                'lowest_price':     entry,
                'breakeven_reached':False,
                'partial_tp_done':  False,
                'open_time':        datetime.now(),
                'sl_order_id':      None,
                'tp_order_id':      None,
                'last_exchange_sl':  0.0,
                'symbol_ref':       matched,
            }
            with state_lock:
                active_positions[matched] = pos
            send_telegram(
                f"Mevcut pozisyon tespit edildi\n"
                f"Sembol: {matched} | Yon: {side}\n"
                f"Giris : {entry:.4f} | Miktar: {amt}\n"
                f"SL    : {sl:.4f}"
            )
    except Exception as e:
        print(f"[Pozisyon Kontrol] {e}")

# ==============================================================================
# 15. BACKTESTING (updated: StochRSI gate, asymmetric TP/SL)
# ==============================================================================
def run_backtest(symbol, tf, limit=300):
    try:
        bars = api_call(exchange.fetch_ohlcv, symbol, timeframe=tf, limit=limit)
        df   = pd.DataFrame(bars,
                            columns=['timestamp','open','high','low','close','volume'])
        df['ADX']     = ADXIndicator(high=df['high'], low=df['low'],
                                     close=df['close'], window=14).adx()
        df['ATR']     = AverageTrueRange(high=df['high'], low=df['low'],
                                         close=df['close'], window=14
                                         ).average_true_range()
        df['VOL_MA']  = df['volume'].rolling(20).mean()
        stoch         = StochRSIIndicator(close=df['close'],
                                          window=14, smooth1=3, smooth2=3)
        df['STOCH_K'] = stoch.stochrsi_k()
        df['STOCH_D'] = stoch.stochrsi_d()
        macd          = MACD(close=df['close'],
                             window_slow=26, window_fast=12, window_sign=9)
        df['MACD_HIST'] = macd.macd_diff()
        df.dropna(inplace=True)
        df.reset_index(drop=True, inplace=True)

        trades  = []
        in_pos  = False
        entry_p = sl_p = tp_p = 0.0
        side    = ''
        equity  = 100.0
        peak    = 100.0
        max_dd  = 0.0
        lev     = CONFIG['LEVERAGE']

        for i in range(2, len(df) - 1):
            row  = df.iloc[i]
            price = float(row['close'])
            hok   = row['volume'] >= row['VOL_MA'] * CONFIG['VOLUME_MULT']
            vok   = (row['ATR'] / price) >= CONFIG['MIN_ATR_PCT']
            s_k   = float(row['STOCH_K'])

            if not in_pos:
                if row['ADX'] > CONFIG['ADX_THRESHOLD'] and hok and vok:
                    if s_k < CONFIG['STOCH_OVERSOLD']:
                        in_pos  = True; side = 'LONG'
                        entry_p = price
                        sl_p    = price - float(row['ATR']) * CONFIG['ATR_SL_MULT_LONG']
                        tp_p    = price * (1 + calc_tp_rate('LONG', lev))
                    elif s_k > CONFIG['STOCH_OVERBOUGHT']:
                        in_pos  = True; side = 'SHORT'
                        entry_p = price
                        sl_p    = price + float(row['ATR']) * CONFIG['ATR_SL_MULT_SHORT']
                        tp_p    = price * (1 - calc_tp_rate('SHORT', lev))
            else:
                nxt   = df.iloc[i + 1]
                is_sl = ((side=='LONG' and float(nxt['low'])<=sl_p) or
                         (side=='SHORT' and float(nxt['high'])>=sl_p))
                is_tp = ((side=='LONG' and float(nxt['high'])>=tp_p) or
                         (side=='SHORT' and float(nxt['low'])<=tp_p))
                if is_sl or is_tp:
                    exit_p  = tp_p if is_tp else sl_p
                    reason  = 'TP' if is_tp else 'SL'
                    raw_pnl = ((exit_p - entry_p)/entry_p if side=='LONG'
                               else (entry_p - exit_p)/entry_p)
                    notional = equity * lev
                    gross    = raw_pnl * notional
                    fee      = notional * CONFIG['TAKER_FEE'] * 2
                    net      = gross - fee
                    equity  += net
                    peak     = max(peak, equity)
                    dd       = (peak - equity) / peak * 100
                    max_dd   = max(max_dd, dd)
                    trades.append({'net': net, 'reason': reason})
                    in_pos = False

        if not trades:
            return f"<b>Backtest [{symbol}]:</b> Sinyal olusumadi."

        df_t = pd.DataFrame(trades)
        wins  = df_t[df_t['net'] > 0]
        losses  = df_t[df_t['net'] <= 0]
        wr   = len(wins) / len(df_t) * 100
        rr   = (abs(wins['net'].mean() / losses['net'].mean())
                if not losses.empty and losses['net'].mean() != 0 else 0)
        ret  = (equity - 100) / 100 * 100
        return (
            f"<b>Backtest — {symbol} ({tf})</b>\n"
            f"Islem : {len(df_t)} | Kaz:{len(wins)} | Kay:{len(losses)}\n"
            f"Win Rate      : <b>%{wr:.1f}</b>\n"
            f"Risk/Odul     : <b>{rr:.2f}</b>\n"
            f"Toplam Getiri : <b>%{ret:.2f}</b>\n"
            f"Maks Drawdown : <b>%{max_dd:.2f}</b>"
        )
    except Exception as e:
        return f"Backtest hatasi ({symbol}): {e}"

# ==============================================================================
# 16. MAIN TRADE LOOP — Multi-position architecture [v3.9]
# ==============================================================================
def trade_loop():
    global bot_active, last_report_time, last_scan_results
    global symbol_loss_count, symbol_ban_until
    global loss_cooldown_until

    print("[AKTIF] Multi-pozisyon scan dongusu baslatildi.")
    HEARTBEAT_SANIYE = CONFIG['HEARTBEAT_MINUTES'] * 60
    sync_counter   = 0

    while True:
        with state_lock:
            _active = bot_active
        if not _active:
            time.sleep(30)
            continue

        try:
            current_balance = get_balance()
            if current_balance == 0:
                time.sleep(CONFIG['LOOP_SLEEP'])
                continue

            if not check_daily_loss():
                time.sleep(CONFIG['LOOP_SLEEP'])
                continue

            # ── STEP A: Scan all symbols ──────────────────────────────────────
            btc_trend = get_btc_trend()
            scan    = scan_symbols(btc_trend)
            with state_lock:
                last_scan_results = scan

            # ── STEP B: Heartbeat ─────────────────────────────────────────────
            with state_lock:
                _rapor = last_report_time
            if (datetime.now() - _rapor).seconds >= HEARTBEAT_SANIYE:
                with state_lock:
                    last_report_time = datetime.now()
                    _positions  = dict(active_positions)
                    _bans    = {k: v for k, v in symbol_ban_until.items()
                                if v > datetime.now()}

                top3 = [r for r in scan if r['signal'] != 'WAIT'][:3]
                top3_str = "".join(
                    f"  {r['symbol']} Skor:{r['score']:.2f} "
                    f"{r['signal']} MACD:{r['macd_dir']}\n"
                    for r in top3
                ) or "  Sinyal yok\n"
                ban_str = ("Banli: " + ", ".join(
                    f"{s}({int((v-datetime.now()).seconds/60)}dk)"
                    for s, v in _bans.items()
                ) + "\n") if _bans else ""
                poz_str = "\n".join(
                    f"  {sym} {p['side']} {p['leverage']}x "
                    f"entry:{p['entry_price']:.4f}"
                    for sym, p in _positions.items()
                ) or "  YOK"

                send_telegram(
                    f"<b>Periyodik Rapor ({datetime.now().strftime('%H:%M')})</b>\n"
                    f"BTC: {btc_trend} | Bakiye: <b>{current_balance:.2f} USDT</b>\n"
                    f"{ban_str}"
                    f"Pozisyonlar ({len(_positions)}):\n{poz_str}\n"
                    f"<b>En Iyi Sinyaller:</b>\n{top3_str}"
                )

            # ── STEP C: Open new position(s) if capacity allows ───────────────
            with state_lock:
                _positions = dict(active_positions)
                _cooldown = loss_cooldown_until

            num_poz       = len(_positions)
            total_margin  = sum(
                p['qty'] * p['entry_price'] / p['leverage']
                for p in _positions.values()
            )
            max_margin    = current_balance * CONFIG['MAX_TOTAL_MARGIN_PCT']
            cooldown_aktif = _cooldown and datetime.now() < _cooldown

            if (num_poz < CONFIG['MAX_CONCURRENT_POSITIONS']
                    and total_margin < max_margin
                    and not cooldown_aktif):

                open_symbols = list(_positions.keys())
                best = select_best_signal(scan, open_symbols)

                if best:
                    symbol = best['symbol']
                    signal = best['signal']
                    price  = best['price']
                    atr    = best['atr']
                    lev    = calc_dynamic_leverage(best['adx'])
                    qty = calc_position_size(
                        symbol, price, atr, current_balance, lev)

                    if qty > 0:
                        # Set leverage for this trade
                        try:
                            api_call(exchange.set_leverage, lev, symbol)
                        except Exception as e:
                            print(f"[Leverage Set] {e}")

                        side_ccxt = 'buy' if signal == 'LONG' else 'sell'

                        if CONFIG['LIMIT_ORDER']:
                            order = place_limit_order(
                                symbol, side_ccxt, qty, price)
                        else:
                            order = api_call(
                                exchange.create_market_order,
                                symbol, side_ccxt, qty)

                        if order is None:
                            time.sleep(CONFIG['LOOP_SLEEP'])
                            continue

                        entry_p = float(order.get('average') or price)
                        sl_mult    = (CONFIG['ATR_SL_MULT_LONG'] if signal == 'LONG'
                                   else CONFIG['ATR_SL_MULT_SHORT'])
                        sl      = (entry_p - atr * sl_mult if signal == 'LONG'
                                   else entry_p + atr * sl_mult)
                        tp_rate = calc_tp_rate(signal, lev)
                        tp1     = (entry_p * (1 + tp_rate) if signal == 'LONG'
                                   else entry_p * (1 - tp_rate))

                        pos = {
                            'side':             signal,
                            'entry_price':      entry_p,
                            'qty':           qty,
                            'qty_full':       qty,
                            'atr':              atr,
                            'leverage':         lev,
                            'stop_loss':        sl,
                            'highest_price':    entry_p,
                            'lowest_price':     entry_p,
                            'breakeven_reached':False,
                            'partial_tp_done':  False,
                            'open_time':        datetime.now(),
                            'sl_order_id':      None,
                            'tp_order_id':      None,
                            'last_exchange_sl':  0.0,
                            'symbol_ref':       symbol,
                        }

                        if CONFIG['BRACKET_ORDERS']:
                            place_bracket_orders(symbol, signal, sl, tp1, pos)

                        with state_lock:
                            active_positions[symbol] = pos

                        volume_usd = round(qty * entry_p, 2)
                        fee     = round(calc_fee(volume_usd), 4)
                        slippage  = abs(entry_p - price) / price * 100
                        slip_str  = (f"\nSlippage : %{slippage:.3f}"
                                     if slippage > 0.1 else "")
                        tp_desc = (
                            f"ROI %{CONFIG['TP_ROI_TARGET' if signal=='LONG' else 'TP_ROI_TARGET_SHORT']*100:.0f} ({tp1:.4f})"
                            if CONFIG['TP_ROI_MODE']
                            else f"%{CONFIG['TP_RATE']*100:.1f} ({tp1:.4f})"
                        )
                        others = [r['symbol'] for r in scan
                                 if r['signal'] != 'WAIT'
                                 and r['symbol'] != symbol]
                        other_str = ", ".join(others[:3]) or "Yok"

                        log_trade({
                            'Time':         datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'Symbol':        symbol,
                            'Status':         'OPENED',
                            'Side':           signal,
                            'Volume_USD':     volume_usd,
                            'Price':         round(entry_p, 4),
                            'PnL_Pct_lev': '-',
                            'PnL_USD_gross':  '-',
                            'Fee_USD':     fee,
                            'PnL_USD_net':   '-',
                            'Close_Reason':'-',
                        }, current_balance)

                        send_telegram(
                            f"<b>POZISYON ACILDI — {signal}</b>\n"
                            f"Sembol   : <b>{symbol}</b>\n"
                            f"Skor     : {best['score']:.3f}\n"
                            f"ADX:{best['adx']:.1f}  "
                            f"StochK:{best['stoch_k']*100:.0f}%\n"
                            f"MACD:{best['macd_dir']}  "
                            f"OBV:{best['obv_trend']}  "
                            f"Bias:{best['bias']}\n"
                            f"Giris    : {entry_p:.4f}\n"
                            f"Miktar   : {qty} ({volume_usd} USDT)\n"
                            f"Kaldirac : {lev}x (dinamik)\n"
                            f"SL       : {sl:.4f} ({sl_mult}x ATR)\n"
                            f"TP       : {tp_desc}\n"
                            f"BTC Trend: {btc_trend} | Bias: {best['bias']}\n"
                            f"Ucret est: {fee} USDT\n"
                            f"Diger sin: {other_str}"
                            f"{slip_str}\n"
                            f"Bracket  : {'AKTIF' if CONFIG['BRACKET_ORDERS'] else 'KAPALI'}\n"
                            f"Toplam pos: {len(active_positions)}/{CONFIG['MAX_CONCURRENT_POSITIONS']}"
                        )

            # ── STEP D: Manage all open positions ─────────────────────────────
            sync_counter += 1

            with state_lock:
                semboller = list(active_positions.keys())

            for symbol in semboller:
                with state_lock:
                    if symbol not in active_positions:
                        continue
                    pos = active_positions[symbol]

                # Periodic exchange sync
                if sync_counter % CONFIG['POSITION_SYNC_INTERVAL'] == 0:
                    try:
                        pos_check     = api_call(exchange.fetch_positions, [symbol])
                        still_open   = any(
                            float(p.get('contracts', 0) or 0) > 0
                            for p in pos_check
                        )
                        if not still_open:
                            if CONFIG['BRACKET_ORDERS']:
                                cancel_bracket_orders(symbol, pos)
                            with state_lock:
                                active_positions.pop(symbol, None)
                            send_telegram(
                                f"Pozisyon harici kapatildi — {symbol}\n"
                                f"Manuel kapama veya likidasyon.\n"
                                f"Bot scan devam ediyor."
                            )
                            continue
                    except Exception as e:
                        print(f"[Sync Hatasi {symbol}] {e}")

                # Max hold time check [v3.9]
                duration_min = (datetime.now() - pos['open_time']).seconds / 60
                if duration_min >= CONFIG['MAX_POSITION_HOURS'] * 60:
                    send_telegram(
                        f"MAX SURE — {symbol}\n"
                        f"{CONFIG['MAX_POSITION_HOURS']}s sure doldu, "
                        f"pozisyon kapatiliyor."
                    )
                    _close_thread([symbol])
                    continue

                price = get_price(symbol)
                if price == 0:
                    continue

                # Trailing stop
                with state_lock:
                    prev_sl    = pos['stop_loss']
                    new_sl, sl_degisti = update_trailing_stop(price, pos)

                if sl_degisti and CONFIG['BRACKET_ORDERS']:
                    with state_lock:
                        update_sl_order(symbol, pos['side'], new_sl, pos)

                with state_lock:
                    _side    = pos['side']
                    _entry   = pos['entry_price']
                    _qty  = pos['qty']
                    _lev     = pos['leverage']
                    _sl      = pos['stop_loss']
                    _partial = pos['partial_tp_done']

                raw_pnl      = (
                    (price - _entry) / _entry if _side == 'LONG'
                    else (_entry - price) / _entry
                )
                lev_pnl_pct  = raw_pnl * _lev * 100
                pnl_usd_gross = (
                    _qty * (price - _entry) if _side == 'LONG'
                    else _qty * (_entry - price)
                )
                notional     = _qty * _entry
                fee        = calc_fee(notional)
                pnl_usd_net  = pnl_usd_gross - fee
                tp_rate      = calc_tp_rate(_side, _lev)
                tp2_rate     = CONFIG['TP2_ROI_TARGET'] / _lev

                is_sl   = ((_side == 'LONG' and price <= _sl) or
                           (_side == 'SHORT' and price >= _sl))
                is_tp1  = (not _partial and raw_pnl >= tp_rate)
                is_tp2  = (_partial and raw_pnl >= tp2_rate)

                # ── Partial TP1 [v3.9] ────────────────────────────────────────
                if is_tp1 and CONFIG['PARTIAL_TP_ACTIVE'] and not _partial:
                    partial_qty = float(
                        exchange.amount_to_precision(
                            symbol,
                            _qty * CONFIG['PARTIAL_TP_PCT']
                        )
                    )
                    remaining_qty = float(
                        exchange.amount_to_precision(
                            symbol,
                            _qty * (1 - CONFIG['PARTIAL_TP_PCT'])
                        )
                    )
                    exit_side = 'sell' if _side == 'LONG' else 'buy'
                    try:
                        # Cancel existing bracket orders
                        if CONFIG['BRACKET_ORDERS']:
                            with state_lock:
                                cancel_bracket_orders(symbol, pos)
                        # Close partial
                        api_call(
                            exchange.create_market_order,
                            symbol, exit_side, partial_qty,
                            params={'reduceOnly': True}
                        )
                        # Update position state
                        with state_lock:
                            if symbol in active_positions:
                                active_positions[symbol]['qty']         = remaining_qty
                                active_positions[symbol]['partial_tp_done'] = True
                        # Place new bracket for remaining at TP2
                        if CONFIG['BRACKET_ORDERS'] and remaining_qty > 0:
                            tp2_price = (price * (1 + tp2_rate) if _side == 'LONG'
                                         else price * (1 - tp2_rate))
                            with state_lock:
                                pos2 = active_positions.get(symbol, {})
                            if pos2:
                                place_bracket_orders(
                                    symbol, _side, _sl, tp2_price, pos2)

                        partial_pnl = partial_qty * (price - _entry if _side == 'LONG'
                                                     else _entry - price)
                        new_balance = get_balance()
                        log_trade({
                            'Time':         datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'Symbol':        symbol,
                            'Status':         'CLOSED',
                            'Side':           _side,
                            'Volume_USD':     round(partial_qty * price, 2),
                            'Price':         round(price, 4),
                            'PnL_Pct_lev': round(lev_pnl_pct, 2),
                            'PnL_USD_gross':  round(partial_pnl, 4),
                            'Fee_USD':     round(calc_fee(partial_qty*price), 4),
                            'PnL_USD_net':   round(partial_pnl - calc_fee(partial_qty*price), 4),
                            'Close_Reason':'PARTIAL_TP1',
                        }, new_balance)
                        send_telegram(
                            f"<b>KISMI TP1 — {symbol}</b>\n"
                            f"%{CONFIG['PARTIAL_TP_PCT']*100:.0f} kapatildi "
                            f"({partial_qty} adet)\n"
                            f"Kalan %{(1-CONFIG['PARTIAL_TP_PCT'])*100:.0f}: "
                            f"{remaining_qty} adet TP2 icin bekliyor\n"
                            f"ROI su an: <b>%{lev_pnl_pct:.2f}</b>\n"
                            f"PnL (kismi): {partial_pnl:+.4f} USDT"
                        )
                    except Exception as e:
                        send_telegram(
                            f"Kismi TP hatasi — {symbol}: {str(e)[:80]}")
                    continue

                # ── Full close (SL or TP2 or TP if no partial) ───────────────
                if is_sl or is_tp2 or (is_tp1 and not CONFIG['PARTIAL_TP_ACTIVE']):
                    reason     = ("TP2" if is_tp2 else
                                 "TP"  if is_tp1 else "SL")
                    neden_str = ("TAKE PROFIT 2" if is_tp2 else
                                 "TAKE PROFIT"   if is_tp1 else "STOP LOSS")
                    exit_side = 'sell' if _side == 'LONG' else 'buy'

                    if CONFIG['BRACKET_ORDERS']:
                        with state_lock:
                            cancel_bracket_orders(symbol, pos)

                    try:
                        pos_check2  = api_call(exchange.fetch_positions, [symbol])
                        still_open  = any(
                            float(p.get('contracts', 0) or 0) > 0
                            for p in pos_check2
                        )
                    except Exception:
                        still_open = True

                    if still_open:
                        api_call(
                            exchange.create_market_order,
                            symbol, exit_side, _qty,
                            params={'reduceOnly': True}
                        )

                    new_balance = get_balance()
                    log_trade({
                        'Time':         datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'Symbol':        symbol,
                        'Status':         'CLOSED',
                        'Side':           _side,
                        'Volume_USD':     round(_qty * price, 2),
                        'Price':         round(price, 4),
                        'PnL_Pct_lev': round(lev_pnl_pct, 2),
                        'PnL_USD_gross':  round(pnl_usd_gross, 4),
                        'Fee_USD':     round(fee, 4),
                        'PnL_USD_net':   round(pnl_usd_net, 4),
                        'Close_Reason':reason,
                    }, new_balance)

                    send_telegram(
                        f"<b>POZISYON KAPANDI — {neden_str}</b>\n"
                        f"Sembol   : <b>{symbol}</b> | {_side} {_lev}x\n"
                        f"Giris    : {_entry:.4f}\n"
                        f"Cikis    : {price:.4f}\n"
                        f"Ham PnL  : %{raw_pnl*100:.2f}\n"
                        f"Lev ROI  : <b>%{lev_pnl_pct:.2f}</b>\n"
                        f"Net PnL  : <b>{pnl_usd_net:+.4f} USDT</b>\n"
                        f"Yeni Bak.: <b>{new_balance:.2f} USDT</b>\n"
                        f"Acik pos.: {len(active_positions)-1}/{CONFIG['MAX_CONCURRENT_POSITIONS']}"
                    )

                    # Per-symbol ban logic
                    if reason == 'SL':
                        with state_lock:
                            cnt = symbol_loss_count.get(symbol, 0) + 1
                            symbol_loss_count[symbol] = cnt
                            if cnt >= CONFIG['MAX_CONSECUTIVE_LOSSES']:
                                expiry = datetime.now() + timedelta(
                                    minutes=CONFIG['SYMBOL_BAN_MINUTES'])
                                symbol_ban_until[symbol]   = expiry
                                symbol_loss_count[symbol] = 0
                        if cnt >= CONFIG['MAX_CONSECUTIVE_LOSSES']:
                            send_telegram(
                                f"{symbol} gecici banlandi\n"
                                f"Sebep: {CONFIG['MAX_CONSECUTIVE_LOSSES']} ardisik SL\n"
                                f"Ban expiry: {expiry.strftime('%H:%M:%S')}"
                            )
                        # Time-based cooldown [v3.9]
                        if CONFIG['LOSS_COOLDOWN_MINUTES'] > 0:
                            with state_lock:
                                loss_cooldown_until = (
                                    datetime.now() + timedelta(
                                        minutes=CONFIG['LOSS_COOLDOWN_MINUTES'])
                                )
                    else:
                        with state_lock:
                            symbol_loss_count[symbol] = 0

                    with state_lock:
                        active_positions.pop(symbol, None)

            time.sleep(CONFIG['LOOP_SLEEP'])

        except Exception as e:
            print(f"[Dongu Hatasi] {e}")
            send_telegram(f"<b>Dongu Hatasi:</b>\n{e}")
            time.sleep(CONFIG['LOOP_SLEEP'])

# ==============================================================================
# 17. ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Multi-Symbol Bot v3.9')
    parser.add_argument('--backtest-only', action='store_true')
    args = parser.parse_args()

    try:
        print("[BASLATILIYOR] Piyasalar yukleniyor...")
        markets = api_call(exchange.load_markets)

        # Validate watchlist — remove any symbol not found in this exchange/mode
        gecersiz = [s for s in CONFIG['SYMBOL_LIST']
                    if s not in markets and
                    s not in markets and s.replace('/', '') not in markets]
        if gecersiz:
            for s in gecersiz:
                CONFIG['SYMBOL_LIST'].remove(s)
                CONFIG['SYMBOL_CATEGORIES'].pop(s, None)
            uyari_str = ', '.join(gecersiz)
            print(f"[UYARI] Bu semboller bu modda mevcut degil, kaldirildi: {uyari_str}")
            send_telegram(
                f"Sembol uyarisi\n"
                f"Su semboller bu exchange/modda bulunamadi ve listeden kaldirildi:\n"
                f"{uyari_str}\n"
                f"Kalan: {len(CONFIG['SYMBOL_LIST'])} symbol"
            )
        print(f"[OK] {len(CONFIG['SYMBOL_LIST'])} symbol aktif")

        if args.backtest_only:
            for sym in CONFIG['SYMBOL_LIST']:
                print(run_backtest(sym, CONFIG['ENTRY_TF'], limit=300))
                print()
            sys.exit(0)

        # Set leverage
        failed = []
        for symbol in CONFIG['SYMBOL_LIST']:
            try:
                api_call(exchange.set_leverage, CONFIG['LEVERAGE'], symbol)
                print(f"[OK] {CONFIG['LEVERAGE']}x → {symbol}")
            except Exception as e:
                print(f"[UYARI] {symbol}: {e}")
                failed.append(symbol)
        if failed:
            send_telegram(
                f"Kaldirac uyarisi:\n{', '.join(failed)}\nElle kontrol edin.")

        reconcile_open_positions()

        with state_lock:
            start_balance = get_balance()
            _start_bal = start_balance

        bt = run_backtest(CONFIG['SYMBOL_LIST'][0],
                               CONFIG['ENTRY_TF'], limit=200)
        sembol_str = "\n".join(f"  {s}" for s in CONFIG['SYMBOL_LIST'])

        send_telegram(
            f"<b>Bot Baslatildi — v3.9</b>\n"
            f"Tarama listesi ({len(CONFIG['SYMBOL_LIST'])} symbol):\n"
            f"{sembol_str}\n"
            f"Max pozisyon   : {CONFIG['MAX_CONCURRENT_POSITIONS']}\n"
            f"Max marjin     : %{CONFIG['MAX_TOTAL_MARGIN_PCT']*100:.0f}\n"
            f"Kaldirac       : {CONFIG['LEVERAGE']}x baz "
            f"(max {CONFIG['MAX_LEVERAGE']}x dinamik)\n"
            f"Risk/islem     : %{CONFIG['RISK_PCT']*100:.0f}\n"
            f"Gunluk SL      : %{CONFIG['DAILY_MAX_LOSS']*100:.0f}\n"
            f"TP AL/SAT      : ROI %{CONFIG['TP_ROI_TARGET']*100:.0f} / "
            f"%{CONFIG['TP_ROI_TARGET_SHORT']*100:.0f}\n"
            f"Kismi TP       : {'AKTIF' if CONFIG['PARTIAL_TP_ACTIVE'] else 'KAPALI'}\n"
            f"Max sure       : {CONFIG['MAX_POSITION_HOURS']}h\n"
            f"Skor motoru    : 8 faktor (RSI kaldirildi, BTC eklendi)\n"
            f"Bakiye         : <b>{_start_bal:.2f} USDT</b>\n"
            f"{bt}",
            reply_markup=main_keyboard()
        )

        t_thread = threading.Thread(target=trade_loop, daemon=True)
        t_thread.start()

        print("[OK] Telegram polling baslatildi. CTRL+C ile dur.")
        _kapatiliyor = False
        while not _kapatiliyor:
            try:
                t_bot.infinity_polling(timeout=30, long_polling_timeout=25,
                                       restart_on_change=False,
                                       allowed_updates=None)
            except KeyboardInterrupt:
                _kapatiliyor = True
            except Exception as e:
                if _kapatiliyor:
                    break
                if 'break' in str(e).lower() or 'interrupt' in str(e).lower():
                    _kapatiliyor = True
                else:
                    print(f"[Polling yeniden] {e}")
                    time.sleep(5)

    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        print("\n[DURDURULDU] Bot kapatildi.")
        try:
            send_telegram("Bot kapatildi.")
        except Exception:
            pass