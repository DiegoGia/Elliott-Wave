#!/usr/bin/env python3
# ------------------------------------------------------------------------------
# Crypto alerts bot: Elliott Wave + Smart Money Concepts + Supertrend -> Telegram
#
# Elliott Wave and SMC modules are derivative works of "Elliott Wave [LuxAlgo]" and
# "Smart Money Concepts" (c) LuxAlgo, published on TradingView under the Creative Commons
# Attribution-NonCommercial-ShareAlike 4.0 International license (CC BY-NC-SA 4.0):
#     https://creativecommons.org/licenses/by-nc-sa/4.0/
# See NOTICE.md and the header of each mod_*.py file for the changes made.
# This project is distributed under the same license: attribution, NON-COMMERCIAL use only,
# share-alike. No warranty.
# ------------------------------------------------------------------------------
"""
Bot de alertas (datos de Binance, sin login) con formato en Telegram.

Modulos (cada uno en su archivo, aislados: si uno falla, los otros siguen):
    mod_elliott.py     ○ CIRCULO ARRIBA / ABAJO                     (top 30, 5m a 1w)
    mod_smc.py         BOS / CHoCH swing + ruptura de Swing OB     (top 10, 4h y 1d)
    mod_supertrend.py  cambio de tendencia                          (top 10, 4h y 1d)

Sin repintado: se calcula solo con velas CERRADAS; cada señal se envia una vez y no se mueve.

Uso:
    python ew_bot.py test     -> prueba Binance y Telegram
    python ew_bot.py --once   -> una pasada y termina (GitHub Actions)
    python ew_bot.py          -> continuo, sincronizado con el cierre de cada vela
"""
import html
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import requests

import mod_elliott
import mod_smc
import mod_supertrend

# ===================== CONFIGURACION =====================
TELEGRAM_TOKEN = os.getenv("TG_TOKEN", "PEGA_AQUI_EL_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT", "PEGA_AQUI_TU_CHAT_ID")

MARKET = os.getenv("MARKET", "spot")           # "spot" o "futures" (futures suele dar 451 en GitHub)
TOP_N = 30                                     # monedas de la lista automatica (los modulos usan las primeras 'top')
MIN_VOLUME_USDT = 100_000_000                  # volumen minimo en 24h (en USDT) para entrar a la lista
EXCLUDE = set()                                # bases a excluir, ej. {"PEPE", "WIF"}
SYMBOLS = []                                   # vacio = automatico. O fija: ["BTCUSDT", "ETHUSDT"]
REFRESH_HOURS = 24                             # la lista automatica se recalcula cada tantas horas
TZ = "UTC"                                     # zona horaria de los mensajes, ej. "America/New_York"
CANDLES = 1000                                 # velas por consulta (maximo de Binance)
WORKERS = 8                                    # consultas en paralelo
LAG_SECONDS = 3                                # modo continuo: espera tras el cierre de la vela
STATE_FILE = "ew_state.json"

# Modulos activos. Para apagar uno, borra su linea o pon "on": False.
ACTIVE = [
    {"mod": mod_elliott,    "on": True, "top": 30, "tfs": ["5m", "15m", "30m", "1h", "4h", "1d", "1w"]},
    {"mod": mod_smc,        "on": True, "top": 10, "tfs": ["4h", "1d"]},
    {"mod": mod_supertrend, "on": True, "top": 10, "tfs": ["4h", "1d"]},
]
# =========================================================

TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
WEEK_OFFSET = 4 * 86_400_000                   # las velas semanales abren el lunes 00:00 UTC
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDD", "EUR", "EURI", "AEUR",
           "PYUSD", "USD1", "USDE", "XUSD", "BFUSD", "RLUSD", "UST", "GBP", "TRY", "BRL"}


