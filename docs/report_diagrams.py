"""
Every diagram in the report, one function each.

Each function returns a ReportLab Drawing sized to the content width, so the
build script can drop it straight into the story. Keep the drawing code here
and the prose in build_report.py.
"""
from __future__ import annotations

from reportlab.graphics.shapes import Circle, Line, Polygon, Rect, String
from reportlab.lib.colors import HexColor, white

from report_kit import (
    ACCENT, ACCENT_MID, ACCENT_SOFT, BAD, BAD_SOFT, BODY, CONTENT_W, FAINT,
    GOOD, GOOD_SOFT, INK, MUTED, NAVY, PAPER, PLUM, PLUM_SOFT, RULE, TEAL,
    TEAL_SOFT, WARN, WARN_SOFT, arrow, band, box, canvas, elbow, pill, text,
)

W = CONTENT_W


# ───────────────────────────────────────────────────────────── D1
def d_stack(with_status=False):
    """The five layers, each with the question it answers and its blind spot."""
    rows = [
        ("L5", "LLM + tool use", PLUM, PLUM_SOFT,
         "\"What should I do about it, and why?\" Orchestration, judgment under "
         "ambiguity, and prose a human can act on.",
         "Cannot produce a calibrated number. Will invent one if you let it.",
         "built"),
        ("L4", "Deep learning", TEAL, TEAL_SOFT,
         "\"What does this unstructured thing mean?\" Text, sequences, "
         "long-range structure — a headline, a price path.",
         "Needs a lot of data. Beaten by gradient boosting on small tabular sets.",
         "partial"),
        ("L3", "Traditional ML", ACCENT, ACCENT_SOFT,
         "\"Given 35 numbers describing today, what is the probability of an "
         "up move?\" Tabular, cheap, calibratable.",
         "Blind to text, to context it was not given, and to anything new.",
         "facade"),
        ("L2", "Feature engineering", WARN, WARN_SOFT,
         "\"What shape is this series in?\" RSI, MACD, SMA, Bollinger turn raw "
         "OHLCV into something learnable.",
         "Decides what the model can possibly see. A missing feature is a blind spot.",
         "built"),
        ("L1", "Backend systems", NAVY, PAPER,
         "\"What is actually true right now?\" Positions, prices, cash, orders, "
         "history, identity, schedule.",
         "Knows facts, infers nothing. No opinion, no forecast, no explanation.",
         "built"),
    ]
    bh, gap = 46, 6.5
    h = len(rows) * bh + (len(rows) - 1) * gap + 20
    d = canvas(h=h)

    tab_w, mid_w = 112, 185
    tab_x, mid_x = 0, 118
    right_x = 310
    right_w = W - right_x - (58 if with_status else 0)

    text(d, tab_x, h - 9, "LAYER", size=6.6, color=FAINT, bold=True)
    text(d, mid_x, h - 9, "THE QUESTION IT ANSWERS", size=6.6, color=FAINT, bold=True)
    text(d, right_x, h - 9, "WHAT IT CANNOT DO", size=6.6, color=FAINT, bold=True)
    if with_status:
        text(d, W - 52, h - 9, "IN THIS REPO", size=6.6, color=FAINT, bold=True)

    y = h - 20 - bh
    for tag, name, c, soft, q, cant, st in rows:
        box(d, tab_x, y, tab_w, bh, name, fill=soft, stroke=c, tc=c, size=8.4,
            sub=tag, sub_size=6.6, sub_color=c, stroke_w=1.0)
        box(d, mid_x, y, mid_w, bh, "", fill=white, stroke=RULE)
        _para(d, mid_x + 7, y, mid_w - 14, bh, q, size=7.4, color=BODY)
        box(d, right_x, y, right_w, bh, "", fill=white, stroke=RULE)
        _para(d, right_x + 7, y, right_w - 14, bh, cant, size=7.4, color=MUTED)
        if with_status:
            from report_kit import STATUS
            label, fg, bg = STATUS[st]
            pill(d, W - 52, y + bh / 2 - 6.5, label, fill=bg, tc=fg)
        y -= bh + gap
    return d


def _para(d, x, y, w, h, s, *, size=7.4, color=BODY, bold=False):
    """Left-aligned, vertically centred wrapped text inside a given rect."""
    from report_kit import wrap
    font = "Helvetica-Bold" if bold else "Helvetica"
    lines = wrap(s, font, size, w)
    lh = size * 1.26
    ty = y + h / 2 + (len(lines) * lh) / 2 - lh * 0.78
    for ln in lines:
        d.add(String(x, ty, ln, fontName=font, fontSize=size, fillColor=color))
        ty -= lh


