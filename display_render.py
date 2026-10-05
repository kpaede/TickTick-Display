"""Render the two 200×300 monochrome display panes."""

import re
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from markdown_it import MarkdownIt
from PIL import Image, ImageDraw, ImageFont, ImageOps

from agenda_format import TIMEZONE

WIDTH, HEIGHT = 200, 300
PAGE_BYTES = WIDTH * HEIGHT // 8
MAX_PAGES = 64
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["strikethrough", "table"])
BIT_REVERSE = bytes(int(f"{n:08b}"[::-1], 2) for n in range(256))


@dataclass(frozen=True)
class Style:
    bold: bool = False
    italic: bool = False
    strike: bool = False
    underline: bool = False
    code: bool = False
    size: int = 13


@dataclass(frozen=True)
class Span:
    text: str
    style: Style = Style()


@dataclass
class Row:
    spans: list[Span]
    indent: int = 0
    marker: str = ""
    rule: bool = False
    keep_next: bool = False

    @property
    def height(self):
        return 8 if self.rule else max((s.style.size for s in self.spans), default=13) + 4


@lru_cache(maxsize=64)
def font(style):
    root = Path("/System/Library/Fonts/Supplemental")
    variant = " Bold Italic" if style.bold and style.italic else " Bold" if style.bold else " Italic" if style.italic else ""
    path = root / f"Arial{variant}.ttf"
    if style.code:
        path = Path("/System/Library/Fonts/Menlo.ttc")
    if not path.exists():
        raise RuntimeError(f"Die Schrift für das Display fehlt: {path.name}")
    return ImageFont.truetype(str(path), style.size)


def inline_spans(tokens, base=Style()):
    style = base
    stack = []
    spans = []
    properties = {"strong": "bold", "em": "italic", "s": "strike", "link": "underline"}
    for token in tokens:
        kind = token.type
        if kind.endswith("_open") and kind[:-5] in properties:
            stack.append(style)
            style = replace(style, **{properties[kind[:-5]]: True})
        elif kind.endswith("_close") and kind[:-6] in properties:
            style = stack.pop() if stack else base
        elif kind in {"softbreak", "hardbreak"}:
            spans.append(Span("\n", style))
        elif kind == "code_inline":
            spans.append(Span(token.content, replace(style, code=True)))
        elif kind in {"text", "image"}:
            # TickTick also supports ==highlight==, in addition to CommonMark.
            for part in re.split(r"(==[^=\n]+==)", token.content):
                if part.startswith("==") and part.endswith("==") and len(part) > 4:
                    spans.append(Span(part[2:-2], replace(style, bold=True, underline=True)))
                elif part:
                    spans.append(Span(part, style))
    return spans


def wrap_spans(spans, indent=0, marker=""):
    rows = []
    current = []
    width = 0
    available = WIDTH - 20 - indent
    first_marker = marker

    def finish():
        nonlocal current, width, first_marker
        # Drop trailing spaces so they cannot extend decorations off the pane.
        while current and not current[-1].text.strip():
            current.pop()
        if current:
            current[-1] = replace(current[-1], text=current[-1].text.rstrip())
        rows.append(Row(current, indent, first_marker))
        current, width, first_marker = [], 0, ""

    for span in spans:
        for word in re.findall(r"\n|[^\S\n]+|[^\s]+", span.text):
            if word == "\n":
                finish()
                continue
            word_width = font(span.style).getlength(word)
            if current and width + word_width > available:
                finish()
            if not current and word.isspace():
                continue
            # Long words/links wrap by Unicode character, keeping every glyph.
            for character in word:
                advance = font(span.style).getlength(character)
                if current and width + advance > available:
                    finish()
                if not current and character.isspace():
                    continue
                if current and current[-1].style == span.style:
                    current[-1] = replace(current[-1], text=current[-1].text + character)
                else:
                    current.append(Span(character, span.style))
                width += advance
    if current or not rows:
        finish()
    return rows


