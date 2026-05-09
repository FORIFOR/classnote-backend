"""PDF renderer for session export using ReportLab.

Generates an A4 PDF that preserves every field of the summary JSON. Supports:
- Callout hero (bottomLine + whyItMatters + outcomeStatus badge)
- Highlights with category chips
- Participants badge list
- Section types: text, bullets, grouped_bullets, table, cards
- Transcript appendix
"""
import io
from typing import List, Optional, Dict, Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, Color
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, PageBreak,
)
import os
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont


def _register_jp_fonts() -> tuple[str, str]:
    """Register Japanese fonts and return ``(sans_name, serif_name)``.

    Prefers TrueType fonts that get **embedded** into the PDF so the
    output renders identically on macOS Preview / Adobe / iOS / Windows.
    Previously we relied on UnicodeCIDFont (HeiseiKakuGo-W5), which
    only embeds the CID descriptor and lets the reader substitute its
    own font — that caused glyph fallback / kerning regressions on
    desktop PC PDF readers (especially Chromium / Edge built-in).

    Search order:
      1. fonts-noto-cjk on Linux (Cloud Run base image), with both the
         Debian Bookworm path and several alternate paths to be robust
         to different package layouts
      2. macOS bundled Hiragino (local dev)
      3. Fall back to UnicodeCIDFont
    """
    import glob as _glob
    # Debian fonts-noto-cjk lays files under several possible directories;
    # also support fonts-noto-cjk-vf (variable font) and any noto cjk file
    # the operator drops into the image manually.
    extra_globs: List[str] = []
    for pat in (
        "/usr/share/fonts/**/NotoSansCJK*.ttc",
        "/usr/share/fonts/**/NotoSansCJK*.otf",
        "/usr/share/fonts/**/NotoSans*JP*.otf",
        "/usr/share/fonts/**/NotoSans*JP*.ttf",
    ):
        extra_globs.extend(_glob.glob(pat, recursive=True))
    extra_globs_serif: List[str] = []
    for pat in (
        "/usr/share/fonts/**/NotoSerifCJK*.ttc",
        "/usr/share/fonts/**/NotoSerif*JP*.otf",
        "/usr/share/fonts/**/NotoSerif*JP*.ttf",
    ):
        extra_globs_serif.extend(_glob.glob(pat, recursive=True))

    # Reportlab TTFont supports TrueType (glyf table) only — NOT OpenType
    # CFF (postscript outlines). fonts-noto-cjk ships .ttc with CFF
    # glyphs, so we use IPAex / IPA Gothic + Mincho (TTF, glyf) instead.
    extra_ipa: List[str] = []
    for pat in (
        "/usr/share/fonts/**/ipaexg.ttf",
        "/usr/share/fonts/**/ipag.ttf",
        "/usr/share/fonts/**/ipagp.ttf",
    ):
        extra_ipa.extend(_glob.glob(pat, recursive=True))
    extra_ipa_serif: List[str] = []
    for pat in (
        "/usr/share/fonts/**/ipaexm.ttf",
        "/usr/share/fonts/**/ipam.ttf",
        "/usr/share/fonts/**/ipamp.ttf",
    ):
        extra_ipa_serif.extend(_glob.glob(pat, recursive=True))

    candidates_sans = [
        # IPAex Gothic (TTF, well-supported by reportlab)
        ("/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf", 0),
        ("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 0),
    ] + [(p, 0) for p in extra_ipa] + [
        # macOS Hiragino — only useful for local dev
        ("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 0),
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ]
    candidates_serif = [
        ("/usr/share/fonts/opentype/ipaexfont-mincho/ipaexm.ttf", 0),
        ("/usr/share/fonts/opentype/ipafont-mincho/ipam.ttf", 0),
    ] + [(p, 0) for p in extra_ipa_serif] + [
        ("/System/Library/Fonts/ヒラギノ明朝 ProN.ttc", 0),
    ]
    # Remove the now-unused glob lists from earlier (we kept the Noto
    # globs but they're guaranteed to fail TTFont registration). Strip
    # them to avoid noisy fallback warnings.
    extra_globs = []
    extra_globs_serif = []

    import logging as _logging
    _diag_logger = _logging.getLogger("app.services.export_pdf")

    def _try_register(name: str, path: str, idx: int) -> bool:
        try:
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=idx))
            return True
        except Exception as e:
            _diag_logger.warning(
                "[export_pdf] TTFont(%r, %r, subfontIndex=%d) failed: %s",
                name, path, idx, e
            )
            return False

    sans_name = None
    for path, idx in candidates_sans:
        if os.path.exists(path) and _try_register('DeepNoteSansJP', path, idx):
            sans_name = 'DeepNoteSansJP'
            break
    serif_name = None
    for path, idx in candidates_serif:
        if os.path.exists(path) and _try_register('DeepNoteSerifJP', path, idx):
            serif_name = 'DeepNoteSerifJP'
            break

    if sans_name is None:
        pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
        sans_name = 'HeiseiKakuGo-W5'
    if serif_name is None:
        pdfmetrics.registerFont(UnicodeCIDFont('HeiseiMin-W3'))
        serif_name = 'HeiseiMin-W3'
    # Log what we ended up with so the operator can confirm Noto is
    # actually being embedded (vs. CID fallback). Visible on Cloud Run
    # cold start logs.
    import logging as _logging
    _logger = _logging.getLogger("app.services.export_pdf")
    if sans_name.startswith("DeepNote"):
        _logger.info("[export_pdf] embedded Noto sans font: %s", sans_name)
    else:
        # Log what fonts ARE on disk so the operator can spot the
        # actual filename without shelling into the container.
        import glob as __g
        present: List[str] = []
        for pat in ("/usr/share/fonts/**/*.ttc",
                    "/usr/share/fonts/**/*.otf",
                    "/usr/share/fonts/**/*.ttf"):
            present.extend(__g.glob(pat, recursive=True))
        _logger.warning(
            "[export_pdf] FALLING BACK to CID font: %s (expected Noto). "
            "Tried: %s. Fonts on disk: %s",
            sans_name,
            ", ".join(p for p, _ in candidates_sans),
            ", ".join(present[:30]) or "(none)")
    return sans_name, serif_name


