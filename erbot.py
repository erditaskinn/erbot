"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         MULTI-SYMBOL FUTURES SCANNER + TRADING BOT  —  v3.6               ║
║                                                                              ║
║  New in v3.6 (full audit + consistency pass):                               ║
║  [A]  5 new indicators added to scoring engine                              ║
║         StochRSI (K/D), MACD histogram, EMA21, OBV, Candle direction       ║
║  [B]  Scoring rebuilt: 7 factors + candle multiplier, weights rebalanced    ║
║  [C]  Per-symbol consecutive-SL ban (2 losses → 30min cooldown)            ║
║  [D]  Score jitter to prevent same symbol winning every scan                ║
║  [E]  RSI thresholds tightened (38→35 / 62→65)                             ║
║  [F]  /durum shows active symbol bans                                       ║
║  [G]  /piyasa shows all 7 indicator values                                  ║
║                                                                              ║
║  Carried from v3.2 (all fixes intact):                                      ║
║  Polling restart loop, fetch_positions symbol filter, backtest SL price,   ║
║  /tara fresh results, thread lock, health check session bypass,             ║
║  backtest rate limit sleep, /piyasa best symbol                             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import ccxt
import pandas as pd
import csv
import os
import sys
import telebot
import threading
import argparse
import random
from telebot import types
from datetime import datetime, timedelta, timezone
from ta.trend import ADXIndicator, EMAIndicator, MACD
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator, StochRSIIndicator
from ta.volume import OnBalanceVolumeIndicator
from dotenv import load_dotenv
import time

load_dotenv()

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
CONFIG = {
    # ── Credentials ───────────────────────────────────────────────────────────
    'API_KEY':           os.environ.get('BINANCE_API_KEY',    'YOUR_API_KEY_HERE'),
    'SECRET_KEY':        os.environ.get('BINANCE_SECRET_KEY', 'YOUR_SECRET_KEY_HERE'),
    'TELEGRAM_TOKEN':    os.environ.get('TELEGRAM_TOKEN',     'YOUR_TELEGRAM_TOKEN_HERE'),
    'CHAT_ID':           os.environ.get('TELEGRAM_CHAT_ID',   'YOUR_CHAT_ID_HERE'),

    # ── Watchlist ─────────────────────────────────────────────────────────────
    'SEMBOL_LISTESI': [
        'HYPE/USDT',
        'SOL/USDT',
        'ETH/USDT',
        'BNB/USDT',
        'AVAX/USDT',
        'DOGE/USDT',
        'LINK/USDT',
        'ARB/USDT',
    ],
    'BTC_SEMBOL': 'BTC/USDT',

    # ── Timeframes ────────────────────────────────────────────────────────────
    # ENTRY_TF : candle size for signal detection and indicator calculation.
    #            '5m'  = ~4-12 signals/day across 8 symbols (recommended)
    #            '3m'  = more signals, more noise
    #            '15m' = fewer signals, higher quality — swing trading style
    # BIAS_TF  : higher timeframe trend confirmation. Should be 3x ENTRY_TF.
    #            '15m' pairs with '5m' entry (recommended)
    #            '1h'  pairs with '15m' entry
    # BTC_TF   : macro BTC trend. '1h' is fine for all entry timeframes.
    'LEVERAGE':   5,
    'ENTRY_TF':   '5m',    # <-- change here to adjust signal timeframe
    'BIAS_TF':    '15m',   # <-- should be 3x ENTRY_TF
    'BTC_TF':     '1h',

    # ── Risk Management ───────────────────────────────────────────────────────
    # RISK_ORANI      : % of account risked per trade.
    #                   0.01 = 1%  (conservative, safe for testing)
    #                   0.02 = 2%  (moderate — recommended after demo testing)
    #                   0.03 = 3%  (aggressive — only if win rate above 50%)
    #                   Never go above 0.03 with 5x leverage.
    #
    # GUNLUK_MAX_ZARAR: daily loss limit as fraction of account.
    #                   0.05 = 5%  (stops bot after 5% daily loss)
    #
    # TP MODES — two options, only one is active at a time:
    # ─────────────────────────────────────────────────────
    # Option A: Price-move TP (TP_ROI_MOD = False)
    #   TP_ORANI = raw asset price move required to close.
    #   0.02 = 2% price move.  With 5x leverage → 10% ROI.
    #   Formula: ROI = TP_ORANI × LEVERAGE
    #   Inconsistent: changing leverage changes your actual return.
    #
    # Option B: ROI-based TP (TP_ROI_MOD = True)  ← RECOMMENDED
    #   TP_ROI_HEDEF = target return ON MARGIN (what you actually care about).
    #   0.10 = 10% ROI on margin regardless of leverage.
    #   The bot converts this to the correct price move automatically.
    #   Formula: price_move_needed = TP_ROI_HEDEF / LEVERAGE
    #   Consistent: changing leverage never changes your target return.
    # ─────────────────────────────────────────────────────
    'TP_ROI_MOD':       True,   # True = ROI-based (recommended) | False = price-move
    'TP_ROI_HEDEF':     0.10,   # ROI target per trade: 0.10 = 10% on margin
    'TP_ORANI':         0.02,   # Used only when TP_ROI_MOD = False
    #
    # ATR_SL_CARPAN   : stop loss distance in ATR multiples.
    #                   1.5 = tight (faster SL, less room for noise)
    #                   2.0 = loose (more room, larger loss if SL hit)
    'RISK_ORANI':       0.02,
    'GUNLUK_MAX_ZARAR': 0.05,
    'ATR_SL_CARPAN':    1.5,
    'BREAKEVEN_TETIK':  0.008,
    'TRAILING_ADIM':    0.005,

    # ── Signal Hard Gates ─────────────────────────────────────────────────────
    # STOCH_ASIRI_SATIS: StochRSI K gate for AL (replaces slow RSI gate).
    #   0.20 = K<20% required (recommended) | 0.25 = more signals
    # STOCH_ASIRI_ALIS : StochRSI K gate for SAT.
    #   0.80 = K>80% required (recommended) | 0.75 = more signals
    # RSI values below are SCORE contributors only, not hard gates.
    'ADX_ESIK':          22,
    'STOCH_ASIRI_SATIS': 0.20,
    'STOCH_ASIRI_ALIS':  0.80,
    'RSI_ASIRI_SATIS':   40,
    'RSI_ASIRI_ALIS':    60,
    'HACIM_CARPAN':      1.5,
    'MIN_ATR_YUZDE':     0.003,
    'MAX_FUNDING_RATE':  0.0003,

    # ── Scoring Weights (7 factors + candle multiplier) [New A/B] ─────────────
    # All weights must sum to 1.0
    'SKOR_ADX_W':   0.18,   # trend strength
    'SKOR_RSI_W':   0.12,   # RSI extremity (reduced — StochRSI now covers this better)
    'SKOR_VOL_W':   0.12,   # volume surge
    'SKOR_STOCH_W': 0.16,   # StochRSI K position + K>D crossover bonus
    'SKOR_MACD_W':  0.18,   # MACD histogram direction (2-candle momentum)
    'SKOR_EMA21_W': 0.12,   # price position relative to EMA21
    'SKOR_OBV_W':   0.12,   # OBV vs its EMA20 (volume quality)
    # Candle direction multiplier: x1.10 if confirms signal, x0.90 if opposes

    # ── Per-Symbol Ban [New C] ─────────────────────────────────────────────────
    'MAX_AYNI_SEMBOL_KAYIP': 2,    # consecutive SLs before ban
    'SEMBOL_BAN_DAKIKA':     30,   # ban duration in minutes

    # ── Score Jitter [New D] ───────────────────────────────────────────────────
    # Adds ±JITTER_PCT random noise to break ties (prevents HYPE always winning)
    'SKOR_JITTER_PCT': 0.03,   # ±3% of score

    # ── Session Filter (UTC) ──────────────────────────────────────────────────
    # Crypto trades 24/7 so this is OFF by default.
    # Set True + adjust hours if you want to restrict to liquid hours only.
    'SESSION_BASLANGIC': 13,
    'SESSION_BITIS':     22,
    'SESSION_FILTRE':    False,   # False = trade 24/7 (recommended for crypto)

    # ── Fees ──────────────────────────────────────────────────────────────────
    'TAKER_FEE': 0.0004,

    # ── System ────────────────────────────────────────────────────────────────
    'LOG_FILE':          'trade_log.csv',
    'HEARTBEAT_DAKIKA':  5,
    'LOOP_UYKU':         15,
    'GIRIS_COOLDOWN':    3,
    'MAX_RETRY':         5,
    # POZ_SYNC_ARALIK: how often (in loop ticks) to verify position still
    # exists on the exchange. Catches manual closes, liquidations, etc.
    # 4 ticks × 15s = every 60 seconds. Lower = more API calls.
    'POZ_SYNC_ARALIK':   4,
}

# Validate weights sum to 1.0
_weight_sum = (CONFIG['SKOR_ADX_W'] + CONFIG['SKOR_RSI_W'] +
               CONFIG['SKOR_VOL_W'] + CONFIG['SKOR_STOCH_W'] +
               CONFIG['SKOR_MACD_W'] + CONFIG['SKOR_EMA21_W'] +
               CONFIG['SKOR_OBV_W'])
assert abs(_weight_sum - 1.0) < 0.001, f"Scoring weights must sum to 1.0, got {_weight_sum}"

# ==============================================================================
# 2. GLOBAL STATE + THREAD LOCK
# ==============================================================================
state_lock = threading.Lock()

bot_aktif_mi          = True
in_position           = False
active_side           = ""
active_sembol         = ""
entry_price           = 0.0
active_miktar         = 0.0
stop_loss             = 0.0
highest_price         = 0.0
lowest_price          = 0.0
breakeven_reached     = False
baslangic_bakiye      = 0.0
son_rapor_zamani      = datetime.now()
kayip_sonrasi_bekleme = 0
son_tarama_sonuclari  = []

# Per-symbol ban state [New C]
sembol_kayip_sayac = {}   # {'HYPE/USDT': 2, ...}  consecutive SL count
sembol_ban_bitis   = {}   # {'HYPE/USDT': datetime(...)}  ban expiry