# ------------------------------------------------------------------
#  Binance (datos publicos, sin API key)
# ------------------------------------------------------------------
class Binance:
    def __init__(self, market):
        if market == "futures":
            self.bases = ["https://fapi.binance.com"]
            self.k, self.t = "/fapi/v1/klines", "/fapi/v1/ticker/24hr"
        else:
            self.bases = ["https://data-api.binance.vision", "https://api.binance.com",
                          "https://api1.binance.com", "https://api-gcp.binance.com"]
            self.k, self.t = "/api/v3/klines", "/api/v3/ticker/24hr"
        self.http = requests.Session()
        self.lock = threading.Lock()

    def get(self, path, params=None):
        err = "sin respuesta"
        for _ in range(2):
            with self.lock:
                bases = list(self.bases)
            for base in bases:
                try:
                    r = self.http.get(base + path, params=params, timeout=20)
                except requests.RequestException as e:
                    err = str(e)
                    continue
                if r.status_code == 200:
                    with self.lock:
                        if base in self.bases:
                            self.bases.remove(base)
                            self.bases.insert(0, base)   # recuerda el host que funciona
                    return r.json()
                err = f"{base} -> HTTP {r.status_code}"
                if r.status_code in (418, 429):
                    time.sleep(min(int(r.headers.get("Retry-After", "5")), 30))
        raise RuntimeError(err + (" (451 = Binance bloquea la region/IP del servidor)" if "451" in err else ""))

    def top_symbols(self, n, min_volume):
        rows = []
        for d in self.get(self.t):
            s = d["symbol"]
            if not s.endswith("USDT") or "_" in s:
                continue
            base = s[:-4]
            vol = float(d["quoteVolume"])
            if base in STABLES or base in EXCLUDE or vol < min_volume:
                continue
            rows.append((vol, s))
        rows.sort(reverse=True)
        return [s for _, s in rows[:n]]

    def klines(self, sym, tf, now_ms):
        raw = self.get(self.k, {"symbol": sym, "interval": tf, "limit": CANDLES})
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in raw if int(r[6]) < now_ms]      # solo velas ya cerradas


def last_closed_open(tf, now_ms):
    """Hora de apertura de la ultima vela que ya deberia estar cerrada."""
    ms = TF_MS[tf]
    off = WEEK_OFFSET if tf == "1w" else 0
    return (now_ms - off) // ms * ms + off - ms


def next_close_ms(now_ms):
    """Proximo instante en que cierra alguna vela de los timeframes activos."""
    best = None
    for m in ACTIVE:
        if not m["on"]:
            continue
        for tf in m["tfs"]:
            ms = TF_MS[tf]
            off = WEEK_OFFSET if tf == "1w" else 0
            t = (now_ms - off) // ms * ms + off + ms
            best = t if best is None else min(best, t)
    return best


# ------------------------------------------------------------------
#  Telegram (formato HTML)
# ------------------------------------------------------------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    for _ in range(3):
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if r.ok:
            return
        raise RuntimeError(f"Telegram: {r.text}")
    raise RuntimeError("Telegram: demasiados reintentos")


# Cada indicador tiene su color e icono: se reconoce a simple vista en el chat.
STYLES = {
    "elliott": ("🟦 🌊", "ELLIOTT WAVE"),
    "smc": ("🟧 🏛", "SMC · SMART MONEY"),
    "supertrend": ("🟪 📈", "SUPERTREND"),
}
TV_INTERVAL = {"5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D", "1w": "W"}


def tv_link(sym, tf):
    suffix = ".P" if MARKET == "futures" else ""
    return f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{sym}{suffix}&interval={TV_INTERVAL[tf]}"


