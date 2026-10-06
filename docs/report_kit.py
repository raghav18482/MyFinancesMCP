"""
Shared layout primitives for the MyFinanceMCP design reports.

Everything here is plain ReportLab: a restrained palette, a handful of
paragraph styles, and small drawing helpers (rounded box, arrow, pill) so each
diagram in ``build_report.py`` stays short enough to read.

Font note: every glyph used must exist in WinAnsiEncoding, which is what the
built-in Helvetica family ships with. Em dash, en dash, bullet, middot and
curly quotes are fine. Arrows, check marks, the rupee sign and the
greater-or-equal sign are NOT — use "->", "Yes"/"No", "Rs" and ">=" instead.
Unicode sub/superscripts render as black boxes and must never appear.
"""
from __future__ import annotations

import math

from reportlab.lib import colors
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Paragraph, Table, TableStyle

# ── Palette ────────────────────────────────────────────────────────────────
# Brand-neutral, checked for contrast on white. Status colours are used only
# for status, never for decoration, so a reader can trust the colour coding.
INK = HexColor("#111827")
BODY = HexColor("#1F2937")
MUTED = HexColor("#6B7280")
FAINT = HexColor("#9CA3AF")
RULE = HexColor("#E5E7EB")
PAPER = HexColor("#F9FAFB")

NAVY = HexColor("#0F172A")
ACCENT = HexColor("#1D4ED8")
ACCENT_MID = HexColor("#60A5FA")
ACCENT_SOFT = HexColor("#EFF4FF")

GOOD = HexColor("#15803D")
GOOD_SOFT = HexColor("#ECFDF3")
WARN = HexColor("#B45309")
WARN_SOFT = HexColor("#FFFBEB")
BAD = HexColor("#B91C1C")
BAD_SOFT = HexColor("#FEF2F2")
PLUM = HexColor("#6D28D9")
PLUM_SOFT = HexColor("#F5F3FF")
TEAL = HexColor("#0F766E")
TEAL_SOFT = HexColor("#F0FDFA")

PAGE_W, PAGE_H = A4
MARGIN_X = 17 * mm
MARGIN_TOP = 19 * mm
MARGIN_BOTTOM = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN_X          # ~176 mm / 499 pt
CONTENT_H = PAGE_H - MARGIN_TOP - MARGIN_BOTTOM

# ── Paragraph styles ───────────────────────────────────────────────────────
S = {
    "title": ParagraphStyle(
        "title", fontName="Helvetica-Bold", fontSize=27, leading=31,
        textColor=NAVY, spaceAfter=0,
    ),
    "subtitle": ParagraphStyle(
        "subtitle", fontName="Helvetica", fontSize=12.5, leading=18,
        textColor=MUTED,
    ),
    "kicker": ParagraphStyle(
        "kicker", fontName="Helvetica-Bold", fontSize=8, leading=11,
        textColor=ACCENT, spaceAfter=3,
    ),
    "h1": ParagraphStyle(
        "h1", fontName="Helvetica-Bold", fontSize=17, leading=21,
        textColor=NAVY, spaceBefore=0, spaceAfter=2,
    ),
    # keepWithNext stops a heading from being stranded at the foot of a page.
    "h2": ParagraphStyle(
        "h2", fontName="Helvetica-Bold", fontSize=11.5, leading=15,
        textColor=NAVY, spaceBefore=13, spaceAfter=4, keepWithNext=1,
    ),
    "h3": ParagraphStyle(
        "h3", fontName="Helvetica-Bold", fontSize=9.6, leading=13,
        textColor=BODY, spaceBefore=9, spaceAfter=2, keepWithNext=1,
    ),
    "body": ParagraphStyle(
        "body", fontName="Helvetica", fontSize=9.4, leading=14.2,
        textColor=BODY, spaceAfter=7,
    ),
    "lead": ParagraphStyle(
        "lead", fontName="Helvetica", fontSize=10.6, leading=16,
        textColor=BODY, spaceAfter=9,
    ),
    "bullet": ParagraphStyle(
        "bullet", fontName="Helvetica", fontSize=9.4, leading=13.8,
        textColor=BODY, leftIndent=11, bulletIndent=1, spaceAfter=3.5,
    ),
    "small": ParagraphStyle(
        "small", fontName="Helvetica", fontSize=8.2, leading=11.6,
        textColor=BODY,
    ),
    "caption": ParagraphStyle(
        "caption", fontName="Helvetica-Oblique", fontSize=7.8, leading=10.5,
        textColor=MUTED, spaceBefore=4, spaceAfter=10,
    ),
    "th": ParagraphStyle(
        "th", fontName="Helvetica-Bold", fontSize=7.9, leading=10.4,
        textColor=white,
    ),
    "td": ParagraphStyle(
        "td", fontName="Helvetica", fontSize=8.1, leading=11.2,
        textColor=BODY,
    ),
    "tdb": ParagraphStyle(
        "tdb", fontName="Helvetica-Bold", fontSize=8.1, leading=11.2,
        textColor=INK,
    ),
    "tdc": ParagraphStyle(
        "tdc", fontName="Helvetica-Bold", fontSize=7.9, leading=10.6,
        textColor=BODY, alignment=TA_CENTER,
    ),
    "mono": ParagraphStyle(
        "mono", fontName="Courier", fontSize=7.6, leading=10.8,
        textColor=BODY,
    ),
    "monosm": ParagraphStyle(
        "monosm", fontName="Courier", fontSize=7.0, leading=9.8,
        textColor=BODY,
    ),
}