# ───────────────────────────────────────────────────────────── D2
def d_two_pipelines():
    """The ML pipeline and the agent loop, and the one wire that joins them."""
    h = 292
    d = canvas(h=h)

    lw, lx = 150, 0
    rx = 196
    rw = W - rx

    text(d, lx, h - 10, "THE PREDICTION PIPELINE  (DETERMINISTIC)", size=6.6,
         color=FAINT, bold=True)
    text(d, rx, h - 10, "THE AGENT LOOP  (GENERATIVE)", size=6.6, color=FAINT,
         bold=True)
    band(d, lx - 5, 6, lw + 10, h - 28, "")
    band(d, rx - 5, 6, rw + 5, h - 28, "")

    # Left: candles -> features -> model -> probability
    chain = [
        ("OHLCV candles", "one fixed interval, enforced"),
        ("Feature engineering", "RSI, MACD, SMA, BB, ADX, ATR, volume, returns"),
        ("LightGBM", "one model per horizon"),
        ("Calibrated probability", "p(up) with a reliability guarantee"),
    ]
    bh, bgap = 42, 18
    top = h - 36
    last_box = None
    for i, (t, s) in enumerate(chain):
        c = ACCENT if i >= 2 else WARN
        soft = ACCENT_SOFT if i >= 2 else WARN_SOFT
        y = top - i * (bh + bgap) - bh
        box(d, lx, y, lw, bh, t, fill=soft, stroke=c, tc=c, size=8.0,
            sub=s, sub_size=6.3, sub_color=MUTED)
        if i:
            arrow(d, lx + lw / 2, y + bh + bgap, lx + lw / 2, y + bh,
                  color=c, w=1.2)
        last_box = (lx, y, lw, bh)

    # Right: user -> agent -> llm -> four tools
    uw = 124
    ux = rx + (rw - uw) / 2
    spine = ux + uw / 2
    stack = [("User question", white, MUTED, INK),
             ("Agent runtime", PLUM_SOFT, PLUM, PLUM),
             ("LLM", PLUM_SOFT, PLUM, PLUM)]
    sh, sgap = 27, 24
    for i, (t, fillc, strokec, tc) in enumerate(stack):
        y = top - i * (sh + sgap) - sh
        box(d, ux, y, uw, sh, t, fill=fillc, stroke=strokec, tc=tc, size=8.2)
        if i:
            arrow(d, spine, y + sh + sgap, spine, y + sh, color=PLUM, w=1.2)
    llm_bottom = top - 2 * (sh + sgap) - sh

    tools = [
        ("Portfolio tool", GOOD, GOOD_SOFT, "built"),
        ("Market tool", GOOD, GOOD_SOFT, "built"),
        ("ML prediction tool", BAD, BAD_SOFT, "not wired"),
        ("Research tool", GOOD, GOOD_SOFT, "built"),
    ]
    tw, th = (rw - 14) / 2, 38
    row_top = llm_bottom - 26
    coords = {}
    for i, (t, c, soft, note) in enumerate(tools):
        col, row = i % 2, i // 2
        x = rx + col * (tw + 14)
        y = row_top - row * (th + 14) - th
        box(d, x, y, tw, th, t, fill=soft, stroke=c, tc=c, size=7.8,
            sub=note, sub_size=6.3, sub_color=c,
            dash=[2.5, 2.5] if note == "not wired" else None)
        coords[t] = (x, y, tw, th)

    # The spine drops between the two tool columns, branching into each
    bottom_row_mid = row_top - (th + 14) - th / 2
    d.add(Line(spine, llm_bottom, spine, bottom_row_mid, strokeColor=PLUM,
               strokeWidth=1.0))
    for name, (x, y, w2, h2) in coords.items():
        mid = y + h2 / 2
        if x < spine:
            arrow(d, spine, mid, x + w2 + 1, mid, color=PLUM, w=0.9, head=4.4)
        else:
            arrow(d, spine, mid, x - 1, mid, color=PLUM, w=0.9, head=4.4)

    # The wire that does not exist
    lx0, ly0, lw0, lh0 = last_box
    mx, my, mw, mh = coords["ML prediction tool"]
    arrow(d, lx0 + lw0 + 2, ly0 + lh0 / 2, mx - 3, my + mh / 2, color=BAD,
          w=1.4, dash=[3, 2.5])
    text(d, (lx0 + lw0 + mx) / 2, ly0 + lh0 / 2 - 13, "the wire that is missing",
         size=6.8, color=BAD, bold=True, anchor="middle")
    return d