# Position sync counter — increments each loop tick while in_position
poz_sync_sayac = 0

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
# 4. EXPONENTIAL BACKOFF API WRAPPER
# ==============================================================================
def api_cagir(fn, *args, **kwargs):
    bekleme = 10
    for deneme in range(CONFIG['MAX_RETRY']):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            if deneme == CONFIG['MAX_RETRY'] - 1:
                telegram_mesaj_gonder(
                    f"API Baglanti Hatasi (5 deneme basarisiz):\n{e}"
                )
                raise
            print(f"[API Retry {deneme+1}] {e} — {bekleme}s bekleniyor")
            time.sleep(bekleme)
            bekleme *= 2
        except ccxt.RateLimitExceeded:
            time.sleep(60)
        except Exception:
            raise

# ==============================================================================
# 5. TELEGRAM BOT
# ==============================================================================
t_bot = telebot.TeleBot(CONFIG['TELEGRAM_TOKEN'])

def telegram_mesaj_gonder(mesaj, reply_markup=None):
    try:
        t_bot.send_message(
            CONFIG['CHAT_ID'], mesaj,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"[Telegram Hatasi] {e}")

def ana_klavye():
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
    )
    return kb

# ── /start ────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['start'])
@t_bot.message_handler(func=lambda m: m.text == "Baslat")
def cmd_start(message):
    global bot_aktif_mi
    with state_lock:
        bot_aktif_mi = True
    telegram_mesaj_gonder(
        "Bot aktif. Tarama ve islem dongusu calisiyor.\n"
        "/yardim — tum komutlar",
        reply_markup=ana_klavye()
    )

# ── /dur ──────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['dur'])
@t_bot.message_handler(func=lambda m: m.text == "Durdur")
def cmd_dur(message):
    global bot_aktif_mi
    with state_lock:
        bot_aktif_mi = False
    telegram_mesaj_gonder(
        "Bot duraklatildi.\n"
        "Acik pozisyon (varsa) korunuyor.\n"
        "Devam icin /start",
        reply_markup=ana_klavye()
    )

# ── /durum ────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['durum'])
@t_bot.message_handler(func=lambda m: m.text == "Durum")
def cmd_durum(message):
    with state_lock:
        _in_pos  = in_position
        _side    = active_side
        _sembol  = active_sembol
        _entry   = entry_price
        _sl      = stop_loss
        _miktar  = active_miktar
        _be      = breakeven_reached
        _aktif   = bot_aktif_mi
        _bans    = {k: v for k, v in sembol_ban_bitis.items()
                    if v > datetime.now()}

    fiyat     = son_fiyat_al(_sembol) if _sembol else 0.0
    bal       = get_balance()
    durum_str = "AKTIF" if _aktif else "DURDURULDU"

    if _in_pos and fiyat > 0:
        raw_pct = (
            (fiyat - _entry) / _entry if _side == 'AL'
            else (_entry - fiyat) / _entry
        )
        lev_pct = raw_pct * CONFIG['LEVERAGE'] * 100
        pnl_usd = (
            _miktar * (fiyat - _entry) if _side == 'AL'
            else _miktar * (_entry - fiyat)
        )
        be_str  = "Evet" if _be else "Hayir"
        poz_str = (
            f"\nAcik Pozisyon: {_side} — {_sembol}\n"
            f"  Giris     : {_entry:.4f}\n"
            f"  Su an     : {fiyat:.4f}\n"
            f"  Stop Loss : {_sl:.4f}\n"
            f"  Breakeven : {be_str}\n"
            f"  ROI       : %{lev_pct:.2f} ({pnl_usd:+.2f} USDT)\n"
            f"  TP hedef  : %{tp_orani_hesapla()*CONFIG['LEVERAGE']*100:.1f} ROI "
            f"({_entry*(1+tp_orani_hesapla()) if _side=='AL' else _entry*(1-tp_orani_hesapla()):.4f})"
        )
    else:
        poz_str = "\nAcik Pozisyon: YOK"

    # Show active bans [New F]
    ban_str = ""
    if _bans:
        ban_str = "\n\n<b>Gecici Banli Semboller:</b>\n"
        for sym, bitis in _bans.items():
            kalan = int((bitis - datetime.now()).seconds / 60)
            ban_str += f"  {sym} — {kalan}dk kaldi\n"

    telegram_mesaj_gonder(
        f"<b>Bot Durum Raporu — v3.6</b>\n"
        f"Durum   : {durum_str}\n"
        f"Kaldirac: {CONFIG['LEVERAGE']}x\n"
        f"Aktif s.: {_sembol if _sembol else '-'}\n"
        f"Bakiye  : <b>{bal:.2f} USDT</b>"
        f"{poz_str}{ban_str}"
    )

# ── /bakiye ───────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['bakiye'])
@t_bot.message_handler(func=lambda m: m.text == "Bakiye")
def cmd_bakiye(message):
    bal = get_balance()
    with state_lock:
        _bas = baslangic_bakiye
    kazanc = bal - _bas
    pct    = (kazanc / _bas * 100) if _bas > 0 else 0
    telegram_mesaj_gonder(
        f"<b>Cuzdan Durumu</b>\n"
        f"Mevcut    : <b>{bal:.2f} USDT</b>\n"
        f"Baslangic : {_bas:.2f} USDT\n"
        f"Net       : <b>{kazanc:+.2f} USDT (%{pct:+.2f})</b>"
    )

# ── /pnl ──────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['pnl'])
@t_bot.message_handler(func=lambda m: m.text == "PnL")
def cmd_pnl(message):
    try:
        if not os.path.isfile(CONFIG['LOG_FILE']):
            telegram_mesaj_gonder("Henuz tamamlanmis islem yok.")
            return
        df = pd.read_csv(
            CONFIG['LOG_FILE'], delimiter=';',
            on_bad_lines='skip', engine='python'
        )
        df['Zaman'] = pd.to_datetime(df['Zaman'])
        kapandi = df[df['Durum'] == 'KAPANDI'].copy()
        kapandi['PnL_USD_net'] = pd.to_numeric(
            kapandi['PnL_USD_net'], errors='coerce'
        )
        bugun = kapandi[
            kapandi['Zaman'] > datetime.now().replace(
                hour=0, minute=0, second=0)
        ]
        hafta = kapandi[
            kapandi['Zaman'] > (datetime.now() - timedelta(days=7))
        ]

        def ozet(sub):
            if sub.empty:
                return "Islem yok"
            pnl = sub['PnL_USD_net'].sum()
            cnt = len(sub)
            kaz = len(sub[sub['PnL_USD_net'] > 0])
            ort = sub['PnL_USD_net'].mean()
            wr  = kaz / cnt * 100 if cnt > 0 else 0
            return (
                f"<b>{pnl:+.2f} USDT</b> | "
                f"{cnt} islem | Win: %{wr:.0f} | Ort: {ort:+.2f}"
            )

        sembol_ozet = ""
        if not kapandi.empty and 'Sembol' in kapandi.columns:
            for sym, grp in kapandi.groupby('Sembol'):
                sym_pnl = grp['PnL_USD_net'].sum()
                sembol_ozet += (
                    f"  {sym}: {sym_pnl:+.2f} USDT "
                    f"({len(grp)} islem)\n"
                )

        telegram_mesaj_gonder(
            f"<b>PnL Ozeti (Ucret Dahil)</b>\n"
            f"Bugun : {ozet(bugun)}\n"
            f"7 Gun : {ozet(hafta)}\n"
            f"Tumu  : {ozet(kapandi)}\n"
            f"<b>Sembole Gore:</b>\n{sembol_ozet}"
        )
    except Exception as e:
        telegram_mesaj_gonder(f"PnL hatasi: {e}")

# ── /son ──────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['son'])
@t_bot.message_handler(func=lambda m: m.text == "Son Islemler")
def cmd_son(message):
    try:
        if not os.path.isfile(CONFIG['LOG_FILE']):
            telegram_mesaj_gonder("Kayitli islem yok.")
            return
        df = pd.read_csv(
            CONFIG['LOG_FILE'], delimiter=';',
            on_bad_lines='skip', engine='python'
        )
        kapandi = df[df['Durum'] == 'KAPANDI'].tail(5)
        if kapandi.empty:
            telegram_mesaj_gonder("Tamamlanmis islem bulunamadi.")
            return
        mesaj = "<b>Son 5 Kapali Islem</b>\n"
        for _, row in kapandi.iloc[::-1].iterrows():
            try:
                pnl_net = float(row['PnL_USD_net'])
                pnl_pct = float(row['PnL_Yuzde_lev'])
                sebep   = row.get('Kapanma_Sebebi', '?')
            except Exception:
                pnl_net, pnl_pct, sebep = 0, 0, "?"
            mesaj += (
                f"{row.get('Sembol','?')} {row.get('Yon','?')} "
                f"[{sebep}] {str(row.get('Zaman',''))[:16]}\n"
                f"  PnL: <b>{pnl_net:+.2f} USDT (%{pnl_pct:.2f})</b>\n"
            )
        telegram_mesaj_gonder(mesaj)
    except Exception as e:
        telegram_mesaj_gonder(f"Son islem hatasi: {e}")

# ── /tara ─────────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['tara'])
@t_bot.message_handler(func=lambda m: m.text == "Tarama")
def cmd_tara(message):
    with state_lock:
        cache = list(son_tarama_sonuclari)
        bans  = {k: v for k, v in sembol_ban_bitis.items()
                 if v > datetime.now()}

    if cache:
        _gonder_tarama_mesaji(cache, bans,
                              baslik="Onceki Tarama (yenileniyor...)")
    else:
        telegram_mesaj_gonder("Tarama baslatiliyor, lutfen bekleyin...")

    threading.Thread(target=_tara_thread, daemon=True).start()

def _gonder_tarama_mesaji(sonuclar, bans=None, baslik="Tarama Sonuclari"):
    bans = bans or {}
    mesaj = f"<b>{baslik}</b>\n"
    for r in sonuclar:
        ban_tag = ""
        if r['sembol'] in bans:
            kalan  = int((bans[r['sembol']] - datetime.now()).seconds / 60)
            ban_tag = f" [BAN {kalan}dk]"
        mesaj += (
            f"{r['sembol']}{ban_tag}  "
            f"Skor:<b>{r['skor']:.2f}</b>  {r['sinyal']}\n"
            f"  ADX:{r['adx']:.1f} RSI:{r['rsi']:.1f} "
            f"StochK:{r['stoch_k']*100:.0f} "
            f"MACD:{r['macd_dir']} "
            f"OBV:{r['obv_trend']}\n"
        )
    mesaj += f"\nGuncellendi: {datetime.now().strftime('%H:%M:%S')}"
    telegram_mesaj_gonder(mesaj)

