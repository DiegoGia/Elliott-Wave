#!/usr/bin/env python3
"""
Elliott Wave [LuxAlgo] -> alertas de CIRCULOS a Telegram (datos de Binance, sin login).

Dos señales:   ○ CIRCULO ARRIBA   ○ CIRCULO ABAJO
(posicion del circulo respecto a la vela, igual que en TradingView)

Sin repintado: se calcula solo con velas CERRADAS; la señal se envia una vez y no se mueve.

Uso:
    python ew_bot.py test     -> prueba Binance y Telegram
    python ew_bot.py --once   -> una pasada y termina (GitHub Actions)
    python ew_bot.py          -> continuo, sincronizado con el cierre de cada vela (PC / servidor)
"""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

import requests

# ===================== CONFIGURACION =====================
TELEGRAM_TOKEN = os.getenv("TG_TOKEN", "PEGA_AQUI_EL_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT", "PEGA_AQUI_TU_CHAT_ID")

MARKET = os.getenv("MARKET", "spot")           # "spot" o "futures" (futures suele dar 451 en GitHub)
TOP_N = 30                                     # maximo de monedas a vigilar
MIN_VOLUME_USDT = 100_000_000                  # volumen minimo en 24h (en USDT) para entrar a la lista
EXCLUDE = set()                                # bases a excluir, ej. {"PEPE", "WIF"}
SYMBOLS = []                                   # vacio = automatico. O fija: ["BTCUSDT", "ETHUSDT"]
REFRESH_HOURS = 24                             # la lista automatica se recalcula cada tantas horas
TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d", "1w"]   # 1w = semanal
LENGTHS = [4, 8, 16]                           # grados del zigzag (rojo, azul, blanco)
FIB = 0.854                                    # limite de la correccion ABC (nivel 4 del indicador)
TZ = "UTC"                                     # zona horaria de los mensajes, ej. "America/New_York"
CANDLES = 1000                                 # velas por consulta (maximo de Binance)
WORKERS = 8                                    # consultas en paralelo
LAG_SECONDS = 3                                # modo continuo: espera tras el cierre de la vela
STATE_FILE = "ew_state.json"
# =========================================================

TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
         "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
WEEK_OFFSET = 4 * 86_400_000                   # las velas semanales abren el lunes 00:00 UTC
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDD", "EUR", "EURI", "AEUR",
           "PYUSD", "USD1", "USDE", "XUSD", "BFUSD", "RLUSD", "UST", "GBP", "TRY", "BRL"}
DEGREE_COLOR = {4: "rojo", 8: "azul", 16: "blanco"}


# ------------------------------------------------------------------
#  Motor Elliott (port de la logica del indicador; solo lo necesario para los circulos)
# ------------------------------------------------------------------
class Event(NamedTuple):
    bar: int       # vela cuyo cierre dispara la señal
    mark: int      # vela donde TradingView dibuja el circulo
    side: int      # +1 arriba de la vela | -1 debajo de la vela
    level: float   # extremo que se supero


def is_pivot_high(h, i, left):
    """Pivote en la vela i-1 confirmado por la vela i (como ta.pivothigh(x, left, 1))."""
    p = i - 1
    if p < left:
        return False
    v = h[p]
    return v > h[i] and all(v > h[p - k] for k in range(1, left + 1))


def is_pivot_low(l, i, left):
    p = i - 1
    if p < left:
        return False
    v = l[p]
    return v < l[i] and all(v < l[p - k] for k in range(1, left + 1))


class Degree:
    """Un grado del zigzag (len 4, 8 o 16). Alcista y bajista comparten codigo (s = +1 / -1)."""

    def __init__(self, left):
        self.left = left
        self.zz = []       # [dir, x, y]; el mas nuevo en el indice 0
        self.waves = []    # impulsos; el mas nuevo en el indice 0
        self.events = []

    def pivot(self, s, i, x2, y2):
        z = self.zz
        if not z or z[0][0] != s:
            z.insert(0, [s, x2, y2])
            del z[12:]
        elif s * y2 > s * z[0][2]:
            z[0][1], z[0][2] = x2, y2          # el ultimo extremo se extiende
        else:
            return                             # el zigzag no cambio
        if len(z) < 6:
            return

        X = [z[k][1] for k in range(5, -1, -1)]    # puntos 1..6
        R = [z[k][2] for k in range(5, -1, -1)]
        Y = [s * v for v in R]                     # multiplicado por s: una sola logica

        # ---- impulso 1-2-3-4-5 ----
        w1, w3, w5 = Y[1] - Y[0], Y[3] - Y[2], Y[5] - Y[4]
        is_wave = w3 != min(w1, w3, w5) and Y[5] > Y[3] and Y[2] > Y[0] and Y[4] > Y[1]
        cur = self.waves[0] if self.waves else None
        same = cur is not None and cur["X"][:4] == X[:4]
        if is_wave:
            if same:
                cur["X"][5], cur["R"][5] = X[5], R[5]
            else:
                self.waves.insert(0, dict(dir=s, X=X[:], R=R[:], on=True, abc=None, next=False))
                del self.waves[15:]
        elif same and cur["on"]:
            cur["on"] = False                      # impulso invalidado

        cur = self.waves[0] if self.waves else None
        if cur is None or not cur["on"]:
            return

        # ---- correccion (a)(b)(c) contra el impulso ----
        if cur["dir"] == -s:
            diff = abs(cur["R"][5] - cur["R"][0])
            gy = s * cur["R"][5]
            same2 = X[0] == cur["X"][3] and X[1] == cur["X"][4] and X[2] == cur["X"][5]
            valid = (X[2] == cur["X"][5] and Y[5] < gy + diff * FIB
                     and Y[3] < gy + diff * FIB and Y[4] > gy)
            abc = cur["abc"]
            if valid:
                if same2 and abc and abc["a"] > X[2]:
                    abc["c"] = X[5]                # (c) se extiende
                else:
                    cur["abc"] = dict(a=X[3], c=X[5], ok=True)
            elif same2 and abc and abc["a"] > X[2]:
                abc["ok"] = False                  # correccion invalidada: ya no avisa

        # ---- circulo: el precio supera el extremo de (5) tras la correccion ----
        abc = cur["abc"]
        if cur["dir"] == s and not cur["next"] and abc and abc["ok"]:
            if X[4] == abc["c"] and Y[5] > s * cur["R"][5]:
                cur["next"] = True
                self.events.append(Event(i, i - 1, s, R[5]))