# ───────────────────────────────────────────────────────────── D3
def d_current_architecture():
    """What is in the repo today, colour-coded by how much of it really works."""
    h = 292
    d = canvas(h=h)
    y = h

    def row_label(yy, s):
        text(d, 0, yy, s.upper(), size=6.6, color=FAINT, bold=True)

    # Clients
    y -= 10
    row_label(y, "clients")
    y -= 36
    cw = (W - 24) / 3
    for i, (t, s) in enumerate([
        ("Web dashboard", "Jinja + SSE prices"),
        ("MCP client", "Cursor, Claude Desktop"),
        ("WhatsApp", "daily briefing"),
    ]):
        box(d, i * (cw + 12), y, cw, 32, t, fill=white, stroke=MUTED, tc=INK,
            size=8.0, sub=s, sub_size=6.3)

    # Entry
    y -= 46
    box(d, 0, y, W, 28, "One Uvicorn process  —  main.py mounts FastAPI at / and FastMCP (SSE) at /mcp",
        fill=NAVY, stroke=NAVY, tc=white, size=8.2)
    arrow(d, W / 2, y + 46, W / 2, y + 29, color=MUTED, w=1.1)

    # Orchestration
    y -= 16
    row_label(y, "orchestration  ·  layer 5")
    y -= 40
    ow = (W - 36) / 4
    for i, (t, s, c, soft) in enumerate([
        ("Finance agent", "27 ADK tools", PLUM, PLUM_SOFT),
        ("Trading agent", "11 tools, proposal-gated", PLUM, PLUM_SOFT),
        ("MCP server", "20 tools", PLUM, PLUM_SOFT),
        ("Scheduler", "APScheduler, 1 min tick", NAVY, PAPER),
    ]):
        box(d, i * (ow + 12), y, ow, 36, t, fill=soft, stroke=c, tc=c,
            size=7.9, sub=s, sub_size=6.3, sub_color=MUTED)

    # Analysis
    y -= 16
    row_label(y, "analysis  ·  layers 2, 3, 4")
    y -= 42
    aw = (W - 60) / 6
    analysis = [
        ("Technical", "RSI/MACD/BB/SMA", GOOD, GOOD_SOFT),
        ("Fundamental", "score vs glossary", GOOD, GOOD_SOFT),
        ("Sentiment", "FinBERT, real DL", WARN, WARN_SOFT),
        ("Sector", "breadth, weights", WARN, WARN_SOFT),
        ("News", "gnews fetch", WARN, WARN_SOFT),
        ("Prediction", "no model on disk", BAD, BAD_SOFT),
    ]
    for i, (t, s, c, soft) in enumerate(analysis):
        box(d, i * (aw + 12), y, aw, 38, t, fill=soft, stroke=c, tc=c,
            size=7.7, sub=s, sub_size=6.0, sub_color=c,
            dash=[2.5, 2.5] if c is BAD else None)

    # Data
    y -= 16
    row_label(y, "data and state  ·  layer 1")
    y -= 40
    dw = (W - 36) / 4
    for i, (t, s) in enumerate([
        ("Angel One SmartAPI", "holdings, orders, candles"),
        ("Postgres (Neon)", "users, schedules, logs"),
        ("yfinance / nselib / gnews", "fundamentals, breadth, news"),
        ("Session manager", "in-RAM, per login"),
    ]):
        box(d, i * (dw + 12), y, dw, 34, t, fill=PAPER, stroke=MUTED, tc=INK,
            size=7.6, sub=s, sub_size=6.2)

    # Legend
    y -= 22
    lx = 0
    for key, label in [("built", "works as described"),
                       ("partial", "works, but isolated from the model"),
                       ("facade", "pipeline exists, model does not")]:
        from report_kit import STATUS
        _, fg, bg = STATUS[key]
        wpill = pill(d, lx, y, STATUS[key][0], fill=bg, tc=fg)
        text(d, lx + wpill + 5, y + 4, label, size=6.8, color=MUTED)
        lx += wpill + 10 + len(label) * 3.5
    return d