FONT, FONT_SERIF = _register_jp_fonts()
PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

# ─────────────────────────────────────────────────────
# Color palette
# ─────────────────────────────────────────────────────
C_PRIMARY       = HexColor('#1F4FD8')    # Accent blue
C_PRIMARY_BG    = HexColor('#EEF3FF')    # Light blue bg
C_TEXT          = HexColor('#111111')
C_HEAD          = HexColor('#1A1A2E')
C_CAPTION       = HexColor('#555566')
C_MUTED         = HexColor('#888899')
C_HAIRLINE      = HexColor('#E4E4EC')
C_CARD_BG       = HexColor('#F7F8FB')
C_TH_BG         = HexColor('#EDEEF5')
C_SUCCESS       = HexColor('#16A34A')
C_SUCCESS_BG    = HexColor('#ECFDF3')
C_WARNING       = HexColor('#EA580C')
C_WARNING_BG    = HexColor('#FFF4ED')
C_NEUTRAL       = HexColor('#6B7280')
C_NEUTRAL_BG    = HexColor('#F3F4F6')

EMPHASIS_COLORS = {
    "primary": (C_PRIMARY, C_PRIMARY_BG),
    "success": (C_SUCCESS, C_SUCCESS_BG),
    "warning": (C_WARNING, C_WARNING_BG),
    "neutral": (C_NEUTRAL, C_NEUTRAL_BG),
}

