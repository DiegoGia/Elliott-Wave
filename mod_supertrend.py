# ------------------------------------------------------------------------------
# Modulo Supertrend (port de Python) - alerta de CAMBIO DE TENDENCIA
#
# Port del indicador "Supertrend" de TradingView (Pine Script v6, ta.supertrend) con los
# mismos parametros por defecto (ATR Length 10, Factor 3.0). Solo velas cerradas.
# El algoritmo es el estandar publicado en la documentacion de Pine Script.
# ------------------------------------------------------------------------------
"""Alerta de Supertrend: cambio de tendencia bajista -> alcista o alcista -> bajista."""

NAME = "supertrend"
ATR_PERIOD = 10
FACTOR = 3.0


def _supertrend(candles, period, factor):
    """Devuelve (direccion, linea). Direccion: -1 = tendencia alcista, +1 = tendencia bajista."""
    n = len(candles)
    tr = []
    for i, c in enumerate(candles):
        hl = c[2] - c[3]
        tr.append(hl if i == 0 else max(hl, abs(c[2] - candles[i - 1][4]), abs(c[3] - candles[i - 1][4])))
    atr = [None] * n                                   # ta.atr = RMA(tr, period)
    if n >= period:
        a = sum(tr[:period]) / period
        atr[period - 1] = a
        for i in range(period, n):
            a = (a * (period - 1) + tr[i]) / period
            atr[i] = a

    nz = lambda v: 0.0 if v is None else v
    ub, lb, st, dirs = [None] * n, [None] * n, [None] * n, [1] * n
    for i in range(n):
        close = candles[i][4]
        src = (candles[i][2] + candles[i][3]) / 2      # hl2
        raw_u = None if atr[i] is None else src + factor * atr[i]
        raw_l = None if atr[i] is None else src - factor * atr[i]
        prev_l = nz(lb[i - 1]) if i > 0 else 0.0
        prev_u = nz(ub[i - 1]) if i > 0 else 0.0
        prev_close = candles[i - 1][4] if i > 0 else None

        if (raw_l is not None and raw_l > prev_l) or (prev_close is not None and prev_close < prev_l):
            lb[i] = raw_l
        else:
            lb[i] = prev_l
        if (raw_u is not None and raw_u < prev_u) or (prev_close is not None and prev_close > prev_u):
            ub[i] = raw_u
        else:
            ub[i] = prev_u

        if i == 0 or atr[i - 1] is None:
            d = 1
        elif st[i - 1] == prev_u:
            d = -1 if close > ub[i] else 1
        else:
            d = 1 if close < lb[i] else -1
        dirs[i] = d
        st[i] = lb[i] if d == -1 else ub[i]
    return dirs, st


def analyze(candles):
    """candles: [(t, o, h, l, c)] solo cerradas. Devuelve señales (dict) por cada cambio de tendencia."""
    dirs, st = _supertrend(candles, ATR_PERIOD, FACTOR)
    out = []
    for i in range(1, len(candles)):
        if dirs[i] != dirs[i - 1]:
            out.append({"bar": i, "to": "up" if dirs[i] == -1 else "down", "line": st[i]})
    return out


def format(ev, sym, tf, candles, now_ms, h):
    bar = ev["bar"]
    up = ev["to"] == "up"
    closed_ms = candles[bar][0] + h.TF_MS[tf]
    lag = max(0, (now_ms - closed_ms) // 1000)
    headline = "CAMBIO DE TENDENCIA ⬆️ ALCISTA" if up else "CAMBIO DE TENDENCIA ⬇️ BAJISTA"
    tag = f"ATR {ATR_PERIOD} · factor {FACTOR:g}"
    rows = [
        "🔁 De tendencia bajista a alcista" if up else "🔁 De tendencia alcista a bajista",
        f"🕯 Vela: <code>{h.fmt_time(candles[bar][0])}</code> ({h.esc(h.TZ)})",
        f"✅ Cerró <code>{h.fmt_time(closed_ms, '%H:%M:%S')}</code> · demora <code>{lag // 60}m{lag % 60:02d}s</code>",
        f"📍 Línea Supertrend <code>{ev['line']:.8g}</code> · Cierre <code>{candles[bar][4]:g}</code>",
    ]
    return h.card(NAME, headline, sym, tf, tag, rows)
