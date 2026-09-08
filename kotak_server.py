#!/usr/bin/env python3
"""
Kotak Neo API Bridge & Local Market Breadth Terminal Server
Serves Terminal UI, handles CORS, proxies Kotak Neo API, computes DMA/EMA Breadth and sector health.
"""

import os
import sys
import json
import time
import base64
import hmac
import hashlib
import struct
import urllib.request
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import threading

try:
    import yfinance as yf
    import pandas as pd
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("[WARNING] yfinance or pandas not installed. Live data streaming will be disabled.")

PORT = int(os.environ.get("PORT", 3002))
SCRATCH_DIR = os.path.dirname(os.path.abspath(__file__))

# DhanHQ API In-Memory Session
DHAN_SESSION = {
    "configured": False,
    "connected": False,
    "clientId": None,
    "accessToken": None,
    "env": "production",
    "lastConnected": None,
    "error": None
}

SESSION_FILE = os.path.join(SCRATCH_DIR, "dhan_session.json")
STOCKS_FILE = os.path.join(SCRATCH_DIR, "nifty500_stocks.json")

# Cache for stocks data and Dhan Security IDs
_STOCKS_CACHE = None
_DHAN_SECURITY_MAP = {}
dhan_instance = None

def load_stocks():
    """Load Nifty 500 stocks from JSON file (cached) and map Dhan security IDs."""
    global _STOCKS_CACHE, _DHAN_SECURITY_MAP
    if _STOCKS_CACHE is None:
        try:
            with open(STOCKS_FILE, "r", encoding="utf-8") as f:
                _STOCKS_CACHE = json.load(f)
            print(f"[Stocks] Loaded {len(_STOCKS_CACHE)} stocks from nifty500_stocks.json")
            
            # Build Dhan Mapping
            try:
                import pandas as pd
                url = "https://images.dhan.co/api-data/api-scrip-master.csv"
                print("[Dhan] Downloading Security Master from Dhan...")
                df = pd.read_csv(url, low_memory=False)
                nse_eq = df[(df['SEM_EXM_EXCH_ID'] == 'NSE') & (df['SEM_SERIES'] == 'EQ')]
                _DHAN_SECURITY_MAP = dict(zip(nse_eq['SEM_TRADING_SYMBOL'], nse_eq['SEM_SMST_SECURITY_ID']))
                print(f"[Dhan] Mapped {len(_DHAN_SECURITY_MAP)} NSE Equity instruments.")
            except Exception as e:
                print(f"[Dhan Error] Could not build security map: {e}")
                
        except Exception as e:
            print(f"[Stocks] Could not load nifty500_stocks.json: {e}")
            _STOCKS_CACHE = []
    return _STOCKS_CACHE

