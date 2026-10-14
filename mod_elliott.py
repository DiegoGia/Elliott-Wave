# ------------------------------------------------------------------------------
# Modulo Elliott Wave (port de Python)
#
# Derivative work based on "Elliott Wave [LuxAlgo]" (c) LuxAlgo, published on
# TradingView (https://es.tradingview.com/script/KvrhsPTp-Elliott-Wave-LuxAlgo/) under the
# Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International license
# (CC BY-NC-SA 4.0): https://creativecommons.org/licenses/by-nc-sa/4.0/
#
# Changes: rewritten from Pine Script to Python; drawings, Fibonacci lines and
# box-break (cross) signals removed; only closed candles are used; bullish and
# bearish code unified; circle signals are sent to Telegram.
# Distributed under the same license (CC BY-NC-SA 4.0): attribution, NON-COMMERCIAL, share-alike.
# ------------------------------------------------------------------------------
"""Alertas de CIRCULOS de Elliott Wave: circulo ARRIBA / circulo ABAJO de la vela."""
from typing import NamedTuple

NAME = "elliott"
LENGTHS = [4, 8, 16]            # grados del zigzag (rojo, azul, verde)
FIB = 0.854                     # limite de la correccion ABC (nivel 4 del indicador)
DEGREE_COLOR = {4: "rojo", 8: "azul", 16: "verde"}
DEGREE_ICON = {4: "🔴", 8: "🔵", 16: "🟢"}


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


def _run(candles):
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


def analyze(candles):
    """candles: [(t, o, h, l, c)] solo cerradas. Devuelve una lista de señales (dict)."""
    out = []
    for d in _run(candles):
        for e in d.events:
            out.append({"bar": e.bar, "mark": e.mark, "side": e.side, "level": e.level, "left": d.left})
    return out


def format(ev, sym, tf, candles, now_ms, h):
    up = ev["side"] == 1
    closed_ms = candles[ev["bar"]][0] + h.TF_MS[tf]      # momento en que cerro la vela que confirma
    lag = max(0, (now_ms - closed_ms) // 1000)
    headline = "○ CÍRCULO ⬆️ ARRIBA" if up else "○ CÍRCULO ⬇️ ABAJO"
    left = ev["left"]
    tag = f"grado {left} {DEGREE_ICON.get(left, '')} ({DEGREE_COLOR.get(left, '')})"
    rows = [
        f"🕯 Sobre la vela: <code>{h.fmt_time(candles[ev['mark']][0])}</code> ({h.esc(h.TZ)})",
        f"✅ Confirmó <code>{h.fmt_time(closed_ms, '%H:%M:%S')}</code> · demora <code>{lag // 60}m{lag % 60:02d}s</code>",
        f"📍 Nivel <code>{ev['level']:g}</code> · Cierre <code>{candles[ev['bar']][4]:g}</code>",
    ]
    return h.card(NAME, headline, sym, tf, tag, rows)