# ─────────────────────────────────────────────────────
# Paragraph styles
# ─────────────────────────────────────────────────────
STYLE_TITLE = ParagraphStyle(
    'Title', fontName=FONT, fontSize=20, leading=26,
    textColor=C_HEAD, spaceAfter=1*mm,
)
STYLE_META = ParagraphStyle(
    'Meta', fontName=FONT, fontSize=9, leading=13,
    textColor=C_CAPTION, spaceAfter=4*mm,
)
STYLE_BADGE = ParagraphStyle(
    'Badge', fontName=FONT, fontSize=8, leading=10,
    textColor=C_PRIMARY,
)
STYLE_BOTTOMLINE = ParagraphStyle(
    'BottomLine', fontName=FONT, fontSize=13, leading=19,
    textColor=C_HEAD, spaceAfter=1*mm,
)
STYLE_WHY = ParagraphStyle(
    'Why', fontName=FONT, fontSize=9, leading=14,
    textColor=C_CAPTION,
)
STYLE_H2 = ParagraphStyle(
    'H2', fontName=FONT, fontSize=12, leading=16,
    textColor=C_HEAD, spaceBefore=5*mm, spaceAfter=2*mm,
    leftIndent=0,
)
STYLE_H3 = ParagraphStyle(
    'H3', fontName=FONT, fontSize=10, leading=14,
    textColor=C_PRIMARY, spaceBefore=2*mm, spaceAfter=1*mm,
)
STYLE_BODY = ParagraphStyle(
    'Body', fontName=FONT, fontSize=9.5, leading=15,
    textColor=C_TEXT, spaceAfter=1.5*mm,
)
STYLE_BULLET = ParagraphStyle(
    'Bullet', fontName=FONT, fontSize=9.5, leading=14,
    textColor=C_TEXT, spaceAfter=1*mm, leftIndent=4*mm,
)
STYLE_HIGHLIGHT = ParagraphStyle(
    'Highlight', fontName=FONT, fontSize=10, leading=15,
    textColor=C_TEXT, spaceAfter=2*mm, leftIndent=6*mm,
)
STYLE_CARD_TITLE = ParagraphStyle(
    'CardTitle', fontName=FONT, fontSize=10, leading=14,
    textColor=C_HEAD, spaceAfter=1*mm,
)
STYLE_CARD_LABEL = ParagraphStyle(
    'CardLabel', fontName=FONT, fontSize=7.5, leading=10,
    textColor=C_MUTED,
)
STYLE_CARD_VALUE = ParagraphStyle(
    'CardValue', fontName=FONT, fontSize=9, leading=13,
    textColor=C_TEXT,
)
STYLE_TH = ParagraphStyle(
    'TH', fontName=FONT, fontSize=8.5, leading=11,
    textColor=C_HEAD,
)
STYLE_TC = ParagraphStyle(
    'TC', fontName=FONT, fontSize=8.5, leading=12,
    textColor=C_TEXT,
)
STYLE_KEYWORD = ParagraphStyle(
    'Keyword', fontName=FONT, fontSize=9, leading=13,
    textColor=C_PRIMARY,
)
STYLE_FOOTER = ParagraphStyle(
    'Footer', fontName=FONT, fontSize=7.5, leading=10,
    textColor=C_MUTED, alignment=2,
)
STYLE_TRANSCRIPT = ParagraphStyle(
    'Transcript', fontName=FONT_SERIF, fontSize=8.5, leading=13,
    textColor=C_TEXT, spaceAfter=1*mm,
)