def _tara_thread():
    global son_tarama_sonuclari
    try:
        btc_trend = btc_trend_al()
        sonuclar  = sembol_tara(btc_trend)
        with state_lock:
            son_tarama_sonuclari = sonuclar
            bans = {k: v for k, v in sembol_ban_bitis.items()
                    if v > datetime.now()}
        _gonder_tarama_mesaji(
            sonuclar, bans,
            baslik="Guncel Tarama Sonuclari"
        )
    except Exception as e:
        print(f"[Tarama Thread] {e}")

# ── /piyasa — shows all 7 indicator values [New G] ───────────────────────────
@t_bot.message_handler(commands=['piyasa'])
@t_bot.message_handler(func=lambda m: m.text == "Piyasa")
def cmd_piyasa(message):
    with state_lock:
        _in_pos = in_position
        _sembol = active_sembol
        _cache  = list(son_tarama_sonuclari)

    if _in_pos and _sembol:
        hedef     = _sembol
        baslik_ek = "(aktif pozisyon)"
    else:
        en_iyi = next(
            (r for r in _cache if r['sinyal'] != 'BEKLE'), None
        )
        if en_iyi:
            hedef     = en_iyi['sembol']
            baslik_ek = f"(en yuksek skor: {en_iyi['skor']:.2f})"
        else:
            hedef     = CONFIG['SEMBOL_LISTESI'][0]
            baslik_ek = "(varsayilan, sinyal yok)"

    telegram_mesaj_gonder(f"{hedef} analizi cekiliyor {baslik_ek}...")
    try:
        r = sembol_analiz_et(hedef)
        if not r:
            telegram_mesaj_gonder("Analiz basarisiz.")
            return
        def eb(v): return "EVET" if v else "HAYIR"
        mum_str  = "YUKARI (yesil)" if r['mum_yukari'] else "ASAGI (kirmizi)"
        _tp_oran = tp_orani_hesapla()
        _tp_al   = r['fiyat'] * (1 + _tp_oran)
        _tp_sat  = r['fiyat'] * (1 - _tp_oran)
        _sl_al   = r['fiyat'] - r['atr'] * CONFIG['ATR_SL_CARPAN']
        _sl_sat  = r['fiyat'] + r['atr'] * CONFIG['ATR_SL_CARPAN']
        if CONFIG['TP_ROI_MOD']:
            tp_label = (f"ROI hedef %{CONFIG['TP_ROI_HEDEF']*100:.0f} → "
                        f"AL:{_tp_al:.4f}  SAT:{_tp_sat:.4f}")
        else:
            tp_label = (f"Fiyat %{CONFIG['TP_ORANI']*100:.1f} → "
                        f"AL:{_tp_al:.4f}  SAT:{_tp_sat:.4f}")
        stoch_gate = ("AL KAPI GECTI" if r['stoch_k'] < CONFIG['STOCH_ASIRI_SATIS']
                      else "SAT KAPI GECTI" if r['stoch_k'] > CONFIG['STOCH_ASIRI_ALIS']
                      else f"KAPI GECMEDI (AL icin K&lt;{CONFIG['STOCH_ASIRI_SATIS']*100:.0f}%)")
        telegram_mesaj_gonder(
            f"<b>Detayli Analiz — {hedef}</b>\n"
            f"Fiyat       : {r['fiyat']:.4f}\n"
            f"ATR         : {r['atr']:.4f} (%{r['atr_pct']*100:.2f})\n"
            f"\n<b>— Gosterge Degerleri —</b>\n"
            f"ADX         : {r['adx']:.1f}\n"
            f"RSI (5m)    : {r['rsi']:.1f} (skor katkilisi)\n"
            f"StochRSI K  : {r['stoch_k']*100:.1f}% → {stoch_gate}\n"
            f"StochRSI D  : {r['stoch_d']*100:.1f}%\n"
            f"MACD Hist.  : {r['macd_dir']} ({r['macd_hist']:+.6f})\n"
            f"EMA21 (5m)  : {r['ema21']:.4f} "
            f"({'Fiyat Ustu' if r['fiyat'] > r['ema21'] else 'Fiyat Alti'})\n"
            f"OBV Trend   : {r['obv_trend']}\n"
            f"Mum Yonu    : {mum_str}\n"
            f"\n<b>— Seviyeler —</b>\n"
            f"TP          : {tp_label}\n"
            f"SL (ATR)    : AL:{_sl_al:.4f}  SAT:{_sl_sat:.4f}\n"
            f"\n<b>— Filtreler —</b>\n"
            f"Bias (15m)  : {r['bias']}\n"
            f"BTC Trendi  : {r['btc_trend']}\n"
            f"Funding     : {r['funding']*100:.4f}%\n"
            f"Vol Oran    : {r['vol_oran']:.2f}x\n"
            f"Hacim onay  : {eb(r['hacim_ok'])}\n"
            f"Volatilite  : {eb(r['vol_ok'])}\n"
            f"Seans       : {eb(r['session_ok'])}\n"
            f"\n<b>Sinyal Skoru : {r['skor']:.3f}</b>\n"
            f"<b>Sinyal       : {r['sinyal']}</b>"
        )
    except Exception as e:
        telegram_mesaj_gonder(f"Piyasa analiz hatasi: {e}")

# ── /backtest ─────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['backtest'])
@t_bot.message_handler(func=lambda m: m.text == "Backtest")
def cmd_backtest(message):
    telegram_mesaj_gonder(
        f"Backtest baslatildi...\n"
        f"Tum semboller ({len(CONFIG['SEMBOL_LISTESI'])} adet), "
        f"300 mum. Bekleyin."
    )
    threading.Thread(target=_backtest_thread, daemon=True).start()

def _backtest_thread():
    for sembol in CONFIG['SEMBOL_LISTESI']:
        try:
            sonuc = backtest_calistir(sembol, CONFIG['ENTRY_TF'], limit=300)
            telegram_mesaj_gonder(sonuc)
            time.sleep(3)
        except Exception as e:
            telegram_mesaj_gonder(f"Backtest hatasi ({sembol}): {e}")

# ── /saglik ───────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['saglik'])
@t_bot.message_handler(func=lambda m: m.text == "Saglik Kontrol")
def cmd_saglik(message):
    telegram_mesaj_gonder(
        "Sistem kontrol ediliyor, lutfen bekleyin (~15 saniye)..."
    )
    threading.Thread(target=_saglik_thread, daemon=True).start()

