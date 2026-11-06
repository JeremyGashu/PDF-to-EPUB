"""PDF -> EPUB conversion engine.

Pipeline
  1. Inspect pages and OCR the ones without a usable text layer (OCRmyPDF/Tesseract).
  2. Pass 1: extract every text line with its typography, gather document-wide
     statistics (body font size, heading sizes), detect running headers/footers,
     page numbers, decorative repeated images and scanned pages.
  3. Pass 2: per page find tables, raster/vector figures and their captions, recover
     the reading order (multi-column aware) and emit a stream of lines, blocks and
     markers (page breaks, outline anchors).
  4. Group lines into paragraphs, headings, lists and code blocks across line, column
     and page boundaries, then split into chapters along the PDF outline.
  5. Write EPUB 3 (nav TOC, page-list, landmarks) with an EPUB 2 NCX fallback.
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pymupdf

from . import ocr
from .epub import EpubBuilder, Item, NavPoint, xhtml

log = logging.getLogger("pdf2epub")
ProgressFn = Callable[[float, str], None]


class ConversionError(Exception):
    pass


@dataclass
class Options:
    title: str | None = None
    author: str | None = None
    language: str = "en"
    ocr: str = "auto"            # auto | force | off
    ocr_languages: str = "eng"
    layout: str = "reflow"       # reflow | fixed
    tables: str = "html"         # html | image
    cover: bool = True
    keep_headers: bool = False   # keep running headers/footers/page numbers
    password: str | None = None
    source_name: str | None = None  # original file name, for the title fallback


@dataclass
class Result:
    title: str
    author: str
    pages: int
    chapters: int
    images: int
    tables: int
    ocr_pages: int
    warnings: list[str]


# --------------------------------------------------------------------------- text helpers
TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
BOLD_RE = re.compile(r"bold|black|heavy|semibold|demi|extrab|ultrab", re.I)
ITALIC_RE = re.compile(r"italic|oblique|-it$|,it$", re.I)
MONO_RE = re.compile(r"mono|courier|consol|menlo|inconsolata|lucidaconsole|sourcecode|code", re.I)
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]")
PUA_RE = re.compile(r"[-]")
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
SYMBOL_BULLETS = str.maketrans({"": "•", "": "▪", "": "➢", "": "❖", "": "✓", "": "■", "": "□"})

BULLET_RE = re.compile(r"^\s*([•●▪▫◦‣∙·○■□◆◇►▸▹➢➤❖✓✔\-–*])\s+(?=\S)")
OCR_BULLET_RE = re.compile(r"^\s*([•●▪▫◦‣∙·○■□◆◇►▸▹➢➤❖✓✔\-–*+»«])\s+(?=\S)")
NUMBER_RE = re.compile(r"^\s*\(?(\d{1,3}|[a-z]|[ivx]{1,5})[.)]\s+(?=\S)")
CAPTION_RE = re.compile(r"^\s*(fig(ure)?|image|photo|chart|diagram|plate|illustration|exhibit|table|graph|map|listing)\.?\s*[\dIVXivx]+", re.I)
PAGE_NUM_RE = re.compile(r"^[\s\-–—|•]*(page\s*)?(\d{1,4}|[ivxlcdm]{1,7})(\s*(of|/)\s*\d+)?[\s\-–—|•]*$", re.I)
TERMINAL = tuple('.!?:;"”’)]…»')
CJK_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]")


def clean_text(s: str) -> str:
    s = CTRL_RE.sub("", s).translate(LIGATURES).translate(SYMBOL_BULLETS)
    return PUA_RE.sub("", s)


def norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def ends_terminal(s: str) -> bool:
    return s.rstrip().endswith(TERMINAL)


def esc(s: str) -> str:
    return html.escape(s, quote=True)


# --------------------------------------------------------------------------- data model
@dataclass
class Span:
    text: str
    size: float
    bold: bool
    italic: bool
    mono: bool
    sup: bool
    bbox: tuple
    link: str | None = None


@dataclass(eq=False)
class Line:
    page: int
    block: int
    bbox: pymupdf.Rect
    block_bbox: pymupdf.Rect
    spans: list[Span]
    text: str
    size: float
    bold: bool
    mono: bool
    ocr: bool = False
    style: str = "body"          # body | small | bold | pre | h
    level: int = 0
    anchors: list[str] = field(default_factory=list)
    forced_break: bool = False
    removed: bool = False
    multicol: bool = False
    area: pymupdf.Rect | None = None

    @property
    def h(self) -> float:
        return max(self.bbox.height, 1.0)


@dataclass(eq=False)
class Block:
    """A positioned item in a page's reading order: a group of lines, a figure or a table."""
    bbox: pymupdf.Rect
    lines: list[Line] = field(default_factory=list)
    html: str | None = None      # pre-rendered figure/table
    kind: str = "text"           # text | figure | table


@dataclass(eq=False)
class Element:
    kind: str                    # p | h | li | pre | figure | table | raw
    html: str
    anchors: list[str] = field(default_factory=list)
    level: int = 0
    text: str = ""
    list_type: str | None = None  # ul | ol | plain


@dataclass(eq=False)
class Para:
    kind: str
    lines: list[Line]
    level: int = 0
    markers: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    anchors: list[str] = field(default_factory=list)
    prefix: list[str] = field(default_factory=list)
    list_type: str | None = None
    list_value: int | None = None


# --------------------------------------------------------------------------- utilities
def rect_union(rects) -> pymupdf.Rect:
    r = pymupdf.Rect(rects[0])
    for x in rects[1:]:
        r |= x
    return r