# ─────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────
def render_pdf(
    title: str,
    date_text: Optional[str] = None,
    duration_text: Optional[str] = None,
    mode: str = "meeting",
    highlights: Optional[List[Any]] = None,
    overview: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    sections: Optional[List[Dict[str, Any]]] = None,
    transcript: Optional[str] = None,
    bottom_line: Optional[str] = None,
    why_it_matters: Optional[str] = None,
    outcome_status: Optional[str] = None,
    participants: Optional[List[Dict[str, str]]] = None,
    **_kwargs,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN + 6*mm,
        title=title,
    )

    story: list = []

    # ── Header: title + meta line ──
    story.append(Paragraph(_escape(title), STYLE_TITLE))
    meta_parts = [p for p in [date_text, duration_text, _mode_label(mode)] if p]
    if meta_parts:
        story.append(Paragraph('  ·  '.join(_escape(p) for p in meta_parts), STYLE_META))

    # ── Hero callout: bottomLine + outcomeStatus + whyItMatters ──
    if bottom_line or why_it_matters:
        story.append(_hero_callout(bottom_line, why_it_matters, outcome_status))
        story.append(Spacer(1, 4*mm))

    # ── Participants ──
    if participants:
        story.append(_section_header("参加者", C_PRIMARY))
        story.append(_participants_table(participants))
        story.append(Spacer(1, 2*mm))

    # ── Highlights ──
    rich_hls = _normalize_highlights(highlights)
    if rich_hls:
        story.append(_section_header("要点", C_PRIMARY))
        for h in rich_hls:
            story.append(_highlight_row(h))
        story.append(Spacer(1, 2*mm))

    # ── Overview ──
    if overview:
        story.append(_section_header("概要", C_PRIMARY))
        for para in str(overview).split('\n'):
            if para.strip():
                story.append(Paragraph(_escape(para.strip()), STYLE_BODY))
        story.append(Spacer(1, 2*mm))

    # ── Sections ──
    for sec in (sections or []):
        _render_section(story, sec)

    # ── Keywords ──
    if keywords:
        story.append(_section_header("キーワード", C_PRIMARY))
        story.append(_keywords_flow(keywords))
        story.append(Spacer(1, 3*mm))

    # ── Transcript appendix ──
    if transcript:
        story.append(PageBreak())
        story.append(Paragraph("文字起こし", STYLE_H2))
        story.append(HRFlowable(width='100%', thickness=0.7, color=C_PRIMARY))
        story.append(Spacer(1, 2*mm))
        for line in str(transcript).split('\n'):
            if line.strip():
                story.append(Paragraph(_escape(line.strip()), STYLE_TRANSCRIPT))

    # ── Footer ──
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=C_HAIRLINE))
    story.append(Spacer(1, 1.5*mm))
    story.append(Paragraph('Created by DeepNote', STYLE_FOOTER))

    doc.build(story, onFirstPage=_page_decoration, onLaterPages=_page_decoration)
    return buf.getvalue()


# ─────────────────────────────────────────────────────
# Section dispatcher
# ─────────────────────────────────────────────────────
def _render_section(story: list, sec: Dict[str, Any]) -> None:
    sec_type = sec.get("type", "text")
    sec_title = sec.get("title", "")
    emphasis = sec.get("emphasis", "primary")
    accent, _bg = EMPHASIS_COLORS.get(emphasis, EMPHASIS_COLORS["primary"])

    if sec_title:
        story.append(_section_header(sec_title, accent))

    if sec_type == "text":
        body = sec.get("body", "")
        for para in str(body).split('\n'):
            if para.strip():
                story.append(Paragraph(_escape(para.strip()), STYLE_BODY))
        story.append(Spacer(1, 2*mm))

    elif sec_type == "bullets":
        for item in sec.get("items", []):
            story.append(Paragraph(f'•  {_escape(str(item))}', STYLE_BULLET))
        story.append(Spacer(1, 2*mm))

    elif sec_type == "grouped_bullets":
        for g in sec.get("groups", []):
            label = g.get("label")
            if label:
                story.append(Paragraph(_escape(label), STYLE_H3))
            for item in g.get("items", []):
                story.append(Paragraph(f'•  {_escape(str(item))}', STYLE_BULLET))
        story.append(Spacer(1, 2*mm))

    elif sec_type == "table":
        columns = sec.get("columns", [])
        rows = sec.get("rows", [])
        if columns and rows:
            story.append(_build_table(columns, rows, accent))
            story.append(Spacer(1, 3*mm))

    elif sec_type == "cards":
        for card in sec.get("cards", []):
            story.append(_build_card(card, accent))
            story.append(Spacer(1, 2*mm))


