# ------------------------------------------------------------------------------
# Modulo Smart Money Concepts (port de Python) - SOLO Swing Structure y Swing Order Blocks
#
# Derivative work based on "Smart Money Concepts" (c) LuxAlgo, published on TradingView
# under the Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
# license (CC BY-NC-SA 4.0): https://creativecommons.org/licenses/by-nc-sa/4.0/
#
# Changes: rewritten from Pine Script to Python; only the swing structure (BOS / CHoCH)
# and swing order block alerts were kept (internal structure, internal order blocks,
# EQH/EQL, FVG, zones, MTF levels and all drawings removed); only closed candles are used.
# Distributed under the same license (CC BY-NC-SA 4.0): attribution, NON-COMMERCIAL, share-alike.
# ------------------------------------------------------------------------------
"""Alertas SMC: BOS / CHoCH de estructura swing y ruptura de Swing Order Blocks."""

NAME = "smc"
SWING_LENGTH = 50            # "Show Swings Points" del indicador (por defecto 50)
OB_MITIGATION = "highlow"    # "highlow" (por defecto) o "close"
ATR_LENGTH = 200             # el indicador usa ta.atr(200) para filtrar velas muy volatiles
MAX_OBS = 100                # el indicador guarda como maximo 100 order blocks

BULLISH, BEARISH = 1, -1


def _atr(candles, length):
    """ta.atr(length): RMA del true range. Devuelve None hasta tener 'length' velas."""
    n = len(candles)
    tr = []
    for i, c in enumerate(candles):
        hl = c[2] - c[3]
        tr.append(hl if i == 0 else max(hl, abs(c[2] - candles[i - 1][4]), abs(c[3] - candles[i - 1][4])))
    out = [None] * n
    if n >= length:
        a = sum(tr[:length]) / length
        out[length - 1] = a
        for i in range(length, n):
            a = (a * (length - 1) + tr[i]) / length
            out[i] = a
    return out


def analyze(candles):
    """candles: [(t, o, h, l, c)] solo cerradas. Devuelve señales (dict) ordenadas por vela."""
    n = len(candles)
    tm = [c[0] for c in candles]
    hi = [c[2] for c in candles]
    lo = [c[3] for c in candles]
    cl = [c[4] for c in candles]
    size = SWING_LENGTH
    atr = _atr(candles, ATR_LENGTH)

    # velas muy volatiles (rango >= 2 ATR) se "invierten" para ubicar el order block
    p_high, p_low = [], []
    for i in range(n):
        vol = atr[i] is not None and (hi[i] - lo[i]) >= 2 * atr[i]
        p_high.append(lo[i] if vol else hi[i])
        p_low.append(hi[i] if vol else lo[i])

    swing_high = dict(level=None, last=None, crossed=False, bar=None)
    swing_low = dict(level=None, last=None, crossed=False, bar=None)
    trend = 0                       # 0 sin definir, +1 alcista, -1 bajista
    leg, prev_leg = 0, 0
    prev_hi_level = prev_lo_level = None
    obs = []                        # order blocks swing vigentes (el mas nuevo primero)
    events = []

    def store_ob(pivot_bar, i, bias):
        if bias == BEARISH:
            seg = p_high[pivot_bar:i]
            idx = pivot_bar + seg.index(max(seg))
        else:
            seg = p_low[pivot_bar:i]
            idx = pivot_bar + seg.index(min(seg))
        ob = dict(high=p_high[idx], low=p_low[idx], time=tm[idx], bias=bias)
        if len(obs) >= MAX_OBS:
            obs.pop()
        obs.insert(0, ob)
        return ob

    for i in range(n):
        # ---- pierna actual (leg): nuevo maximo/minimo respecto a las ultimas 'size' velas ----
        if i >= size:
            if hi[i - size] > max(hi[i - size + 1:i + 1]):
                leg = 0
            elif lo[i - size] < min(lo[i - size + 1:i + 1]):
                leg = 1
        if i > 0 and leg != prev_leg:
            if leg - prev_leg == 1:    # empieza pierna alcista: pivote bajo
                swing_low = dict(level=lo[i - size], last=swing_low["level"], crossed=False, bar=i - size)
            else:                      # empieza pierna bajista: pivote alto
                swing_high = dict(level=hi[i - size], last=swing_high["level"], crossed=False, bar=i - size)
        prev_leg = leg

        # ---- ruptura alcista: el cierre cruza hacia arriba el ultimo swing alto ----
        lvl = swing_high["level"]
        if (lvl is not None and prev_hi_level is not None and i > 0
                and cl[i] > lvl and cl[i - 1] <= prev_hi_level and not swing_high["crossed"]):
            tag = "CHoCH" if trend == BEARISH else "BOS"
            swing_high["crossed"] = True
            trend = BULLISH
            ob = store_ob(swing_high["bar"], i, BULLISH)
            events.append(dict(bar=i, kind="structure", tag=tag, side=BULLISH, level=lvl,
                               pivot_time=tm[swing_high["bar"]], ob=ob))
        prev_hi_level = lvl

        # ---- ruptura bajista: el cierre cruza hacia abajo el ultimo swing bajo ----
        lvl = swing_low["level"]
        if (lvl is not None and prev_lo_level is not None and i > 0
                and cl[i] < lvl and cl[i - 1] >= prev_lo_level and not swing_low["crossed"]):
            tag = "CHoCH" if trend == BULLISH else "BOS"
            swing_low["crossed"] = True
            trend = BEARISH
            ob = store_ob(swing_low["bar"], i, BEARISH)
            events.append(dict(bar=i, kind="structure", tag=tag, side=BEARISH, level=lvl,
                               pivot_time=tm[swing_low["bar"]], ob=ob))
        prev_lo_level = lvl

        # ---- order blocks rotos (mitigados) ----
        src_bear = cl[i] if OB_MITIGATION == "close" else hi[i]
        src_bull = cl[i] if OB_MITIGATION == "close" else lo[i]
        keep, broke_bear, broke_bull = [], [], []
        for ob in obs:
            if ob["bias"] == BEARISH and src_bear > ob["high"]:
                broke_bear.append(ob)
            elif ob["bias"] == BULLISH and src_bull < ob["low"]:
                broke_bull.append(ob)
            else:
                keep.append(ob)
        obs[:] = keep
        if broke_bull:
            events.append(dict(bar=i, kind="ob_break", bias=BULLISH, zones=broke_bull))
        if broke_bear:
            events.append(dict(bar=i, kind="ob_break", bias=BEARISH, zones=broke_bear))
    return events