def merge_rects(rects: list[pymupdf.Rect], pad: float = 4) -> list[pymupdf.Rect]:
    rects = [pymupdf.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        out: list[pymupdf.Rect] = []
        for r in rects:
            for o in out:
                if (o + (-pad, -pad, pad, pad)).intersects(r):
                    o |= r
                    changed = True
                    break
            else:
                out.append(pymupdf.Rect(r))
        rects = out
    return rects


def center_in(line: Line, r: pymupdf.Rect) -> bool:
    c = (line.bbox.tl + line.bbox.br) / 2
    return r.contains(c)


def overlap_ratio(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    i = a & b
    if i.is_empty:
        return 0.0
    return i.get_area() / max(min(a.get_area(), b.get_area()), 1e-6)


def clean_title(t: str | None) -> str | None:
    if not t:
        return None
    t = re.sub(r"^microsoft (word|powerpoint|excel) - ", "", t.strip(), flags=re.I)
    t = re.sub(r"\.(docx?|pptx?|xlsx?|pdf|indd|tex|odt|rtf)$", "", t, flags=re.I).strip()
    if len(t) < 2 or t.lower() in {"untitled", "document", "title", "untitled document"}:
        return None
    return t


def page_label(page: pymupdf.Page) -> str:
    try:
        lbl = page.get_label()
    except Exception:
        lbl = ""
    return lbl or str(page.number + 1)


# --------------------------------------------------------------------------- converter
class Converter:
    def __init__(self, pdf_path: str, epub_path: str, opts: Options, progress: ProgressFn | None = None):
        self.pdf_path = pdf_path
        self.epub_path = epub_path
        self.opts = opts
        self._progress = progress or (lambda f, m: None)
        self.warnings: list[str] = []
        self.work = tempfile.mkdtemp(prefix="pdf2epub-")
        self.ocr_pages: set[int] = set()
        self.images: dict[str, Item] = {}  # sha1 -> item
        self.n_tables = 0
        self.anchor_seq = 0

    def progress(self, frac: float, msg: str) -> None:
        self._progress(max(0.0, min(1.0, frac)), msg)

    def warn(self, msg: str) -> None:
        log.warning(msg)
        if msg not in self.warnings:
            self.warnings.append(msg)

    # ------------------------------------------------------------------ entry
    def run(self) -> Result:
        try:
            return self._run()
        finally:
            shutil.rmtree(self.work, ignore_errors=True)

    def _open(self, path: str) -> pymupdf.Document:
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            raise ConversionError(f"Could not open PDF: {e}") from e
        if not doc.is_pdf:
            raise ConversionError("The file is not a PDF.")
        if doc.needs_pass and not doc.authenticate(self.opts.password or ""):
            raise ConversionError("This PDF is password protected. Provide the password to convert it.")
        if doc.page_count == 0:
            raise ConversionError("The PDF has no pages.")
        return doc

    def _run(self) -> Result:
        self.progress(0.01, "Opening PDF")
        doc = self._open(self.pdf_path)
        meta = doc.metadata or {}
        toc = doc.get_toc(simple=False)

        if self.opts.ocr != "off" and self.opts.layout != "fixed":
            doc = self._maybe_ocr(doc)

        self.doc = doc
        self.toc = toc
        title = self.opts.title or clean_title(meta.get("title"))
        author = (self.opts.author or (meta.get("author") or "").strip() or "Unknown")
        if self.opts.layout == "fixed":
            book, n_chapters, title = self._build_fixed(title, author)
        else:
            book, n_chapters, title = self._build_reflow(title, author)

        self.progress(0.97, "Writing EPUB")
        book.write(self.epub_path)
        self.progress(1.0, "Done")
        return Result(title=title, author=author, pages=doc.page_count, chapters=n_chapters,
                      images=len(self.images) if self.opts.layout != "fixed" else doc.page_count, tables=self.n_tables, ocr_pages=len(self.ocr_pages),
                      warnings=self.warnings)

    # ------------------------------------------------------------------ OCR
    def _page_needs_ocr(self, page: pymupdf.Page) -> bool:
        text = re.sub(r"\s+", "", page.get_text("text"))
        area = page.rect.get_area()
        img_cover = 0.0
        for info in page.get_image_info():
            img_cover = max(img_cover, (pymupdf.Rect(info["bbox"]) & page.rect).get_area() / area)
        if len(text) < 25:
            return img_cover >= 0.5
        bad = sum(1 for c in text if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
        good = sum(1 for c in text if c.isalnum())
        return bad / len(text) > 0.15 or good / len(text) < 0.45

    def _maybe_ocr(self, doc: pymupdf.Document) -> pymupdf.Document:
        if self.opts.ocr == "force":
            pages = list(range(doc.page_count))
        else:
            self.progress(0.02, "Checking which pages need OCR")
            pages = [p.number for p in doc if self._page_needs_ocr(p)]
        if not pages:
            return doc
        if not ocr.available():
            self.warn(f"{len(pages)} page(s) look scanned but OCR is not installed; their text may be missing.")
            return doc
        langs, missing = ocr.normalize_languages(self.opts.ocr_languages)
        if missing:
            self.warn(f"OCR language(s) not installed, ignored: {', '.join(missing)}")
        src = os.path.join(self.work, "clean.pdf")
        dst = os.path.join(self.work, "ocr.pdf")
        doc.save(src, garbage=1, encryption=pymupdf.PDF_ENCRYPT_NONE)
        self.progress(0.03, f"Running OCR on {len(pages)} page(s) [{langs}]")

        def cb(done: int, total: int) -> None:
            self.progress(0.03 + 0.37 * done / max(total, 1), f"OCR: page {done}/{total}")

        try:
            ocr.run(src, dst, pages, langs, cb)
        except Exception as e:
            self.warn(f"OCR failed, continuing without it: {e}")
            return doc
        self.ocr_pages = set(pages)
        new = pymupdf.open(dst)
        doc.close()
        return new

    # ------------------------------------------------------------------ images
    def add_image(self, data: bytes, ext: str) -> str:
        key = hashlib.sha1(data).hexdigest()
        if key not in self.images:
            n = len(self.images) + 1
            mt = "image/jpeg" if ext == "jpg" else "image/png"
            self.images[key] = Item(f"img{n}", f"images/img{n:04d}.{ext}", mt, data)
        return self.images[key].href

    def render_region(self, page: pymupdf.Page, r: pymupdf.Rect, native_zoom: float | None = None,
                      photo: bool = False, blank: list[pymupdf.Rect] | None = None) -> str:
        zoom = min(max(native_zoom or 3.0, 2.0), 4.0)
        zoom = min(zoom, 2400 / max(r.width, r.height, 1))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=r, alpha=False, colorspace=pymupdf.csRGB)
        for b in blank or ():  # paint out text that stays in the text flow
            # a clipped pixmap keeps page-space device coordinates (pix.irect does not start at 0,0)
            ir = pymupdf.IRect(int(b.x0 * zoom), int(b.y0 * zoom), int(b.x1 * zoom) + 1, int(b.y1 * zoom) + 1)
            ir &= pix.irect
            if not ir.is_empty:
                pix.set_rect(ir, (255, 255, 255))
        png = pix.tobytes("png")
        if photo or len(png) > 350_000:
            jpg = pix.tobytes("jpeg", jpg_quality=88)
            if photo or len(jpg) < 0.7 * len(png):
                return self.add_image(jpg, "jpg")
        return self.add_image(png, "png")

    # ------------------------------------------------------------------ pass 1
    def extract_lines(self, page: pymupdf.Page, links: list[dict], ocr_page: bool) -> list[Line]:
        d = page.get_text("dict", flags=TEXT_FLAGS)
        out: list[Line] = []
        for bno, b in enumerate(d["blocks"]):
            if b.get("type") != 0:
                continue
            bb = pymupdf.Rect(b["bbox"])
            for l in b["lines"]:
                dx, dy = l["dir"]
                if abs(dy) > 0.1 or dx <= 0:
                    continue  # rotated / vertical text (margin notes, spine labels)
                spans: list[Span] = []
                for s in l["spans"]:
                    t = clean_text(s["text"])
                    if not t:
                        continue
                    font = s.get("font", "")
                    fl = s.get("flags", 0)
                    spans.append(Span(
                        text=t, size=float(s["size"]),
                        bold=bool(fl & 16) or bool(BOLD_RE.search(font)),
                        italic=bool(fl & 2) or bool(ITALIC_RE.search(font)),
                        mono=bool(fl & 8) or bool(MONO_RE.search(font)),
                        sup=bool(fl & 1), bbox=tuple(s["bbox"]),
                    ))
                text = "".join(sp.text for sp in spans)
                if not text.strip():
                    continue
                weights = [(sp, len(sp.text.strip())) for sp in spans if sp.text.strip()]
                main = [(sp, w) for sp, w in weights if not sp.sup] or weights
                tot = sum(w for _, w in main) or 1
                size = sum(sp.size * w for sp, w in main) / tot
                # size-based superscript detection (footnote markers in fonts lacking the flag)
                lb = pymupdf.Rect(l["bbox"])
                for sp in spans:
                    if not sp.sup and sp.size < 0.78 * size and len(sp.text.strip()) <= 4:
                        mid = (sp.bbox[1] + sp.bbox[3]) / 2
                        if mid < lb.y0 + lb.height * 0.45:
                            sp.sup = True
                for sp in spans:
                    sr = pymupdf.Rect(sp.bbox)
                    c = (sr.tl + sr.br) / 2
                    for ln in links:
                        if ln["from"].contains(c):
                            sp.link = ln["href"]
                            break
                bold = sum(w for sp, w in weights if sp.bold) >= 0.9 * sum(w for _, w in weights)
                mono = sum(w for sp, w in weights if sp.mono) >= 0.9 * sum(w for _, w in weights)
                out.append(Line(page=page.number, block=bno, bbox=lb, block_bbox=bb, spans=spans,
                                text=text, size=size, bold=bold, mono=mono, ocr=ocr_page))
        return out

    def page_links(self, page: pymupdf.Page) -> list[dict]:
        out = []
        for ln in page.get_links():
            href = None
            if ln.get("kind") == pymupdf.LINK_URI:
                uri = ln.get("uri") or ""
                if re.match(r"^(https?|mailto|ftp):", uri, re.I):
                    href = uri
            elif ln.get("kind") == pymupdf.LINK_GOTO and ln.get("page", -1) >= 0:
                href = f"__PG_{ln['page']}__"
            if href:
                out.append({"from": pymupdf.Rect(ln["from"]), "href": href})
        return out

    def invisible_ratio(self, page: pymupdf.Page) -> float:
        try:
            tr = page.get_texttrace()
        except Exception:
            return 0.0
        tot = inv = 0
        for s in tr:
            n = len(s.get("chars", ()))
            tot += n
            if s.get("type") == 3 or s.get("opacity", 1) == 0:
                inv += n
        return inv / tot if tot else 0.0

    def pass1(self) -> None:
        doc = self.doc
        n = doc.page_count
        self.lines: list[list[Line]] = []
        self.image_infos: list[list[dict]] = []
        self.scanned: list[bool] = []
        digest_pages: dict[bytes, set[int]] = defaultdict(set)
        for page in doc:
            self.progress(0.40 + 0.15 * page.number / n, f"Analyzing page {page.number + 1}/{n}")
            area = page.rect.get_area()
            infos = []
            try:
                raw = page.get_image_info(hashes=True)
            except Exception:
                raw = []
            for info in raw:
                r = pymupdf.Rect(info["bbox"]) & page.rect
                if r.is_empty:
                    continue
                info["rect"] = r
                infos.append(info)
                if info.get("digest"):
                    digest_pages[info["digest"]].add(page.number)
            big = max((i["rect"].get_area() / area for i in infos), default=0)
            ocr_page = page.number in self.ocr_pages
            lines = self.extract_lines(page, self.page_links(page), False)
            scanned = big >= 0.85 and (ocr_page or not lines or self.invisible_ratio(page) > 0.5)
            for l in lines:
                l.ocr = scanned
            self.lines.append(lines)
            self.image_infos.append(infos)
            self.scanned.append(scanned)

        thr = max(3, int(0.15 * n))
        self.decor = {d for d, ps in digest_pages.items() if len(ps) >= thr and n >= 3}

        # body font size = the size carrying the most characters
        sizes, ocr_sizes, ocr_heights = Counter(), Counter(), []
        for lines in self.lines:
            for l in lines:
                if l.ocr:
                    ocr_sizes[round(l.size * 2) / 2] += len(l.text.strip())
                    ocr_heights.append(l.bbox.height)
                    continue
                for sp in l.spans:
                    if not sp.sup and sp.text.strip():
                        sizes[round(sp.size * 2) / 2] += len(sp.text.strip())
        self.ocr_body = ocr_sizes.most_common(1)[0][0] if ocr_sizes else 10.0
        self.ocr_line_h = statistics.median(ocr_heights) if ocr_heights else 12.0
        self.body = sizes.most_common(1)[0][0] if sizes else self.ocr_body

        self._remove_headers_footers()

        # heading size levels (born-digital lines only; OCR sizes are noisy)
        hs = Counter()
        for lines in self.lines:
            for l in lines:
                if not l.removed and not l.ocr and l.size >= self.body * 1.12 and len(l.text) <= 200:
                    hs[round(l.size * 2) / 2] += 1
        levels: list[float] = []
        for s in sorted(hs, reverse=True):
            if not levels or levels[-1] - s > 0.75:
                levels.append(s)
        self.heading_sizes = levels[:5]

        # typical gap between consecutive body lines of one block
        gaps = []
        for lines in self.lines:
            for a, b in zip(lines, lines[1:]):
                if a.block == b.block and abs(a.size - self.body) < 1 and 0 <= b.bbox.y0 - a.bbox.y1 < a.h:
                    gaps.append(b.bbox.y0 - a.bbox.y1)
        self.line_gap = statistics.median(gaps) if gaps else self.body * 0.25

    def _remove_headers_footers(self) -> None:
        if self.opts.keep_headers:
            return
        n = self.doc.page_count
        keys: dict[tuple, set[int]] = defaultdict(set)
        zone_lines = []
        for pno, lines in enumerate(self.lines):
            ph = self.doc[pno].rect.height
            for l in lines:
                zone = "top" if l.bbox.y1 <= ph * 0.09 else "bot" if l.bbox.y0 >= ph * 0.91 else None
                if zone is None or l.size > (self.ocr_body if l.ocr else self.body) * 1.15:
                    continue
                key = (zone, re.sub(r"\d+", "#", norm_key(l.text)) or l.text.strip())
                keys[key].add(pno)
                zone_lines.append((key, l))
        for key, l in zone_lines:
            if PAGE_NUM_RE.match(l.text) or (n >= 3 and len(keys[key]) >= 3):
                l.removed = True

    # ------------------------------------------------------------------ classification
    def classify(self, l: Line) -> None:
        if l.style == "h":
            return
        t = l.text.strip()
        if l.ocr:
            rel = l.size / self.ocr_body
            if rel >= 1.25 and len(t) <= 150:
                l.style, l.level = "h", (1 if rel >= 1.8 else 2)
            else:
                l.style = "body"
            return
        rel = l.size / self.body
        if rel >= 1.12 and len(t) <= 200 and not l.mono:
            lvl = 1
            for i, s in enumerate(self.heading_sizes):
                if l.size >= s - 0.5:
                    lvl = i + 1
                    break
            else:
                lvl = len(self.heading_sizes) or 1
            l.style, l.level = "h", min(lvl, 6)
        elif l.mono and len(t) > 0:
            l.style = "pre"
        elif l.bold and rel >= 0.95:
            l.style = "bold"
        elif rel <= 0.86:
            l.style = "small"
        else:
            l.style = "body"

    # ------------------------------------------------------------------ pass 2 helpers
    def find_tables(self, page: pymupdf.Page, lines: list[Line], n_drawings: int) -> list[Block]:
        if n_drawings < 4:
            return []
        try:
            found = page.find_tables()
        except Exception as e:
            log.debug("find_tables failed on page %s: %s", page.number, e)
            return []
        out = []
        chars = None
        for t in found.tables:
            r = pymupdf.Rect(t.bbox)
            if t.row_count < 2 or t.col_count < 2:
                continue
            try:
                data = t.extract()
            except Exception:
                continue
            if chars is None:
                chars = self.page_chars(page)
            try:  # rebuild cell text from glyph positions (fixes misordered glyphs and lost spaces)
                for ri, row in enumerate(t.rows):
                    for ci, cell in enumerate(row.cells):
                        if cell is not None and ri < len(data) and ci < len(data[ri]) and data[ri][ci] is not None:
                            data[ri][ci] = self.text_in(chars, pymupdf.Rect(cell))
            except Exception as e:
                log.debug("cell text rebuild failed: %s", e)
            filled = sum(1 for row in data for c in row if c and str(c).strip())
            if filled < 3:
                continue
            caption = self.take_caption(lines, r, above=True)
            if self.opts.tables == "image":
                src = self.render_region(page, r)
                body = f'<img src="../{src}" alt="{esc(caption or "Table")}"/>'
                cap = f"<figcaption>{esc(caption)}</figcaption>" if caption else ""
                h = f'<figure class="table-image">{body}{cap}</figure>'
            else:
                h = self.table_html(t, data, caption)
            self.n_tables += 1
            out.append(Block(bbox=r, html=h, kind="table"))
        return out

    @staticmethod
    def page_chars(page: pymupdf.Page) -> list[tuple]:
        out = []
        for b in page.get_text("rawdict", flags=TEXT_FLAGS)["blocks"]:
            for l in b.get("lines", ()):
                if abs(l["dir"][1]) > 0.1:
                    continue
                for sp in l["spans"]:
                    for ch in sp["chars"]:
                        x0, y0, x1, y1 = ch["bbox"]
                        out.append(((x0 + x1) / 2, (y0 + y1) / 2, x0, x1, ch["origin"][1], sp["size"], ch["c"]))
        return out

    @staticmethod
    def text_in(chars: list[tuple], r: pymupdf.Rect) -> str:
        """Text inside a rectangle, read row by row (baseline) and left to right."""
        inside = [c for c in chars if r.x0 <= c[0] <= r.x1 and r.y0 <= c[1] <= r.y1]
        if not inside:
            return ""
        rows: list[list[tuple]] = []
        for c in sorted(inside, key=lambda c: c[4]):
            if rows and abs(c[4] - rows[-1][-1][4]) <= 0.45 * c[5]:
                rows[-1].append(c)
            else:
                rows.append([c])
        lines = []
        for row in rows:
            row.sort(key=lambda c: c[2])
            txt, prev = "", None
            for c in row:
                if prev is not None and c[2] - prev[3] > 0.22 * c[5] and not txt.endswith(" ") and c[6] != " ":
                    txt += " "
                txt += c[6]
                prev = c
            txt = clean_text(re.sub(r"\s+", " ", txt)).strip()
            if txt in (",", ".") and lines:
                continue  # stray thousands separator drawn as its own glyph
            if txt:
                lines.append(txt)
        return "\n".join(lines)

    @staticmethod
    def segments(l: Line, words: list[tuple]) -> list[tuple[pymupdf.Rect, str, bool, list]]:
        """Split a line where words are separated by a wide gap (cells of a borderless table)."""
        mine = sorted((w for w in words if l.bbox.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))),
                      key=lambda w: w[0])
        if not mine:
            return [(pymupdf.Rect(l.bbox), l.text.strip(), l.bold, [(pymupdf.Rect(l.bbox), l.text.strip())])]
        out: list[list] = []
        gap = max(0.9 * l.size, 5)
        for w in mine:
            r = pymupdf.Rect(w[:4])
            if out and r.x0 - out[-1][0].x1 < gap:
                out[-1][0] |= r
                out[-1][1] += " " + w[4]
                out[-1][2].append((r, w[4]))
            else:
                out.append([r, w[4], [(r, w[4])]])
        return [(r, t.strip(), l.bold, ws) for r, t, ws in out]

    def text_tables(self, page: pymupdf.Page, lines: list[Line]) -> list[Block]:
        """Detect tables without ruling lines: runs of rows whose cells align in columns."""
        cand = [l for l in lines if not l.removed and l.style not in ("h", "pre")]
        if len(cand) < 6:
            return []
        words = page.get_text("words", flags=TEXT_FLAGS)
        rows: list[dict] = []
        for l in sorted(cand, key=lambda l: (l.bbox.y0 + l.bbox.y1) / 2):
            cy = (l.bbox.y0 + l.bbox.y1) / 2
            segs = self.segments(l, words)
            if rows and abs(rows[-1]["cy"] - cy) < 0.45 * l.h:
                rows[-1]["segs"].extend(segs)
                rows[-1]["lines"].append(l)
            else:
                rows.append({"cy": cy, "segs": segs, "lines": [l], "h": l.h})
        for r in rows:
            r["segs"].sort(key=lambda s: s[0].x0)
        out: list[Block] = []
        i = 0
        while i < len(rows):
            if len(rows[i]["segs"]) < 2:
                i += 1
                continue
            j = i
            while (j + 1 < len(rows) and len(rows[j + 1]["segs"]) >= 2
                   and rows[j + 1]["cy"] - rows[j]["cy"] < 2.6 * rows[j]["h"]):
                j += 1
            run, i = rows[i: j + 1], j + 1
            if len(run) < 3:
                continue
            segs = [s for r in run for s in r["segs"]]
            if statistics.mean(len(s[1]) for s in segs) > 35:
                continue  # prose in columns, not a table
            if sum(1 for r in run if BULLET_RE.match(r["segs"][0][1] + " x") or NUMBER_RE.match(r["segs"][0][1] + " x")) > len(run) / 2:
                continue  # a list with hanging markers
            # column starts: left edges shared by many rows
            xs = sorted(s[0].x0 for s in segs)
            clusters: list[list[float]] = []
            for x in xs:
                if clusters and x - clusters[-1][-1] <= 5:
                    clusters[-1].append(x)
                else:
                    clusters.append([x])
            starts = [min(c) for c in clusters if len(c) >= max(2, 0.4 * len(run))]
            if len(starts) < 2:
                continue
            def cells_of(row: dict) -> list[str]:
                cells = [""] * len(starts)
                for *_, ws in row["segs"]:
                    for rect, text in ws:
                        ci = max([k for k, x in enumerate(starts) if x <= rect.x0 + 5] or [0])
                        cells[ci] = (cells[ci] + " " + text).strip()
                return cells

            # adjacent single-segment rows (often the header) that still spread over the columns
            first = rows.index(run[0])
            while first > 0 and run[0]["cy"] - rows[first - 1]["cy"] < 2.6 * rows[first - 1]["h"] \
                    and sum(1 for c in cells_of(rows[first - 1]) if c) >= 2 \
                    and all(len(c) <= 40 for c in cells_of(rows[first - 1])):
                first -= 1
                run.insert(0, rows[first])
                segs = [s for r in run for s in r["segs"]]
            grid = [cells_of(r) for r in run]
            if sum(1 for cells in grid if sum(1 for c in cells if c) >= 2) < 0.7 * len(grid):
                continue
            bbox = rect_union([s[0] for s in segs])
            for r in run:
                for l in r["lines"]:
                    l.removed = True
            header = 1 if all(s[2] for s in run[0]["segs"]) and not all(s[2] for r in run[1:] for s in r["segs"]) else 0
            caption = self.take_caption(lines, bbox, above=True)
            if self.opts.tables == "image":
                src = self.render_region(page, bbox + (-4, -4, 4, 4))
                cap = f"<figcaption>{esc(caption)}</figcaption>" if caption else ""
                h = f'<figure class="table-image"><img src="../{src}" alt="{esc(caption or "Table")}"/>{cap}</figure>'
            else:
                trs = []
                for ri, cells in enumerate(grid):
                    tag = "th" if ri < header else "td"
                    trs.append("<tr>" + "".join(f"<{tag}>{esc(c)}</{tag}>" for c in cells) + "</tr>")
                cap = f"<caption>{esc(caption)}</caption>" if caption else ""
                head = f"<thead>{trs[0]}</thead>" if header else ""
                h = f'<table class="borderless">{cap}{head}<tbody>{"".join(trs[header:])}</tbody></table>'
            self.n_tables += 1
            out.append(Block(bbox=bbox, html=h, kind="table"))
        return out

    def table_html(self, t, data: list[list], caption: str | None) -> str:
        # drop blank rows and spacer columns (all cells empty, none merged)
        data = [row for row in data if any(c and str(c).strip() for c in row) or any(c is None for c in row)]
        if data:
            keep = [ci for ci in range(len(data[0]))
                    if any(ci < len(row) and (row[ci] is None or str(row[ci]).strip()) for row in data)]
            data = [[row[ci] for ci in keep if ci < len(row)] for row in data]
        header_rows = 0
        try:
            if t.header and not t.header.external:
                header_rows = 1
        except Exception:
            pass
        rows_html = []
        for ri, row in enumerate(data):
            cells = []
            ci = 0
            while ci < len(row):
                val = row[ci]
                span = 1
                while ci + span < len(row) and row[ci + span] is None and val is not None:
                    span += 1
                if val is None and ci > 0:
                    ci += 1  # covered by a horizontal merge already emitted
                    continue
                tag = "th" if ri < header_rows else "td"
                txt = "<br/>".join(esc(x.strip()) for x in str(val or "").split("\n"))
                cs = f' colspan="{span}"' if span > 1 else ""
                cells.append(f"<{tag}{cs}>{txt}</{tag}>")
                ci += span
            rows_html.append("<tr>" + "".join(cells) + "</tr>")
        cap = f"<caption>{esc(caption)}</caption>" if caption else ""
        head = f"<thead>{''.join(rows_html[:header_rows])}</thead>" if header_rows else ""
        return f"<table>{cap}{head}<tbody>{''.join(rows_html[header_rows:])}</tbody></table>"

    def take_caption(self, lines: list[Line], r: pymupdf.Rect, above: bool = False) -> str | None:
        """Find a caption block right below (or above) a region; mark its lines as used."""
        tol = max(self.body * 2.5, 24)
        cands = []
        for l in lines:
            if l.removed or not CAPTION_RE.match(l.text):
                continue
            if l.bbox.x1 < r.x0 or l.bbox.x0 > r.x1:
                continue
            below = r.y1 - 2 <= l.bbox.y0 <= r.y1 + tol
            abv = above and r.y0 - tol <= l.bbox.y1 <= r.y0 + 2
            if below or abv:
                cands.append(l)
        if not cands:
            return None
        first = cands[0]
        group = [l for l in lines if not l.removed and l.page == first.page and l.block == first.block]
        for l in group:
            l.removed = True
        return re.sub(r"\s+", " ", " ".join(l.text.strip() for l in group)).strip()

    def find_figures(self, page: pymupdf.Page, lines: list[Line], tables: list[Block],
                     drawings: list[dict]) -> list[Block]:
        prect = page.rect
        parea = prect.get_area()
        cands: list[tuple[pymupdf.Rect, dict | None]] = []
        for info in self.image_infos[page.number]:
            r = info["rect"]
            if r.width < 24 or r.height < 24 or info.get("digest") in self.decor:
                continue
            if self.scanned[page.number] and r.get_area() / parea >= 0.5:
                continue
            cands.append((r, info))

        # vector graphics: cluster drawings and keep the ones that look like charts/diagrams
        paths = [d for d in drawings if d["rect"].get_area() < 0.9 * parea]
        if paths:
            if len(paths) > 4000:
                clusters = [rect_union([d["rect"] for d in paths])]
            else:
                clusters = merge_rects([d["rect"] for d in paths], pad=8)
            for c in clusters:
                if c.width < 40 or c.height < 30 or c.get_area() < 0.015 * parea or c.get_area() > 0.9 * parea:
                    continue
                inside = [d for d in paths if c.contains(d["rect"])]
                n_items = sum(len(d.get("items", ())) for d in inside)
                curves = any(it[0] == "c" for d in inside for it in d.get("items", ()))
                fills = sum(1 for d in inside if d.get("fill") is not None)
                if n_items < 3 or (not curves and n_items < 5 and fills < 3):
                    continue  # boxes, shading, rules
                text_area = sum((l.bbox & c).get_area() for l in lines if not l.removed and c.intersects(l.bbox))
                if text_area > 0.3 * c.get_area():
                    continue  # a text box or an undetected table grid: keep it as text
                if any(overlap_ratio(c, t.bbox) > 0.5 for t in tables):
                    continue
                cands.append((c, None))

        regions = merge_rects([r for r, _ in cands], pad=2)
        out = []
        for r in regions:
            if any(overlap_ratio(r, t.bbox) > 0.5 for t in tables):
                continue
            inside = [l for l in lines if not l.removed and center_in(l, r)]
            chars = sum(len(l.text) for l in inside)
            cover = r.get_area() / parea
            if chars > 600 or (cover > 0.6 and chars > 150):
                continue  # a background image/box with real text on top: keep the text
            infos = [i for rr, i in cands if i is not None and r.contains(rr)]
            native = None
            photo = False
            if infos:
                big = max(infos, key=lambda i: i["rect"].get_area())
                native = big["width"] / max(big["rect"].width, 1)
                photo = big.get("bpc", 8) >= 8 and big.get("colorspace", 3) >= 3 and big["rect"].get_area() > 0.6 * r.get_area()
            for l in inside:
                l.removed = True
            caption = self.take_caption(lines, r)
            out.append(self.figure_block(page, r, caption, native, photo))
        return out

    def figure_block(self, page, r, caption, native=None, photo=False, blank=None) -> Block:
        r = r + (-1, -1, 1, 1)
        r &= page.rect
        src = self.render_region(page, r, native, photo, blank)
        alt = esc(caption or "")
        cap = f"<figcaption>{esc(caption)}</figcaption>" if caption else ""
        wide = " wide" if r.width > 0.45 * page.rect.width else ""
        h = f'<figure class="figure{wide}"><img src="../{src}" alt="{alt}"/>{cap}</figure>'
        return Block(bbox=r, html=h, kind="figure")

    def scan_figures(self, page: pymupdf.Page, lines: list[Line]) -> list[Block]:
        """Recover pictures on a scanned page: ink that is not covered by OCR'd text."""
        z = 1.5
        pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
        ink = a < 185
        H, W = ink.shape
        for l in lines:
            r = l.bbox
            pad = int(max(3, 0.35 * r.height * z))  # glyphs often overhang OCR boxes, more so for big type
            ink[max(0, int(r.y0 * z) - pad): int(r.y1 * z) + pad, max(0, int(r.x0 * z) - pad): int(r.x1 * z) + pad] = False
        mx, my = int(W * 0.05), int(H * 0.04)
        ink[:my, :] = ink[H - my:, :] = False
        ink[:, :mx] = ink[:, W - mx:] = False
        rows = ink.sum(axis=1) > max(3, 0.01 * W)
        bands, start, last = [], None, None
        gap = max(2, int(0.015 * H))
        for y, on in enumerate(rows):
            if on:
                if start is None:
                    start = y
                last = y
            elif start is not None and y - last > gap:
                bands.append((start, last))
                start = None
        if start is not None:
            bands.append((start, last))
        out = []
        for y0, y1 in bands:
            if y1 - y0 < 0.06 * H:
                continue
            # keep the heaviest run of ink columns: drops glyph remnants of text wrapped around the picture
            prof = ink[y0: y1 + 1].sum(axis=0)
            on = np.nonzero(prof > max(2, 0.03 * (y1 - y0)))[0]
            if len(on) == 0:
                continue
            runs, rs = [], on[0]
            for a_, b_ in zip(on, on[1:]):
                if b_ - a_ > max(3, int(0.04 * W)):
                    runs.append((rs, a_))
                    rs = b_
            runs.append((rs, on[-1]))
            for x0, x1 in runs:
                x0, x1 = int(x0), int(x1)
                rows_on = np.nonzero(ink[y0: y1 + 1, x0: x1 + 1].sum(axis=1) > 0)[0]
                if len(rows_on) == 0:
                    continue
                fy0, fy1 = y0 + int(rows_on[0]), y0 + int(rows_on[-1])
                if fy1 - fy0 < 0.06 * H or x1 - x0 < 0.1 * W or ink[fy0: fy1 + 1, x0: x1 + 1].mean() < 0.02:
                    continue
                r = pymupdf.Rect(x0 / z - 4, fy0 / z - 4, x1 / z + 4, fy1 / z + 4) & page.rect
                blank = []
                for l in lines:
                    if l.removed or not l.bbox.intersects(r):
                        continue
                    if (r + (-2, -2, 2, 2)).contains(l.bbox):
                        l.removed = True  # a label inside the picture
                    else:
                        blank.append(l.bbox + (-1, -1, 1, 1))  # wrapped body text: keep as text
                caption = self.take_caption(lines, r)
                out.append(self.figure_block(page, r, caption, native=2.8, photo=True, blank=blank))
        return out

    def is_ocr_junk(self, l: Line) -> bool:
        """OCR noise: speckles or pictures misread as text (often with huge bounding boxes)."""
        t = l.text.strip()
        if l.bbox.height > 3.2 * self.ocr_line_h:
            return True
        alnum = sum(c.isalnum() for c in t)
        return alnum <= 2 and not PAGE_NUM_RE.match(t)

    @staticmethod
    def ocr_quality(lines: list[Line]) -> float:
        words = [w for l in lines for w in l.text.split()]
        if not words:
            return 0.0
        good = sum(1 for w in words if len(w) >= 2 and sum(c.isalpha() for c in w) / len(w) >= 0.6)
        return good / len(words)

    def order_blocks(self, blocks: list[Block], area: pymupdf.Rect) -> tuple[list[Block], bool]:
        """Reading order. Detects column layouts; full-width blocks separate column bands."""
        if not blocks:
            return [], False
        w = max(area.width, 1)
        narrow = sorted([b for b in blocks if b.bbox.width < 0.55 * w], key=lambda b: b.bbox.x0)
        cols: list[list[float]] = []
        for b in narrow:
            if cols and b.bbox.x0 < cols[-1][1] - 2:
                cols[-1][1] = max(cols[-1][1], b.bbox.x1)
            else:
                cols.append([b.bbox.x0, b.bbox.x1])
        members = [[b for b in narrow if c[0] - 1 <= (b.bbox.x0 + b.bbox.x1) / 2 <= c[1] + 1] for c in cols]
        multicol = False
        if len(cols) >= 2:
            populated = [m for m in members if len(m) >= 2]
            if len(populated) >= 2:
                for m1, m2 in zip(populated, populated[1:]):
                    if any(min(a.bbox.y1, b.bbox.y1) - max(a.bbox.y0, b.bbox.y0) > 0 for a in m1 for b in m2):
                        multicol = True
                        break
        if not multicol:
            ordered = sorted(blocks, key=lambda b: b.bbox.y0)
            rows: list[list[Block]] = []
            for b in ordered:
                if rows:
                    ref = rows[-1][-1].bbox
                    ov = min(ref.y1, b.bbox.y1) - max(ref.y0, b.bbox.y0)
                    if ov > 0.5 * min(ref.height, b.bbox.height):
                        rows[-1].append(b)
                        continue
                rows.append([b])
            return [b for row in rows for b in sorted(row, key=lambda b: b.bbox.x0)], False

        def col_of(b: Block) -> int | None:
            hits = [i for i, c in enumerate(cols) if min(c[1], b.bbox.x1) - max(c[0], b.bbox.x0) > 10]
            if len(hits) == 1:
                return hits[0]
            if not hits:
                cx = (b.bbox.x0 + b.bbox.x1) / 2
                return min(range(len(cols)), key=lambda i: abs((cols[i][0] + cols[i][1]) / 2 - cx))
            return None  # spans several columns

        out: list[Block] = []
        band: list[tuple[int, Block]] = []
        for b in sorted(blocks, key=lambda b: b.bbox.y0):
            ci = col_of(b)
            if ci is None:
                out.extend(x for _, x in sorted(band, key=lambda t: (t[0], t[1].bbox.y0)))
                band = []
                out.append(b)
            else:
                band.append((ci, b))
        out.extend(x for _, x in sorted(band, key=lambda t: (t[0], t[1].bbox.y0)))
        return out, True

    def page_area(self, page: pymupdf.Page, lines: list[Line]) -> pymupdf.Rect:
        body = [l for l in lines if l.style in ("body", "bold")] or lines
        if len(body) < 3:
            r = page.rect
            return pymupdf.Rect(r.x0 + r.width * 0.1, r.y0, r.x1 - r.width * 0.1, r.y1)
        x0s = sorted(l.bbox.x0 for l in body)
        x1s = sorted(l.bbox.x1 for l in body)
        return pymupdf.Rect(x0s[len(x0s) // 10], page.rect.y0, x1s[-1 - len(x1s) // 10], page.rect.y1)

    # ------------------------------------------------------------------ outline
    def outline_entries(self) -> dict[int, list[dict]]:
        by_page: dict[int, list[dict]] = defaultdict(list)
        self.outline: list[dict] = []
        for i, e in enumerate(self.toc):
            lvl, title, pno = e[0], (e[1] or "").strip(), e[2] - 1
            if not title or not (0 <= pno < self.doc.page_count):
                continue
            dest = e[3] if len(e) > 3 and isinstance(e[3], dict) else {}
            y = None
            to = dest.get("to")
            if to is not None:
                try:
                    y = float(to.y if hasattr(to, "y") else to[1])
                except Exception:
                    y = None
                if y is not None and not (0 <= y <= self.doc[pno].rect.height):
                    y = None
            ent = {"id": f"toc{i + 1}", "level": max(1, int(lvl)), "title": title, "page": pno, "y": y}
            self.outline.append(ent)
            by_page[pno].append(ent)
        return by_page

    def match_outline(self, entries: list[dict], ordered: list[Block]) -> list[tuple[dict, Block | None]]:
        """Attach outline entries to the heading line they point at; return unmatched entries."""
        lines = [l for b in ordered if b.kind == "text" for l in b.lines]
        unmatched = []
        for e in entries:
            key = norm_key(e["title"])[:40]
            best = None
            if key:
                for i, l in enumerate(lines):
                    lk = norm_key(l.text)
                    nxt = norm_key(lines[i + 1].text) if i + 1 < len(lines) else ""
                    ok = (lk + nxt).startswith(key) or (len(lk) >= 4 and key.startswith(lk))
                    if not ok:
                        continue
                    if l.style != "h" and len(lk) > len(key) * 1.6 + 6:
                        continue
                    score = (0 if l.style == "h" else 1,
                             abs(l.bbox.y0 - e["y"]) if e["y"] is not None else 0)
                    if best is None or score < best[0]:
                        best = (score, l)
            if best:
                l = best[1]
                l.anchors.append(e["id"])
                l.forced_break = True
                l.style = "h"
                l.level = self.outline_hlevel(e["level"])
                e["matched"] = True
            else:
                target = None
                if e["y"] is not None:
                    for b in ordered:
                        if b.bbox.y0 >= e["y"] - 3:
                            target = b
                            break
                unmatched.append((e, target))
        return unmatched

    def outline_hlevel(self, level: int) -> int:
        return max(1, min(6, level - (self.split_level - 1)))

    # ------------------------------------------------------------------ pass 2
    def page_stream(self, pno: int, by_page_outline: dict[int, list[dict]]) -> list:
        page = self.doc[pno]
        lines = [l for l in self.lines[pno] if not l.removed]
        for l in lines:
            self.classify(l)
        stream: list = [("page", pno)]
        figures: list[Block] = []
        tables: list[Block] = []
        if self.scanned[pno]:
            if len(lines) < 3 or self.ocr_quality(lines) < 0.5:
                # picture page (or OCR found only noise): keep the whole page as an image
                for l in lines:
                    l.removed = True
                figures = [self.figure_block(page, page.rect, None, native=2.2, photo=True)]
                lines = []
            else:
                for l in lines:
                    if self.is_ocr_junk(l):
                        l.removed = True
                lines = [l for l in lines if not l.removed]
                figures = self.scan_figures(page, lines)
                lines = [l for l in lines if not l.removed]
                tables = self.text_tables(page, lines)
        else:
            try:
                drawings = page.get_drawings()
            except Exception:
                drawings = []
            tables = self.find_tables(page, lines, len(drawings))
            for t in tables:
                for l in lines:
                    if center_in(l, t.bbox):
                        l.removed = True
            lines = [l for l in lines if not l.removed]
            figures = self.find_figures(page, lines, tables, drawings)
            lines = [l for l in lines if not l.removed]
            tables += self.text_tables(page, lines)
        lines = [l for l in lines if not l.removed]

        blocks: dict[int, Block] = {}
        for l in lines:
            b = blocks.get(l.block)
            if b is None:
                blocks[l.block] = Block(bbox=pymupdf.Rect(l.bbox), lines=[l])
            else:
                b.lines.append(l)
                b.bbox |= l.bbox
        area = self.page_area(page, lines)
        ordered, multicol = self.order_blocks(list(blocks.values()) + tables + figures, area)
        for b in ordered:
            for l in b.lines:
                l.multicol = multicol
                l.area = area

        unmatched = self.match_outline(by_page_outline.get(pno, []), ordered)
        before: dict[int, list[str]] = defaultdict(list)
        start_anchors = []
        end_anchors = []
        for e, target in unmatched:
            if target is None and e["y"] is not None and e["y"] > page.rect.height * 0.5 and ordered:
                end_anchors.append(e["id"])
            elif target is None:
                start_anchors.append(e["id"])
            else:
                before[id(target)].append(e["id"])
        for a in start_anchors:
            stream.append(("anchor", a))
        for b in ordered:
            for a in before.get(id(b), []):
                stream.append(("anchor", a))
            if b.kind == "text":
                stream.extend(b.lines)
            else:
                stream.append(b)
        for a in end_anchors:
            stream.append(("anchor", a))
        return stream

    # ------------------------------------------------------------------ grouping
    def line_kind(self, l: Line) -> str:
        return {"bold": "body"}.get(l.style, l.style)

    def should_break(self, p: Para, b: Line) -> bool:
        a = p.lines[-1]
        if b.forced_break:
            return True
        kind_a = p.kind
        kind_b = b.style
        if kind_a == "li":
            kind_a = "body"
        if kind_b == "h" or kind_a == "h":
            if kind_a != kind_b or a.level != b.level or len(p.lines) >= 4:
                return True
            return b.page != a.page or b.bbox.y0 - a.bbox.y1 > a.h * 1.6 or b.bbox.y0 < a.bbox.y0
        if kind_a == "pre" or kind_b == "pre":
            if kind_a != kind_b:
                return True
            return b.page == a.page and b.bbox.y0 - a.bbox.y1 > a.h * 1.5
        if {kind_a, kind_b} - {"body", "bold"} and kind_a != kind_b:
            return True  # body <-> small
        if self.bullet(b) or (NUMBER_RE.match(b.text) and (p.kind == "li" or ends_terminal(a.text) or a.text.rstrip().endswith(":"))):
            return True
        tol = 0.35 if a.ocr else 0.15
        if abs(b.size - a.size) > tol * max(a.size, 1):
            return True
        if p.kind == "bold" and b.style != "bold" and (self._is_centered(a) or a.bbox.x1 < a.block_bbox.x1 - 0.15 * a.block_bbox.width):
            return True  # a bold title line followed by normal text
        if self._is_centered(a) and self._is_centered(b) and a.area is not None and \
                max(a.bbox.width, b.bbox.width) < 0.7 * a.area.width:
            return True  # centered short lines (titles, equations, verse) keep their line breaks

        jumped = b.page != a.page or b.bbox.y0 < a.bbox.y0 - 1 or b.bbox.x0 > a.bbox.x1 + 2
        if jumped:
            # continuation across a page or column: only if the sentence is unfinished
            return ends_terminal(a.text)

        gap = b.bbox.y0 - a.bbox.y1
        if gap > self.line_gap + 0.5 * a.h + (0.4 * a.h if a.ocr else 0):
            return True
        if p.kind == "li":
            first = p.lines[0]
            if b.bbox.x0 > first.bbox.x0 + 2:
                return False  # hanging indent of the list item
            return ends_terminal(a.text) and a.bbox.x1 < a.block_bbox.x1 - 0.15 * a.block_bbox.width
        left = a.bbox.x0 if len(p.lines) > 1 else min(a.bbox.x0, b.bbox.x0)
        if b.bbox.x0 - left > 0.6 * b.size and not a.text.rstrip().endswith("-"):
            if not self._is_centered(b):
                return True  # first-line indent starts a new paragraph
        bw = a.block_bbox.width
        if ends_terminal(a.text) and a.bbox.x1 < a.block_bbox.x1 - max(0.12 * bw, 2 * a.size) and not self._is_centered(a):
            return True  # short last line
        if b.block != a.block:
            return ends_terminal(a.text)
        return False

    def _is_centered(self, l: Line) -> bool:
        if l.multicol or l.area is None:
            return False
        lm = l.bbox.x0 - l.area.x0
        rm = l.area.x1 - l.bbox.x1
        return lm > 0.08 * l.area.width and abs(lm - rm) < max(6.0, 0.04 * l.area.width)

    def group(self, stream: list) -> list[Element]:
        elements: list[Element] = []
        cur: Para | None = None
        pending_anchors: list[str] = []
        pending_markers: list[str] = []   # page-break markers not yet placed
        deferred: list[Block] = []
        dropcap = ""
        bullet = None

        def emit_block(b: Block) -> None:
            nonlocal pending_anchors, pending_markers
            pre = "".join(pending_markers)
            anchors = pending_anchors
            pending_anchors, pending_markers = [], []
            h = b.html
            if anchors:
                h = re.sub(r"^<(\w+)", lambda m: f'<{m.group(1)} id="{anchors[0]}"', h, count=1)
                h = "".join(f'<span id="{a}"></span>' for a in anchors[1:]) + h
            if b.kind == "figure" and pre:
                h = h.replace(">", ">" + pre, 1)
                pre = ""
            if pre:
                elements.append(Element("raw", pre))
            elements.append(Element(b.kind, h, anchors=list(anchors)))

        def close() -> None:
            nonlocal cur, deferred, pending_markers
            if cur is not None:
                tail = cur.markers.pop(len(cur.lines), [])
                elements.append(self.render_para(cur))
                pending_markers = tail + pending_markers
                cur = None
            for d in deferred:
                emit_block(d)
            deferred = []

        for item in stream:
            if isinstance(item, tuple):
                kind, val = item
                if kind == "page":
                    marker = self.page_marker(val)
                    if cur is not None:
                        cur.markers[len(cur.lines)].append(marker)
                    else:
                        pending_markers.append(marker)
                elif kind == "anchor":
                    close()
                    pending_anchors.append(val)
                continue
            if isinstance(item, Block):
                if cur is not None and not pending_anchors and (
                        cur.kind == "h" or cur.kind in ("body", "bold", "li") and not ends_terminal(cur.lines[-1].text)):
                    deferred.append(item)
                else:
                    close()
                    emit_block(item)
                continue
            l: Line = item
            t = l.text.strip()
            if l.style == "h" and len(t) <= 2 and t.isalpha() and l.size >= 1.8 * self.body and not l.anchors:
                dropcap = t
                continue
            if len(t) == 1 and BULLET_RE.match(t + " x"):
                bullet = t
                continue
            if cur is not None and not self.should_break(cur, l) and not dropcap and not bullet:
                # markers recorded at len(lines) now sit between the previous line and this one
                cur.lines.append(l)
                continue
            close()
            kind = l.style
            if dropcap:
                l.spans[0].text = dropcap + l.spans[0].text.lstrip()
                l.text = dropcap + l.text.lstrip()
                dropcap = ""
                kind = "body" if kind == "h" else kind
            if bullet:
                l.spans[0].text = bullet + " " + l.spans[0].text.lstrip()
                l.text = bullet + " " + l.text.lstrip()
                bullet = None
            if self.bullet(l) and kind != "pre":
                kind = "li"
            elif NUMBER_RE.match(l.text) and kind in ("body", "bold") and len(l.text) > 3:
                kind = "li"
            cur = Para(kind=kind, lines=[l], level=l.level)
            cur.anchors = pending_anchors + l.anchors
            cur.prefix = pending_markers
            pending_anchors, pending_markers = [], []
        close()
        if pending_markers or pending_anchors:
            elements.append(Element("raw", "".join(pending_markers) + "".join(f'<span id="{a}"></span>' for a in pending_anchors), anchors=pending_anchors))
        return elements

    @staticmethod
    def bullet(l: Line) -> re.Match | None:
        return (OCR_BULLET_RE if l.ocr else BULLET_RE).match(l.text)

    def page_marker(self, pno: int) -> str:
        lbl = esc(self.labels[pno])
        return f'<span epub:type="pagebreak" role="doc-pagebreak" id="pg{pno}" aria-label="{lbl}"></span>'

    # ------------------------------------------------------------------ rendering
    def inline_html(self, p: Para, strip_bold: bool = False) -> str:
        runs: list[list] = []  # [text, style]  style None = raw html

        def last_text_run():
            for r in reversed(runs):
                if r[1] is not None:
                    return r
            return None

        for i, l in enumerate(p.lines):
            if i > 0:
                prev = last_text_run()
                nxt = l.text.lstrip()
                sep = " "
                if prev is not None:
                    pt = prev[0].rstrip()
                    if pt.endswith("­"):
                        prev[0], sep = pt[:-1], ""
                    elif re.search(r"\w-$", pt) and nxt[:1].islower():
                        prev[0], sep = pt[:-1], ""
                    elif pt.endswith(("-", "/", "—")):
                        prev[0], sep = pt, ""
                    elif CJK_RE.match(pt[-1:] or " ") and CJK_RE.match(nxt[:1] or " "):
                        prev[0], sep = pt, ""
                    else:
                        prev[0] = pt
                for m in p.markers.get(i, []):
                    runs.append([m, None])
                if sep:
                    runs.append([sep, ("", False, False, False, None)])
            for sp in l.spans:
                style = ("b" if sp.bold and not strip_bold else "", sp.italic, sp.mono and not l.mono, sp.sup, sp.link)
                runs.append([sp.text.replace("­", "") if i == len(p.lines) - 1 else sp.text, style])
        for m in p.markers.get(len(p.lines), []):
            runs.append([m, None])

        # merge equal styles, collapse whitespace
        merged: list[list] = []
        for text, style in runs:
            if style is not None:
                text = re.sub(r"\s+", " ", text)
                if merged and merged[-1][1] is not None and merged[-1][0].endswith(" ") and text.startswith(" "):
                    text = text[1:]
                if not text:
                    continue
                # whitespace-only runs take on the neighbor's style to keep runs merged
                if merged and merged[-1][1] is not None and (merged[-1][1] == style or not text.strip()):
                    merged[-1][0] += text
                    continue
            merged.append([text, style])
        # trim
        for r in merged:
            if r[1] is not None:
                r[0] = r[0].lstrip()
                if r[0]:
                    break
        for r in reversed(merged):
            if r[1] is not None:
                r[0] = r[0].rstrip()
                if r[0]:
                    break

        out = []
        for text, style in merged:
            if style is None:
                out.append(text)
                continue
            if not text:
                continue
            bold, italic, mono, sup, link = style
            h = esc(text)
            lead = trail = ""
            if bold or italic or mono or sup:
                m = re.match(r"^(\s*)(.*?)(\s*)$", h, re.S)
                lead, h, trail = m.group(1), m.group(2), m.group(3)
                if not h:
                    out.append(lead + trail)
                    continue
            if mono:
                h = f"<code>{h}</code>"
            if italic:
                h = f"<i>{h}</i>"
            if bold:
                h = f"<b>{h}</b>"
            if sup:
                h = f"<sup>{h}</sup>"
            if link:
                h = f'<a href="{esc(link)}">{h}</a>'
            out.append(lead + h + trail)
        return "".join(out)

    def render_para(self, p: Para) -> Element:
        text = " ".join(l.text.strip() for l in p.lines)
        kind = p.kind
        level = p.level
        if kind == "bold":
            words = text.split()
            if len(p.lines) <= 2 and len(words) <= 15 and not text.rstrip().endswith((".", ",", ";")):
                kind, level = "h", min(len(self.heading_sizes) + 1, 4)
            else:
                kind = "body"
        anchors = list(p.anchors)
        if kind == "h" and not anchors:
            self.anchor_seq += 1
            anchors = [f"h{self.anchor_seq}"]
        prefix = "".join(p.prefix)
        extra = "".join(f'<span id="{a}"></span>' for a in anchors[1:])
        idattr = f' id="{anchors[0]}"' if anchors else ""

        if kind == "pre":
            widths = [l.bbox.width / max(len(l.text), 1) for l in p.lines if l.text.strip()]
            cw = statistics.median(widths) if widths else 5
            x0 = min(l.bbox.x0 for l in p.lines)
            rows = []
            for i, l in enumerate(p.lines):
                for m in p.markers.get(i, []):
                    rows.append(m)
                ind = " " * max(0, round((l.bbox.x0 - x0) / max(cw, 1)))
                rows.append(esc(ind + l.text.rstrip()) + ("\n" if i < len(p.lines) - 1 else ""))
            rows.extend(p.markers.get(len(p.lines), []))
            return Element("pre", f"{prefix}<pre{idattr}>{extra}{''.join(rows)}</pre>", anchors=anchors, text=text)

        if kind == "h":
            inner = self.inline_html(p, strip_bold=True)
            level = max(1, min(level or 1, 6))
            return Element("h", f"{prefix}<h{level}{idattr}>{extra}{inner}</h{level}>", anchors=anchors,
                           level=level, text=re.sub(r"\s+", " ", text).strip())

        if kind == "li":
            first = p.lines[0]
            m = NUMBER_RE.match(first.text)
            b = self.bullet(first)
            if b:
                lt, value, cut = "ul", None, b.end()
            elif m and m.group(1).isdigit():
                lt, value, cut = "ol", int(m.group(1)), m.end()
            else:
                lt, value, cut = "plain", None, 0
            for sp in first.spans:  # drop the marker characters, which may span several spans
                if cut <= 0:
                    break
                take = min(cut, len(sp.text))
                sp.text, cut = sp.text[take:], cut - take
            inner = self.inline_html(p)
            val = f' value="{value}"' if value is not None else ""
            return Element("li", f"<li{idattr}{val}>{prefix}{extra}{inner}</li>", anchors=anchors,
                           text=text, list_type=lt)

        inner = self.inline_html(p)
        if not inner.strip() and not prefix and not anchors:
            return Element("raw", "")
        cls = []
        if kind == "small":
            cls.append("small")
        first = p.lines[0]
        if not first.multicol and first.area is not None and kind != "small":
            if all(self._is_centered(l) for l in p.lines):
                cls.append("center")
            else:
                ar = first.area
                lm = min(l.bbox.x0 for l in p.lines) - ar.x0
                rm = ar.x1 - max(l.bbox.x1 for l in p.lines)
                if lm > 0.3 * ar.width and rm < 0.03 * ar.width and len(p.lines) <= 3:
                    cls.append("right")
                elif lm > 0.07 * ar.width and rm > 0.04 * ar.width and len(p.lines) >= 2:
                    cls.append("quote")
        c = f' class="{" ".join(cls)}"' if cls else ""
        if "quote" in cls:
            return Element("p", f'{prefix}<blockquote><p{idattr}>{extra}{inner}</p></blockquote>', anchors=anchors, text=text)
        return Element("p", f"{prefix}<p{idattr}{c}>{extra}{inner}</p>", anchors=anchors, text=text)

    # ------------------------------------------------------------------ assembly
    def _cover(self, book: EpubBuilder, page: pymupdf.Page) -> None:
        zoom = min(1600 / max(page.rect.height, 1), 1200 / max(page.rect.width, 1))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
        book.add(Item("cover-image", "images/cover.jpg", "image/jpeg", pix.tobytes("jpeg", jpg_quality=88), "cover-image"))
        book.cover_image_id = "cover-image"
        body = (f'<section epub:type="cover" class="cover"><img src="../images/cover.jpg" '
                f'alt="{esc(book.title)}"/></section>')
        doc = xhtml("Cover", body, book.language)
        book.add(Item("cover", "text/cover.xhtml", "application/xhtml+xml", doc.encode()), spine=True)
        book.landmarks.append(("cover", "Cover", "text/cover.xhtml"))

    def _is_cover_page(self, page: pymupdf.Page) -> bool:
        """A first page that is mostly artwork: it becomes the cover instead of content."""
        if len(page.get_text("text").strip()) > 600:
            return False
        area = page.rect.get_area()
        img = sum(i["rect"].get_area() for i in self.image_infos[0]) / area
        try:
            fill = max(((d["rect"] & page.rect).get_area() / area for d in page.get_drawings()
                        if d.get("fill") is not None), default=0)
        except Exception:
            fill = 0
        return self.scanned[0] or img >= 0.15 or fill >= 0.5

    def _build_reflow(self, title: str | None, author: str) -> tuple[EpubBuilder, int, str]:
        doc = self.doc
        n = doc.page_count
        self.labels = [page_label(p) for p in doc]
        self.pass1()

        top = [e for e in self.toc if e[0] == 1]
        self.split_level = 2 if len(top) <= 1 and any(e[0] == 2 for e in self.toc) else 1
        by_page_outline = self.outline_entries()

        skip_first = self.opts.cover and n > 1 and self._is_cover_page(doc[0])
        stream: list = []
        for pno in range(n):
            self.progress(0.55 + 0.35 * pno / n, f"Laying out page {pno + 1}/{n}")
            if pno == 0 and skip_first:
                continue
            stream.extend(self.page_stream(pno, by_page_outline))

        self.progress(0.91, "Building chapters")
        elements = self.group(stream)
        elements = [e for e in elements if e.html]

        title = title or self.title_from_name()
        book = EpubBuilder(title, author, self.opts.language)
        css = (Path(__file__).parent / "static" / "book.css").read_bytes()
        book.add(Item("css", "styles/book.css", "text/css", css))
        if self.opts.cover:
            self._cover(book, doc[0])

        # chapter split points
        if self.outline:
            split_ids = {e["id"] for e in self.outline if e["level"] <= self.split_level}
            starts = lambda el: bool(split_ids.intersection(el.anchors))
        else:
            n_h1 = sum(1 for el in elements if el.kind == "h" and el.level == 1 and self.good_title(el.text))
            split_at = 2 if n_h1 < 3 and any(el.kind == "h" and el.level == 2 for el in elements) else 1
            starts = lambda el: el.kind == "h" and el.level <= split_at and self.good_title(el.text)
        chapters: list[list[Element]] = [[]]
        for el in elements:
            if starts(el) and any(x.kind not in ("raw",) for x in chapters[-1]):
                chapters.append([])
            chapters[-1].append(el)
        # split very long chapters into parts to keep reading systems fast
        final: list[list[Element]] = []
        for ch in chapters:
            part: list[Element] = []
            size = 0
            for el in ch:
                if size > 180_000 and el.kind in ("p", "h", "figure", "table", "pre"):
                    final.append(part)
                    part, size = [], 0
                part.append(el)
                size += len(el.html)
            if part:
                final.append(part)
        final = [c for c in final if c]

        id_file: dict[str, str] = {}
        chapter_docs = []
        for ci, ch in enumerate(final, 1):
            href = f"text/ch{ci:04d}.xhtml"
            body = self.wrap_lists(ch)
            for m in re.finditer(r'\bid="([^"]+)"', body):
                id_file.setdefault(m.group(1), href)
            h = next((e for e in ch if e.kind == "h"), None)
            chapter_docs.append((href, h.text if h else f"Part {ci}", body))
        skipped = "text/cover.xhtml" if self.opts.cover else (chapter_docs[0][0] if chapter_docs else "")

        def resolve(m: re.Match) -> str:
            pno = int(m.group(1))
            f = id_file.get(f"pg{pno}")
            if f is None:
                return 'href="' + (skipped or "#") + '"' if pno == 0 and skip_first else "data-broken-link=\"\""
            return f'href="../{f}#pg{pno}"'

        for ci, (href, ctitle, body) in enumerate(chapter_docs, 1):
            body = re.sub(r'href="__PG_(\d+)__"', resolve, body)
            body = body.replace("data-broken-link=\"\"", "")
            content = xhtml(ctitle, body, self.opts.language)
            book.add(Item(f"ch{ci:04d}", href, "application/xhtml+xml", content.encode()), spine=True)
        if not chapter_docs:
            content = xhtml(title, "<p></p>", self.opts.language)
            book.add(Item("ch0001", "text/ch0001.xhtml", "application/xhtml+xml", content.encode()), spine=True)
            chapter_docs.append(("text/ch0001.xhtml", title, ""))

        # navigation
        flat = []
        if self.outline:
            for e in self.outline:
                f = id_file.get(e["id"]) or id_file.get(f"pg{e['page']}")
                if f is None:
                    f = skipped if e["page"] == 0 and skip_first else None
                    href = f
                else:
                    href = f"{f}#{e['id']}" if e["id"] in id_file else f"{f}#pg{e['page']}"
                if href:
                    flat.append((e["level"], e["title"], href))
        else:
            heads = [el for el in elements if el.kind == "h" and el.anchors and self.good_title(el.text)]
            levels = sorted({el.level for el in heads})[:3]
            limit = max(40, n // 2)
            while len(levels) > 1 and sum(1 for el in heads if el.level in levels) > limit:
                levels.pop()
            for el in heads:
                f = id_file.get(el.anchors[0])
                if f and el.level in levels:
                    flat.append((el.level, el.text, f"{f}#{el.anchors[0]}"))
        book.toc = self.nest(flat) or [NavPoint(title, chapter_docs[0][0])]
        for pno in range(n):
            f = id_file.get(f"pg{pno}")
            if f:
                book.page_list.append((self.labels[pno], f"{f}#pg{pno}"))
        book.landmarks.append(("toc", "Table of Contents", "nav.xhtml#toc"))
        book.landmarks.append(("bodymatter", "Start of Content", chapter_docs[0][0]))
        for it in self.images.values():
            book.add(it)
        return book, len(chapter_docs), title

    def title_from_name(self) -> str:
        stem = Path(self.opts.source_name or self.pdf_path).stem
        stem = re.sub(r"^_?(oceanofpdf\.com|z-lib\.org|libgen\.\w+)_?", "", stem, flags=re.I)
        stem = re.sub(r"[_]+", " ", stem)
        stem = re.sub(r"\s*\((z-lib|libgen)[^)]*\)", "", stem, flags=re.I)
        return re.sub(r"\s+", " ", stem).strip(" -") or "Untitled"

    @staticmethod
    def good_title(t: str) -> bool:
        t = t.strip()
        letters = sum(c.isalpha() for c in t)
        return 2 <= len(t) <= 100 and letters >= 2 and letters / len(t) >= 0.5

    @staticmethod
    def nest(flat: list[tuple[int, str, str]]) -> list[NavPoint]:
        root: list[NavPoint] = []
        stack: list[tuple[int, list[NavPoint]]] = [(0, root)]
        for lvl, title, href in flat:
            node = NavPoint(title, href)
            while stack[-1][0] >= lvl:
                stack.pop()
            stack[-1][1].append(node)
            stack.append((lvl, node.children))
        return root

    @staticmethod
    def wrap_lists(elements: list[Element]) -> str:
        out = []
        open_type = None
        for el in elements:
            if el.kind == "li":
                if open_type != el.list_type:
                    if open_type:
                        out.append(f"</{'ul' if open_type != 'ol' else 'ol'}>")
                    tag = "ol" if el.list_type == "ol" else 'ul class="plain"' if el.list_type == "plain" else "ul"
                    out.append(f"<{tag}>")
                    open_type = el.list_type
                out.append(el.html)
                continue
            if el.kind == "raw" and open_type and not el.anchors:
                # page markers between list items go inside the next item
                out.append(f"<li class=\"marker\">{el.html}</li>")
                continue
            if open_type:
                out.append(f"</{'ol' if open_type == 'ol' else 'ul'}>")
                open_type = None
            out.append(el.html)
        if open_type:
            out.append(f"</{'ol' if open_type == 'ol' else 'ul'}>")
        return "\n".join(out)

    # ------------------------------------------------------------------ fixed layout
    def _build_fixed(self, title: str | None, author: str) -> tuple[EpubBuilder, int, str]:
        doc = self.doc
        n = doc.page_count
        title = title or self.title_from_name()
        book = EpubBuilder(title, author, self.opts.language, fixed_layout=True)
        css = (Path(__file__).parent / "static" / "book.css").read_bytes()
        book.add(Item("css", "styles/book.css", "text/css", css))
        labels = [page_label(p) for p in doc]
        hrefs = []
        for page in doc:
            self.progress(0.05 + 0.9 * page.number / n, f"Rendering page {page.number + 1}/{n}")
            zoom = min(2000 / max(page.rect.height, 1), 1600 / max(page.rect.width, 1))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
            data = pix.tobytes("jpeg", jpg_quality=85)
            props = None
            if page.number == 0 and self.opts.cover:
                props = "cover-image"
                book.cover_image_id = f"pimg{page.number + 1}"
            img = Item(f"pimg{page.number + 1}", f"images/page{page.number + 1:04d}.jpg", "image/jpeg", data, props)
            book.add(img)
            w, h = pix.width, pix.height
            text = esc(" ".join(page.get_text("text").split()))[:20000]
            body = (f'<div class="fxl" id="pg{page.number}"><img src="../{img.href}" alt="Page {esc(labels[page.number])}" '
                    f'style="width:{w}px;height:{h}px"/></div>'
                    + (f'<p class="fxl-text">{text}</p>' if text else ""))
            head = f'\n<meta name="viewport" content="width={w}, height={h}"/>\n<style>html,body{{margin:0;padding:0;width:{w}px;height:{h}px;overflow:hidden}} .fxl-text{{position:absolute;left:-99999px}}</style>'
            content = xhtml(f"Page {labels[page.number]}", body, self.opts.language, head_extra=head)
            href = f"text/page{page.number + 1:04d}.xhtml"
            book.add(Item(f"p{page.number + 1}", href, "application/xhtml+xml", content.encode()), spine=True)
            hrefs.append(href)
            book.page_list.append((labels[page.number], f"{href}#pg{page.number}"))
        flat = [(max(1, e[0]), e[1], hrefs[e[2] - 1]) for e in self.toc if e[1] and 1 <= e[2] <= n]
        book.toc = self.nest(flat) or [NavPoint(title, hrefs[0])]
        book.landmarks.append(("bodymatter", "Start of Content", hrefs[0]))
        return book, n, title


def convert(pdf_path: str, epub_path: str, opts: Options | None = None, progress: ProgressFn | None = None) -> Result:
    return Converter(pdf_path, epub_path, opts or Options(), progress).run()