def P(text, style="body", **kw):
    st = S[style] if isinstance(style, str) else style
    if kw:
        st = ParagraphStyle("x", parent=st, **kw)
    return Paragraph(text, st)


def BL(text, style="bullet"):
    """A bulleted paragraph using the WinAnsi bullet glyph."""
    return Paragraph(text, S[style], bulletText="•")


# ── Tables ─────────────────────────────────────────────────────────────────
def table(rows, widths, *, header=True, head_bg=NAVY, zebra=True,
          align_center=(), cell_bg=None, font_size=8.1, pad=5,
          valign="TOP", box_color=RULE):
    """
    rows: list of lists of str (wrapped into Paragraphs) or ready Flowables.
    widths: list of fractions of CONTENT_W, or absolute points if > 1.
    cell_bg: dict of (col, row) -> colour, row counted including the header.
    """
    total = sum(widths)
    if total <= 1.001:
        widths = [w * CONTENT_W for w in widths]

    body_style = ParagraphStyle("tdx", parent=S["td"], fontSize=font_size,
                                leading=font_size * 1.38)
    center_style = ParagraphStyle("tdcx", parent=body_style, alignment=TA_CENTER,
                                  fontName="Helvetica-Bold")

    data = []
    for r_i, row in enumerate(rows):
        out = []
        for c_i, cell in enumerate(row):
            if isinstance(cell, str):
                if header and r_i == 0:
                    out.append(Paragraph(cell, S["th"]))
                elif c_i in align_center:
                    out.append(Paragraph(cell, center_style))
                else:
                    out.append(Paragraph(cell, body_style))
            else:
                out.append(cell)
        data.append(out)

    cmds = [
        ("VALIGN", (0, 0), (-1, -1), valign),
        ("LEFTPADDING", (0, 0), (-1, -1), pad),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad),
        ("TOPPADDING", (0, 0), (-1, -1), pad - 0.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad - 0.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("BOX", (0, 0), (-1, -1), 0.6, box_color),
    ]
    if header:
        cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), head_bg),
            ("TOPPADDING", (0, 0), (-1, 0), pad),
            ("BOTTOMPADDING", (0, 0), (-1, 0), pad),
        ]
        if zebra:
            for i in range(1, len(data)):
                if i % 2 == 0:
                    cmds.append(("BACKGROUND", (0, i), (-1, i), PAPER))
    elif zebra:
        for i in range(len(data)):
            if i % 2 == 1:
                cmds.append(("BACKGROUND", (0, i), (-1, i), PAPER))

    for (c, r), col in (cell_bg or {}).items():
        cmds.append(("BACKGROUND", (c, r), (c, r), col))

    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    t.setStyle(TableStyle(cmds))
    t.hAlign = "LEFT"
    return t