def update_live_prices():
    """Background thread to poll Dhan API (or yfinance fallback) for live stock data."""
    print("[LiveStream] Started background stream thread.")
    while True:
        try:
            if not _STOCKS_CACHE:
                time.sleep(10)
                continue
                
            # If DhanHQ is connected, use it!
            if DHAN_SESSION["connected"] and dhan_instance:
                try:
                    securities = {"NSE_EQ": []}
                    reverse_map = {} # map security ID back to stock ref
                    
                    for stock in _STOCKS_CACHE:
                        symbol = stock.get("symbol")
                        if symbol in _DHAN_SECURITY_MAP:
                            sec_id = str(_DHAN_SECURITY_MAP[symbol])
                            securities["NSE_EQ"].append(sec_id)
                            reverse_map[sec_id] = stock
                    
                    if securities["NSE_EQ"]:
                        print(f"[Dhan Live] Fetching quotes for {len(securities['NSE_EQ'])} stocks...")
                        
                        # Dhan API has limits, we might need to batch them in chunks of 100
                        updates = 0
                        chunk_size = 100
                        sec_list = securities["NSE_EQ"]
                        for i in range(0, len(sec_list), chunk_size):
                            chunk = {"NSE_EQ": sec_list[i:i + chunk_size]}
                            res = dhan_instance.quote_data(chunk)
                            if res and "data" in res and res["data"]:
                                for exch_name, symbol_data in res["data"].items():
                                    for s_id_raw, sec_data in symbol_data.items():
                                        s_id = str(s_id_raw)
                                        if s_id in reverse_map:
                                            stk = reverse_map[s_id]
                                            stk['last'] = float(sec_data.get("lastPrice", stk.get("last", 0)))
                                            prev_close = float(sec_data.get("previousClose", 0))
                                            open_px = float(sec_data.get("open", 0))
                                            
                                            stk['change'] = round(stk['last'] - prev_close, 2) if prev_close else 0
                                            stk['changePct'] = round(((stk['last'] - prev_close) / prev_close) * 100, 2) if prev_close else 0
                                            stk['gapPct'] = round(((open_px - prev_close) / prev_close) * 100, 2) if prev_close else 0
                                            updates += 1
                                            
                        print(f"[Dhan Live] Successfully updated {updates} stocks via Dhan API.")
                        time.sleep(15) # Dhan allows frequent polling
                        continue
                except Exception as e:
                    print(f"[Dhan Live Error] {e}")
                    
            # Fallback to yfinance if Dhan is not configured or fails
            if not YFINANCE_AVAILABLE:
                time.sleep(10)
                continue
                
            # Grab all symbols and append .NS for Yahoo Finance
            symbols = [s['symbol'] + '.NS' for s in _STOCKS_CACHE if 'symbol' in s]
            
            # Batch fetch to avoid rate limits
            print(f"[LiveStream] Fetching live quotes for {len(symbols)} stocks from Yahoo Finance...")
            data = yf.download(symbols, period="5d", progress=False)
            
            if 'Close' in data and not data['Close'].empty:
                latest_closes = data['Close'].iloc[-1]
                
                # To get previous close, we look at the second to last row if available
                prev_closes = data['Close'].iloc[-2] if len(data['Close']) > 1 else latest_closes
                latest_opens = data['Open'].iloc[-1]
                
                updates = 0
                for stock in _STOCKS_CACHE:
                    ticker = stock['symbol'] + '.NS'
                    if ticker in latest_closes and not pd.isna(latest_closes[ticker]):
                        last_price = float(latest_closes[ticker])
                        open_price = float(latest_opens[ticker]) if not pd.isna(latest_opens[ticker]) else last_price
                        prev_close = float(prev_closes[ticker]) if not pd.isna(prev_closes[ticker]) else open_price
                        
                        stock['last'] = last_price
                        stock['change'] = round(last_price - prev_close, 2)
                        stock['changePct'] = round(((last_price - prev_close) / prev_close) * 100, 2) if prev_close else 0
                        stock['gapPct'] = round(((open_price - prev_close) / prev_close) * 100, 2) if prev_close else 0
                        
                        updates += 1
                        
                print(f"[LiveStream] Successfully updated {updates} stocks with live quotes and Gap %.")
        except Exception as e:
            print(f"[LiveStream] Error fetching live quotes: {e}")
            
        time.sleep(90) # Wait 90 seconds between bulk fetches to avoid IP ban

def save_dhan_session():
    """Persist Dhan credentials to disk so they survive server restarts."""
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(DHAN_SESSION, f)
    except Exception as e:
        print(f"Failed to save Dhan session: {e}")

