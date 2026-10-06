#!/usr/bin/env python3
"""
Build docs/MyFinanceMCP-System-Design-Report.pdf.

    .venv/bin/python docs/build_report.py

Sections:
    1  The five layers of a modern AI finance application
    2  Audit: what this repository fulfils today
    3  The ML layer: twelve findings
    4  How to raise model accuracy
    5  System design: the full stock dossier
    6  System design: global news -> sector -> stock
    7  Building for mutual fund managers
    8  Roadmap
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reportlab.lib.colors import white
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, NextPageTemplate, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

import report_diagrams as DG
from report_kit import (
    ACCENT, ACCENT_SOFT, BAD, BAD_SOFT, BL, BODY, CONTENT_H, CONTENT_W, FAINT,
    GOOD, GOOD_SOFT, INK, MARGIN_BOTTOM, MARGIN_TOP, MARGIN_X, MUTED, NAVY, P,
    PAGE_H, PAGE_W, PAPER, PLUM, PLUM_SOFT, RULE, S, TEAL, TEAL_SOFT, WARN,
    WARN_SOFT, callout, code_block, status_bg, status_cell, table,
)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "MyFinanceMCP-System-Design-Report.pdf")
TODAY = date.today().strftime("%d %B %Y")
DOC_TITLE = "MyFinanceMCP — System Design and ML Audit"


# ── Page furniture ─────────────────────────────────────────────────────────
def cover_page(c, doc):
    c.saveState()
    c.setFillColor(NAVY)
    c.rect(0, PAGE_H - 13, PAGE_W, 13, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.rect(0, PAGE_H - 13, PAGE_W * 0.38, 13, stroke=0, fill=1)
    c.setFillColor(FAINT)
    c.setFont("Helvetica", 7.5)
    c.drawString(MARGIN_X, 22, "Internal design document — not investment advice")
    c.restoreState()


def body_page(c, doc):
    c.saveState()
    c.setStrokeColor(RULE)
    c.setLineWidth(0.5)
    c.line(MARGIN_X, PAGE_H - MARGIN_TOP + 11, PAGE_W - MARGIN_X,
           PAGE_H - MARGIN_TOP + 11)
    c.setFont("Helvetica", 7.2)
    c.setFillColor(FAINT)
    c.drawString(MARGIN_X, PAGE_H - MARGIN_TOP + 16, DOC_TITLE)
    c.drawRightString(PAGE_W - MARGIN_X, PAGE_H - MARGIN_TOP + 16, TODAY)

    c.line(MARGIN_X, MARGIN_BOTTOM - 10, PAGE_W - MARGIN_X, MARGIN_BOTTOM - 10)
    c.setFont("Helvetica-Bold", 7.6)
    c.setFillColor(MUTED)
    c.drawRightString(PAGE_W - MARGIN_X, MARGIN_BOTTOM - 20, str(doc.page))
    c.setFont("Helvetica", 7.2)
    c.setFillColor(FAINT)
    c.drawString(MARGIN_X, MARGIN_BOTTOM - 20,
                 "Internal design document — not investment advice")
    c.restoreState()


def build_doc():
    doc = BaseDocTemplate(
        OUT, pagesize=(PAGE_W, PAGE_H),
        leftMargin=MARGIN_X, rightMargin=MARGIN_X,
        topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
        title=DOC_TITLE, author="MyFinanceMCP", subject="Architecture and ML audit",
    )
    frame = Frame(MARGIN_X, MARGIN_BOTTOM, CONTENT_W, CONTENT_H, id="f",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[frame], onPage=cover_page),
        PageTemplate(id="body", frames=[frame], onPage=body_page),
    ])
    return doc


# ── Small content helpers ──────────────────────────────────────────────────
def section(num, title, kicker=None):
    out = [P(f"PART {num}", "kicker")] if num else []
    out.append(P(title, "h1"))
    if kicker:
        out.append(Spacer(1, 3))
        out.append(P(kicker, "lead", textColor=MUTED))
    out.append(Spacer(1, 9))
    return out


def caption(s):
    return P(s, "caption")


def finding(fid, title, body, evidence, impact, tone="bad"):
    """One numbered ML finding: claim, code evidence, consequence."""
    edge = {"bad": BAD, "warn": WARN, "accent": ACCENT}[tone]
    head = Paragraph(
        f'<font color="{edge.hexval()}"><b>{fid}</b></font>&nbsp;&nbsp;<b>{title}</b>',
        ParagraphStyle("fh", fontName="Helvetica", fontSize=9.6, leading=13,
                       textColor=INK, spaceAfter=4),
    )
    rows = [[head]]
    rows.append([P(body, "small")])
    rows.append([Paragraph(
        "<b>Evidence</b>&nbsp; " + evidence,
        ParagraphStyle("fe", fontName="Courier", fontSize=7.2, leading=10.2,
                       textColor=MUTED, spaceBefore=4))])
    rows.append([Paragraph(
        "<b>Consequence</b>&nbsp; " + impact,
        ParagraphStyle("fi", fontName="Helvetica", fontSize=8.1, leading=11.4,
                       textColor=edge, spaceBefore=4))])
    t = Table([[rows]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, edge),
        ("BACKGROUND", (0, 0), (-1, -1), PAPER),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    t.hAlign = "LEFT"
    return [t, Spacer(1, 7)]


def scorecard_row(layer, verdict, score, note, key):
    return [layer, verdict, score, note]


# ══════════════════════════════════════════════════════════════════════════
def story():
    s = []

    # ─────────────────────────────────────────────── COVER
    s += [
        Spacer(1, 54),
        P("SYSTEM DESIGN &amp; TECHNICAL AUDIT", "kicker", fontSize=9,
          textColor=ACCENT),
        Spacer(1, 10),
        P("Traditional ML, deep learning,<br/>LLMs, tools and backends —<br/>"
          "and where MyFinanceMCP<br/>actually stands.", "title"),
        Spacer(1, 18),
        Table([[None]], colWidths=[94], rowHeights=[3],
              style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)])),
        Spacer(1, 18),
        P("An honest read of what the repository fulfils today, why the "
          "prediction layer cannot be trusted yet, how to raise its accuracy, "
          "and how to grow the product into a stock-research and sector-rotation "
          "system that a mutual fund manager would actually use.", "lead"),
        Spacer(1, 26),
    ]
    s.append(table(
        [
            ["Repository", "MyFinanceMCP — Angel One portfolio tracker, MCP server, "
             "ADK agents, LightGBM prediction service"],
            ["Branch examined", "fundamentalAnalysisScoring at 0d187b0 "
             "(\"Score fundamentals in Python, explain them in plain English\")"],
            ["Scope", "Architecture review, ML pipeline audit, and forward design "
             "for stock analysis, sector rotation and institutional use"],
            ["Date", TODAY],
            ["Status", "Design document. No code was changed to produce it."],
        ],
        [0.17, 0.83], header=False, zebra=True, font_size=8.4, pad=7,
    ))
    s += [
        Spacer(1, 22),
        callout(
            "The one-line version",
            "Four of the five layers are genuinely built. The fifth — traditional "
            "ML — is a working pipeline with no model in it: there is no trained "
            "artefact on disk, so every \"LightGBM prediction\" the dashboard, the "
            "MCP tool and the WhatsApp briefing show today is a hand-weighted rule "
            "score. And the training script, if you ran it, would produce a model "
            "that is worse than that heuristic, for three independent reasons. "
            "Fix that before building anything on top of it.",
            tone="bad"),
    ]
    s += [NextPageTemplate("body"), PageBreak()]

    # ─────────────────────────────────────────────── SUMMARY
    s += section(None, "Executive summary",
                 "Read this page and the scorecard; the rest is the working.")

    s += [
        P("You asked three questions. Short answers first, then the evidence.", "body"),
        Spacer(1, 2),
        P("1. How do you build a system that combines traditional ML, deep "
          "learning, LLMs, tools and backend systems?", "h3"),
        P("By giving each layer the job it is uniquely good at and refusing to let "
          "it do the others. The backend states facts. Feature engineering turns "
          "noisy prices into learnable shape. Gradient boosting turns that shape "
          "into a calibrated probability. Deep learning reads the things that are "
          "not numbers — headlines, filings, sequences. The LLM orchestrates, "
          "decides what to look up, and writes the reasoning. The single rule that "
          "keeps it honest: <b>the number comes from the model, the sentence comes "
          "from the LLM</b>. Part 1 works this through.", "body"),

        P("2. How many of these are you fulfilling right now?", "h3"),
        P("Four and a half out of five in <i>form</i>; about three in <i>substance</i>. "
          "The backend is genuinely production-shaped — one Uvicorn process serving "
          "both FastAPI and MCP, Postgres with Fernet-encrypted credentials, a "
          "scheduler that claims rows with <font face=\"Courier\">FOR UPDATE SKIP "
          "LOCKED</font>, SSE price streaming, pinned dependencies, a test suite. "
          "The tool and agent layer is real: two ADK agents, 27 and 11 tools, "
          "proposal-gated order execution, plus 20 MCP tools for external clients. "
          "Feature engineering is thorough — 35 features, and a fundamental scorer "
          "that grades against a glossary so the threshold and the explanation can "
          "never drift apart. Deep learning is present but isolated: FinBERT really "
          "runs, and its output never reaches the price model. Traditional ML is the "
          "hole. Part 2 scores each layer against the code.", "body"),

        P("3. How do you increase the accuracy of the ML model?", "h3"),
        P("Start by not measuring accuracy. Directional accuracy on a near-50/50 "
          "base rate, ignoring move size and trading cost, cannot tell you whether "
          "a model is worth running. Then, in order of payoff: enforce one candle "
          "interval end-to-end (training uses 5-minute bars, every serving path "
          "sends daily bars — the features mean different things in each); replace "
          "the shuffled split with purged walk-forward validation; cut seven "
          "horizons down to three; label with triple barriers instead of "
          "<font face=\"Courier\">close &gt; close</font>; add cross-sectional and "
          "market-context features; wire in the fundamental score and FinBERT "
          "sentiment you already compute; calibrate the probabilities. Then widen "
          "the data — 20 symbols over 30 days is the binding constraint, not the "
          "algorithm. Parts 3 and 4.", "body"),
        Spacer(1, 4),
        callout(
            "Two things you did not ask for, which the rest of the document covers",
            "You also asked for a design for full stock analysis, and for a "
            "news-to-sector-to-stock recommendation engine aimed at mutual fund "
            "managers. Part 5 specifies an eight-panel stock dossier where every "
            "number carries provenance and the LLM may not write a figure that is "
            "not in the payload. Part 6 specifies the macro chain, whose key design "
            "move is mapping news to <i>economic drivers</i> rather than to sectors. "
            "Part 7 covers what changes when the user is a fund manager rather than "
            "a retail investor — mandate compliance as a hard gate, liquidity and "
            "capacity, attribution, and an immutable decision record.",
            tone="accent"),
    ]
    s.append(PageBreak())

    # ─────────────────────────────────────────────── SCORECARD
    s += section(None, "The scorecard",
                 "Each layer graded against what is in the repository, not against "
                 "what the README claims.")
    s.append(DG.d_stack(with_status=True))
    s.append(caption("Figure 1 — The five layers, the question each one answers, "
                     "the question it cannot answer, and its status in this "
                     "repository."))

    rows = [["Layer", "Score", "What is genuinely there", "What is missing"]]
    rows += [
        ["L1 Backend systems", "5 / 5",
         "One process mounts FastAPI and FastMCP (<font face=\"Courier\">main.py</font>). "
         "Postgres via SQLModel, Fernet-encrypted credentials "
         "(<font face=\"Courier\">db/crypto.py</font>), APScheduler claiming work with "
         "<font face=\"Courier\">FOR UPDATE SKIP LOCKED</font> "
         "(<font face=\"Courier\">services/schedular/repository.py</font>), SSE live "
         "prices, Docker, fully pinned requirements, five test modules.",
         "Point-in-time history. Multi-user roles. Sessions are per-credential and "
         "live in RAM only."],
        ["L2 Feature engineering", "4 / 5",
         "35 features in <font face=\"Courier\">FEATURE_NAMES</font> — RSI, MACD, SMA, "
         "EMA, Bollinger, ADX, Stochastic, ATR, volume, multi-period returns and "
         "volatility, candle geometry, cyclical time. Plus a deterministic "
         "fundamental scorer with sector overrides for lenders "
         "(<font face=\"Courier\">services/fundamental_scoring.py</font>).",
         "No cross-sectional features (a stock's RSI versus the universe today). "
         "No market or sector context. Indicators recomputed from scratch per row "
         "during training."],
        ["L3 Traditional ML", "1 / 5",
         "The plumbing is complete and correct in shape: feature extraction, "
         "per-horizon models, lazy loading, caching, a graceful heuristic fallback, "
         "and three call sites.",
         "<b>The model itself.</b> No <font face=\"Courier\">.pkl</font> exists, so the "
         "heuristic always runs. The training script has train/serve skew, label "
         "leakage and wrong horizons. Twelve findings in Part 3."],
        ["L4 Deep learning", "2 / 5",
         "FinBERT (<font face=\"Courier\">ProsusAI/finbert</font>, a real 110M-parameter "
         "transformer) is loaded, cached and used for news sentiment, and is exposed "
         "to the agent as <font face=\"Courier\">research_financial_text_sentiment</font>.",
         "Its output never reaches the price model — "
         "<font face=\"Courier\">extract_features</font> accepts "
         "<font face=\"Courier\">sentiment_scores</font> and ignores it. No sequence "
         "model on price paths. No embeddings over filings."],
        ["L5 LLM + tools", "4 / 5",
         "Two Google ADK agents on OpenRouter via LiteLLM. Finance agent: 18 broker "
         "plus 9 research tools. Trading agent: 11 tools with proposal-gated "
         "execution and a risk-profile check written into the instruction. Separately, "
         "20 MCP tools over SSE for Cursor and Claude Desktop.",
         "<b>The ML prediction tool is not on the agent's tool belt.</b> "
         "<font face=\"Courier\">predict_direction</font> is reachable from MCP, the "
         "dashboard route and the briefing, but "
         "<font face=\"Courier\">make_market_tools()</font> never exposes it."],
    ]
    bg = {}
    for i, key in enumerate(["built", "built", "facade", "partial", "built"], start=1):
        bg[(1, i)] = status_bg(key)
    s.append(table(rows, [0.155, 0.065, 0.42, 0.36], align_center=(1,),
                   cell_bg=bg, font_size=7.7))
    s.append(caption("Figure 2 — Layer scorecard. Total 16 / 25. The gap is "
                     "concentrated almost entirely in one layer."))
    s.append(PageBreak())

    # ─────────────────────────────────────────────── PART 1
    s += section(1, "What \"traditional ML + deep learning + LLMs + tools + "
                    "backends\" actually means",
                 "Five kinds of machinery. Each answers a question the others "
                 "cannot, and each fails in a way the others do not.")

    s += [
        P("The reason a modern AI application looks like a stack rather than a "
          "model is that the questions a user asks are not all the same kind of "
          "question. \"What do I own?\" is a lookup. \"Is this chart overbought?\" is "
          "a transformation. \"What is the chance this rises next week?\" is a "
          "calibrated estimate. \"Is this headline bad news?\" is comprehension of "
          "text. \"What should I do about it?\" is judgment. Pointing one technology "
          "at all five produces something that is mediocre at each.", "body"),
        P("The division of labour, and why it is not negotiable", "h2"),
        P("Use the deterministic layer where you need auditability, the "
          "probabilistic layer where you need calibration, and the generative layer "
          "where you need explanation. Inverting any of these is the standard way "
          "these systems fail:", "body"),
        BL("<b>An LLM asked to produce the number</b> will produce one. It will be "
           "fluent, plausible and unbacked. There is no calibration, no "
           "reproducibility, and no way to measure whether it was right over 500 "
           "calls."),
        BL("<b>A model asked to produce the explanation</b> gives you feature "
           "importances, which are not reasons. \"<font face=\"Courier\">bb_position "
           "= 0.08</font>\" is not something a portfolio manager can put in a note."),
        BL("<b>A backend asked to produce the opinion</b> gives you a dashboard the "
           "user has to interpret themselves — which is the thing they wanted help "
           "with."),
        Spacer(1, 4),
        callout("The rule that holds the stack together",
                "The number comes from the model. The sentence comes from the LLM. "
                "The LLM is allowed to <i>select</i>, <i>combine</i> and "
                "<i>narrate</i> numbers; it is never allowed to <i>invent</i> one. "
                "Everything in Part 5 about provenance envelopes and verifier "
                "passes exists to enforce exactly this one rule mechanically rather "
                "than by hoping.",
                tone="accent"),
        Spacer(1, 10),
        P("How your two diagrams join up", "h2"),
        P("The sketch in your brief has two separate pipelines — features into "
          "LightGBM into a prediction, and user into agent into LLM into a set of "
          "tools. They are the same system. The prediction pipeline is not a "
          "parallel product; it is one of the tools on the agent's belt. The agent "
          "decides <i>whether this question needs a forecast</i>, calls the tool, "
          "gets back a calibrated probability with its drivers, and writes the "
          "answer around it.", "body"),
        Spacer(1, 2),
        DG.d_two_pipelines(),
        caption("Figure 3 — The deterministic pipeline on the left feeds one tool "
                "on the right. In this repository that wire does not exist: "
                "<font face=\"Courier\">make_market_tools()</font> in "
                "agents/finance/tools_market.py:131 returns nine research tools and "
                "none of them is the predictor."),
    ]
    s += [
        P("Where deep learning earns its place — and where it does not", "h2"),
        P("Deep learning is the most over-prescribed item on this list. On small "
          "tabular datasets — which is what 35 engineered features over a few "
          "thousand rows is — gradient boosting beats neural networks reliably, "
          "trains in seconds, and is far easier to debug. If the only thing you do "
          "with \"deep learning\" is replace LightGBM with an MLP, you will lose "
          "performance and gain nothing.", "body"),
        P("Deep learning earns its place on the <i>non-tabular</i> inputs, which is "
          "exactly where you already use it:", "body"),
        BL("<b>Text.</b> FinBERT on headlines, and later on filings and concall "
           "transcripts. No feature engineering can substitute for reading."),
        BL("<b>Sequences, eventually.</b> A temporal model over the price path can "
           "capture ordering that a snapshot of 35 indicators flattens away. This "
           "is worth doing <i>after</i> the gradient-boosted baseline is validated, "
           "never before — otherwise you cannot tell whether the architecture "
           "helped or the extra data did."),
        BL("<b>Embeddings for retrieval.</b> Finding the five most similar past "
           "setups, or the filings that mention a given risk, is a vector-search "
           "problem, not a classifier."),
        Spacer(1, 6),
        P("A useful way to hold it: <b>deep learning turns unstructured inputs into "
          "features; gradient boosting turns features into probabilities; the LLM "
          "turns probabilities into decisions a human can audit.</b>", "body"),
        Spacer(1, 10),
        P("What \"backend systems\" is really carrying", "h2"),
        P("It is tempting to treat the backend as the boring part. In a finance "
          "product it is the part that determines whether any of the rest is "
          "trustworthy, because it owns four things no model can supply: what is "
          "<i>true</i> (positions, cash, fills), what was <i>known at the time</i> "
          "(point-in-time data, without which every backtest lies), <i>who did "
          "what</i> (the audit trail), and <i>when</i> (scheduling, market hours, "
          "staleness). Your backend is the strongest layer in this repository, "
          "which is a better position to be in than the reverse.", "body"),
    ]
    s.append(PageBreak())

    # ─────────────────────────────────────────────── PART 2
    s += section(2, "Audit: what this repository fulfils today",
                 "Graded against the code on branch fundamentalAnalysisScoring, "
                 "not against the README.")
    s.append(DG.d_current_architecture())
    s.append(caption("Figure 4 — The system as it exists. Green is working as "
                     "described; amber works but is disconnected from the model; "
                     "red is a pipeline with nothing in it."))

    s += [
        P("What is genuinely good here", "h2"),
        P("Three things are better than the usual standard for a project at this "
          "stage, and they are worth naming before the criticism, because they are "
          "the foundation everything else gets built on.", "body"),
        BL("<b>The process topology is right.</b> Mounting FastMCP and FastAPI in "
           "one Uvicorn process means the MCP tools and the web dashboard share the "
           "same session manager and the same service layer. Most projects end up "
           "with two codebases that drift."),
        BL("<b>The scheduler is written like infrastructure, not like a cron "
           "script.</b> Claiming due rows with <font face=\"Courier\">FOR UPDATE "
           "SKIP LOCKED</font>, a retry path for Angel rate limits, and run history "
           "in a logs table — that survives a second replica. Most do not."),
        BL("<b>The fundamental scorer is the best-designed component in the "
           "repo.</b> Grading against the same <font face=\"Courier\">metric_glossary.json"
           "</font> the /learn page renders means the threshold used to judge a metric "
           "and the \"ideal range\" shown to the user cannot drift apart. Excluding "
           "missing metrics rather than penalising them, and reporting coverage as "
           "<font face=\"Courier\">confidence</font>, is the right call. Dropping the "
           "whole balance-sheet pillar for lenders — with the reasoning written in a "
           "comment — is the kind of domain judgment most ML pipelines never make."),
        Spacer(1, 6),
        callout("The irony worth noticing",
                "The most carefully reasoned numerical component in the repository — "
                "the fundamental scorer, with its sector overrides and coverage-based "
                "confidence — is not used by the machine learning model at all. "
                "<font face=\"Courier\">FEATURE_NAMES</font> contains no fundamental "
                "input. For horizons of a month or more, that score is probably more "
                "predictive than any of the 35 technical features it sits next to.",
                tone="warn"),
        Spacer(1, 10),
        P("Where the gaps are", "h2"),
    ]

    gap_rows = [["Capability", "Status", "Detail"]]
    gap_rows += [
        ["Trained prediction model", "facade",
         "<font face=\"Courier\">models/</font> contains only "
         "<font face=\"Courier\">train_prediction.py</font>. No artefact, so "
         "<font face=\"Courier\">_load_models()</font> returns empty and "
         "<font face=\"Courier\">_heuristic_predict()</font> serves every request. "
         "The response field <font face=\"Courier\">model_type</font> reads "
         "\"heuristic\" and nothing in the UI surfaces that."],
        ["ML tool on the agent", "missing",
         "Present in <font face=\"Courier\">mcp_server.py</font> as "
         "<font face=\"Courier\">predict_price_direction</font>, in the dashboard "
         "route, and in the daily briefing — but not in "
         "<font face=\"Courier\">make_market_tools()</font>. The agent in your own "
         "diagram is missing its ML branch."],
        ["Sentiment into the model", "missing",
         "<font face=\"Courier\">extract_features(candles, sentiment_scores)</font> "
         "takes the argument at prediction_service.py:92 and the body never reads it."],
        ["Fundamentals into the model", "missing",
         "A clean 0–100 score with four pillar sub-scores exists and is unused by "
         "the predictor."],
        ["Validation you could defend", "missing",
         "One accuracy number from a shuffled split. No walk-forward, no purging, "
         "no calibration check, no per-regime breakdown, no cost-aware backtest."],
        ["Peer-relative valuation", "missing",
         "Fundamentals are graded against absolute glossary bands. A P/E of 28 means "
         "something very different in IT than in cement; percentile-versus-sector is "
         "the missing dimension."],
        ["Ownership and flows data", "missing",
         "Promoter holding and change, pledge, FII/DII, bulk and block deals, mutual "
         "fund holdings. Strong, cheap signals that no current source supplies."],
        ["Earnings estimates and revisions", "missing",
         "Revenue and profit history exist; consensus estimates, surprise history and "
         "revision momentum do not. Revision momentum is one of the better-documented "
         "equity signals."],
        ["Point-in-time data discipline", "missing",
         "Fundamentals are fetched as they are <i>now</i>. Any backtest that uses them "
         "is look-ahead biased by construction."],
        ["Institutional controls", "missing",
         "Mandate and compliance gating, liquidity and capacity analysis, performance "
         "attribution, immutable decision records, role separation. Part 7."],
    ]
    bgs = {}
    keys = ["facade", "missing", "missing", "missing", "missing", "missing",
            "missing", "missing", "missing", "missing"]
    for i, k in enumerate(keys, start=1):
        gap_rows[i][1] = ""
        bgs[(1, i)] = status_bg(k)
    gt = table(gap_rows, [0.23, 0.085, 0.685], align_center=(1,), cell_bg=bgs,
               font_size=7.7)
    # overlay the status labels as proper cells
    gap_rows2 = [r[:] for r in gap_rows]
    for i, k in enumerate(keys, start=1):
        gap_rows2[i][1] = status_cell(k)
    s.append(table(gap_rows2, [0.23, 0.085, 0.685], cell_bg=bgs, font_size=7.7))
    s.append(caption("Figure 5 — Capability gaps, ordered roughly by how much each "
                     "one blocks the others."))
    s.append(PageBreak())

    # ─────────────────────────────────────────────── PART 3
    s += section(3, "The ML layer: twelve findings",
                 "Why the prediction numbers in the dashboard, the MCP tool and the "
                 "WhatsApp briefing cannot currently be trusted.")

    s += [
        P("These are ordered by impact. The first four are each independently "
          "sufficient to make the model worse than the heuristic it replaces; "
          "together they mean the reported training accuracy carries no information "
          "about live performance at all.", "body"),
        Spacer(1, 4),
    ]

    s += finding(
        "F1", "There is no trained model, so nothing you see is a model prediction",
        "<font face=\"Courier\">models/</font> contains the training script and "
        "nothing else. <font face=\"Courier\">_load_models()</font> finds no "
        "<font face=\"Courier\">.pkl</font>, logs \"No trained models found\", and "
        "returns an empty dict, so every call falls through to "
        "<font face=\"Courier\">_heuristic_predict()</font> — a hand-weighted score "
        "over RSI, MACD, Bollinger position, SMA distance and Stochastic. That "
        "heuristic is reasonable technical analysis. It is not machine learning, and "
        "the README describes something else.",
        "services/prediction_service.py:47-70 · the response carries "
        "model_type=\"heuristic\", which no view surfaces",
        "The dashboard, the MCP tool predict_price_direction and the daily WhatsApp "
        "briefing all present rule-based scores as model output. Users cannot tell. "
        "Confidence values between 0.35 and 0.85 are produced by a formula, not by "
        "anything fitted to data.")

    s += finding(
        "F2", "Training and serving use different candle intervals",
        "The training script fetches <font face=\"Courier\">FIVE_MINUTE</font> bars "
        "over 30 days. Every path that calls the predictor sends "
        "<font face=\"Courier\">ONE_DAY</font> bars. The feature names match "
        "perfectly, so nothing errors — but <font face=\"Courier\">return_1</font>, "
        "<font face=\"Courier\">volatility_20</font>, "
        "<font face=\"Courier\">atr_14_pct</font> and "
        "<font face=\"Courier\">volume_ratio</font> are an order of magnitude apart "
        "between the two, and the cyclical hour features are constant on daily bars. "
        "Worse, when the 5-minute fetch fails the script silently falls back to a "
        "year of daily bars and appends them to the same dataset with no marker, so "
        "the training set itself mixes two frequencies under one label column.",
        "train: models/train_prediction.py:170 and the fallback at :174 · "
        "serve: mcp_server.py:682, web/routers/portfolio.py:109, "
        "services/schedular/daily_briefing.py:123",
        "This is the single most damaging defect. A model trained here and served "
        "there is reading a different language at inference time. It will produce "
        "confident, stable, meaningless output — and it will never raise an error.")
    s.append(DG.d_train_serve_skew())
    s.append(caption("Figure 6 — Identical feature names, incompatible "
                     "distributions. Scale factors are indicative, computed from "
                     "the ratio of a 5-minute to a daily bar over an NSE session of "
                     "375 minutes."))
    s += finding(
        "F3", "The train/test split is shuffled, on overlapping time-series rows",
        "Rows are generated by sliding a window forward one bar at a time, so row "
        "<i>i</i> and row <i>i+1</i> share 49 of their 50 bars of history and their "
        "forward label windows overlap almost entirely. "
        "<font face=\"Courier\">train_test_split(..., shuffle=True)</font> then "
        "scatters these near-duplicates across both sides of the split. The test set "
        "is not held out in any meaningful sense.",
        "models/train_prediction.py:128 (sliding window) and :205-206 "
        "(shuffle=True, random_state=42)",
        "The reported accuracy measures how well the model recognises rows it has "
        "effectively already seen. Expect the printed number to be far above "
        "anything achievable live. Any decision made on that number — which features "
        "to keep, which horizon looks best, whether to ship — is being made on noise.")
    s.append(DG.d_leakage())
    s.append(caption("Figure 7 — Why a shuffled split cannot work on a sliding "
                     "window, and what replaces it."))
    s += finding(
        "F4", "Four of the seven horizons are mislabelled by one to two orders of magnitude",
        "<font face=\"Courier\">TIMEFRAME_BARS</font> holds "
        "<font face=\"Courier\">{10min: 2, 1hr: 12, 4hr: 48, 1day: 1, 1week: 5, "
        "1month: 22, 1year: 252}</font>. On the 5-minute bars that training actually "
        "uses, the first three are correct. The last four are daily-bar conventions "
        "applied to intraday data. An NSE session is 375 minutes, so one trading day "
        "is about 75 five-minute bars — not 1. One week is roughly 375 bars, one "
        "month 1,650, one year about 19,000.",
        "services/prediction_service.py:28 · consumed at "
        "models/train_prediction.py:126 and :136-139",
        "The model labelled \"1 day\" is trained to predict five minutes ahead. The "
        "one labelled \"1 year\" predicts about 21 trading hours ahead. Separately, "
        "30 days of 5-minute data is roughly 5,600 bars, which cannot support a "
        "one-year horizon under any labelling.")

    s += finding(
        "F5", "The label ignores both move size and trading cost", tone="warn",
        body="<font face=\"Courier\">1 if future_close &gt; current_close else 0</font> "
             "gives a +0.02 % drift and a +4 % breakout the same label. On 5-minute "
             "bars most moves are inside the bid-ask spread plus brokerage plus STT, "
             "so a large share of the training signal is in moves that could never "
             "have been traded profitably.",
        evidence="models/train_prediction.py:136-139",
        impact="Model capacity is spent learning microstructure noise. A model that "
               "is 'right' on untradeable moves scores well and earns nothing.")

    s += finding(
        "F6", "Sentiment is accepted as a parameter and silently discarded", tone="warn",
        body="<font face=\"Courier\">extract_features(candles, sentiment_scores)</font> "
             "declares the argument; the function body never references it, and "
             "<font face=\"Courier\">FEATURE_NAMES</font> has no sentiment entry. "
             "FinBERT scores are computed elsewhere in the system and thrown away "
             "before they reach the model.",
        evidence="services/prediction_service.py:92 (signature) and :73-89 (feature list)",
        impact="The deep-learning layer contributes nothing to prediction. The "
               "signature implies otherwise, which is how this survives review.")

    s += finding(
        "F7", "Probabilities are presented as confidence without being calibrated",
        tone="warn",
        body="<font face=\"Courier\">confidence = max(up_prob, 1 - up_prob)</font>. "
             "Raw gradient-boosting outputs are not calibrated probabilities; 0.80 "
             "from LightGBM does not mean the event happens 80 % of the time. This "
             "number is rendered in the dashboard and sent over WhatsApp.",
        evidence="services/prediction_service.py:381-388",
        impact="Any position sizing derived from this is wrong in a direction you "
               "cannot predict. Calibration is cheap to add and makes the number mean "
               "what users already assume it means.")
    s += [P("The remaining findings", "h2")]
    minor = [["ID", "Finding", "Evidence", "Why it matters"]]
    minor += [
        ["F8", "No model metadata is saved",
         "models/train_prediction.py:245 — <font face=\"Courier\">joblib.dump(model, path)</font>",
         "The bare estimator is persisted with no feature list, interval, date range, "
         "metrics or code version. Nothing structurally prevents F2 from recurring "
         "silently after a refactor."],
        ["F9", "No backtest and no economic evaluation",
         "no harness exists in the repo",
         "Accuracy says nothing about whether a strategy built on the signal makes "
         "money after brokerage, STT, slippage and impact. That is the only question "
         "that matters."],
        ["F10", "Training is quadratic in bars per symbol",
         "models/train_prediction.py:128-130 calls "
         "<font face=\"Courier\">extract_features</font> per row",
         "Every indicator is recomputed over the whole window for each row — roughly "
         "110,000 full recomputes at current settings. Vectorising it once makes "
         "experimentation cheap enough to actually iterate."],
        ["F11", "The prediction cache key is keyed on candle count",
         "services/prediction_service.py:360 — "
         "<font face=\"Courier\">md5(str(len(candles)))</font>",
         "Two different windows of equal length collide. The 120-second TTL limits "
         "the damage, but the key should hash the last timestamp and the interval."],
        ["F12", "The universe and history are far too small",
         "models/train_prediction.py:166 — 20 symbols, 30 days",
         "This is the binding constraint on achievable accuracy — more than any "
         "algorithm choice on the list. See the data section in Part 4."],
    ]
    s.append(table(minor, [0.055, 0.225, 0.29, 0.43], font_size=7.6))
    s.append(caption("Figure 8 — Findings F8 to F12. None of these alone breaks the "
                     "model; together they make it impossible to improve "
                     "systematically."))
    s += [
        Spacer(1, 6),
        callout("What this adds up to",
                "There is no evidence either way about whether this model works, "
                "because no measurement taken so far could have told you. That is a "
                "different and more recoverable situation than a model that has been "
                "measured and does not work. The fixes in Part 4 are mostly "
                "mechanical, and the first of them takes an afternoon.",
                tone="accent"),
    ]
    s.append(PageBreak())

    # ─────────────────────────────────────────────── PART 4
    s += section(4, "How to raise the accuracy of the model",
                 "First by measuring something that matters, then by fixing the "
                 "pipeline, then by feeding it better data.")

    s += [
        P("Step zero: stop optimising accuracy", "h2"),
        P("Directional accuracy is close to useless for this problem. The base rate "
          "is near 50 %, so the headline number barely moves even when the signal "
          "changes materially; it treats a 0.1 % move and a 5 % move identically; "
          "and it is blind to cost. A model at 52 % accuracy concentrated in large, "
          "liquid moves is worth a great deal. A model at 58 % concentrated in moves "
          "smaller than the spread is worth less than nothing. Replace the single "
          "number with four:", "body"),
    ]
    mets = [["Metric", "What it tells you", "Target worth aiming at"]]
    mets += [
        ["AUC / precision@k", "Ranking quality — if you act on the top <i>k</i> names "
         "each day, are they better than random?", "AUC 0.53–0.58 out of sample is a "
         "real signal at daily-to-monthly horizons"],
        ["Brier score plus a reliability curve", "Is a stated 70 % actually 70 %? "
         "Calibration is what makes the number usable for sizing.",
         "Reliability curve close to the diagonal; Brier better than the base-rate "
         "benchmark"],
        ["Information coefficient", "Rank correlation between prediction and realised "
         "forward return. The standard buy-side measure.",
         "IC of 0.03–0.06 is a usable signal; above 0.10 out of sample, suspect a bug"],
        ["Net Sharpe and hit rate after cost", "The only metric that pays. Include "
         "brokerage, STT, exchange charges, slippage and impact.",
         "Net Sharpe above 0.5 before you consider the signal real"],
    ]
    s.append(table(mets, [0.235, 0.42, 0.345], font_size=7.8))
    s.append(caption("Figure 9 — The metric stack that replaces a single accuracy "
                     "figure."))

    s += [
        Spacer(1, 8),
        P("Be realistic about the ceiling", "h2"),
        P("Different horizons are not equally tractable from daily OHLCV plus news, "
          "and pretending otherwise is how models get shipped that quietly do "
          "nothing. The table below is the honest version.", "body"),
    ]
    hz = [["Horizon", "Realistic out-of-sample AUC", "What actually drives it", "Verdict"]]
    hz += [
        ["10 minutes", "0.50 – 0.52", "Order-book imbalance, queue position, tick "
         "flow — none of which you have, and Angel's candle API cannot give you",
         "Drop it"],
        ["1 hour", "0.51 – 0.53", "Intraday momentum and mean reversion; costs eat "
         "most of the edge", "Drop it"],
        ["4 hours", "0.51 – 0.53", "Same as above, slightly more tractable", "Drop it"],
        ["1 day", "0.52 – 0.55", "Momentum, reversal, volume, overnight news, index "
         "flow", "<b>Keep</b>"],
        ["1 week", "0.53 – 0.56", "The best risk-adjusted zone; sector rotation and "
         "sentiment start to matter", "<b>Keep — priority</b>"],
        ["1 month", "0.54 – 0.58", "Fundamentals, estimate revisions, flows, sector "
         "cycle. Most tractable of all.", "<b>Keep</b>"],
        ["1 year", "not a classification problem", "Valuation, earnings growth, "
         "capital allocation, cycle position — candles are the wrong input entirely",
         "Replace with a scenario model"],
    ]
    cell_bgs = {}
    for i in (1, 2, 3):
        cell_bgs[(3, i)] = BAD_SOFT
    for i in (4, 5, 6):
        cell_bgs[(3, i)] = GOOD_SOFT
    cell_bgs[(3, 7)] = WARN_SOFT
    s.append(table(hz, [0.12, 0.185, 0.47, 0.225], align_center=(3,),
                   cell_bg=cell_bgs, font_size=7.7))
    s.append(caption("Figure 10 — Going from seven horizons to three roughly "
                     "triples the data available per model and removes the four you "
                     "could never have validated."))
    s += [
        P("The pipeline that replaces the current one", "h2"),
        P("Nine stages. Stages 2, 3, 4 and 6 are the ones that decide whether your "
          "measured performance survives contact with live data; the rest improve "
          "the signal itself.", "body"),
        DG.d_target_pipeline(),
        caption("Figure 11 — The target training pipeline. The vocabulary here — "
                "triple barriers, purging and embargo, meta-labelling, sample "
                "uniqueness — is standard practice in financial ML and is worth "
                "reading up on properly before implementing."),
    ]

    s += [
        P("Three stages worth explaining properly", "h3"),
        P("<b>Triple-barrier labelling.</b> Instead of asking \"was the close higher "
          "<i>n</i> bars later\", place three barriers from the entry point: an upper "
          "one at +<i>k</i> times ATR, a lower one at -<i>k</i> times ATR, and a "
          "vertical one at the horizon. The label is whichever barrier is touched "
          "first. This does three things at once — it makes the label volatility-aware "
          "so a 1 % move in a quiet stock and a 3 % move in a volatile one are treated "
          "comparably, it encodes an actual trade with a stop and a target rather than "
          "an abstract direction, and it lets you set the barriers wide enough that "
          "the move clears your trading costs.", "body"),
        P("<b>Purging and embargo.</b> When you split by time, a training row whose "
          "label window extends into the test period has seen the future. Purge those "
          "rows. Then add an embargo — a gap after the test block before training "
          "resumes — because serial correlation leaks information across the boundary "
          "even without direct overlap. Expect your headline number to fall when you "
          "do this. That drop is not a regression; it is the measurement finally "
          "becoming real.", "body"),
        P("<b>Meta-labelling.</b> Keep the primary model's job as \"which direction\", "
          "then train a second model on a different question: \"given the primary "
          "model said up, is it going to be right this time?\" The second model gets "
          "to use features about the situation — regime, volatility, liquidity, news "
          "density — rather than about direction. Its output drives bet size. In "
          "practice this is where most of the precision improvement comes from, "
          "because it lets you be right less often and still make money by being "
          "right bigger.", "body"),
    ]
    s += [
        P("The ranked action list", "h2"),
        P("Ordered by expected improvement in <i>live</i> performance per unit of "
          "effort. Do them top down; several of the early ones will make the "
          "measured number worse and the real number better.", "body"),
    ]
    acts = [["#", "Change", "Eff.", "Effect", "Why it ranks here"]]
    acts += [
        ["1", "Enforce one candle interval end to end, recorded in model metadata "
              "and checked at predict time", "S", "Critical",
         "Removes a defect that silently destroys the model at inference. Nothing "
         "below matters until this is done."],
        ["2", "Purged, embargoed walk-forward validation; delete "
              "<font face=\"Courier\">shuffle=True</font>", "M", "Critical",
         "Without it you cannot tell whether anything else on this list helped."],
        ["3", "Cut to three horizons: 1 day, 1 week, 1 month", "S", "High",
         "Three times the data per model, and removes the horizons whose labels were "
         "wrong anyway."],
        ["4", "Widen the universe and history: NIFTY 500, 8–10 years of daily bars",
         "M", "High",
         "The binding constraint today. More data will beat any algorithm change on "
         "this list."],
        ["5", "Triple-barrier labels sized to clear trading cost", "M", "High",
         "Makes the model learn tradeable moves instead of noise."],
        ["6", "Cross-sectional features — rank each feature within the universe each "
              "day", "M", "High",
         "Strips out the market-wide component so the model learns relative strength "
         "rather than market direction. Usually a bigger win than any new indicator."],
        ["7", "Market and sector context — index return, sector relative strength, "
              "breadth, India VIX", "S", "Med-high",
         "A stock's probability of rising is mostly about the market and its sector. "
         "Right now the model cannot see either."],
        ["8", "Wire in the existing fundamental score and its four pillars", "S",
         "Medium", "Already computed, already sector-adjusted. Matters most at the "
                   "one-month horizon."],
        ["9", "Wire in FinBERT sentiment with decay windows and article counts", "M",
         "Medium", "Connects the deep-learning layer to the prediction layer for the "
                   "first time."],
        ["10", "Probability calibration on a held-out time slice", "S", "Medium",
         "Makes the confidence number honest, which is a prerequisite for sizing."],
        ["11", "Event-based sampling plus sample-uniqueness weights", "M", "Medium",
         "Removes the near-duplicate rows that make validation optimistic."],
        ["12", "Meta-labelling for bet sizing", "M", "Medium",
         "Where precision gains usually come from once the base model is sound."],
        ["13", "Model registry, feature contract, live prediction log, weekly drift "
               "scoring", "M", "Medium",
         "Prevents silent regression and gives you the dataset to evaluate yourself "
         "honestly over time."],
        ["14", "Sequence model (LSTM or temporal fusion transformer) as a challenger",
         "L", "Low-med",
         "<b>Do this last.</b> On engineered tabular features at this data scale, "
         "gradient boosting is the stronger baseline. Only meaningful once 1–13 are "
         "done."],
    ]
    eff_bg = {}
    for i, a in enumerate(acts[1:], start=1):
        eff_bg[(3, i)] = {"Critical": BAD_SOFT, "High": WARN_SOFT,
                          "Med-high": WARN_SOFT, "Medium": ACCENT_SOFT,
                          "Low-med": PAPER}[a[3]]
    s.append(table(acts, [0.05, 0.25, 0.055, 0.105, 0.54],
                   align_center=(0, 2, 3), cell_bg=eff_bg, font_size=7.5))
    s.append(caption("Figure 12 — Effort: S is up to about two days, M up to about "
                     "two weeks, L longer."))
    s += [
        P("Data is the binding constraint, not the algorithm", "h2"),
        P("It is worth being precise about how little data the current setup has. "
          "Twenty symbols over thirty days of 5-minute bars is roughly 112,000 rows, "
          "which sounds like plenty. But the rows overlap by 98 %, so the number of "
          "genuinely independent observations is closer to the number of "
          "non-overlapping label windows — a few hundred per symbol at short "
          "horizons, and fewer than thirty per symbol for anything daily. Against 35 "
          "features and seven models, that is nowhere near enough.", "body"),
    ]
    data_rows = [["Dimension", "Today", "Target", "Why"]]
    data_rows += [
        ["Universe", "20 symbols", "NIFTY 500, with the AMFI large / mid / small "
         "classification attached",
         "Cross-sectional features need a universe. Twenty names cannot produce a "
         "meaningful daily rank."],
        ["History", "30 days", "8–10 years of daily bars; 2–3 years of 5-minute",
         "You need several market regimes — 2018, 2020, 2021, 2022, 2024 all look "
         "different. A model fitted on one regime fails in the next."],
        ["Interval", "mixed, silently", "One per model, declared in metadata and "
         "enforced at inference",
         "Finding F2. This is non-negotiable."],
        ["Storage", "re-fetched from Angel each run",
         "Local Parquet store, incremental, with the fetch date recorded",
         "Rate limits make iteration painful, and you cannot reproduce an experiment "
         "whose inputs you did not keep."],
        ["Fundamentals", "live yfinance snapshot", "Point-in-time, stamped with the "
         "date the figure was first reported",
         "Using today's restated fundamentals in a 2019 backtest is look-ahead bias. "
         "It will make the backtest look excellent and the live model fail."],
        ["News", "gnews on demand", "Stored archive with publish timestamps and "
         "FinBERT scores computed once",
         "You cannot build a news feature without history, and re-scoring on every "
         "call is slow and non-reproducible."],
    ]
    s.append(table(data_rows, [0.13, 0.19, 0.3, 0.38], font_size=7.7))
    s.append(caption("Figure 13 — The data work. Unglamorous, and worth more than "
                     "every modelling change on the previous page combined."))

    s += [
        Spacer(1, 8),
        callout("The honest expectation to set",
                "After all of this, a good daily-horizon equity model on Indian "
                "large and mid caps lands somewhere around AUC 0.54 and an IC near "
                "0.04. That sounds disappointingly small, and it is genuinely "
                "valuable — applied consistently across 500 names with disciplined "
                "sizing, a stable IC of 0.04 is a real strategy. Any number far "
                "above that in your own backtest is almost always a leak, not an "
                "edge. Treat a surprisingly good result as a bug report.",
                tone="warn"),
    ]
    s.append(PageBreak())

    # ─────────────────────────────────────────────── PART 5
    s += section(5, "System design: the full stock analysis",
                 "What a complete, defensible view of a single stock contains, and "
                 "how it is assembled without the LLM inventing anything.")

    s += [
        P("The product goal is that an analyst selects a stock and gets, in a few "
          "seconds, everything they would otherwise assemble from five browser tabs "
          "and a spreadsheet — with the reasoning written out and every figure "
          "traceable. Three design decisions make that work.", "body"),
        BL("<b>Panels are independent and assembled in parallel.</b> yfinance is slow "
           "and news is slower. A panel that is not ready renders as a skeleton and "
           "fills in; it never blocks the page. Cache TTL is set by how fast the "
           "underlying data actually changes: price in seconds, technicals hourly, "
           "fundamentals daily, ownership weekly."),
        BL("<b>Every number carries a provenance envelope</b> — source, as-of "
           "timestamp, unit, and the fetch time. This is what lets the UI grey out "
           "stale figures, and what lets a compliance officer answer \"where did this "
           "come from\" eighteen months later."),
        BL("<b>The LLM narrates, it does not compute.</b> It receives the assembled "
           "payload and writes the memo, under a hard constraint that no figure may "
           "appear in the output that is not in the payload. A verifier pass then "
           "matches every number in the draft back against the payload before it is "
           "shown. This is cheap and it eliminates the failure mode that would "
           "otherwise sink the product."),
        Spacer(1, 6),
        DG.d_dossier(),
        caption("Figure 14 — The eight-panel dossier. Status colours show what "
                "already exists in the repository versus what has to be built."),
    ]
    panel_rows = [["Panel", "Contents", "Source", "Today"]]
    panel_rows += [
        ["1. Identity and mandate fit",
         "Sector and industry, market-cap band per the AMFI classification, index "
         "membership, free float, liquidity tier, and — for institutional users — "
         "whether the name is eligible for this scheme at all",
         "AMFI cap list, NSE indices, <font face=\"Courier\">data/sector_map.json</font>",
         "partial"],
        ["2. Price and technical state",
         "The existing indicator set, plus support and resistance, plus an explicit "
         "regime label (trending or range-bound via ADX; high or low volatility via "
         "ATR percentile) so the reader knows which indicators to trust",
         "<font face=\"Courier\">services/technical_service.py</font>", "built"],
        ["3. Valuation and quality",
         "The existing 0–100 score and its four pillars, <b>plus percentile versus "
         "sector peers</b> — the missing dimension. A P/E of 28 is cheap in one "
         "sector and expensive in another",
         "<font face=\"Courier\">services/fundamental_scoring.py</font> plus a peer "
         "set", "partial"],
        ["4. Earnings and estimates",
         "Revenue and profit history (exists), consensus estimates, surprise history, "
         "and revision momentum — one of the better-documented equity signals and "
         "entirely absent today",
         "A paid fundamentals feed; yfinance is not sufficient here", "missing"],
        ["5. Ownership and flows",
         "Promoter holding and its change, pledged shares, FII and DII trends, bulk "
         "and block deals, and which mutual funds already hold it and at what weight",
         "NSE and BSE shareholding filings, AMFI monthly portfolios", "missing"],
        ["6. News and narrative",
         "Deduplicated, event-typed and FinBERT-scored, with a materiality weight so "
         "a results announcement outranks a listicle, and an explicit 'nothing "
         "material' state",
         "<font face=\"Courier\">news_service.py</font> plus "
         "<font face=\"Courier\">sentiment_service.py</font>", "partial"],
        ["7. Model view",
         "Calibrated probability per surviving horizon, the contributing features by "
         "<b>SHAP value</b> rather than by sorting on absolute magnitude, and an "
         "honest uncertainty band",
         "<font face=\"Courier\">services/prediction_service.py</font> after Part 4",
         "facade"],
        ["8. Risk and liquidity",
         "Beta (exists), realised volatility, max drawdown, average daily value "
         "traded, days to exit a given position size, estimated impact cost, and "
         "factor exposures",
         "Angel candles plus a factor model", "partial"],
    ]
    pbg = {}
    keys = ["partial", "built", "partial", "missing", "missing", "partial",
            "facade", "partial"]
    for i, k in enumerate(keys, start=1):
        panel_rows[i][3] = status_cell(k)
        pbg[(3, i)] = status_bg(k)
    s.append(table(panel_rows, [0.15, 0.40, 0.295, 0.085], cell_bg=pbg,
                   font_size=7.6))
    s.append(caption("Figure 15 — Panel specification. Panels 4 and 5 are the two "
                     "that need a data source you do not currently have, and they "
                     "are also two of the most predictive."))

    s += [
        Spacer(1, 8),
        P("What the top of a good memo looks like", "h3"),
        P("Not a wall of indicators. A verdict, the two or three things that drive "
          "it, and what would change your mind:", "body"),
        code_block([
            "RELIANCE  ·  Oil to Chemicals, Telecom, Retail  ·  large cap  ·  as of 04 Oct 2026, 15:30 IST",
            "",
            "VIEW        Constructive, 1 month.  Model p(up) 0.57  [calibrated, +/- 0.06]",
            "",
            "DRIVERS     + Fundamental score 71/100, 84th percentile in sector (coverage 0.9)",
            "            + Price above 20 and 50 DMA; ADX 28 means the trend reading is reliable",
            "            - Crude at a 6-month high compresses refining margin: driver crude_oil_up, -2",
            "",
            "WATCH       Q2 results 18 Oct. Consensus EPS 24.1, revision momentum -1.8 % over 30 days.",
            "",
            "RISK        ADV Rs 1,840 cr. A 2 % NAV position in a Rs 5,000 cr scheme is 0.5 days of ADV.",
            "            Impact at 10 % participation: ~11 bps.",
            "",
            "Every figure above resolves to a source and an as-of timestamp in the payload.",
        ]),
    ]
    s.append(PageBreak())

    # ─────────────────────────────────────────────── PART 6
    s += section(6, "System design: global news to sector to stock",
                 "The chain that turns \"OPEC announced a cut\" into \"trim aviation, "
                 "add upstream, here is the size.\"")

    s += [
        P("This is the most valuable thing in the document to get architecturally "
          "right, because the obvious implementation does not work. The obvious "
          "implementation is: run sentiment on news, group by sector, recommend the "
          "sectors with positive sentiment. That fails because sentiment is not "
          "impact — a story can be unambiguously negative in tone and already fully "
          "priced, and a dry policy circular with neutral tone can be the most "
          "important thing that happened all week.", "body"),
        Spacer(1, 2),
        callout("The design move that makes this work",
                "Map news to <b>economic drivers</b>, not to sectors. \"OPEC cuts "
                "output\" is not an energy-sector story; it is a "
                "<font face=\"Courier\">crude_oil_up</font> driver. That driver then "
                "propagates through a maintained exposure matrix to upstream "
                "producers (positive), refiners, aviation, paints, tyres and "
                "logistics (negative to varying degrees), and to banks indirectly "
                "through inflation and rates. The second-order names are precisely "
                "the ones a human reading headlines misses, and they are where the "
                "system earns its keep.",
                tone="accent"),
        Spacer(1, 8),
        DG.d_news_to_stock(),
        caption("Figure 16 — The six-stage chain. Stages 2 and 3 are the "
                "intellectual property; the rest is engineering."),
    ]
    s += [
        P("Stage 2: what the extractor actually emits", "h3"),
        P("Use the LLM with a strict structured-output schema. The output is a typed "
          "record, not prose and not a score:", "body"),
        code_block([
            "{",
            '  "event_type":      "supply_cut",',
            '  "drivers": [',
            '      {"name": "crude_oil_up",  "direction": "+", "magnitude": "high"},',
            '      {"name": "usd_inr_up",    "direction": "+", "magnitude": "low"}',
            "  ],",
            '  "horizon":         "quarters",',
            '  "geography":       ["global", "IN"],',
            '  "confidence":      0.78,',
            '  "novelty":         0.35,      // is this new, or the fourth retelling?',
            '  "source":          {"url": "...", "publisher": "...", "tier": 1},',
            '  "published_at":    "2026-10-03T18:42:00Z"',
            "}",
        ]),
        P("Two fields there do a lot of work. <b>Novelty</b> handles the fact that "
          "most of a news feed is the same story retold; without it, a widely "
          "syndicated item overwhelms a single exclusive. <b>Horizon</b> prevents a "
          "structural multi-quarter driver from being treated as a one-day trade.", "body"),
        Spacer(1, 6),
        P("Stage 3: the driver-by-sector exposure matrix", "h3"),
        P("This is the artefact that makes the whole chain defensible. Rows are "
          "drivers, columns are sectors, cells are signed sensitivities. Populate it "
          "<i>twice</i>: analyst-written priors, which supply the human-readable "
          "reason, and empirically estimated coefficients from regressing ten years "
          "of sector index returns on changes in each driver, which supply the "
          "number used in scoring. When the two disagree, that disagreement is "
          "itself worth surfacing to an analyst.", "body"),
        DG.d_exposure_matrix(),
        caption("Figure 17 — An illustrative slice of the matrix, shown to convey "
                "its shape. Note that crude rising is strongly positive for upstream "
                "producers and strongly negative for aviation — one driver, opposite "
                "signs, which is exactly the information a sentiment score destroys."),
    ]
    s += [
        P("Stage 4: three independent blocks, and never average away a disagreement",
          "h3"),
        P("A sector score built from one input is fragile. Build it from three that "
          "fail differently:", "body"),
    ]
    blocks = [["Block", "Inputs", "What it catches", "What it misses"]]
    blocks += [
        ["News and driver impulse",
         "Stage 2 events mapped through the Stage 3 matrix, decayed by recency, "
         "weighted by source tier and novelty",
         "Sudden, identifiable shocks and their second-order effects",
         "Slow structural change. Also noisy — most news is already priced"],
        ["Macro cycle position",
         "Yield curve slope, policy rate trajectory, PMI, credit growth, inflation "
         "trend, currency, commodity complex",
         "The cyclical-versus-defensive tilt. This is the classic sector-rotation "
         "engine and it works on a horizon of quarters",
         "Turning points, which it identifies late. It will keep you in a leader "
         "past the top"],
        ["Market confirmation",
         "Sector relative strength versus the index, breadth within the sector, "
         "distance from the 200-day average, FII sector-wise flows",
         "What is actually happening, as opposed to what should be. Prevents long "
         "arguments with the tape",
         "It is coincident, not leading. On its own it is momentum chasing"],
    ]
    s.append(table(blocks, [0.17, 0.3, 0.3, 0.23], font_size=7.6))
    s.append(caption("Figure 18 — Score each block separately and show all three. "
                     "The disagreements are the most informative output."))

    s += [
        Spacer(1, 8),
        P("Stage 5: not all of a sector is equally exposed", "h3"),
        P("Having picked a sector, the ranking inside it must not be done on the "
          "sector label alone. Two auto companies can have opposite exposure to the "
          "same driver depending on export share, import content and input mix. The "
          "ranking inputs are: company-level driver exposure (from revenue geography, "
          "import and export share, and input-cost composition — obtainable from "
          "annual report segment data), the model's cross-sectional score, the "
          "fundamental quality score so you do not buy the worst balance sheet in a "
          "hot sector, and a liquidity filter.", "body"),
        Spacer(1, 4),
        P("Stage 6: a recommendation is relative to the book you already hold", "h3"),
        P("\"Buy TITAN\" is not actionable. \"You are 180 bps underweight discretionary "
          "versus the benchmark; TITAN at 90 bps closes half of that, takes 1.2 days "
          "of ADV to build at 15 % participation, and correlates 0.74 with a position "
          "you already hold\" is actionable. The output of the chain is a sized trade "
          "list with reasons, not a watchlist.", "body"),
        Spacer(1, 8),
        callout("What this system can and cannot do — state this to users explicitly",
                "<b>It will not</b> beat the market on headline speed. For liquid "
                "large caps, obvious news is priced within minutes, and you are "
                "reading the same public feed as everyone else.<br/><br/>"
                "<b>It will</b> deliver four things that are genuinely hard for a "
                "human team: <i>coverage</i> — surfacing the second- and third-order "
                "affected names nobody thought to check; <i>consistency</i> — the "
                "same framework applied to every event every day, without Friday "
                "fatigue; <i>synthesis speed</i> — minutes instead of a morning; and "
                "<i>an audit trail</i> — a dated record of what was known and why the "
                "call was made. For an institutional user, the fourth one alone "
                "justifies the build.",
                tone="warn"),
    ]
    s.append(PageBreak())

    # ─────────────────────────────────────────────── PART 7
    s += section(7, "Building it for mutual fund managers",
                 "What changes when the user is a fiduciary managing other people's "
                 "money under a mandate.")

    s += [
        P("A retail tool and an institutional tool can share a data layer and almost "
          "nothing else. The difference is not polish; it is that an institutional "
          "user is accountable, constrained, and operating at a size where their own "
          "trading moves the price.", "body"),
    ]
    diff = [["Dimension", "Retail tool (today)", "Fund-manager tool"]]
    diff += [
        ["Unit of analysis", "One stock, one user's holdings",
         "A scheme portfolio measured against its benchmark and its mandate"],
        ["Position sizing", "\"Buy 10 shares\"",
         "Basis points of NAV, with days-to-build, days-to-exit and impact cost"],
        ["Constraint", "The user's own stated risk appetite",
         "SEBI scheme-category rules plus the scheme information document plus the "
         "fund's internal policy — enforced as a hard block, not as advice"],
        ["Output", "A chart and a chat answer",
         "A dated investment note and a trade file, both reproducible"],
        ["Accountability", "None",
         "A regulator or trustee can ask, years later, why this was bought on this "
         "date and on what evidence"],
        ["Data recency", "Best effort, live snapshot",
         "Point-in-time: what was known on the date, never what was later restated"],
        ["Users", "One person, one session",
         "PM, analyst, dealer, risk and compliance — distinct roles, distinct rights"],
        ["Scale effect", "Irrelevant",
         "Your own order moves the price. Capacity analysis is part of the "
         "recommendation, not an afterthought"],
    ]
    s.append(table(diff, [0.165, 0.29, 0.545], font_size=7.8))
    s.append(caption("Figure 19 — The eight dimensions on which the requirements "
                     "diverge."))

    s += [
        Spacer(1, 8),
        DG.d_fund_manager(),
        caption("Figure 20 — The idea-to-trade path. The proposal-and-approval "
                "pattern already in <font face=\"Courier\">agents/trading/</font> is "
                "the right shape; what it needs is institutional gates behind it."),
    ]
    s += [
        P("The four subsystems, in the order they should be built", "h2"),
        P("<b>1. The mandate and compliance engine.</b> This is a hard gate, not a "
          "warning. Every proposed trade is checked and a breach blocks the trade "
          "with the specific rule cited. The rules live in a dated, versioned "
          "ruleset file — never hardcoded — because they change and because you need "
          "to be able to reconstruct which version was in force on a given date. The "
          "ruleset covers scheme-category definitions (which depend on the AMFI "
          "large/mid/small-cap list, republished half-yearly), single-issuer and "
          "sector concentration caps, instrument eligibility, liquidity norms, the "
          "fund's restricted list, and the scheme's own stated limits.", "body"),
        Spacer(1, 2),
        callout("Source the actual numbers, do not copy them from anywhere",
                "The specific limits — issuer caps, sector caps, cap-band "
                "definitions, stress-testing and disclosure requirements — must be "
                "read from the current SEBI master circular for mutual funds, the "
                "relevant scheme information document, and the current AMFI "
                "categorisation list. They have changed repeatedly and will change "
                "again. Design the engine so the ruleset is data with an effective "
                "date, and so a rule change is a file update rather than a "
                "deployment. Treat any numeric limit written into a design document "
                "— including this one — as needing verification before it governs a "
                "real trade.",
                tone="bad"),
        Spacer(1, 8),
        P("<b>2. Liquidity, capacity and impact.</b> The constraint that kills more "
          "good institutional ideas than any other is \"we cannot own enough of it to "
          "matter, or we cannot get out.\" Compute, per name and per scheme: 20-day "
          "average daily value traded, days to build and days to liquidate at a "
          "stated participation rate, estimated impact cost at that rate, and a "
          "scheme-level stress test against redemption scenarios. SEBI requires "
          "periodic stress testing and liquidity disclosure for mid- and small-cap "
          "schemes, so this subsystem is simultaneously an alpha filter and a "
          "compliance deliverable — which makes it the easiest one to justify.", "body"),
        P("<b>3. Attribution.</b> Without it you cannot learn. Brinson-style "
          "decomposition separates the sector-allocation call from the stock-selection "
          "call, which directly answers whether the Part 6 engine or the Part 4 model "
          "is earning its keep. Add active share, tracking error, factor exposures "
          "across size, value, momentum, quality and low volatility, and contribution "
          "by name. Attribution is also what closes the loop on the system itself: "
          "log every recommendation, then score it.", "body"),
        P("<b>4. The decision record.</b> Store, immutably, for every recommendation "
          "and every trade: a hash of the input payload, the model version and "
          "feature set, the sector scores and their three component blocks, the "
          "generated rationale text, who approved it, and when. You already have the "
          "beginnings of this — <font face=\"Courier\">db/models.py</font> has a Log "
          "table and <font face=\"Courier\">services/feedback.py</font> captures user "
          "input. Extend that into an append-only decision store. It serves three "
          "masters at once: the regulator, the investment committee, and your own "
          "model evaluation.", "body"),
        Spacer(1, 6),
        P("Three cross-cutting changes", "h3"),
        BL("<b>Point-in-time everything.</b> Without it, every backtest and every "
           "attribution number is quietly wrong. This is a storage decision, and it "
           "is much cheaper to make now than to retrofit."),
        BL("<b>Role-based access.</b> Sessions today are keyed to a set of Angel "
           "credentials held in RAM. An AMC needs distinct identities, distinct "
           "permissions, and four-eyes approval on anything that reaches a dealer."),
        BL("<b>Know what not to build.</b> Do not build an order management system, "
           "a market-data ticker plant, or a tick-level alpha model — integrate with "
           "what the AMC already has. And be clear about the regulatory perimeter: a "
           "research and decision-support tool used by a registered entity sits in a "
           "very different place from a product that gives investment advice to the "
           "public."),
    ]
    s.append(PageBreak())

    # ─────────────────────────────────────────────── PART 8
    s += section(8, "Roadmap",
                 "Sequenced so that nothing downstream is built on a number you "
                 "cannot yet defend.")
    s.append(DG.d_roadmap())
    s.append(caption("Figure 21 — Indicative sequencing for one focused engineer. "
                     "Phases overlap where they do not depend on each other."))

    road = [["Phase", "Scope", "Exit criterion — do not move on until this is true"]]
    road += [
        ["<b>Phase 0</b><br/>Make the ML layer honest<br/><font color=\"#6B7280\">~2 weeks</font>",
         "Enforce one interval end to end and fail loudly on mismatch. Save model "
         "metadata alongside the artefact. Replace the shuffled split with purged "
         "walk-forward. Cut to three horizons. Vectorise feature extraction. Label "
         "the UI honestly when the heuristic is serving. Add the prediction tool to "
         "<font face=\"Courier\">make_market_tools()</font> so the agent can reach it.",
         "A published out-of-sample AUC and IC, produced by a validation scheme you "
         "would defend to a quant, with the whole run reproducible from a command."],
        ["<b>Phase 1</b><br/>Make the model good<br/><font color=\"#6B7280\">~5 weeks</font>",
         "Build the Parquet data store. Widen to NIFTY 500 and 8–10 years. "
         "Triple-barrier labels. Cross-sectional, market-context, fundamental and "
         "sentiment features. Calibration. A backtest harness with realistic Indian "
         "costs. Model registry and a live prediction log.",
         "IC above 0.03 out of sample at the one-week horizon, a reliability curve "
         "near the diagonal, and net Sharpe above 0.5 after costs — or a documented, "
         "honest decision that the signal is not there."],
        ["<b>Phase 2</b><br/>The stock dossier<br/><font color=\"#6B7280\">~4 weeks</font>",
         "Parallel panel assembly with per-panel TTL. Peer-relative valuation "
         "percentiles. SHAP attributions replacing magnitude sorting. The provenance "
         "envelope. The narrative generator plus its verifier pass. PDF export of a "
         "single-stock dossier.",
         "An analyst reads the dossier instead of opening five tabs, and every "
         "number in the generated memo resolves to a source and a timestamp."],
        ["<b>Phase 3</b><br/>News to sector to stock<br/><font color=\"#6B7280\">~8 weeks</font>",
         "Source expansion beyond gnews to filings and policy text. Structured event "
         "extraction. The driver taxonomy. The exposure matrix, estimated empirically "
         "and reviewed by hand. The macro cycle module. Three-block sector scoring "
         "with disagreement surfaced. In-sector ranking.",
         "A daily sector note that a human analyst reads voluntarily, plus six months "
         "of logged calls scored against outcomes — so you know whether it works "
         "rather than believing it does."],
        ["<b>Phase 4</b><br/>Institutional layer<br/><font color=\"#6B7280\">~10 weeks</font>",
         "Versioned compliance ruleset and hard gating. Liquidity, capacity and "
         "impact. Brinson attribution and factor exposures. The append-only decision "
         "record. Role-based access and four-eyes approval. Point-in-time store.",
         "A compliance officer reviews a simulated trade end to end — proposal, "
         "rule check, sizing, approval, audit record — and signs off on the process."],
    ]
    s.append(table(road, [0.17, 0.46, 0.37], font_size=7.6))

    s += [
        Spacer(1, 10),
        P("If you only do one thing", "h2"),
        P("Do Phase 0. Not because it is the most interesting work on the list — it "
          "is the least — but because every other item depends on being able to tell "
          "whether a change helped. Right now you cannot. A single afternoon spent "
          "enforcing one candle interval and deleting "
          "<font face=\"Courier\">shuffle=True</font> converts this project from one "
          "that produces numbers into one that produces measurements, and everything "
          "else on the roadmap becomes tractable the moment that is true.", "body"),
        Spacer(1, 6),
        callout("A closing note on the brief you were given",
                "\"That's exactly the kind of system thinking we want you to develop\" "
                "— the system thinking is mostly already here. The topology is right, "
                "the layer boundaries are right, the proposal-gating instinct on order "
                "execution is right, and the fundamental scorer shows real domain "
                "judgment. What is missing is not architecture. It is the discipline "
                "of measurement: validating honestly, labelling what is actually "
                "serving, and refusing to show a number you cannot defend. That gap "
                "is narrower than it looks, and closing it is the difference between "
                "a system that demonstrates the pattern and one that can be trusted "
                "with money.",
                tone="good"),
        Spacer(1, 16),
        P("Your questions, answered in one line each", "h2"),
    ]
    recap = [["Question", "Answer", "Where"]]
    recap += [
        ["How do you build a system combining traditional ML, deep learning, LLMs, "
         "tools and backends?",
         "Give each layer the job only it can do, and enforce one rule: the number "
         "comes from the model, the sentence comes from the LLM.", "Part 1"],
        ["How many of these are you fulfilling right now?",
         "Four and a half of five in form, about three in substance. 16 / 25 on the "
         "scorecard. Backend, features and tools are solid; deep learning is isolated; "
         "traditional ML is a pipeline with no model in it.", "Part 2"],
        ["How do you increase the accuracy of the ML model?",
         "Stop measuring accuracy; measure AUC, calibration, IC and net Sharpe. Then "
         "fix the interval skew, the shuffled split and the horizon labels, cut to "
         "three horizons, and widen the data. Fourteen ranked actions.", "Parts 3–4"],
        ["How do you design a system for full stock analysis?",
         "An eight-panel dossier assembled in parallel, where every number carries "
         "provenance and the LLM may not write a figure that is not in the payload.",
         "Part 5"],
        ["How do you go from global news to affected sectors to stock picks?",
         "Map news to economic drivers rather than to sectors, propagate through a "
         "driver-by-sector exposure matrix, score each sector from three independent "
         "blocks, rank within the sector on company-level exposure, and size against "
         "the existing book.", "Part 6"],
        ["What changes if the user is a mutual fund manager?",
         "Mandate compliance becomes a hard gate, liquidity and capacity become part "
         "of the recommendation, attribution closes the loop, and every decision needs "
         "an immutable record.", "Part 7"],
    ]
    s.append(table(recap, [0.27, 0.62, 0.11], align_center=(2,), font_size=7.7))

    return s


if __name__ == "__main__":
    doc = build_doc()
    doc.build(story())
    size = os.path.getsize(OUT)
    print(f"wrote {OUT}  ({size/1024:.0f} KB)")