# ─────────────────────────────────────────────────────
# Components
# ─────────────────────────────────────────────────────
def _section_header(label: str, accent: Color) -> Table:
    """Colored bar + title. Renders as a 2-cell table for the accent stripe."""
    p = Paragraph(_escape(label), ParagraphStyle(
        'SH', fontName=FONT, fontSize=11.5, leading=15, textColor=C_HEAD,
    ))
    t = Table([[p]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ('LINEBEFORE', (0, 0), (0, 0), 3, accent),
        ('LEFTPADDING', (0, 0), (-1, -1), 4*mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1*mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1.5*mm),
    ]))
    return t


def _hero_callout(
    bottom_line: Optional[str],
    why_it_matters: Optional[str],
    outcome_status: Optional[str],
) -> Table:
    """A rounded callout box for the meeting bottom line."""
    inner: list = []
    if outcome_status:
        inner.append(Paragraph(
            f'<font color="#FFFFFF"> {_escape(_outcome_label(outcome_status))} </font>',
            ParagraphStyle('Outcome', fontName=FONT, fontSize=8.5, leading=12,
                           backColor=C_PRIMARY, textColor=HexColor('#FFFFFF'),
                           borderPadding=(1, 4, 1, 4)),
        ))
        inner.append(Spacer(1, 1.5*mm))
    if bottom_line:
        inner.append(Paragraph(_escape(bottom_line), STYLE_BOTTOMLINE))
    if why_it_matters:
        inner.append(Paragraph(_escape(why_it_matters), STYLE_WHY))

    t = Table([[inner]], colWidths=[CONTENT_W])
    # Background tint alone is enough accent for the hero callout. The
    # 3pt LINEBEFORE that used to be here doubled visually with the
    # ``_section_header`` LINEBEFORE 3pt immediately below
    # ("参加者" / "要点" / "概要"), producing the "blue stripe twice"
    # the user reported on PC PDF readers.
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_PRIMARY_BG),
        ('LEFTPADDING', (0, 0), (-1, -1), 5*mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5*mm),
        ('TOPPADDING', (0, 0), (-1, -1), 4*mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4*mm),
    ]))
    return t


def _highlight_row(h: Dict[str, Any]) -> Paragraph:
    category = h.get("category") or ""
    text = h.get("text") or ""
    need = h.get("needConfirm")
    chip = ""
    if category:
        chip = f'<b><font color="#1F4FD8">[{_escape(_category_label(category))}]</font></b>  '
    confirm = ' <font color="#EA580C">[要確認]</font>' if need else ''
    return Paragraph(f'• {chip}{_escape(text)}{confirm}', STYLE_HIGHLIGHT)


def _participants_table(participants: List[Dict[str, str]]) -> Table:
    cells = []
    for p in participants:
        name = p.get("name", "") or "(不明)"
        role = p.get("role", "")
        label = f'<b>{_escape(name)}</b>' + (f'  <font color="#888899">{_escape(role)}</font>' if role else '')
        cells.append(Paragraph(label, STYLE_BADGE))
    # Wrap into a simple 3-column grid
    cols = 3
    rows = []
    for i in range(0, len(cells), cols):
        row = cells[i:i + cols]
        while len(row) < cols:
            row.append(Paragraph("", STYLE_BADGE))
        rows.append(row)
    col_w = CONTENT_W / cols
    t = Table(rows, colWidths=[col_w] * cols)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_CARD_BG),
        ('BOX', (0, 0), (-1, -1), 0.5, C_HAIRLINE),
        ('INNERGRID', (0, 0), (-1, -1), 0.3, C_HAIRLINE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3*mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3*mm),
        ('TOPPADDING', (0, 0), (-1, -1), 2*mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2*mm),
    ]))
    return t