def _saglik_thread():
    satirlar     = []
    hata_sayisi  = 0
    uyari_sayisi = 0

    def ok(etiket, deger=""):
        satirlar.append("OK    " + etiket + (f": {deger}" if deger else ""))

    def hata(etiket, deger=""):
        nonlocal hata_sayisi
        hata_sayisi += 1
        satirlar.append("HATA  " + etiket + (f": {deger}" if deger else ""))

    def uyari(etiket, deger=""):
        nonlocal uyari_sayisi
        uyari_sayisi += 1
        satirlar.append("UYARI " + etiket + (f": {deger}" if deger else ""))

    def bolum(baslik):
        satirlar.append("")
        satirlar.append(f"[ {baslik} ]")

    satirlar.append("=== SISTEM SAGLIK RAPORU ===")
    satirlar.append(f"Zaman: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    bolum("API BAGLANTISI")
    try:
        t0 = time.time()
        exchange.fetch_time()
        ms = int((time.time() - t0) * 1000)
        if ms < 500: ok("API baglantisi", f"{ms}ms")
        else:        uyari("API yavas", f"{ms}ms")
    except Exception as e:
        hata("API baglantisi", str(e)[:60])

    bolum("BAKIYE")
    try:
        t0  = time.time()
        bal = get_balance()
        ms  = int((time.time() - t0) * 1000)
        if bal > 0: ok("Bakiye okundu", f"{bal:.2f} USDT ({ms}ms)")
        else:       uyari("Bakiye sifir", "Demo hesap bos olabilir")
    except Exception as e:
        hata("Bakiye okunamadi", str(e)[:60])

    bolum("PIYASA VERISI + INDIKTORLER")
    test_sembol = CONFIG['SEMBOL_LISTESI'][0]
    try:
        t0   = time.time()
        r    = sembol_analiz_et(test_sembol, bypass_session=True)
        ms   = int((time.time() - t0) * 1000)
        if r:
            ok("ADX",      f"{r['adx']:.1f}")
            ok("RSI",      f"{r['rsi']:.1f} (skor katkilisi, kapi degil)")
            stk = r['stoch_k'] * 100
            std = r['stoch_d'] * 100
            stoch_durum = "ASIRI SATIS" if r['stoch_k'] < CONFIG['STOCH_ASIRI_SATIS']                           else "ASIRI ALIS" if r['stoch_k'] > CONFIG['STOCH_ASIRI_ALIS']                           else "NOTR"
            ok("StochRSI K/D", f"K:{stk:.1f}% D:{std:.1f}% → {stoch_durum}")
            ok("Stoch AL esik", f"K &lt; {CONFIG['STOCH_ASIRI_SATIS']*100:.0f}%  "
                                f"(simdi: {'GECTI' if r['stoch_k'] < CONFIG['STOCH_ASIRI_SATIS'] else 'GECMEDI'})")
            ok("Stoch SAT esik", f"K &gt; {CONFIG['STOCH_ASIRI_ALIS']*100:.0f}%  "
                                 f"(simdi: {'GECTI' if r['stoch_k'] > CONFIG['STOCH_ASIRI_ALIS'] else 'GECMEDI'})")
            ok("MACD",     f"{r['macd_dir']} ({ms}ms)")
            ok("EMA21",    f"{r['ema21']:.4f} "
                           f"({'Fiyat ustu' if r['fiyat'] > r['ema21'] else 'Fiyat alti'})")
            ok("OBV",      r['obv_trend'])
            ok("Mum yonu", "YUKARI" if r['mum_yukari'] else "ASAGI")
            ok("Sinyal",   f"{r['sinyal']} skor:{r['skor']:.3f}")
            if r['adx'] < 10:
                uyari("ADX cok dusuk", "Trend yok")
        else:
            hata("Indiktor hesabi", "None dondu")
    except Exception as e:
        hata("Indiktor/Piyasa", str(e)[:60])

    # TP modu raporu
    bolum("TP MODU")
    if CONFIG['TP_ROI_MOD']:
        hedef_roi = CONFIG['TP_ROI_HEDEF'] * 100
        fiyat_hareketi = tp_orani_hesapla() * 100
        ok("TP Modu", f"ROI bazli (onerilen)")
        ok("ROI hedefi", f"%{hedef_roi:.1f} margin uzerinden")
        ok("Gerekli fiyat hareketi", f"%{fiyat_hareketi:.2f} "
                                     f"({CONFIG['LEVERAGE']}x kaldirac ile)")
    else:
        ok("TP Modu", "Fiyat hareketi bazli")
        ok("TP orani", f"%{CONFIG['TP_ORANI']*100:.1f} fiyat hareketi "
                       f"= %{CONFIG['TP_ORANI']*CONFIG['LEVERAGE']*100:.1f} ROI")

    bolum("BTC TREND FILTRESI")
    try:
        t0 = time.time()
        bt = btc_trend_al()
        ms = int((time.time() - t0) * 1000)
        ok("BTC 200 EMA", f"{bt} ({ms}ms)")
    except Exception as e:
        hata("BTC trend", str(e)[:60])

    bolum("HTF BIAS")
    try:
        t0   = time.time()
        bias = htf_bias_al(test_sembol)
        ms   = int((time.time() - t0) * 1000)
        ok(f"15m bias ({test_sembol})", f"{bias} ({ms}ms)")
    except Exception as e:
        hata("HTF bias", str(e)[:60])

    bolum("FUNDING RATE")
    try:
        t0      = time.time()
        funding = funding_rate_al(test_sembol)
        ms      = int((time.time() - t0) * 1000)
        f_pct   = funding * 100
        if abs(funding) > CONFIG['MAX_FUNDING_RATE']:
            uyari(f"Funding yuksek", f"%{f_pct:.4f}")
        else:
            ok(f"Funding ({test_sembol})", f"%{f_pct:.4f} ({ms}ms)")
    except Exception as e:
        hata("Funding rate", str(e)[:60])

    bolum("LOG DOSYASI")
    try:
        test_kayit = {
            'Zaman':         datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'Sembol':        'TEST', 'Durum': 'SAGLIK_TEST',
            'Yon':           '-',   'Hacim_USD': 0, 'Fiyat': 0,
            'PnL_Yuzde_lev': '-',  'PnL_USD_brut': '-',
            'Ucret_USD':     '-',  'PnL_USD_net': '-',
            'Kapanma_Sebebi':'-',
        }
        log_trade(test_kayit, 0)
        df = pd.read_csv(CONFIG['LOG_FILE'], delimiter=';',
                         on_bad_lines='skip', engine='python')
        ok("Log yazma", "Basarili")
        ok("Log okuma", f"{len(df)} kayit")
        df = df[df['Durum'] != 'SAGLIK_TEST']
        df.to_csv(CONFIG['LOG_FILE'], sep=';', index=False,
                  quoting=csv.QUOTE_ALL)
        ok("Test satiri temizlendi", "")
    except Exception as e:
        hata("Log dosyasi", str(e)[:60])

    bolum("DEVRE KESICI")
    try:
        if not os.path.isfile(CONFIG['LOG_FILE']):
            ok("Log yok", "Yeni baslar")
        else:
            df = pd.read_csv(CONFIG['LOG_FILE'], delimiter=';',
                             on_bad_lines='skip', engine='python')
            df['Zaman'] = pd.to_datetime(df['Zaman'])
            bugun = df[
                (df['Zaman'] > datetime.now().replace(
                    hour=0, minute=0, second=0)) &
                (df['Durum'] == 'KAPANDI')
            ]
            if bugun.empty:
                ok("Gunluk PnL", "Bugun islem yok")
            else:
                gnl  = pd.to_numeric(
                    bugun['PnL_USD_net'], errors='coerce').sum()
                bal  = get_balance()
                ref  = bal + abs(gnl) if bal > 0 else 1
                oran = abs(gnl) / ref * 100 if gnl < 0 else 0
                esik = CONFIG['GUNLUK_MAX_ZARAR'] * 100
                if oran >= esik:
                    hata("Devre kesici AKTIF",
                         f"Kayip %{oran:.1f} / limit %{esik:.0f}")
                elif oran > esik * 0.7:
                    uyari("Limite yaklasiliyor",
                          f"%{oran:.1f} / limit %{esik:.0f}")
                else:
                    ok("Devre kesici normal",
                       f"PnL: {gnl:+.2f} USDT")
    except Exception as e:
        hata("Devre kesici", str(e)[:60])

    bolum("SEANS FILTRESI")
    saat_utc = datetime.now(timezone.utc).hour
    if not CONFIG['SESSION_FILTRE']:
        ok("Seans filtresi", "Kapali — 24/7")
    elif CONFIG['SESSION_BASLANGIC'] <= saat_utc < CONFIG['SESSION_BITIS']:
        ok("Seans ACIK", f"{saat_utc}:xx UTC")
    else:
        if saat_utc < CONFIG['SESSION_BASLANGIC']:
            dk = (CONFIG['SESSION_BASLANGIC'] - saat_utc) * 60
        else:
            dk = (24 - saat_utc + CONFIG['SESSION_BASLANGIC']) * 60
        uyari("Seans KAPALI", f"~{dk}dk sonra acilir")

    bolum("POZISYON + BOT DURUMU")
    with state_lock:
        _in_pos = in_position
        _side   = active_side
        _sembol = active_sembol
        _entry  = entry_price
        _sl     = stop_loss
        _be     = breakeven_reached
        _aktif  = bot_aktif_mi
        _rapor  = son_rapor_zamani
        _bans   = {k: v for k, v in sembol_ban_bitis.items()
                   if v > datetime.now()}

    if _in_pos:
        f_now   = son_fiyat_al(_sembol)
        raw_pnl = (
            (f_now - _entry) / _entry if _side == 'AL'
            else (_entry - f_now) / _entry
        )
        lev_pnl = raw_pnl * CONFIG['LEVERAGE'] * 100
        ok("Acik pozisyon", f"{_sembol} {_side}")
        ok("Giris / Simdi", f"{_entry:.4f} / {f_now:.4f}")
        ok("SL", f"{_sl:.4f}")
        if lev_pnl >= 0: ok("PnL", f"+%{lev_pnl:.2f}")
        else:             uyari("PnL negatif", f"%{lev_pnl:.2f}")
    else:
        ok("Pozisyon yok", "Sinyal bekleniyor")

    if _aktif: ok("Bot dongusu", "AKTIF")
    else:      hata("Bot dongusu", "DURDURULDU")

    gecen = (datetime.now() - _rapor).seconds
    if gecen < CONFIG['HEARTBEAT_DAKIKA'] * 60 * 2:
        ok("Son heartbeat", f"{gecen}sn once")
    else:
        uyari("Heartbeat gecikti", f"{gecen}sn")

    if _bans:
        uyari("Aktif banlar", f"{len(_bans)} sembol gecici banlandi")
        for sym, bitis in _bans.items():
            kalan = int((bitis - datetime.now()).seconds / 60)
            uyari(f"  Ban: {sym}", f"{kalan}dk kaldi — "
                  f"{CONFIG['MAX_AYNI_SEMBOL_KAYIP']} ardisik SL sonrasi")
    else:
        ok("Sembol ban durumu", f"Banli sembol yok "
                                f"(kural: {CONFIG['MAX_AYNI_SEMBOL_KAYIP']} ardisik SL → "
                                f"{CONFIG['SEMBOL_BAN_DAKIKA']}dk ban)")

    bolum("IZLEME LISTESI")
    ok("Sembol sayisi", str(len(CONFIG['SEMBOL_LISTESI'])))
    for s in CONFIG['SEMBOL_LISTESI']:
        if s in exchange.markets: ok(s, "Mevcut")
        else:                     uyari(s, "Bulunamadi")

    satirlar.append("")
    satirlar.append("=" * 30)
    if hata_sayisi == 0 and uyari_sayisi == 0:
        satirlar.append("SONUC: TUM SISTEMLER NORMAL")
        satirlar.append("Bot calismaya hazir.")
    elif hata_sayisi == 0:
        satirlar.append(f"SONUC: {uyari_sayisi} UYARI, HATA YOK")
        satirlar.append("Bot calisiyor, uyarilari inceleyin.")
    else:
        satirlar.append(f"SONUC: {hata_sayisi} HATA  {uyari_sayisi} UYARI")
        satirlar.append("Hatalari duzeltmeden islem yapmayin!")

    telegram_mesaj_gonder("\n".join(satirlar))

# ── /yardim ───────────────────────────────────────────────────────────────────
@t_bot.message_handler(commands=['yardim', 'help'])
def cmd_yardim(message):
    telegram_mesaj_gonder(
        "<b>Komut Listesi — v3.6</b>\n"
        "/durum       — Bot, pozisyon ve ban durumu\n"
        "/bakiye      — Cuzdan ve net kazanc\n"
        "/pnl         — PnL ozeti\n"
        "/son         — Son 5 kapali islem\n"
        "/tara        — 8 sembolun tarama tablosu\n"
        "/piyasa      — En iyi sembolun 7 gosterge analizi\n"
        "/backtest    — Tum semboller icin backtest\n"
        "/saglik      — Tam sistem saglik kontrolu\n"
        "/dur         — Botu duraklatir\n"
        "/start       — Botu baslatir\n"
        "/yardim      — Bu menu",
        reply_markup=ana_klavye()
    )

# ==============================================================================
# 6. CORE HELPERS
# ==============================================================================

def son_fiyat_al(sembol):
    try:
        ticker = api_cagir(exchange.fetch_ticker, sembol)
        return float(ticker['last'])
    except Exception as e:
        print(f"[Fiyat Hatasi {sembol}] {e}")
        return 0.0

def get_balance():
    try:
        balance = api_cagir(exchange.fetch_balance)
        return float(balance['total'].get('USDT', 0))
    except Exception as e:
        print(f"[Bakiye Hatasi] {e}")
        return 0.0

def ucret_hesapla(notional):
    return notional * CONFIG['TAKER_FEE'] * 2

def tp_orani_hesapla():
    """
    Returns the effective TP price-move ratio based on config mode.
    ROI mode  : target_roi / leverage  (e.g. 10% ROI / 5x = 2% price move)
    Price mode: TP_ORANI directly.
    """
    if CONFIG['TP_ROI_MOD']:
        return CONFIG['TP_ROI_HEDEF'] / CONFIG['LEVERAGE']
    return CONFIG['TP_ORANI']


def gunluk_zarar_kontrol():
    global bot_aktif_mi
    if not os.path.isfile(CONFIG['LOG_FILE']):
        return True
    try:
        df = pd.read_csv(CONFIG['LOG_FILE'], delimiter=';',
                         on_bad_lines='skip', engine='python')
        df['Zaman'] = pd.to_datetime(df['Zaman'])
        bugun = df[
            (df['Zaman'] > datetime.now().replace(
                hour=0, minute=0, second=0)) &
            (df['Durum'] == 'KAPANDI')
        ]
        if bugun.empty:
            return True
        daily_pnl = pd.to_numeric(
            bugun['PnL_USD_net'], errors='coerce').sum()
        bal  = get_balance()
        ref  = bal + abs(daily_pnl) if bal > 0 else 1
        oran = abs(daily_pnl) / ref if daily_pnl < 0 else 0
        if oran >= CONFIG['GUNLUK_MAX_ZARAR']:
            with state_lock:
                bot_aktif_mi = False
            telegram_mesaj_gonder(
                f"<b>DEVRE KESICI</b>\n"
                f"Gunluk kayip limiti asildi!\n"
                f"Kayip : {daily_pnl:.2f} USDT (%{oran*100:.1f})\n"
                f"Limit : %{CONFIG['GUNLUK_MAX_ZARAR']*100:.0f}\n"
                f"Bot DURDURULDU. /start ile yeniden baslatIn."
            )
            return False
        return True
    except Exception as e:
        print(f"[Risk Kontrol] {e}")
        return True

def log_trade(data, current_balance):
    data['Bakiye_USDT'] = round(current_balance, 2)
    fieldnames = [
        'Zaman', 'Sembol', 'Durum', 'Yon', 'Hacim_USD',
        'Fiyat', 'PnL_Yuzde_lev', 'PnL_USD_brut', 'Ucret_USD',
        'PnL_USD_net', 'Kapanma_Sebebi', 'Bakiye_USDT'
    ]
    file_exists = os.path.isfile(CONFIG['LOG_FILE'])
    try:
        with open(CONFIG['LOG_FILE'], mode='a', newline='') as f:
            writer = csv.DictWriter(
                f, fieldnames=fieldnames, delimiter=';',
                quoting=csv.QUOTE_ALL, extrasaction='ignore'
            )
            if not file_exists:
                writer.writeheader()
            writer.writerow({k: data.get(k, '-') for k in fieldnames})
    except Exception as e:
        print(f"[Log Hatasi] {e}")

# ==============================================================================
# 7. MARKET DATA + BASE INDICATORS
# ==============================================================================

def ohlcv_al(sembol, tf, limit=200):
    bars = api_cagir(
        exchange.fetch_ohlcv, sembol, timeframe=tf, limit=limit
    )
    df = pd.DataFrame(
        bars,
        columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
    )
    return df

def btc_trend_al():
    try:
        df = ohlcv_al(CONFIG['BTC_SEMBOL'], CONFIG['BTC_TF'], limit=220)
        df['EMA200'] = EMAIndicator(
            close=df['close'], window=200).ema_indicator()
        df.dropna(inplace=True)
        son = df.iloc[-2]
        if son['close'] > son['EMA200']:   return 'YUKARI'
        elif son['close'] < son['EMA200']: return 'ASAGI'
        return 'NOTR'
    except Exception as e:
        print(f"[BTC Trend] {e}")
        return 'NOTR'

def htf_bias_al(sembol):
    try:
        df = ohlcv_al(sembol, CONFIG['BIAS_TF'], limit=80)
        df['EMA50'] = EMAIndicator(
            close=df['close'], window=50).ema_indicator()
        df['ADX'] = ADXIndicator(
            high=df['high'], low=df['low'],
            close=df['close'], window=14).adx()
        df.dropna(inplace=True)
        son = df.iloc[-2]
        if son['ADX'] > CONFIG['ADX_ESIK']:
            if son['close'] > son['EMA50']:   return 'YUKARI'
            elif son['close'] < son['EMA50']: return 'ASAGI'
        return 'NOTR'
    except Exception as e:
        print(f"[HTF Bias {sembol}] {e}")
        return 'NOTR'

def funding_rate_al(sembol):
    try:
        fr = api_cagir(exchange.fetch_funding_rate, sembol)
        return float(fr.get('fundingRate', 0) or 0)
    except Exception as e:
        print(f"[Funding {sembol}] {e}")
        return 0.0

# ==============================================================================
# 8. SINGLE-SYMBOL FULL ANALYSIS — 7-FACTOR SCORING ENGINE [New A/B]
# ==============================================================================

def sembol_analiz_et(sembol, btc_trend='NOTR', bypass_session=False):
    try:
        # Fetch enough candles for MACD (26+9=35 minimum, 200 is ample)
        df = ohlcv_al(sembol, CONFIG['ENTRY_TF'], limit=200)

        # ── Hard-gate indicators ──────────────────────────────────────────────
        df['ADX']    = ADXIndicator(
            high=df['high'], low=df['low'],
            close=df['close'], window=14).adx()
        df['ATR']    = AverageTrueRange(
            high=df['high'], low=df['low'],
            close=df['close'], window=14).average_true_range()
        df['RSI']    = RSIIndicator(
            close=df['close'], window=14).rsi()
        df['VOL_MA'] = df['volume'].rolling(20).mean()

        # ── Score-contributing indicators [New A] ─────────────────────────────
        # StochRSI
        stoch_ind    = StochRSIIndicator(
            close=df['close'], window=14, smooth1=3, smooth2=3)
        df['STOCH_K'] = stoch_ind.stochrsi_k()
        df['STOCH_D'] = stoch_ind.stochrsi_d()

        # MACD histogram
        macd_ind     = MACD(
            close=df['close'],
            window_slow=26, window_fast=12, window_sign=9)
        df['MACD_HIST'] = macd_ind.macd_diff()

        # EMA21 on entry timeframe
        df['EMA21']  = EMAIndicator(
            close=df['close'], window=21).ema_indicator()

        # OBV and its EMA20
        df['OBV']    = OnBalanceVolumeIndicator(
            close=df['close'],
            volume=df['volume']).on_balance_volume()
        df['OBV_EMA'] = df['OBV'].ewm(span=20, adjust=False).mean()

        df.dropna(inplace=True)
        if len(df) < 4:
            return None

        son  = df.iloc[-2]   # ANTI-REPAINTING: last closed candle
        son2 = df.iloc[-3]   # one candle before son
        son3 = df.iloc[-4]   # two candles before son

        # ── Read values ───────────────────────────────────────────────────────
        fiyat    = float(son['close'])
        atr      = float(son['ATR'])
        rsi      = float(son['RSI'])
        adx      = float(son['ADX'])
        vol      = float(son['volume'])
        vol_ma   = float(son['VOL_MA'])
        vol_oran = vol / vol_ma if vol_ma > 0 else 0
        atr_pct  = atr / fiyat if fiyat > 0 else 0

        stoch_k  = float(son['STOCH_K'])   # 0..1
        stoch_d  = float(son['STOCH_D'])   # 0..1

        macd_h0  = float(son['MACD_HIST'])
        macd_h1  = float(son2['MACD_HIST'])
        macd_h2  = float(son3['MACD_HIST'])

        ema21    = float(son['EMA21'])
        obv      = float(son['OBV'])
        obv_ema  = float(son['OBV_EMA'])

        mum_yukari = float(son['close']) > float(son['open'])

        # ── Hard gates ────────────────────────────────────────────────────────
        hacim_ok = vol_oran >= CONFIG['HACIM_CARPAN']
        vol_ok   = atr_pct  >= CONFIG['MIN_ATR_YUZDE']

        if bypass_session:
            session_ok = True
        else:
            saat_utc   = datetime.now(timezone.utc).hour
            session_ok = (
                CONFIG['SESSION_BASLANGIC'] <= saat_utc < CONFIG['SESSION_BITIS']
                if CONFIG['SESSION_FILTRE'] else True
            )

        bias    = htf_bias_al(sembol)
        funding = funding_rate_al(sembol)

        # ── Signal direction — StochRSI gate + softened BTC filter ─────────────
        # Gate: StochK (fast, sensitive) instead of RSI (slow, stays neutral).
        # BTC filter: block only if BTC strongly opposes the direction,
        #             not if it's merely trending the other way.
        sinyal = 'BEKLE'
        if adx > CONFIG['ADX_ESIK'] and hacim_ok and vol_ok and session_ok:
            if stoch_k < CONFIG['STOCH_ASIRI_SATIS']:
                if bias in ('YUKARI', 'NOTR') and btc_trend != 'ASAGI':
                    if funding <= CONFIG['MAX_FUNDING_RATE']:
                        sinyal = 'AL'
            elif stoch_k > CONFIG['STOCH_ASIRI_ALIS']:
                if bias in ('ASAGI', 'NOTR') and btc_trend != 'YUKARI':
                    if funding >= -CONFIG['MAX_FUNDING_RATE']:
                        sinyal = 'SAT'

        # ── 7-factor scoring [New B] ──────────────────────────────────────────
        skor = 0.0
        macd_dir  = "NOTR"
        obv_trend = "NOTR"

        if sinyal != 'BEKLE':
            # 1. ADX — same as before
            adx_norm = min(adx / 50.0, 1.0)

            # 2. RSI extremity
            rsi_norm = min(abs(rsi - 50) / 30.0, 1.0)

            # 3. Volume surge
            vol_norm = min((vol_oran - 1.0) / 3.0, 1.0) if vol_oran > 1 else 0.0

            # 4. StochRSI: position + K/D crossover bonus
            if sinyal == 'AL':
                # K below 0.5 = oversold territory
                stoch_norm = max(0.0, 1.0 - stoch_k * 2)
                if stoch_k > stoch_d:   # momentum turning up
                    stoch_norm = min(stoch_norm * 1.2, 1.0)
            else:   # SAT
                stoch_norm = max(0.0, stoch_k * 2 - 1.0)
                if stoch_k < stoch_d:   # momentum turning down
                    stoch_norm = min(stoch_norm * 1.2, 1.0)

            # 5. MACD histogram direction (2-candle momentum confirmation)
            if sinyal == 'AL':
                if macd_h0 > macd_h1 > macd_h2:
                    macd_norm = 1.0;  macd_dir = "YUKARI-YUKARI"
                elif macd_h0 > macd_h1:
                    macd_norm = 0.6;  macd_dir = "YUKARI"
                else:
                    macd_norm = 0.0;  macd_dir = "ASAGI"
            else:   # SAT
                if macd_h0 < macd_h1 < macd_h2:
                    macd_norm = 1.0;  macd_dir = "ASAGI-ASAGI"
                elif macd_h0 < macd_h1:
                    macd_norm = 0.6;  macd_dir = "ASAGI"
                else:
                    macd_norm = 0.0;  macd_dir = "YUKARI"

            # 6. EMA21: price position relative to EMA21
            if sinyal == 'AL':
                ema21_norm = 1.0 if fiyat > ema21 else 0.0
            else:
                ema21_norm = 1.0 if fiyat < ema21 else 0.0

            # 7. OBV trend vs its EMA20
            if sinyal == 'AL':
                obv_norm  = 1.0 if obv > obv_ema else 0.0
                obv_trend = "YUKARI" if obv > obv_ema else "ASAGI"
            else:
                obv_norm  = 1.0 if obv < obv_ema else 0.0
                obv_trend = "ASAGI" if obv < obv_ema else "YUKARI"

            # Candle direction multiplier
            if sinyal == 'AL':
                mum_mult = 1.10 if mum_yukari else 0.90
            else:
                mum_mult = 1.10 if not mum_yukari else 0.90

            skor = (
                CONFIG['SKOR_ADX_W']   * adx_norm   +
                CONFIG['SKOR_RSI_W']   * rsi_norm   +
                CONFIG['SKOR_VOL_W']   * vol_norm   +
                CONFIG['SKOR_STOCH_W'] * stoch_norm +
                CONFIG['SKOR_MACD_W']  * macd_norm  +
                CONFIG['SKOR_EMA21_W'] * ema21_norm +
                CONFIG['SKOR_OBV_W']   * obv_norm
            ) * mum_mult
        else:
            # Still compute display values for /piyasa
            macd_dir  = ("YUKARI" if macd_h0 > macd_h1 else "ASAGI")
            obv_trend = ("YUKARI" if obv > obv_ema else "ASAGI")

        return {
            'sembol':     sembol,    'fiyat':     fiyat,
            'atr':        atr,       'atr_pct':   atr_pct,
            'rsi':        rsi,       'adx':       adx,
            'vol_oran':   vol_oran,  'bias':      bias,
            'btc_trend':  btc_trend, 'funding':   funding,
            'hacim_ok':   hacim_ok,  'vol_ok':    vol_ok,
            'session_ok': session_ok,
            # New indicator values
            'stoch_k':    stoch_k,   'stoch_d':   stoch_d,
            'macd_hist':  macd_h0,   'macd_dir':  macd_dir,
            'ema21':      ema21,     'obv_trend':  obv_trend,
            'mum_yukari': mum_yukari,
            # Signal and score
            'sinyal':     sinyal,    'skor':      skor,
        }
    except Exception as e:
        print(f"[Analiz Hatasi {sembol}] {e}")
        return None

# ==============================================================================
# 9. MULTI-SYMBOL SCANNER
# ==============================================================================

def sembol_tara(btc_trend):
    sonuclar = []
    for sembol in CONFIG['SEMBOL_LISTESI']:
        r = sembol_analiz_et(sembol, btc_trend)
        if r:
            sonuclar.append(r)
        time.sleep(0.4)
    sonuclar.sort(key=lambda x: x['skor'], reverse=True)
    return sonuclar

def en_iyi_sinyal_sec(sonuclar):
    """
    Picks highest-scoring non-BEKLE signal.
    Skips temporarily banned symbols.
    Applies small score jitter to prevent the same symbol
    winning every single scan when scores are close.  [New C/D]
    """
    simdi = datetime.now()
    candidates = []

    for r in sonuclar:
        if r['sinyal'] == 'BEKLE' or r['skor'] <= 0:
            continue
        with state_lock:
            ban_bitis = sembol_ban_bitis.get(r['sembol'])
        if ban_bitis and simdi < ban_bitis:
            continue    # symbol is temporarily banned
        candidates.append(r)

    if not candidates:
        return None

    # Add small random jitter to break score ties  [New D]
    j = CONFIG['SKOR_JITTER_PCT']
    candidates.sort(
        key=lambda x: x['skor'] * (1 + random.uniform(-j, j)),
        reverse=True
    )
    return candidates[0]

# ==============================================================================
# 10. POSITION SIZE
# ==============================================================================

def miktar_hesapla(sembol, fiyat, atr, balance):
    try:
        risk_val = balance * CONFIG['RISK_ORANI']
        sl_dist  = atr * CONFIG['ATR_SL_CARPAN'] if atr > 0 else fiyat * 0.01
        qty      = risk_val / sl_dist
        max_qty  = (balance * CONFIG['LEVERAGE'] * 0.95) / fiyat
        qty      = min(qty, max_qty)
        return float(exchange.amount_to_precision(sembol, qty))
    except Exception as e:
        print(f"[Miktar Hatasi] {e}")
        return 0.0

# ==============================================================================
# 11. TRAILING STOP
# Called only from trade_loop — state_lock already held by caller
# ==============================================================================

def trailing_stop_guncelle(fiyat):
    global stop_loss, highest_price, lowest_price, breakeven_reached

    if active_side == 'AL':
        highest_price = max(highest_price, fiyat)
        raw_pnl = (fiyat - entry_price) / entry_price
        if not breakeven_reached and raw_pnl >= CONFIG['BREAKEVEN_TETIK']:
            breakeven_reached = True
            new_sl = entry_price * 1.0002
            if new_sl > stop_loss:
                stop_loss = new_sl
                telegram_mesaj_gonder(
                    f"Breakeven SL aktif! [{active_sembol}]\n"
                    f"SL -> {stop_loss:.4f}"
                )
        trail_sl = highest_price * (1 - CONFIG['TRAILING_ADIM'])
        if trail_sl > stop_loss:
            stop_loss = trail_sl

    elif active_side == 'SAT':
        lowest_price = min(lowest_price, fiyat)
        raw_pnl = (entry_price - fiyat) / entry_price
        if not breakeven_reached and raw_pnl >= CONFIG['BREAKEVEN_TETIK']:
            breakeven_reached = True
            new_sl = entry_price * 0.9998
            if new_sl < stop_loss:
                stop_loss = new_sl
                telegram_mesaj_gonder(
                    f"Breakeven SL aktif! [{active_sembol}]\n"
                    f"SL -> {stop_loss:.4f}"
                )
        trail_sl = lowest_price * (1 + CONFIG['TRAILING_ADIM'])
        if trail_sl < stop_loss:
            stop_loss = trail_sl

    return stop_loss

# ==============================================================================
# 12. POSITION RECONCILIATION
# ==============================================================================

def acik_pozisyon_kontrol():
    global in_position, active_side, active_sembol, entry_price
    global active_miktar, stop_loss, highest_price, lowest_price
    global breakeven_reached
    try:
        # CCXT perpetual futures format: 'HYPE/USDT' -> 'HYPE/USDT:USDT'
        syms = [s + ':USDT' for s in CONFIG['SEMBOL_LISTESI']]
        positions = api_cagir(exchange.fetch_positions, syms)

        for pos in positions:
            amt = float(pos.get('contracts', 0) or 0)
            if amt > 0:
                raw_sym = pos.get('symbol', '')
                matched = next(
                    (s for s in CONFIG['SEMBOL_LISTESI']
                     if s.replace('/', '') in raw_sym or raw_sym in s),
                    raw_sym
                )
                with state_lock:
                    active_sembol     = matched
                    active_side       = (
                        'AL' if pos.get('side') == 'long' else 'SAT'
                    )
                    entry_price       = float(pos.get('entryPrice', 0) or 0)
                    active_miktar     = amt
                    atr_est           = entry_price * 0.01
                    stop_loss         = (
                        entry_price - atr_est * CONFIG['ATR_SL_CARPAN']
                        if active_side == 'AL'
                        else entry_price + atr_est * CONFIG['ATR_SL_CARPAN']
                    )
                    highest_price     = entry_price
                    lowest_price      = entry_price
                    breakeven_reached = False
                    in_position       = True

                telegram_mesaj_gonder(
                    f"Mevcut pozisyon tespit edildi\n"
                    f"Sembol: {active_sembol} | Yon: {active_side}\n"
                    f"Giris : {entry_price:.4f} | Miktar: {active_miktar}\n"
                    f"Trailing SL takibi baslatildi."
                )
                return
    except Exception as e:
        print(f"[Pozisyon Kontrol] {e}")

# ==============================================================================
# 13. BACKTESTING
# ==============================================================================

def backtest_calistir(sembol, tf, limit=300):
    try:
        bars = api_cagir(exchange.fetch_ohlcv, sembol, timeframe=tf, limit=limit)
        df   = pd.DataFrame(
            bars, columns=['timestamp','open','high','low','close','volume'])

        df['ADX']     = ADXIndicator(
            high=df['high'], low=df['low'], close=df['close'], window=14).adx()
        df['ATR']     = AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close'], window=14
        ).average_true_range()
        df['RSI']     = RSIIndicator(close=df['close'], window=14).rsi()
        df['VOL_MA']  = df['volume'].rolling(20).mean()
        stoch         = StochRSIIndicator(
            close=df['close'], window=14, smooth1=3, smooth2=3)
        df['STOCH_K'] = stoch.stochrsi_k()
        df['STOCH_D'] = stoch.stochrsi_d()
        macd          = MACD(close=df['close'],
                             window_slow=26, window_fast=12, window_sign=9)
        df['MACD_HIST']  = macd.macd_diff()
        df['EMA21']      = EMAIndicator(close=df['close'], window=21).ema_indicator()
        df['OBV']        = OnBalanceVolumeIndicator(
            close=df['close'], volume=df['volume']).on_balance_volume()
        df['OBV_EMA']    = df['OBV'].ewm(span=20, adjust=False).mean()
        df.dropna(inplace=True)
        df.reset_index(drop=True, inplace=True)

        trades  = []
        in_pos  = False
        entry_p = sl_p = tp_p = 0.0
        side    = ""
        equity  = 100.0
        peak    = 100.0
        max_dd  = 0.0

        for i in range(2, len(df) - 1):
            row   = df.iloc[i]
            prev  = df.iloc[i - 1]
            prev2 = df.iloc[i - 2]
            fiyat = float(row['close'])

            if not in_pos:
                hok = row['volume'] >= row['VOL_MA'] * CONFIG['HACIM_CARPAN']
                vok = (row['ATR'] / fiyat) >= CONFIG['MIN_ATR_YUZDE']

                # Signal gate mirrors live bot: StochRSI + ADX + volume/ATR
                s_k = float(row['STOCH_K'])
                if row['ADX'] > CONFIG['ADX_ESIK'] and hok and vok:
                    if s_k < CONFIG['STOCH_ASIRI_SATIS']:
                        in_pos  = True; side = 'AL'
                        entry_p = fiyat
                        sl_p    = fiyat - float(row['ATR']) * CONFIG['ATR_SL_CARPAN']
                        tp_p    = fiyat * (1 + tp_orani_hesapla())
                    elif s_k > CONFIG['STOCH_ASIRI_ALIS']:
                        in_pos  = True; side = 'SAT'
                        entry_p = fiyat
                        sl_p    = fiyat + float(row['ATR']) * CONFIG['ATR_SL_CARPAN']
                        tp_p    = fiyat * (1 - tp_orani_hesapla())
            else:
                nxt   = df.iloc[i + 1]
                is_sl = (
                    (side == 'AL'  and float(nxt['low'])  <= sl_p) or
                    (side == 'SAT' and float(nxt['high']) >= sl_p)
                )
                is_tp = (
                    (side == 'AL'  and float(nxt['high']) >= tp_p) or
                    (side == 'SAT' and float(nxt['low'])  <= tp_p)
                )
                if is_sl or is_tp:
                    exit_price = tp_p if is_tp else sl_p
                    reason     = 'TP' if is_tp else 'SL'
                    raw_pnl    = (
                        (exit_price - entry_p) / entry_p if side == 'AL'
                        else (entry_p - exit_price) / entry_p
                    )
                    notional = equity * CONFIG['LEVERAGE']
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
            return f"<b>Backtest [{sembol}]:</b> Sinyal olusumadi."

        df_t = pd.DataFrame(trades)
        kaz  = df_t[df_t['net'] > 0]
        kay  = df_t[df_t['net'] <= 0]
        wr   = len(kaz) / len(df_t) * 100
        rr   = (
            abs(kaz['net'].mean() / kay['net'].mean())
            if not kay.empty and kay['net'].mean() != 0 else 0
        )
        ret  = (equity - 100) / 100 * 100

        return (
            f"<b>Backtest — {sembol} ({tf})</b>\n"
            f"Islem : {len(df_t)} | Kaz: {len(kaz)} | Kay: {len(kay)}\n"
            f"Win Rate      : <b>%{wr:.1f}</b>\n"
            f"Risk/Odul     : <b>{rr:.2f}</b>\n"
            f"Toplam Getiri : <b>%{ret:.2f}</b>\n"
            f"Maks Drawdown : <b>%{max_dd:.2f}</b>"
        )
    except Exception as e:
        return f"Backtest hatasi ({sembol}): {e}"