def callout(title, body, *, tone="accent", width=None):
    """A single-cell tinted box used for the 'read this twice' passages."""
    tones = {
        "accent": (ACCENT, ACCENT_SOFT),
        "good": (GOOD, GOOD_SOFT),
        "warn": (WARN, WARN_SOFT),
        "bad": (BAD, BAD_SOFT),
        "plum": (PLUM, PLUM_SOFT),
        "teal": (TEAL, TEAL_SOFT),
    }
    edge, fill = tones[tone]
    inner = []
    if title:
        inner.append(Paragraph(
            title,
            ParagraphStyle("cot", fontName="Helvetica-Bold", fontSize=9,
                           leading=12, textColor=edge, spaceAfter=3),
        ))
    inner.append(Paragraph(
        body,
        ParagraphStyle("cob", fontName="Helvetica", fontSize=8.8, leading=13,
                       textColor=BODY),
    ))
    t = Table([[inner]], colWidths=[width or CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("LINEBEFORE", (0, 0), (0, -1), 2.4, edge),
        ("BOX", (0, 0), (-1, -1), 0.5, edge),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    t.hAlign = "LEFT"
    return t


def code_block(lines, *, width=None):
    body = "<br/>".join(
        ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
          .replace(" ", "&nbsp;")
        for ln in lines
    )
    t = Table([[Paragraph(body, S["monosm"])]], colWidths=[width or CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F3F4F6")),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    t.hAlign = "LEFT"
    return t


# ── Drawing helpers ────────────────────────────────────────────────────────
def wrap(text, font, size, max_w):
    words = str(text).split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if pdfmetrics.stringWidth(trial, font, size) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def box(d, x, y, w, h, text="", *, fill=white, stroke=RULE, tc=INK, size=8.2,
        bold=True, radius=4, sub=None, sub_size=7, sub_color=None, pad=6,
        stroke_w=0.8, align="center", dash=None):
    r = Rect(x, y, w, h, rx=radius, ry=radius, fillColor=fill,
             strokeColor=stroke, strokeWidth=stroke_w)
    if dash:
        r.strokeDashArray = dash
    d.add(r)

    font = "Helvetica-Bold" if bold else "Helvetica"
    lines = wrap(text, font, size, w - 2 * pad) if text else []
    subs = wrap(sub, "Helvetica", sub_size, w - 2 * pad) if sub else []
    lh, slh = size * 1.2, sub_size * 1.2
    block = len(lines) * lh + (len(subs) * slh + 3 if subs else 0)
    ty = y + h / 2 + block / 2 - lh * 0.76

    for ln in lines:
        if align == "center":
            d.add(String(x + w / 2, ty, ln, fontName=font, fontSize=size,
                         fillColor=tc, textAnchor="middle"))
        else:
            d.add(String(x + pad, ty, ln, fontName=font, fontSize=size,
                         fillColor=tc))
        ty -= lh
    if subs:
        ty -= 3
        for ln in subs:
            if align == "center":
                d.add(String(x + w / 2, ty, ln, fontName="Helvetica",
                             fontSize=sub_size, fillColor=sub_color or MUTED,
                             textAnchor="middle"))
            else:
                d.add(String(x + pad, ty, ln, fontName="Helvetica",
                             fontSize=sub_size, fillColor=sub_color or MUTED))
            ty -= slh
    return x, y, w, h


def arrow(d, x1, y1, x2, y2, *, color=ACCENT, w=1.3, head=5.4, dash=None):
    ang = math.atan2(y2 - y1, x2 - x1)
    bx = x2 - head * math.cos(ang)
    by = y2 - head * math.sin(ang)
    ln = Line(x1, y1, bx, by, strokeColor=color, strokeWidth=w)
    if dash:
        ln.strokeDashArray = dash
    d.add(ln)
    sx, sy = head * 0.58 * math.sin(ang), head * 0.58 * math.cos(ang)
    d.add(Polygon([x2, y2, bx + sx, by - sy, bx - sx, by + sy],
                  fillColor=color, strokeColor=color, strokeWidth=0.4))


def elbow(d, x1, y1, x2, y2, *, color=ACCENT, w=1.1, via="v", head=5.0):
    """Right-angled connector: 'v' goes vertical first, 'h' horizontal first."""
    if via == "v":
        d.add(Line(x1, y1, x1, y2, strokeColor=color, strokeWidth=w))
        arrow(d, x1, y2, x2, y2, color=color, w=w, head=head)
    else:
        d.add(Line(x1, y1, x2, y1, strokeColor=color, strokeWidth=w))
        arrow(d, x2, y1, x2, y2, color=color, w=w, head=head)


def text(d, x, y, s, *, size=7.6, color=MUTED, bold=False, anchor="start"):
    d.add(String(x, y, s, fontName="Helvetica-Bold" if bold else "Helvetica",
                 fontSize=size, fillColor=color, textAnchor=anchor))


def pill(d, x, y, label, *, fill=GOOD_SOFT, tc=GOOD, size=6.8, h=13, padx=6):
    w = pdfmetrics.stringWidth(label, "Helvetica-Bold", size) + 2 * padx
    d.add(Rect(x, y, w, h, rx=h / 2, ry=h / 2, fillColor=fill,
               strokeColor=tc, strokeWidth=0.6))
    d.add(String(x + w / 2, y + h / 2 - size * 0.36, label,
                 fontName="Helvetica-Bold", fontSize=size, fillColor=tc,
                 textAnchor="middle"))
    return w


def band(d, x, y, w, h, title, *, fill=PAPER, stroke=RULE, tc=MUTED, size=7.2):
    """A faint grouping container drawn behind boxes."""
    d.add(Rect(x, y, w, h, rx=5, ry=5, fillColor=fill, strokeColor=stroke,
               strokeWidth=0.7, strokeDashArray=[2.5, 2.5]))
    if title:
        d.add(String(x + 8, y + h - size - 5, title.upper(),
                     fontName="Helvetica-Bold", fontSize=size, fillColor=tc))


def canvas(w=None, h=200):
    d = Drawing(w or CONTENT_W, h)
    d.hAlign = "LEFT"
    return d


# ── Status vocabulary, used identically everywhere ─────────────────────────
STATUS = {
    "built": ("Built", GOOD, GOOD_SOFT),
    "partial": ("Partial", WARN, WARN_SOFT),
    "facade": ("Facade", BAD, BAD_SOFT),
    "missing": ("Missing", BAD, BAD_SOFT),
    "planned": ("To build", MUTED, PAPER),
}


def status_cell(key):
    label, fg, _ = STATUS[key]
    return Paragraph(
        label,
        ParagraphStyle("st", fontName="Helvetica-Bold", fontSize=7.7,
                       leading=10, textColor=fg, alignment=TA_CENTER),
    )


def status_bg(key):
    return STATUS[key][2]