# ───────────────────────────────────────────────────────────── D4
def d_train_serve_skew():
    """One table of feature names, two completely different distributions."""
    h = 248
    d = canvas(h=h)
    colw = (W - 70) / 2
    rx = colw + 70

    box(d, 0, h - 26, colw, 24, "WHAT TRAINING SAW", fill=WARN_SOFT,
        stroke=WARN, tc=WARN, size=8.0)
    box(d, rx, h - 26, colw, 24, "WHAT SERVING SENDS", fill=BAD_SOFT,
        stroke=BAD, tc=BAD, size=8.0)

    left = [
        "interval = FIVE_MINUTE",
        "30 calendar days",
        "20 symbols",
        "~5,600 bars per symbol",
        "fallback: silently mixes in",
        "ONE_DAY rows when the",
        "5-minute fetch fails",
    ]
    right = [
        "interval = ONE_DAY",
        "90 calendar days",
        "whatever the user opened",
        "~60 bars",
        "dashboard, MCP tool and",
        "the WhatsApp briefing all",
        "send daily candles",
    ]
    for i, (a, b) in enumerate(zip(left, right)):
        yy = h - 48 - i * 13
        mono = "Courier-Bold" if i == 0 else "Helvetica"
        d.add(String(6, yy, a, fontName=mono, fontSize=7.3,
                     fillColor=WARN if i == 0 else BODY))
        d.add(String(rx + 6, yy, b, fontName=mono, fontSize=7.3,
                     fillColor=BAD if i == 0 else BODY))

    # the collision
    cx = colw + 35
    d.add(Circle(cx, h - 92, 26, fillColor=white, strokeColor=BAD,
                 strokeWidth=1.4))
    text(d, cx, h - 86, "SAME", size=7.0, color=BAD, bold=True, anchor="middle")
    text(d, cx, h - 95, "35", size=10.5, color=BAD, bold=True, anchor="middle")
    text(d, cx, h - 105, "NAMES", size=7.0, color=BAD, bold=True, anchor="middle")

    # Feature scale comparison
    yb = 14
    box(d, 0, yb, W, 92, "", fill=PAPER, stroke=RULE)
    text(d, 10, yb + 80, "THE SAME FEATURE, TWO SCALES", size=6.8,
         color=FAINT, bold=True)
    cols = [("feature", 120), ("on a 5-minute bar", 150), ("on a daily bar", 130),
            ("factor", 60)]
    cx2 = 10
    for name, cw in cols:
        text(d, cx2, yb + 65, name, size=6.6, color=MUTED, bold=True)
        cx2 += cw
    comp = [
        ("return_1", "std ~0.10 %", "std ~1.5 %", "15x"),
        ("volatility_20", "~0.0012", "~0.016", "13x"),
        ("atr_14_pct", "~0.0015", "~0.021", "14x"),
        ("volume_ratio", "intraday U-shape", "no intraday shape", "shape"),
        ("hour_sin / hour_cos", "varies all session", "constant", "dead"),
    ]
    for i, row in enumerate(comp):
        yy = yb + 50 - i * 11
        cx2 = 10
        for j, (val, (_, cw)) in enumerate(zip(row, cols)):
            fn = "Courier" if j == 0 else "Helvetica"
            col = INK if j == 0 else (BAD if j == 3 else BODY)
            fb = "Helvetica-Bold" if j == 3 else fn
            d.add(String(cx2, yy, val, fontName=fb, fontSize=7.0, fillColor=col))
            cx2 += cw
    return d


# ───────────────────────────────────────────────────────────── D5
def d_leakage():
    """Why shuffle=True on a sliding window reports a number you cannot keep."""
    h = 286
    d = canvas(h=h)

    # ── A: how rows are built
    y = h - 11
    text(d, 0, y, "A  ·  HOW ROWS ARE BUILT — A WINDOW THAT SLIDES ONE BAR AT A TIME",
         size=6.8, color=FAINT, bold=True)
    y -= 20
    wdt = 168
    for i in range(3):
        yy = y - i * 15
        d.add(Rect(i * 14, yy, wdt, 11, fillColor=ACCENT_SOFT,
                   strokeColor=ACCENT, strokeWidth=0.6))
        text(d, i * 14 + wdt + 8, yy + 2.8,
             f"row {i + 1}  =  bars {i + 1} to {i + 50}", size=6.9, color=BODY)
    y -= 2 * 15 + 20
    text(d, 0, y, "Row 1 and row 2 share 49 of their 50 bars, and their forward "
                  "label windows overlap almost completely. They are near-duplicates.",
         size=7.1, color=BAD)

    # ── B: shuffled split
    y -= 26
    text(d, 0, y, "B  ·  TODAY — train_test_split(shuffle=True)", size=7.0,
         color=BAD, bold=True)
    y -= 22
    n = 40
    cw = W / n
    import random
    random.seed(7)
    for i in range(n):
        is_test = random.random() < 0.25
        d.add(Rect(i * cw, y, cw - 1, 16,
                   fillColor=BAD_SOFT if is_test else ACCENT_SOFT,
                   strokeColor=BAD if is_test else ACCENT_MID, strokeWidth=0.5))
    text(d, 0, y - 11, "Red cells are test rows. Each one sits between training "
                       "rows that share its history — the model recognises its "
                       "neighbours, it does not forecast.",
         size=7.1, color=BAD)

    # ── C: purged walk-forward
    y -= 36
    text(d, 0, y, "C  ·  CORRECT — purged, embargoed walk-forward", size=7.0,
         color=GOOD, bold=True)
    y -= 20
    fold_h, fold_gap = 14, 5
    for k, b in enumerate([0.40, 0.58, 0.76]):
        yy = y - k * (fold_h + fold_gap) - fold_h
        tr_w = b * W
        d.add(Rect(0, yy, tr_w, fold_h, fillColor=ACCENT_SOFT,
                   strokeColor=ACCENT_MID, strokeWidth=0.5))
        text(d, 5, yy + 4.2, "train", size=6.4, color=ACCENT)
        pg_x, pg_w = tr_w, 0.045 * W
        d.add(Rect(pg_x, yy, pg_w, fold_h, fillColor=HexColor("#D1D5DB"),
                   strokeColor=MUTED, strokeWidth=0.5))
        te_x, te_w = pg_x + pg_w, 0.13 * W
        d.add(Rect(te_x, yy, te_w, fold_h, fillColor=GOOD_SOFT,
                   strokeColor=GOOD, strokeWidth=0.6))
        text(d, te_x + 5, yy + 4.2, "test", size=6.4, color=GOOD)
        d.add(Rect(te_x + te_w, yy, 0.04 * W, fold_h,
                   fillColor=HexColor("#E5E7EB"), strokeColor=FAINT,
                   strokeWidth=0.5))
    y -= 3 * (fold_h + fold_gap) + 8
    text(d, 0, y, "Grey = purge (drop training rows whose label window reaches "
                  "into the test block), then embargo (a gap before training resumes).",
         size=7.1, color=MUTED)
    text(d, 0, y - 12, "Expect the headline number to FALL when you switch to this. "
                       "That is the point — the lower number is the real one.",
         size=7.1, color=GOOD, bold=True)
    return d