def format(ev, sym, tf, candles, now_ms, h):
    bar = ev["bar"]
    closed_ms = candles[bar][0] + h.TF_MS[tf]
    lag = max(0, (now_ms - closed_ms) // 1000)
    tag = f"swing {SWING_LENGTH}"
    when = [
        f"🕯 Vela: <code>{h.fmt_time(candles[bar][0])}</code> ({h.esc(h.TZ)})",
        f"✅ Cerró <code>{h.fmt_time(closed_ms, '%H:%M:%S')}</code> · demora <code>{lag // 60}m{lag % 60:02d}s</code>",
    ]

    if ev["kind"] == "structure":
        up = ev["side"] == BULLISH
        headline = f"{ev['tag']} {'ALCISTA ⬆️' if up else 'BAJISTA ⬇️'}"
        meaning = ("🔄 Cambio de carácter: rompe contra la tendencia previa" if ev["tag"] == "CHoCH"
                   else "➡️ Continuación: rompe a favor de la tendencia previa")
        ob = ev["ob"]
        rows = [meaning] + when + [
            f"📍 Rompió el swing {'alto' if up else 'bajo'} <code>{ev['level']:g}</code> "
            f"(del <code>{h.fmt_time(ev['pivot_time'])}</code>) · Cierre <code>{candles[bar][4]:g}</code>",
            f"🧱 Order block: <code>{ob['low']:g} – {ob['high']:g}</code> (vela <code>{h.fmt_time(ob['time'])}</code>)",
        ]
        return h.card(NAME, headline, sym, tf, tag, rows)

    # ruptura de order block
    bull = ev["bias"] == BULLISH
    headline = ("SWING ORDER BLOCK ALCISTA ROTO ⬇️" if bull else "SWING ORDER BLOCK BAJISTA ROTO ⬆️")
    how = "El precio perforó la zona por debajo" if bull else "El precio superó la zona por arriba"
    rows = [f"💥 {how}"] + when
    zones = ev["zones"]
    for z in zones[:3]:
        rows.append(f"🧱 Zona <code>{z['low']:g} – {z['high']:g}</code> (vela <code>{h.fmt_time(z['time'])}</code>)")
    if len(zones) > 3:
        rows.append(f"… y {len(zones) - 3} más")
    rows.append(f"💵 Cierre <code>{candles[bar][4]:g}</code>")
    return h.card(NAME, headline, sym, tf, tag, rows)