def fmt_time(ms, fmt="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(TZ)).strftime(fmt)


def card(module, headline, sym, tf, tag, rows):
    """Tarjeta de alerta con formato (HTML de Telegram). La usan todos los modulos."""
    icon, name = STYLES[module]
    lines = [f"{icon} <b>{name}</b>",
             f"<b>{html.escape(headline)}</b>",
             f"🪙 <b>{html.escape(sym)}</b> · ⏱ {tf}" + (f" · {html.escape(tag)}" if tag else "")]
    lines += rows
    lines.append(f'<a href="{tv_link(sym, tf)}">📈 Abrir gráfico</a>')
    return "\n".join(lines)


# utilidades que reciben los modulos
HELPERS = SimpleNamespace(card=card, fmt_time=fmt_time, esc=html.escape, TF_MS=TF_MS, TZ=TZ)


def send_batched(blocks):
    chunk, size = [], 0
    for b in blocks:
        if chunk and size + len(b) > 3500:
            send_telegram("\n\n".join(chunk))
            chunk, size = [], 0
        chunk.append(b)
        size += len(b) + 2
    if chunk:
        send_telegram("\n\n".join(chunk))


# ------------------------------------------------------------------
#  Ejecucion
# ------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=0, sort_keys=True)


def state_key(module, sym, tf):
    # Elliott conserva su clave original para no perder el estado ya guardado
    return f"{sym}|{tf}" if module.NAME == "elliott" else f"{module.NAME}|{sym}|{tf}"


def get_symbols(client, state, now):
    if SYMBOLS:
        return SYMBOLS
    cache = state.get("_symbols")
    if cache and now - cache["ts"] < REFRESH_HOURS * 3_600_000:
        return cache["list"]
    try:
        lst = client.top_symbols(TOP_N, MIN_VOLUME_USDT)
        state["_symbols"] = {"ts": now, "list": lst}
        return lst
    except Exception as e:
        if cache:
            print("No pude actualizar la lista, uso la anterior:", e)
            return cache["list"]
        raise


def run_once(client, state):
    """Devuelve cuantas series aun no tienen publicada su ultima vela cerrada (para reintentar)."""
    now = int(time.time() * 1000)
    symbols = get_symbols(client, state, now)
    jobs = {}                                             # (simbolo, tf) -> modulos que lo necesitan
    for m in ACTIVE:
        if not m["on"]:
            continue
        for sym in symbols[:m["top"]]:
            for tf in m["tfs"]:
                if state.get(state_key(m["mod"], sym, tf), 0) < last_closed_open(tf, now):
                    jobs.setdefault((sym, tf), []).append(m["mod"])

    def fetch(job):
        try:
            return job, client.klines(job[0], job[1], now), None
        except Exception as e:
            return job, None, e

    blocks, pending, stale = [], {}, 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for (sym, tf), candles, err in pool.map(fetch, list(jobs)):
            if err:
                print(f"{sym}|{tf}: {err}")
                continue
            if len(candles) < 60:
                continue
            if candles[-1][0] < last_closed_open(tf, now):
                stale += 1                                # Binance aun no publica la vela
                continue
            last = candles[-1][0]
            for mod in jobs[(sym, tf)]:                   # cada modulo va aislado de los demas
                key = state_key(mod, sym, tf)
                prev = state.get(key)
                if prev is None:                          # primera vez: sin alertas viejas
                    state[key] = last
                    continue
                try:
                    for ev in mod.analyze(candles):
                        if candles[ev["bar"]][0] > prev:
                            blocks.append(mod.format(ev, sym, tf, candles, now, HELPERS))
                    pending[key] = last
                except Exception as e:
                    print(f"{mod.NAME} {sym}|{tf}: error {e!r}")
    if blocks:
        print(f"{len(blocks)} señal(es)")
        send_batched(blocks)                              # si falla, el estado no avanza
    state.update(pending)
    return stale


def main():
    client = Binance(MARKET)
    if "test" in sys.argv:
        print("Top 5:", client.top_symbols(5, 0))
        send_telegram("🟦 🌊 <b>ELLIOTT WAVE</b> · 🟧 🏛 <b>SMC</b> · 🟪 📈 <b>SUPERTREND</b>\n"
                      "✅ Prueba OK: Binance responde y Telegram te puede escribir.")
        return
    state = load_state()
    if "--once" in sys.argv:
        run_once(client, state)
        save_state(state)
        return
    print("Bot en marcha. Ctrl+C para parar.")
    while True:
        wait = 0
        try:
            if run_once(client, state):
                wait = 5                                  # reintenta en 5 s si falta publicar una vela
            save_state(state)
        except Exception as e:
            print("error:", e)
            wait = 15
        if not wait:
            wait = max(1, (next_close_ms(int(time.time() * 1000)) - int(time.time() * 1000)) / 1000 + LAG_SECONDS)
        time.sleep(wait)


if __name__ == "__main__":
    main()