# ==============================================================================
# 14. MAIN TRADING LOOP
# ==============================================================================

def trade_loop():
    global in_position, entry_price, active_miktar, stop_loss
    global active_side, active_sembol, bot_aktif_mi, son_rapor_zamani
    global highest_price, lowest_price, breakeven_reached
    global kayip_sonrasi_bekleme, son_tarama_sonuclari
    global sembol_kayip_sayac, sembol_ban_bitis

    print("[AKTIF] Multi-symbol tarama dongusu baslatildi.")
    HEARTBEAT_SANIYE = CONFIG['HEARTBEAT_DAKIKA'] * 60

    while True:
        with state_lock:
            _aktif = bot_aktif_mi
        if not _aktif:
            time.sleep(30)
            continue

        try:
            current_balance = get_balance()
            if current_balance == 0:
                time.sleep(CONFIG['LOOP_UYKU'])
                continue

            if not gunluk_zarar_kontrol():
                time.sleep(CONFIG['LOOP_UYKU'])
                continue

            # STEP A: Scan all symbols
            btc_trend = btc_trend_al()
            tarama    = sembol_tara(btc_trend)
            with state_lock:
                son_tarama_sonuclari = tarama

            # STEP B: Heartbeat
            with state_lock:
                _rapor_zamani = son_rapor_zamani

            if (datetime.now() - _rapor_zamani).seconds >= HEARTBEAT_SANIYE:
                with state_lock:
                    son_rapor_zamani = datetime.now()
                    _in_pos  = in_position
                    _side    = active_side
                    _sembol  = active_sembol
                    _entry   = entry_price
                    _sl      = stop_loss
                    _miktar  = active_miktar
                    _bans    = {k: v for k, v in sembol_ban_bitis.items()
                                if v > datetime.now()}

                top3     = [r for r in tarama if r['sinyal'] != 'BEKLE'][:3]
                top3_str = "".join(
                    f"  {r['sembol']} Skor:{r['skor']:.2f} "
                    f"{r['sinyal']} MACD:{r['macd_dir']}\n"
                    for r in top3
                ) or "  Sinyal yok\n"

                ban_str = ""
                if _bans:
                    ban_str = "Banli: " + ", ".join(
                        f"{s}({int((v-datetime.now()).seconds/60)}dk)"
                        for s, v in _bans.items()
                    ) + "\n"

                poz_str = "YOK"
                if _in_pos:
                    f_now = son_fiyat_al(_sembol)
                    raw   = (
                        (f_now - _entry) / _entry if _side == 'AL'
                        else (_entry - f_now) / _entry
                    )
                    lev = raw * CONFIG['LEVERAGE'] * 100
                    usd = (
                        _miktar * (f_now - _entry) if _side == 'AL'
                        else _miktar * (_entry - f_now)
                    )
                    poz_str = (
                        f"{_sembol} {_side}\n"
                        f"  Giris: {_entry:.4f} | SL: {_sl:.4f}\n"
                        f"  PnL: %{lev:.2f} ({usd:+.2f} USDT)"
                    )

                telegram_mesaj_gonder(
                    f"<b>Periyodik Rapor "
                    f"({datetime.now().strftime('%H:%M')})</b>\n"
                    f"BTC: {btc_trend} | "
                    f"Bakiye: <b>{current_balance:.2f} USDT</b>\n"
                    f"{ban_str}"
                    f"Pozisyon: {poz_str}\n"
                    f"<b>En Iyi Sinyaller:</b>\n{top3_str}"
                )

            # Snapshot state for this tick
            with state_lock:
                _in_pos        = in_position
                _kayip_bekleme = kayip_sonrasi_bekleme

            # STEP C: Entry
            if not _in_pos:
                if _kayip_bekleme > 0:
                    with state_lock:
                        kayip_sonrasi_bekleme -= 1
                    time.sleep(CONFIG['LOOP_UYKU'])
                    continue

                en_iyi = en_iyi_sinyal_sec(tarama)
                if en_iyi:
                    sembol = en_iyi['sembol']
                    sinyal = en_iyi['sinyal']
                    fiyat  = en_iyi['fiyat']
                    atr    = en_iyi['atr']
                    miktar = miktar_hesapla(
                        sembol, fiyat, atr, current_balance
                    )
                    if miktar > 0:
                        side_ccxt = 'buy' if sinyal == 'AL' else 'sell'
                        emir      = api_cagir(
                            exchange.create_market_order,
                            sembol, side_ccxt, miktar
                        )
                        with state_lock:
                            in_position       = True
                            active_side       = sinyal
                            active_sembol     = sembol
                            entry_price       = float(
                                emir.get('average') or fiyat)
                            active_miktar     = miktar
                            highest_price     = entry_price
                            lowest_price      = entry_price
                            breakeven_reached = False
                            stop_loss         = (
                                entry_price - atr * CONFIG['ATR_SL_CARPAN']
                                if sinyal == 'AL'
                                else entry_price + atr * CONFIG['ATR_SL_CARPAN']
                            )
                            _entry_snap = entry_price
                            _sl_snap    = stop_loss

                        _tp_oran  = tp_orani_hesapla()
                        tp_fiyat  = (
                            _entry_snap * (1 + _tp_oran)
                            if sinyal == 'AL'
                            else _entry_snap * (1 - _tp_oran)
                        )
                        hacim_usd = round(miktar * _entry_snap, 2)
                        ucret     = round(ucret_hesapla(hacim_usd), 4)
                        slippage  = abs(_entry_snap - fiyat) / fiyat * 100
                        slip_str  = (
                            f"\nSlippage : %{slippage:.3f}"
                            if slippage > 0.1 else ""
                        )
                        diger = [
                            r['sembol'] for r in tarama
                            if r['sinyal'] != 'BEKLE'
                            and r['sembol'] != sembol
                        ]
                        diger_str = ", ".join(diger[:3]) or "Yok"

                        log_trade({
                            'Zaman':         datetime.now().strftime(
                                             '%Y-%m-%d %H:%M:%S'),
                            'Sembol':        sembol,
                            'Durum':         'ACILDI',
                            'Yon':           sinyal,
                            'Hacim_USD':     hacim_usd,
                            'Fiyat':         round(_entry_snap, 4),
                            'PnL_Yuzde_lev': '-',
                            'PnL_USD_brut':  '-',
                            'Ucret_USD':     ucret,
                            'PnL_USD_net':   '-',
                            'Kapanma_Sebebi':'-',
                        }, current_balance)

                        if CONFIG['TP_ROI_MOD']:
                            tp_aciklama = (f"ROI %{CONFIG['TP_ROI_HEDEF']*100:.0f} "
                                          f"({tp_fiyat:.4f})")
                        else:
                            tp_aciklama = (f"Fiyat %{CONFIG['TP_ORANI']*100:.1f} "
                                          f"({tp_fiyat:.4f})")
                        telegram_mesaj_gonder(
                            f"<b>POZISYON ACILDI — {sinyal}</b>\n"
                            f"Sembol   : <b>{sembol}</b>\n"
                            f"Skor     : {en_iyi['skor']:.3f}\n"
                            f"ADX:{en_iyi['adx']:.1f}  "
                            f"RSI:{en_iyi['rsi']:.1f}  "
                            f"StochK:{en_iyi['stoch_k']*100:.0f}%\n"
                            f"MACD:{en_iyi['macd_dir']}  "
                            f"OBV:{en_iyi['obv_trend']}  "
                            f"EMA21:{'USTU' if en_iyi['fiyat'] > en_iyi['ema21'] else 'ALTI'}\n"
                            f"Giris    : {_entry_snap:.4f}\n"
                            f"Miktar   : {miktar} ({hacim_usd} USDT)\n"
                            f"Kaldirac : {CONFIG['LEVERAGE']}x\n"
                            f"SL       : {_sl_snap:.4f}\n"
                            f"TP       : {tp_aciklama}\n"
                            f"Bias 15m : {en_iyi['bias']} | BTC: {btc_trend}\n"
                            f"Ucret est: {ucret} USDT\n"
                            f"Diger sin: {diger_str}"
                            f"{slip_str}"
                        )

            # STEP D: Manage open position
            else:
                with state_lock:
                    _sembol = active_sembol
                    _side   = active_side
                    _entry  = entry_price
                    _miktar = active_miktar
                    poz_sync_sayac += 1
                    _sync_sayac = poz_sync_sayac

                # ── Periodic exchange sync (detects manual closes) ────────────
                if _sync_sayac % CONFIG['POZ_SYNC_ARALIK'] == 0:
                    try:
                        syms      = [_sembol + ':USDT']
                        pozisyonlar = api_cagir(exchange.fetch_positions, syms)
                        hala_acik   = any(
                            float(p.get('contracts', 0) or 0) > 0
                            for p in pozisyonlar
                        )
                        if not hala_acik:
                            # Position closed externally (manual, liquidation, etc.)
                            with state_lock:
                                in_position       = False
                                active_side       = ""
                                active_sembol     = ""
                                entry_price       = 0.0
                                active_miktar     = 0.0
                                stop_loss         = 0.0
                                highest_price     = 0.0
                                lowest_price      = 0.0
                                breakeven_reached = False
                                poz_sync_sayac    = 0
                            telegram_mesaj_gonder(
                                f"Pozisyon harici kapatildi tespit edildi\n"
                                f"Sembol : {_sembol}\n"
                                f"Sebep  : Manuel kapama veya likidasyon\n"
                                f"Bot yeni sinyal aramaya devam ediyor."
                            )
                            time.sleep(CONFIG['LOOP_UYKU'])
                            continue
                    except Exception as e:
                        print(f"[Pozisyon Sync Hatasi] {e}")

                fiyat = son_fiyat_al(_sembol)
                if fiyat == 0:
                    time.sleep(CONFIG['LOOP_UYKU'])
                    continue

                with state_lock:
                    trailing_stop_guncelle(fiyat)
                    _sl = stop_loss

                raw_pnl      = (
                    (fiyat - _entry) / _entry if _side == 'AL'
                    else (_entry - fiyat) / _entry
                )
                lev_pnl_pct  = raw_pnl * CONFIG['LEVERAGE'] * 100
                pnl_usd_brut = (
                    _miktar * (fiyat - _entry) if _side == 'AL'
                    else _miktar * (_entry - fiyat)
                )
                notional     = _miktar * _entry
                ucret        = ucret_hesapla(notional)
                pnl_usd_net  = pnl_usd_brut - ucret

                is_sl = (
                    (_side == 'AL'  and fiyat <= _sl) or
                    (_side == 'SAT' and fiyat >= _sl)
                )
                is_tp = raw_pnl >= tp_orani_hesapla()

                if is_sl or is_tp:
                    sebep     = "TP" if is_tp else "SL"
                    neden_str = "TAKE PROFIT" if is_tp else "STOP LOSS"
                    exit_side = 'sell' if _side == 'AL' else 'buy'

                    api_cagir(
                        exchange.create_market_order,
                        _sembol, exit_side, _miktar,
                        params={'reduceOnly': True}
                    )
                    yeni_bakiye = get_balance()

                    log_trade({
                        'Zaman':         datetime.now().strftime(
                                         '%Y-%m-%d %H:%M:%S'),
                        'Sembol':        _sembol,
                        'Durum':         'KAPANDI',
                        'Yon':           _side,
                        'Hacim_USD':     round(_miktar * fiyat, 2),
                        'Fiyat':         round(fiyat, 4),
                        'PnL_Yuzde_lev': round(lev_pnl_pct, 2),
                        'PnL_USD_brut':  round(pnl_usd_brut, 4),
                        'Ucret_USD':     round(ucret, 4),
                        'PnL_USD_net':   round(pnl_usd_net, 4),
                        'Kapanma_Sebebi':sebep,
                    }, yeni_bakiye)

                    telegram_mesaj_gonder(
                        f"<b>POZISYON KAPANDI — {neden_str}</b>\n"
                        f"Sembol   : <b>{_sembol}</b> | {_side}\n"
                        f"Giris    : {_entry:.4f}\n"
                        f"Cikis    : {fiyat:.4f}\n"
                        f"Ham PnL  : %{raw_pnl*100:.2f}\n"
                        f"Lev PnL  : <b>%{lev_pnl_pct:.2f}</b>\n"
                        f"Brut PnL : {pnl_usd_brut:+.4f} USDT\n"
                        f"Ucret    : -{ucret:.4f} USDT\n"
                        f"Net PnL  : <b>{pnl_usd_net:+.4f} USDT</b>\n"
                        f"Yeni Bak.: <b>{yeni_bakiye:.2f} USDT</b>"
                    )

                    # Per-symbol ban logic [New C]
                    if sebep == 'SL':
                        with state_lock:
                            cnt = sembol_kayip_sayac.get(_sembol, 0) + 1
                            sembol_kayip_sayac[_sembol] = cnt
                            if cnt >= CONFIG['MAX_AYNI_SEMBOL_KAYIP']:
                                bitis = datetime.now() + timedelta(
                                    minutes=CONFIG['SEMBOL_BAN_DAKIKA']
                                )
                                sembol_ban_bitis[_sembol]   = bitis
                                sembol_kayip_sayac[_sembol] = 0
                        if cnt >= CONFIG['MAX_AYNI_SEMBOL_KAYIP']:
                            telegram_mesaj_gonder(
                                f"{_sembol} gecici banlandi\n"
                                f"Sebep: {CONFIG['MAX_AYNI_SEMBOL_KAYIP']} "
                                f"ardisik SL\n"
                                f"Ban bitis: "
                                f"{bitis.strftime('%H:%M:%S')}"
                            )
                    else:
                        # TP resets the loss counter
                        with state_lock:
                            sembol_kayip_sayac[_sembol] = 0

                    if pnl_usd_net < 0:
                        with state_lock:
                            kayip_sonrasi_bekleme = CONFIG['GIRIS_COOLDOWN']

                    with state_lock:
                        in_position       = False
                        active_side       = ""
                        active_sembol     = ""
                        entry_price       = 0.0
                        active_miktar     = 0.0
                        stop_loss         = 0.0
                        highest_price     = 0.0
                        lowest_price      = 0.0
                        breakeven_reached = False

            time.sleep(CONFIG['LOOP_UYKU'])

        except Exception as e:
            print(f"[Dongu Hatasi] {e}")
            telegram_mesaj_gonder(f"<b>Dongu Hatasi:</b>\n{e}")
            time.sleep(CONFIG['LOOP_UYKU'])

