from flask import Flask, render_template, jsonify, request
import json
import ccxt
import os

app = Flask(__name__)
# Grafik verileri için bağımsız, public bir borsa objesi oluşturuyoruz
# API key gerektirmez, sadece mum verisi okumak içindir.
exchange = ccxt.binance({'enableRateLimit': True})

@app.route('/')
def index():
    # Frontend HTML dosyasını render et
    return render_template('index.html')

@app.route('/api/state')
def get_state():
    # Botun ürettiği JSON dosyasını oku ve arayüze gönder
    try:
        if os.path.exists('bot_state.json'):
            with open('bot_state.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                return jsonify(data)
        else:
            return jsonify({"error": "bot_state.json henüz oluşturulmadı. Botun çalışmasını bekleyin."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/klines')
def get_klines():
    # Arayüzdeki grafik için TradingView formatında mum (OHLCV) verisi sağla
    symbol = request.args.get('symbol', 'BTC/USDT')
    timeframe = request.args.get('timeframe', '5m')
    try:
        # Son 150 mumu çek
        klines = exchange.fetch_ohlcv(symbol, timeframe, limit=150)
        # TradingView Lightweight Charts formatına dönüştür
        formatted_data = [
            {
                "time": int(k[0] / 1000), # Saniye cinsinden timestamp
                "open": k[1],
                "high": k[2],
                "low": k[3],
                "close": k[4]
            } for k in klines
        ]
        return jsonify(formatted_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("Dashboard başlatılıyor... Tarayıcıda http://127.0.0.1:5000 adresine gidin.")
    app.run(port=5000, threaded=True, debug=False)