def analyze(candles):
    """candles: [(t, o, h, l, c)] solo cerradas. Devuelve los grados con sus eventos."""
    hi = [c[2] for c in candles]
    lo = [c[3] for c in candles]
    degs = [Degree(n) for n in LENGTHS]
    for i in range(1, len(candles)):
        for d in degs:
            if is_pivot_high(hi, i, d.left):
                d.pivot(1, i, i - 1, hi[i - 1])
            if is_pivot_low(lo, i, d.left):
                d.pivot(-1, i, i - 1, lo[i - 1])
    return degs


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
    """Proximo instante en que cierra alguna vela de los timeframes configurados."""
    best = None
    for tf in TIMEFRAMES:
        ms = TF_MS[tf]
        off = WEEK_OFFSET if tf == "1w" else 0
        t = (now_ms - off) // ms * ms + off + ms
        best = t if best is None else min(best, t)
    return best


# ------------------------------------------------------------------
#  Telegram
# ------------------------------------------------------------------
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for _ in range(3):
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if r.ok:
            return
        raise RuntimeError(f"Telegram: {r.text}")
    raise RuntimeError("Telegram: demasiados reintentos")


def fmt_time(ms, fmt="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(TZ)).strftime(fmt)


def format_signal(sym, tf, left, ev, candles, now_ms):
    pos = "ARRIBA" if ev.side == 1 else "ABAJO"
    closed_ms = candles[ev.bar][0] + TF_MS[tf]          # momento en que cerro la vela que confirma
    lag = max(0, (now_ms - closed_ms) // 1000)
    return (f"○ CÍRCULO {pos}\n"
            f"{sym} · {tf} · grado {left} {DEGREE_COLOR.get(left, '')}\n"
            f"Sobre la vela de las {fmt_time(candles[ev.mark][0])} ({TZ})\n"
            f"Confirmó al cierre de las {fmt_time(closed_ms, '%H:%M:%S')} | demora: {lag // 60}m{lag % 60:02d}s\n"
            f"Nivel: {ev.level:g} | Cierre: {candles[ev.bar][4]:g}")


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
    jobs = []
    for sym in get_symbols(client, state, now):
        for tf in TIMEFRAMES:
            if state.get(f"{sym}|{tf}", 0) < last_closed_open(tf, now):   # cerro una vela nueva
                jobs.append((sym, tf))

    def fetch(job):
        try:
            return job, client.klines(job[0], job[1], now), None
        except Exception as e:
            return job, None, e

    blocks, pending, stale = [], {}, 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for (sym, tf), candles, err in pool.map(fetch, jobs):
            key = f"{sym}|{tf}"
            if err:
                print(f"{key}: {err}")
                continue
            if len(candles) < 60:
                continue
            if candles[-1][0] < last_closed_open(tf, now):
                stale += 1                                    # Binance aun no publica la vela
                continue
            last, prev = candles[-1][0], state.get(key)
            if prev is None:                                  # primera vez: sin alertas viejas
                state[key] = last
                continue
            for d in analyze(candles):
                for ev in d.events:
                    if candles[ev.bar][0] > prev:
                        blocks.append(format_signal(sym, tf, d.left, ev, candles, now))
            pending[key] = last
    if blocks:
        print(f"{len(blocks)} señal(es)")
        send_batched(blocks)                                  # si falla, el estado no avanza
    state.update(pending)
    return stale


def main():
    client = Binance(MARKET)
    if "test" in sys.argv:
        print("Top 5:", client.top_symbols(5, 0))
        send_telegram("✅ Prueba OK: Binance responde y Telegram te puede escribir.")
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
                wait = 5                                      # reintenta en 5 s si falta publicar una vela
            save_state(state)
        except Exception as e:
            print("error:", e)
            wait = 15
        if not wait:
            wait = max(1, (next_close_ms(int(time.time() * 1000)) - int(time.time() * 1000)) / 1000 + LAG_SECONDS)
        time.sleep(wait)


if __name__ == "__main__":
    main()