# ───────────────────────────────────────────────────────────── D6
def d_target_pipeline():
    """The nine stages of a training pipeline you could defend to a quant."""
    h = 234
    d = canvas(h=h)
    stages = [
        ("1 Data contract", "one interval, one universe, point-in-time, written into model metadata", NAVY, PAPER),
        ("2 Event sampling", "CUSUM filter: keep bars where something happened, drop the rest", WARN, WARN_SOFT),
        ("3 Triple-barrier labels", "+k*ATR profit, -k*ATR stop, horizon cap; label = first barrier hit", WARN, WARN_SOFT),
        ("4 Sample weights", "down-weight overlapping labels by uniqueness", WARN, WARN_SOFT),
        ("5 Feature matrix", "cross-sectional ranks, market and sector context, fundamentals, sentiment", WARN, WARN_SOFT),
        ("6 Purged walk-forward CV", "time-ordered folds, purge plus embargo, no shuffle ever", ACCENT, ACCENT_SOFT),
        ("7 Calibration", "isotonic on a held-out time slice, so 0.70 means 70 %", ACCENT, ACCENT_SOFT),
        ("8 Meta-labelling", "a second model sizes the bet: will the primary signal be right?", ACCENT, ACCENT_SOFT),
        ("9 Registry plus drift", "model, features, interval, dates, metrics, git SHA; score live calls weekly", GOOD, GOOD_SOFT),
    ]
    cols = 3
    bw = (W - 2 * 16) / cols
    bh = 58
    for i, (t, s, c, soft) in enumerate(stages):
        col, row = i % cols, i // cols
        x = col * (bw + 16)
        y = h - 24 - row * (bh + 24) - bh
        box(d, x, y, bw, bh, t, fill=soft, stroke=c, tc=c, size=8.0,
            sub=s, sub_size=6.4, sub_color=MUTED)
        if col < cols - 1:
            arrow(d, x + bw + 1, y + bh / 2, x + bw + 14, y + bh / 2,
                  color=c, w=1.1, head=4.6)
        elif row < 2:
            # snake back to the start of the next row
            d.add(Line(x + bw / 2, y, x + bw / 2, y - 11, strokeColor=c,
                       strokeWidth=1.0))
            d.add(Line(x + bw / 2, y - 11, bw / 2, y - 11, strokeColor=c,
                       strokeWidth=1.0))
            arrow(d, bw / 2, y - 11, bw / 2, y - 23, color=c, w=1.0, head=4.6)
    text(d, 0, 4, "Stages 2, 3, 4 and 6 are the ones that decide whether your "
                  "measured performance survives contact with live data.",
         size=7.0, color=MUTED)
    return d