def markdown_rows(text):
    rows = []
    lists = []
    pending_marker = ""
    heading = False
    quote_depth = 0
    table_row = None
    table_heading = False
    for token in MARKDOWN.parse(text):
        kind = token.type
        if kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append(token.attrGet("start") or 1 if kind == "ordered_list_open" else None)
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif kind == "list_item_open":
            pending_marker = "•" if lists[-1] is None else f"{lists[-1]}."
            if lists[-1] is not None:
                lists[-1] += 1
        elif kind == "heading_open":
            heading = True
        elif kind == "heading_close":
            heading = False
        elif kind == "blockquote_open":
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
        elif kind == "thead_open":
            table_heading = True
        elif kind == "thead_close":
            table_heading = False
            rows.append(Row([], rule=True))
        elif kind == "tr_open":
            table_row = []
        elif kind == "tr_close":
            rows.extend(wrap_spans(table_row or []))
            table_row = None
        elif kind == "inline":
            spans = inline_spans(token.children or [], Style(bold=heading or table_heading, size=12 if heading else 13))
            marker = pending_marker
            pending_marker = ""
            # Task checkboxes can occur with or without a Markdown list marker.
            if spans and (match := re.match(r"^\[([ xX])\]\s*", spans[0].text)):
                marker = "checked" if match[1].lower() == "x" else "checkbox"
                spans[0] = replace(spans[0], text=spans[0].text[match.end():])
            if table_row is not None:
                if table_row:
                    table_row.append(Span(" · "))
                table_row.extend(spans)
            else:
                indent = min(42, max(len(lists) - 1, 0) * 10 + quote_depth * 8 + (14 if marker else 0))
                rows.extend(wrap_spans(spans, indent, marker))
        elif kind == "hr":
            rows.append(Row([], rule=True))
        elif kind in {"fence", "code_block"}:
            rows.extend(wrap_spans([Span(token.content.rstrip("\n"), Style(code=True))], min(32, quote_depth * 8)))
    return rows


def draw_row(draw, row, y):
    if row.rule:
        draw.line((10, y + 3, WIDTH - 10, y + 3), fill=0)
        return
    x = 10 + row.indent
    baseline = y + max((font(span.style).getmetrics()[0] for span in row.spans), default=13)
    if row.marker in {"checkbox", "checked"}:
        left = x - 14
        draw.rectangle((left, y + 4, left + 8, y + 12), outline=0)
        if row.marker == "checked":
            draw.line([(left + 1, y + 8), (left + 3, y + 11), (left + 7, y + 5)], fill=0, width=1)
    elif row.marker:
        draw.text((10 + max(row.indent - 14, 0), baseline), row.marker, font=font(Style(size=11)), fill=0, anchor="ls")
    for span in row.spans:
        draw.text((x, baseline), span.text, font=font(span.style), fill=0, anchor="ls")
        end = x + font(span.style).getlength(span.text)
        if span.style.strike:
            draw.line((x, baseline - span.style.size // 3, end, baseline - span.style.size // 3), fill=0)
        if span.style.underline:
            draw.line((x, baseline + 1, end, baseline + 1), fill=0)
        x = end


def render_pages(text):
    stamp_match = re.match(r"<!-- agenda-stamp: (\d{2}\.\d{2}\.\d{4}) (\d{2}:\d{2}) -->\n?", text)
    if stamp_match:
        _, time_text = stamp_match.groups()
        text = text[stamp_match.end():]
    else:
        now = datetime.now(TIMEZONE)
        time_text = now.strftime("%H:%M")
    rows = markdown_rows(text)
    return render_rows(rows, "Tasks", time_text)


def render_rows(rows, heading, time_text):
    page_rows = [[]]
    y = 34
    for index, row in enumerate(rows):
        needed = row.height
        if row.keep_next and index + 1 < len(rows):
            needed += rows[index + 1].height
        if y + needed > 283:
            page_rows.append([])
            y = 34
        page_rows[-1].append(row)
        y += row.height
    if len(page_rows) > MAX_PAGES:
        raise RuntimeError(f"Die Agenda überschreitet {MAX_PAGES} Displayseiten.")
    images = []
    for index, contents in enumerate(page_rows):
        image = Image.new("L", (WIDTH, HEIGHT), 255)
        draw = ImageDraw.Draw(image)
        draw.text((10, 7), heading, font=font(Style(bold=True, size=17)), fill=0)
        small = font(Style(size=9))
        # A drawn refresh icon avoids missing Unicode glyphs on the device.
        update_font = font(Style(size=10))
        icon_x = round(190 - update_font.getlength(time_text) - 16)
        draw.arc((icon_x, 11, icon_x + 10, 21), 25, 160, fill=0)
        draw.arc((icon_x, 11, icon_x + 10, 21), 205, 340, fill=0)
        draw.polygon([(icon_x, 14), (icon_x, 19), (icon_x + 4, 17)], fill=0)
        draw.polygon([(icon_x + 10, 13), (icon_x + 10, 18), (icon_x + 6, 15)], fill=0)
        draw.text((190, 11), time_text, font=update_font, fill=0, anchor="rt")
        y = 34
        for row in contents:
            draw_row(draw, row, y)
            y += row.height
        draw.text((190, 288), f"{index + 1}/{len(page_rows)}", font=small, fill=0, anchor="rt")
        images.append(image.point(lambda value: 255 if value >= 150 else 0, mode="1"))
    return images


def pack_page(image):
    # U8g2 XBMP: row-major bytes, least-significant bit first, 1 means ink.
    return ImageOps.invert(image.convert("L")).convert("1").tobytes().translate(BIT_REVERSE)