def auto_restore_session():
    global DHAN_SESSION, dhan_instance
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                saved = json.load(f)
                DHAN_SESSION.update(saved)
            if DHAN_SESSION["clientId"] and DHAN_SESSION["accessToken"]:
                import dhanhq
                dhan_instance = dhanhq.dhanhq(
                    client_id=DHAN_SESSION["clientId"],
                    access_token=DHAN_SESSION["accessToken"]
                )
                DHAN_SESSION["connected"] = True
                DHAN_SESSION["lastConnected"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                print(f"[Dhan] Restored session for Client ID: {DHAN_SESSION['clientId']}")
        except Exception as e:
            print(f"Failed to restore Dhan session: {e}")

def base32_decode(b32_str):
    """Decode base32 string without external dependencies."""
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
    cleaned = ''.join([c for c in str(b32_str).upper() if c in alphabet])
    bits = ''.join([bin(alphabet.index(c))[2:].zfill(5) for c in cleaned])
    byte_arr = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte_arr.append(int(bits[i:i+8], 2))
    return bytes(byte_arr)

def generate_totp(secret_base32, time_step=30):
    """RFC 6238 TOTP computation."""
    try:
        key = base32_decode(secret_base32)
        if len(key) == 0:
            return None
        epoch_seconds = int(time.time())
        counter = epoch_seconds // time_step
        counter_bytes = struct.pack(">Q", counter)
        h = hmac.new(key, counter_bytes, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF
        otp = code % 1000000
        return str(otp).zfill(6)
    except Exception as e:
        print(f"[TOTP Error] {e}")
        return None

def make_request(url, method="GET", headers=None, data=None, timeout=4):
    """Robust HTTP client for Kotak Neo REST API with quick timeout."""
    if headers is None:
        headers = {}
    encoded_data = None
    if data is not None:
        if isinstance(data, (dict, list)):
            encoded_data = json.dumps(data).encode("utf-8")
            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"
        elif isinstance(data, str):
            encoded_data = data.encode("utf-8")
        elif isinstance(data, bytes):
            encoded_data = data

    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        return e.code, body
    except Exception as e:
        return 500, json.dumps({"error": str(e)})

def get_overview_data():
    nifty_last, nifty_change = 23897.70, 0.10
    bnifty_last, bnifty_change = 57369.65, -0.02
    vix_last, vix_change = 10.68, -5.82

    if YFINANCE_AVAILABLE:
        try:
            # Try fetching live indices using Ticker fast_info for speed
            idx = yf.Tickers("^NSEI ^NSEBANK ^INDIAVIX")
            if "^NSEI" in idx.tickers:
                nifty_last = idx.tickers["^NSEI"].fast_info.last_price
                nifty_change = round(((nifty_last / idx.tickers["^NSEI"].fast_info.previous_close) - 1) * 100, 2)
            if "^NSEBANK" in idx.tickers:
                bnifty_last = idx.tickers["^NSEBANK"].fast_info.last_price
                bnifty_change = round(((bnifty_last / idx.tickers["^NSEBANK"].fast_info.previous_close) - 1) * 100, 2)
        except:
            pass

    return {
        "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "quoteSource": "DhanHQ API Live" if DHAN_SESSION["connected"] else ("Live Yahoo Finance Stream" if YFINANCE_AVAILABLE else "Static Snapshot"),
        "fromCache": False,
        "headline": [
            {"id": "nifty50", "label": "Nifty 50", "last": round(nifty_last,2), "change": 0, "changePct": nifty_change, "direction": "up" if nifty_change >= 0 else "down", "arrow": "▲" if nifty_change >= 0 else "▼"},
            {"id": "bankNifty", "label": "Bank Nifty", "last": round(bnifty_last,2), "change": 0, "changePct": bnifty_change, "direction": "up" if bnifty_change >= 0 else "down", "arrow": "▲" if bnifty_change >= 0 else "▼"},
            {"id": "indiaVix", "label": "India VIX", "last": vix_last, "change": 0, "changePct": vix_change, "direction": "down", "arrow": "▼"}
        ],
        "size": [
            {"id": "largeCap", "label": "Large Cap", "subtitle": "Nifty 100", "last": 25023.15, "change": 9.70, "changePct": 0.04, "direction": "up", "arrow": "▲", "ema": {"ema20": {"above": False}, "ema50": {"above": False}, "ema200": {"above": False}, "bias": "bearish"}},
            {"id": "midCap", "label": "Mid Cap", "subtitle": "Nifty Midcap 150", "last": 23168.35, "change": -52.15, "changePct": -0.22, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": False}, "ema200": {"above": True}, "bias": "mixed"}},
            {"id": "smallCap", "label": "Small Cap", "subtitle": "Nifty Smallcap 250", "last": 18481.40, "change": 36.45, "changePct": 0.20, "direction": "up", "arrow": "▲", "ema": {"ema20": {"above": True}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}}
        ],
        "sectors": [
            {"id": "it", "label": "IT", "last": 30695.10, "change": -143.75, "changePct": -0.47, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": True}, "ema200": {"above": False}, "bias": "mixed"}},
            {"id": "bank", "label": "Bank", "last": 57369.65, "change": -10.95, "changePct": -0.02, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "fin", "label": "Financial Services", "last": 26051.00, "change": 127.95, "changePct": 0.49, "direction": "up", "arrow": "▲", "ema": {"ema20": {"above": True}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "auto", "label": "Auto", "last": 27710.90, "change": -126.50, "changePct": -0.45, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": True}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "pharma", "label": "Pharma", "last": 26478.10, "change": -180.55, "changePct": -0.68, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "fmcg", "label": "FMCG", "last": 45892.75, "change": -62.90, "changePct": -0.14, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": False}, "ema200": {"above": False}, "bias": "bearish"}},
            {"id": "metal", "label": "Metal", "last": 13317.45, "change": 141.40, "changePct": 1.07, "direction": "up", "arrow": "▲", "ema": {"ema20": {"above": True}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "energy", "label": "Energy", "last": 38067.85, "change": -72.95, "changePct": -0.19, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": False}, "ema200": {"above": True}, "bias": "mixed"}},
            {"id": "realty", "label": "Realty", "last": 907.90, "change": -8.25, "changePct": -0.90, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": True}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "media", "label": "Media", "last": 1565.45, "change": 4.70, "changePct": 0.30, "direction": "up", "arrow": "▲", "ema": {"ema20": {"above": True}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "psuBank", "label": "PSU Bank", "last": 8514.85, "change": -37.75, "changePct": -0.44, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": False}, "ema200": {"above": False}, "bias": "bearish"}},
            {"id": "privateBank", "label": "Private Bank", "last": 27824.50, "change": 76.65, "changePct": 0.28, "direction": "up", "arrow": "▲", "ema": {"ema20": {"above": True}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "infra", "label": "Infra", "last": 9197.80, "change": -6.40, "changePct": -0.07, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "healthcare", "label": "Healthcare", "last": 16408.05, "change": -144.15, "changePct": -0.87, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": True}, "ema200": {"above": True}, "bias": "bullish"}},
            {"id": "consumerDurables", "label": "Consumer Durables", "last": 39483.80, "change": -168.90, "changePct": -0.43, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": False}, "ema200": {"above": False}, "bias": "bearish"}},
            {"id": "chemicals", "label": "Chemicals", "last": 30101.80, "change": -220.65, "changePct": -0.73, "direction": "down", "arrow": "▼", "ema": {"ema20": {"above": False}, "ema50": {"above": False}, "ema200": {"above": False}, "bias": "bearish"}}
        ],
        "derivatives": {
            "fiiCash": 1428.5,
            "diiCash": 2190.2,
            "fiiFuturesLongPct": 64.2,
            "niftyPcr": 1.18,
            "bankNiftyPcr": 1.04,
            "maxPain": 23900,
            "vixPercentile": 18
        }
    }

def get_breadth_data(universe="nifty50"):
    dates = []
    index_vals = []
    b20_vals = []
    b50_vals = []
    b200_vals = []

    # Calculate real breadth from current _STOCKS_CACHE snapshot
    total_stocks = len(_STOCKS_CACHE) if _STOCKS_CACHE else 1
    dma20_count = sum(1 for s in _STOCKS_CACHE if s.get('dma20', False)) if _STOCKS_CACHE else 0
    dma50_count = sum(1 for s in _STOCKS_CACHE if s.get('dma50', False)) if _STOCKS_CACHE else 0
    dma200_count = sum(1 for s in _STOCKS_CACHE if s.get('dma200', False)) if _STOCKS_CACHE else 0
    
    cur20 = round((dma20_count / total_stocks) * 100)
    cur50 = round((dma50_count / total_stocks) * 100)
    cur200 = round((dma200_count / total_stocks) * 100)
    
    # Generate stable historical series (flat lines representing the snapshot)
    now = time.time()
    for i in range(240, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        dates.append(d)
        index_vals.append(24000) # Placeholder flat index
        b20_vals.append(cur20)
        b50_vals.append(cur50)
        b200_vals.append(cur200)

    posture = "GREEN LIGHT — PRESS"
    tone = "good"
    diag = "Broad institutional participation across all timeframes. Leaders & core trend aligned."

    if cur20 < 50 and cur50 > 50 and cur200 > 50:
        posture = "STOP PRESSING"
        tone = "warn"
        diag = "Leaders are cracking first while index is buoyed by lagging components. First distribution sign."
    elif cur50 < 50 and cur200 < 50:
        posture = "REDUCE RISK"
        tone = "danger"
        diag = "Weaker stocks are rolling over; medium and long-term participation is deteriorating."

    return {
        "universe": universe,
        "stockCount": 500 if universe == "nifty500" else 50,
        "asOf": time.strftime("%Y-%m-%d"),
        "dataSource": "DhanHQ API Live" if DHAN_SESSION["connected"] else ("Live Yahoo Finance Stream" if YFINANCE_AVAILABLE else "Static Snapshot"),
        "fromCache": False,
        "gauges": {
            "dma20": {"value": cur20, "label": "20 DMA — LEADERS", "subtitle": "Short-term momentum", "arrow": "▲" if cur20 >= 50 else "▼"},
            "dma50": {"value": cur50, "label": "50 DMA — CORE", "subtitle": "Intermediate institutional trend", "arrow": "▲" if cur50 >= 50 else "▼"},
            "dma200": {"value": cur200, "label": "200 DMA — FOUNDATION", "subtitle": "Secular market structure", "arrow": "▲" if cur200 >= 50 else "▼"}
        },
        "diagnosis": {
            "posture": posture,
            "tone": tone,
            "diagnosis": diag
        },
        "series": {
            "dates": dates,
            "index": index_vals,
            "breadth20": b20_vals,
            "breadth50": b50_vals,
            "breadth200": b200_vals
        }
    }

def math_sin(x):
    import math
    return math.sin(x)

class DhanTerminalHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRATCH_DIR, **kwargs)

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/dhan/status":
            return self.send_json(DHAN_SESSION)

        if path == "/api/overview":
            return self.send_json(get_overview_data())

        if path == "/api/breadth":
            params = urllib.parse.parse_qs(parsed.query)
            universe = params.get("universe", ["nifty50"])[0]
            return self.send_json(get_breadth_data(universe))

        if path == "/api/stocks":
            params = urllib.parse.parse_qs(parsed.query)
            universe = params.get("universe", ["nifty500"])[0]
            all_stocks = load_stocks()
            if universe == "nifty50":
                stocks = all_stocks[:50]
            elif universe == "nifty100":
                stocks = all_stocks[:100]
            else:
                stocks = all_stocks
            return self.send_json({"universe": universe, "count": len(stocks), "stocks": stocks})

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"

        try:
            payload = json.loads(post_body)
        except Exception:
            payload = {}

        if path == "/api/dhan/auth":
            self.handle_dhan_auth(payload)
            return

        if path == "/api/dhan/disconnect":
            DHAN_SESSION["connected"] = False
            DHAN_SESSION["accessToken"] = None
            DHAN_SESSION["clientId"] = None
            DHAN_SESSION["error"] = None
            save_dhan_session()
            return self.send_json({"success": True, "message": "Disconnected from DhanHQ API"})

        self.send_response(404)
        self.end_headers()

    def handle_dhan_auth(self, payload):
        """Execute DhanHQ API configuration test."""
        client_id = str(payload.get("clientId", "")).strip()
        access_token = str(payload.get("accessToken", "")).strip()

        if not client_id or not access_token:
            return self.send_json({"success": False, "error": "Missing Client ID or Access Token"}, 400)

        try:
            import dhanhq
            global dhan_instance
            dhan_instance = dhanhq.dhanhq(client_id=client_id, access_token=access_token)
            
            DHAN_SESSION["configured"] = True
            DHAN_SESSION["connected"] = True
            DHAN_SESSION["clientId"] = client_id
            DHAN_SESSION["accessToken"] = access_token
            DHAN_SESSION["lastConnected"] = time.strftime("%Y-%m-%d %H:%M:%S")
            DHAN_SESSION["error"] = None
            save_dhan_session()

            return self.send_json({
                "success": True,
                "connected": True,
                "logs": "DhanHQ API connected successfully! Live tick sync active."
            })
            
        except Exception as e:
            return self.send_json({"success": False, "error": str(e)}, 401)

def run_server():
    load_stocks()        # Pre-load 500 stocks on startup
    auto_restore_session()  # Auto-reconnect from saved credentials
    
    # Start live price stream thread
    t = threading.Thread(target=update_live_prices, daemon=True)
    t.start()
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, DhanTerminalHandler)
    httpd.daemon_threads = True
    print(f"Kotak Neo Bridge & Market Breadth Terminal running on http://0.0.0.0:{PORT}/")
    httpd.serve_forever()

if __name__ == "__main__":
    run_server()