# ───────────────────────────────────────────────────────────── D7
def d_dossier():
    """The eight panels of a full stock dossier, and how the memo is written."""
    h = 278
    d = canvas(h=h)

    box(d, (W - 190) / 2, h - 28, 190, 26, "Analyst selects a stock",
        fill=NAVY, stroke=NAVY, tc=white, size=8.4)

    text(d, 0, h - 44, "EIGHT PANELS, ASSEMBLED IN PARALLEL  —  A SLOW PANEL "
                       "RENDERS LATE, IT DOES NOT BLOCK THE PAGE",
         size=6.6, color=FAINT, bold=True)

    panels = [
        ("Identity and mandate fit", "cap band, index, liquidity tier", "partial"),
        ("Price and technical state", "indicators plus a regime label", "built"),
        ("Valuation and quality", "score, 4 pillars, peer percentile", "partial"),
        ("Earnings and estimates", "surprise history, revisions", "missing"),
        ("Ownership and flows", "promoter, FII/DII, pledge, MF holding", "missing"),
        ("News and narrative", "deduped, event-typed, materiality", "partial"),
        ("Model view", "calibrated p per horizon, SHAP drivers", "facade"),
        ("Risk and liquidity", "ADV, days to exit, impact, beta, factors", "partial"),
    ]
    from report_kit import STATUS
    cols = 4
    bw = (W - 3 * 11) / cols
    bh = 58
    top = h - 56
    for i, (t, s, st) in enumerate(panels):
        col, row = i % cols, i // cols
        x = col * (bw + 11)
        y = top - row * (bh + 11) - bh
        _, fg, bg = STATUS[st]
        box(d, x, y, bw, bh, t, fill=bg, stroke=fg, tc=fg, size=7.6,
            sub=s, sub_size=6.2, sub_color=MUTED,
            dash=[2.5, 2.5] if st in ("missing", "facade") else None)
        pill(d, x + 5, y + 5, STATUS[st][0], fill=white, tc=fg, size=6.0, h=11)

    # Payload -> narrative -> verifier
    y = top - 2 * (bh + 11) - 34
    box(d, 0, y, W, 28,
        "Dossier payload  —  every number carries its source, its as-of timestamp and its unit",
        fill=TEAL_SOFT, stroke=TEAL, tc=TEAL, size=8.0)
    arrow(d, W / 2, y + 44, W / 2, y + 29, color=MUTED, w=1.1)

    y -= 44
    hw = (W - 14) / 2
    box(d, 0, y, hw, 32, "LLM writes the memo",
        fill=PLUM_SOFT, stroke=PLUM, tc=PLUM, size=8.0,
        sub="hard rule: no number that is not in the payload", sub_size=6.3,
        sub_color=MUTED)
    box(d, hw + 14, y, hw, 32, "Verifier re-checks the draft",
        fill=PLUM_SOFT, stroke=PLUM, tc=PLUM, size=8.0,
        sub="every figure matched back to the payload before display", sub_size=6.3,
        sub_color=MUTED)
    arrow(d, W / 2, y + 44, W / 2, y + 33, color=MUTED, w=1.1)
    arrow(d, hw + 1, y + 16, hw + 12, y + 16, color=PLUM, w=1.1, head=4.6)
    return d


# ───────────────────────────────────────────────────────────── D8
def d_news_to_stock():
    """News to sector to stock: six stages, with the design decision in each."""
    # 6 boxes of 56 + 5 gaps of 11 + top margin; sized exactly so the last box
    # does not spill past the drawing and collide with the caption.
    h = 14 + 6 * 56 + 5 * 11 + 6
    d = canvas(h=h)
    bw = 196
    nw = W - bw - 18
    stages = [
        ("1  Ingest and normalise",
         "exchange filings, RBI/SEBI circulars, global macro, commodities, FX, freight",
         "Headlines alone are not enough. Filings and policy text are where unpriced "
         "information actually lives. Dedupe near-identical copies; keep source, "
         "jurisdiction and publish time.", NAVY, PAPER),
        ("2  Extract typed events",
         "LLM with a structured output schema, not a sentiment score",
         "The key move: map news to ECONOMIC DRIVERS, not to sectors. \"OPEC cuts "
         "output\" becomes driver = crude_oil_up, magnitude = high, horizon = quarters.", PLUM, PLUM_SOFT),
        ("3  Driver x sector exposure matrix",
         "analyst priors for the explanation, regression betas for the score",
         "Rows are drivers, columns are sectors, cells are signed sensitivities. "
         "Estimate them by regressing 10 years of sector returns on driver changes "
         "— do not hand-wave them.", TEAL, TEAL_SOFT),
        ("4  Score the sector",
         "news impulse + macro cycle position + market confirmation",
         "Three independent blocks. When they disagree, SHOW the disagreement instead "
         "of averaging it away: \"macro favours utilities, price does not — you are "
         "early, or wrong.\"", ACCENT, ACCENT_SOFT),
        ("5  Rank stocks inside the sector",
         "company-level exposure, model score, quality, liquidity filter",
         "Not all \"Auto\" is equally exposed to steel. Use revenue geography, import "
         "and export share, and input-cost share — so you do not buy the junk in a "
         "hot sector.", WARN, WARN_SOFT),
        ("6  Portfolio-aware trade list",
         "sizes, not tickers",
         "A recommendation only means something against the book you already hold: "
         "current weight versus target, concentration limits, turnover and tax cost, "
         "correlation with existing names.", GOOD, GOOD_SOFT),
    ]
    bh = 56
    gap = 11
    y = h - 14 - bh
    for i, (t, s, note, c, soft) in enumerate(stages):
        box(d, 0, y, bw, bh, t, fill=soft, stroke=c, tc=c, size=8.2,
            sub=s, sub_size=6.3, sub_color=MUTED, align="center")
        box(d, bw + 18, y, nw, bh, "", fill=white, stroke=RULE)
        _para(d, bw + 25, y, nw - 14, bh, note, size=7.2, color=BODY)
        if i < len(stages) - 1:
            arrow(d, bw / 2, y, bw / 2, y - gap + 1, color=c, w=1.3, head=5.0)
        y -= bh + gap
    return d