def _build_table(columns: List[str], rows: List[list], accent: Color) -> Table:
    col_w = CONTENT_W / len(columns)
    header = [Paragraph(f'<b>{_escape(c)}</b>', STYLE_TH) for c in columns]
    data = [header]
    for row in rows:
        padded = list(row) + [""] * (len(columns) - len(row))
        data.append([Paragraph(_escape(str(cell or "")), STYLE_TC) for cell in padded[:len(columns)]])

    t = Table(data, colWidths=[col_w] * len(columns), repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), C_TH_BG),
        ('LINEABOVE', (0, 0), (-1, 0), 1.2, accent),
        ('LINEBELOW', (0, 0), (-1, 0), 0.5, C_HAIRLINE),
        ('LINEBELOW', (0, 1), (-1, -1), 0.3, C_HAIRLINE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 2.2*mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.2*mm),
        ('LEFTPADDING', (0, 0), (-1, -1), 2*mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2*mm),
    ]))
    return t


def _build_card(card: Dict[str, Any], accent: Color) -> Table:
    inner: list = []
    title = card.get("title", "")
    if title:
        inner.append(Paragraph(f'<b>{_escape(title)}</b>', STYLE_CARD_TITLE))
    for key, value in card.get("fields", []):
        if not value:
            continue
        if key:
            inner.append(Paragraph(f'<font color="#888899">{_escape(key)}</font>  {_escape(str(value))}', STYLE_CARD_VALUE))
        else:
            inner.append(Paragraph(_escape(str(value)), STYLE_CARD_VALUE))
    if not inner:
        inner.append(Paragraph("—", STYLE_CARD_VALUE))

    t = Table([[inner]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), C_CARD_BG),
        ('BOX', (0, 0), (-1, -1), 0.5, C_HAIRLINE),
        ('LINEBEFORE', (0, 0), (0, 0), 2, accent),
        ('LEFTPADDING', (0, 0), (-1, -1), 4*mm),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4*mm),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5*mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5*mm),
    ]))
    return KeepTogether(t)


def _keywords_flow(keywords: List[str]) -> Paragraph:
    parts = [
        f'<b><font color="#1F4FD8">#{_escape(k)}</font></b>'
        for k in keywords if k
    ]
    return Paragraph('   '.join(parts), STYLE_KEYWORD)


# ─────────────────────────────────────────────────────
# Page decoration
# ─────────────────────────────────────────────────────
def _page_decoration(canvas, doc):
    canvas.saveState()
    # Top hairline
    canvas.setStrokeColor(C_HAIRLINE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, PAGE_H - MARGIN + 6*mm, PAGE_W - MARGIN, PAGE_H - MARGIN + 6*mm)
    # Footer page number
    canvas.setFont(FONT, 8)
    canvas.setFillColor(C_MUTED)
    canvas.drawRightString(PAGE_W - MARGIN, MARGIN - 8*mm, f"— {doc.page} —")
    canvas.restoreState()


# ─────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────
def _normalize_highlights(highlights) -> List[Dict[str, Any]]:
    if not highlights:
        return []
    out = []
    for h in highlights:
        if isinstance(h, str):
            out.append({"text": h, "category": None, "needConfirm": False})
        elif isinstance(h, dict):
            out.append({
                "text": h.get("text") or h.get("title") or "",
                "category": h.get("category"),
                "needConfirm": bool(h.get("needConfirm")),
            })
    return [x for x in out if x.get("text")]


def _mode_label(mode: str) -> str:
    return {"lecture": "講義", "meeting": "会議", "translate": "翻訳"}.get(mode, mode)


def _category_label(cat: str) -> str:
    return {
        "decision": "決定",
        "todo": "TODO",
        "concern": "懸念",
        "insight": "気づき",
        "fact": "事実",
        "info": "情報",
        "risk": "リスク",
    }.get(cat, cat)


def _outcome_label(status: str) -> str:
    return {
        "action_agreed": "✓ 合意・次アクション確定",
        "tentative_alignment": "△ 暫定合意",
        "discussion_only": "・ 議論のみ",
        "unresolved": "! 未解決",
    }.get(status, status)


def _escape(s: Any) -> str:
    if s is None:
        return ""
    text = str(s)
    return (
        text.replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
    )