# ==============================================================================
# 15. ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Multi-Symbol Bot v3.6')
    parser.add_argument(
        '--backtest-only', action='store_true',
        help='Run backtest for all symbols then exit'
    )
    args = parser.parse_args()

    try:
        print("[BASLATILIYOR] Piyasalar yukleniyor...")
        api_cagir(exchange.load_markets)

        if args.backtest_only:
            for sym in CONFIG['SEMBOL_LISTESI']:
                print(backtest_calistir(sym, CONFIG['ENTRY_TF'], limit=300))
                print()
            sys.exit(0)

        # Set leverage for every symbol
        hatali = []
        for sembol in CONFIG['SEMBOL_LISTESI']:
            try:
                api_cagir(exchange.set_leverage, CONFIG['LEVERAGE'], sembol)
                print(f"[OK] Kaldirac {CONFIG['LEVERAGE']}x -> {sembol}")
            except Exception as e:
                print(f"[UYARI] {sembol}: {e}")
                hatali.append(sembol)

        if hatali:
            telegram_mesaj_gonder(
                f"Kaldirac uyarisi:\n"
                f"Ayarlanamayan: {', '.join(hatali)}\n"
                f"Elle kontrol edin."
            )

        acik_pozisyon_kontrol()

        with state_lock:
            baslangic_bakiye = get_balance()
            _bas = baslangic_bakiye

        bt_sonuc = backtest_calistir(
            CONFIG['SEMBOL_LISTESI'][0], CONFIG['ENTRY_TF'], limit=200
        )
        sembol_listesi_str = "\n".join(
            f"  - {s}" for s in CONFIG['SEMBOL_LISTESI']
        )

        telegram_mesaj_gonder(
            f"<b>Bot Baslatildi — v3.6</b>\n"
            f"Tarama listesi:\n{sembol_listesi_str}\n"
            f"Kaldirac   : {CONFIG['LEVERAGE']}x\n"
            f"Risk/islem : %{CONFIG['RISK_ORANI']*100:.0f}\n"
            f"Gunluk SL  : %{CONFIG['GUNLUK_MAX_ZARAR']*100:.0f}\n"
            f"TP hedef   : %{CONFIG['TP_ORANI']*100:.1f}\n"
            f"Sinyal esik: StochK &lt;{int(CONFIG['STOCH_ASIRI_SATIS']*100)}% AL | "
            f"StochK &gt;{int(CONFIG['STOCH_ASIRI_ALIS']*100)}% SAT\n"
            f"Skor motor : 7 faktor + mum carpan\n"
            f"Zaman cer. : {CONFIG['ENTRY_TF']} + "
            f"{CONFIG['BIAS_TF']} bias\n"
            f"Bakiye     : <b>{_bas:.2f} USDT</b>\n"
            f"{bt_sonuc}",
            reply_markup=ana_klavye()
        )

        t_thread = threading.Thread(target=trade_loop, daemon=True)
        t_thread.start()

        print("[OK] Telegram polling baslatildi. Cikmak icin CTRL+C.")

        # Restart polling on connection reset (Windows error 10054).
        # KeyboardInterrupt is caught separately to allow clean shutdown
        # without spamming "Break infinity polling" to the terminal.
        _kapatiliyor = False
        while not _kapatiliyor:
            try:
                t_bot.infinity_polling(
                    timeout=30,
                    long_polling_timeout=25,
                    restart_on_change=False,
                    allowed_updates=None,
                )
            except KeyboardInterrupt:
                _kapatiliyor = True
            except Exception as e:
                if _kapatiliyor:
                    break
                err_str = str(e).lower()
                if 'break' in err_str or 'interrupt' in err_str:
                    _kapatiliyor = True
                else:
                    print(f"[Polling yeniden baslatiliyor] {e}")
                    time.sleep(5)

    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        print("\n[DURDURULDU] Bot kapatildi.")
        try:
            telegram_mesaj_gonder("Bot kapatildi.")
        except Exception:
            pass