# ───────────────────────────────────────────────────────────── D9
def d_exposure_matrix():
    """A worked slice of the driver-by-sector matrix."""
    sectors = ["Oil upstream", "Refiners / OMC", "Aviation", "Paints",
               "Tyres", "IT services", "Banks", "Metals"]
    drivers = [
        ("crude_oil_up", [3, -2, -3, -2, -2, 0, -1, 0]),
        ("usd_inr_up", [1, -1, -2, -1, -1, 3, -1, 1]),
        ("policy_rate_down", [0, 1, 1, 2, 1, 1, 2, 1]),
        ("us_tech_capex_up", [0, 0, 0, 0, 0, 3, 0, 1]),
        ("china_stimulus_up", [1, 0, 0, 1, 1, 0, 0, 3]),
    ]
    lab_w = 104
    cell_w = (W - lab_w) / len(sectors)
    rh = 20
    h = (len(drivers) + 1) * rh + 34
    d = canvas(h=h)

    for j, s in enumerate(sectors):
        x = lab_w + j * cell_w
        from report_kit import wrap
        lines = wrap(s, "Helvetica-Bold", 6.3, cell_w - 3)
        yy = h - 16
        for ln in lines:
            d.add(String(x + cell_w / 2, yy, ln, fontName="Helvetica-Bold",
                         fontSize=6.3, fillColor=MUTED, textAnchor="middle"))
            yy -= 7.5

    def tint(v):
        if v >= 3:
            return GOOD, white
        if v == 2:
            return HexColor("#86EFAC"), INK
        if v == 1:
            return GOOD_SOFT, GOOD
        if v == 0:
            return PAPER, FAINT
        if v == -1:
            return BAD_SOFT, BAD
        if v == -2:
            return HexColor("#FCA5A5"), INK
        return BAD, white

    y = h - 34 - rh
    for name, vals in drivers:
        d.add(String(0, y + rh / 2 - 2.6, name, fontName="Courier-Bold",
                     fontSize=7.0, fillColor=INK))
        for j, v in enumerate(vals):
            x = lab_w + j * cell_w
            fill, tc = tint(v)
            d.add(Rect(x + 1, y + 1, cell_w - 2, rh - 2, fillColor=fill,
                       strokeColor=white, strokeWidth=0.8))
            d.add(String(x + cell_w / 2, y + rh / 2 - 2.8,
                         f"{v:+d}" if v else "0", fontName="Helvetica-Bold",
                         fontSize=7.2, fillColor=tc, textAnchor="middle"))
        y -= rh
    text(d, 0, 4, "Illustrative signs and magnitudes, drawn to show the shape of "
                  "the artefact. Real cells come from regressing sector index "
                  "returns on driver changes.",
         size=6.8, color=MUTED)
    return d


# ───────────────────────────────────────────────────────────── D10
def d_fund_manager():
    """What an institutional build adds: four gates between idea and trade."""
    h = 212
    d = canvas(h=h)

    text(d, 0, h - 10, "THE IDEA-TO-TRADE PATH  —  EVERY GATE CAN SAY NO",
         size=6.8, color=FAINT, bold=True)
    gates = [
        ("Idea", "dossier, sector score,\nmodel view", NAVY, PAPER, "built"),
        ("Mandate and\ncompliance", "scheme category, issuer\nand sector caps, no-go list", BAD, BAD_SOFT, "missing"),
        ("Liquidity and\ncapacity", "ADV, days to build and exit,\nimpact cost", BAD, BAD_SOFT, "missing"),
        ("Sizing", "% of NAV, risk budget,\ncorrelation with the book", WARN, WARN_SOFT, "partial"),
        ("Approval", "4-eyes; PM proposes,\nrisk signs", WARN, WARN_SOFT, "partial"),
        ("Trade file plus\naudit record", "immutable: inputs, model\nversion, rationale, approver", BAD, BAD_SOFT, "missing"),
    ]
    bw = (W - 5 * 9) / 6
    y = h - 96
    from report_kit import STATUS
    for i, (t, s, c, soft, st) in enumerate(gates):
        x = i * (bw + 9)
        box(d, x, y, bw, 68, t.replace("\n", " "), fill=soft, stroke=c, tc=c,
            size=7.8, sub=s.replace("\n", " "), sub_size=6.1, sub_color=MUTED,
            dash=[2.5, 2.5] if st == "missing" else None)
        _, fg, bg = STATUS[st]
        # Pill sits above the box: inside it, it collided with the sub-text.
        pill(d, x, y + 68 + 3, STATUS[st][0], fill=bg, tc=fg, size=5.9, h=10.5)
        if i < 5:
            arrow(d, x + bw + 0.5, y + 34, x + bw + 8, y + 34, color=MUTED,
                  w=1.1, head=4.4)

    # The four subsystems that have to exist underneath
    y -= 36
    text(d, 0, y + 20, "WHAT HAS TO EXIST UNDERNEATH", size=6.8, color=FAINT,
         bold=True)
    y -= 66
    sw = (W - 3 * 11) / 4
    subs = [
        ("Versioned rule set", "SEBI master circular limits plus the scheme SID and the "
                               "fund's own IPS, loaded from a dated file — never hardcoded"),
        ("Point-in-time data store", "a metric as it was known on the date, not as later "
                                     "restated; otherwise every backtest is look-ahead"),
        ("Attribution engine", "Brinson allocation versus selection, active share, tracking "
                               "error, factor exposures, contribution by name"),
        ("Decision record", "payload hash, model version, features, rationale text, "
                            "approver, timestamp — the regulator's question, answered"),
    ]
    for i, (t, s) in enumerate(subs):
        x = i * (sw + 11)
        box(d, x, y, sw, 62, t, fill=white, stroke=TEAL, tc=TEAL, size=7.6,
            sub=s, sub_size=6.1, sub_color=MUTED)
    return d


# ───────────────────────────────────────────────────────────── D11
def d_roadmap():
    """Five phases, ordered so that nothing is built on an untrusted number."""
    h = 226
    d = canvas(h=h)
    lab_w = 150
    track_w = W - lab_w
    weeks = 26
    unit = track_w / weeks

    text(d, lab_w, h - 8, "WEEKS", size=6.4, color=FAINT, bold=True)
    for wk in range(0, weeks + 1, 2):
        x = lab_w + wk * unit
        d.add(Line(x, 10, x, h - 18, strokeColor=HexColor("#F3F4F6"),
                   strokeWidth=0.6))
        if wk % 4 == 0:
            text(d, x, h - 16, str(wk), size=6.2, color=FAINT, anchor="middle")

    phases = [
        ("Phase 0", "Make the ML layer honest", 0, 2, BAD, BAD_SOFT),
        ("Phase 1", "Make the model actually good", 2, 7, WARN, WARN_SOFT),
        ("Phase 2", "The full stock dossier", 5, 9, ACCENT, ACCENT_SOFT),
        ("Phase 3", "News to sector to stock", 9, 17, TEAL, TEAL_SOFT),
        ("Phase 4", "Institutional / fund-manager layer", 15, 25, PLUM, PLUM_SOFT),
    ]
    bh = 26
    y = h - 40 - bh
    for tag, name, a, b, c, soft in phases:
        d.add(String(0, y + bh / 2 - 2.4, tag, fontName="Helvetica-Bold",
                     fontSize=7.8, fillColor=c))
        d.add(String(43, y + bh / 2 - 2.4, name, fontName="Helvetica",
                     fontSize=7.4, fillColor=BODY))
        x0, x1 = lab_w + a * unit, lab_w + b * unit
        d.add(Rect(x0, y + 4, x1 - x0, bh - 8, rx=4, ry=4, fillColor=soft,
                   strokeColor=c, strokeWidth=0.9))
        d.add(String((x0 + x1) / 2, y + bh / 2 - 2.6, f"{b - a} wk",
                     fontName="Helvetica-Bold", fontSize=6.8, fillColor=c,
                     textAnchor="middle"))
        y -= bh + 7
    text(d, 0, 12, "Phases overlap on purpose: the dossier work does not depend on "
                   "the model being finished.", size=6.8, color=MUTED)
    text(d, 0, 2, "But nothing downstream should ship on a number you have not yet "
                  "validated.", size=6.8, color=MUTED)
    return d
