"""Minimal, standards-compliant EPUB 3 writer with an EPUB 2 NCX fallback."""
from __future__ import annotations

import html
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone


def esc(s: str) -> str:
    return html.escape(s, quote=True)


@dataclass
class NavPoint:
    title: str
    href: str
    children: list["NavPoint"] = field(default_factory=list)


@dataclass
class Item:
    id: str
    href: str  # relative to OEBPS/
    media_type: str
    data: bytes
    properties: str | None = None


def xhtml(title: str, body: str, lang: str, css_href: str | None = "../styles/book.css",
          head_extra: str = "", body_attrs: str = "") -> str:
    css = f'<link rel="stylesheet" type="text/css" href="{css_href}"/>' if css_href else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{esc(lang)}" lang="{esc(lang)}">\n<head>\n<meta charset="utf-8"/>\n'
        f"<title>{esc(title)}</title>\n{css}{head_extra}\n</head>\n"
        f"<body{body_attrs}>\n{body}\n</body>\n</html>\n"
    )


class EpubBuilder:
    def __init__(self, title: str, author: str, language: str, fixed_layout: bool = False):
        self.title = title
        self.author = author
        self.language = language
        self.fixed_layout = fixed_layout
        self.identifier = f"urn:uuid:{uuid.uuid4()}"
        self.items: list[Item] = []
        self.spine: list[tuple[str, bool]] = []  # (item id, linear)
        self.toc: list[NavPoint] = []
        self.page_list: list[tuple[str, str]] = []  # (label, href)
        self.landmarks: list[tuple[str, str, str]] = []  # (epub:type, title, href)
        self.cover_image_id: str | None = None

    def add(self, item: Item, spine: bool = False, linear: bool = True) -> Item:
        self.items.append(item)
        if spine:
            self.spine.append((item.id, linear))
        return item

    # ------------------------------------------------------------------ nav
    def _nav_ol(self, points: list[NavPoint], depth: int = 0) -> str:
        pad = "  " * depth
        out = [f"{pad}<ol>"]
        for p in points:
            child = ("\n" + self._nav_ol(p.children, depth + 1) + f"\n{pad}  ") if p.children else ""
            out.append(f'{pad}  <li><a href="{esc(p.href)}">{esc(p.title)}</a>{child}</li>')
        out.append(f"{pad}</ol>")
        return "\n".join(out)

    def _nav_doc(self) -> str:
        toc = self.toc or [NavPoint(self.title, self.spine_hrefs()[0])]
        body = [f'<nav epub:type="toc" id="toc" role="doc-toc">\n<h1>Contents</h1>\n{self._nav_ol(toc)}\n</nav>']
        if self.page_list:
            lis = "\n".join(f'  <li><a href="{esc(h)}">{esc(l)}</a></li>' for l, h in self.page_list)
            body.append(f'<nav epub:type="page-list" id="page-list" hidden="hidden">\n<h2>Pages</h2>\n<ol>\n{lis}\n</ol>\n</nav>')
        if self.landmarks:
            lis = "\n".join(f'  <li><a epub:type="{t}" href="{esc(h)}">{esc(n)}</a></li>' for t, n, h in self.landmarks)
            body.append(f'<nav epub:type="landmarks" id="landmarks" hidden="hidden">\n<h2>Guide</h2>\n<ol>\n{lis}\n</ol>\n</nav>')
        return xhtml("Contents", "\n".join(body), self.language, css_href="styles/book.css")

    def _ncx(self) -> str:
        counter = [0]

        def points(ps: list[NavPoint], depth: int) -> str:
            out = []
            for p in ps:
                counter[0] += 1
                n = counter[0]
                pad = "  " * depth
                out.append(
                    f'{pad}<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{esc(p.title)}</text></navLabel>'
                    f'<content src="{esc(p.href)}"/>'
                )
                if p.children:
                    out.append(points(p.children, depth + 1))
                out.append(f"{pad}</navPoint>")
            return "\n".join(out)

        toc = self.toc or [NavPoint(self.title, self.spine_hrefs()[0])]
        nav_map = points(toc, 1)
        depth = self._depth(toc)
        page_list = ""
        if self.page_list:
            targets = "\n".join(
                f'  <pageTarget id="pt{i}" type="normal" value="{i}"><navLabel><text>{esc(l)}</text></navLabel>'
                f'<content src="{esc(h)}"/></pageTarget>'
                for i, (l, h) in enumerate(self.page_list, 1)
            )
            page_list = f"<pageList><navLabel><text>Pages</text></navLabel>\n{targets}\n</pageList>"
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            f'<head><meta name="dtb:uid" content="{esc(self.identifier)}"/>'
            f'<meta name="dtb:depth" content="{depth}"/>'
            '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
            f"<docTitle><text>{esc(self.title)}</text></docTitle>\n"
            f"<docAuthor><text>{esc(self.author)}</text></docAuthor>\n"
            f"<navMap>\n{nav_map}\n</navMap>\n{page_list}\n</ncx>\n"
        )

    def _depth(self, ps: list[NavPoint]) -> int:
        return 1 + max((self._depth(p.children) for p in ps if p.children), default=0)

    def spine_hrefs(self) -> list[str]:
        by_id = {i.id: i for i in self.items}
        return [by_id[i].href for i, _ in self.spine]

    # ------------------------------------------------------------------ opf
    def _opf(self) -> str:
        modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = [
            f'<dc:identifier id="bookid">{esc(self.identifier)}</dc:identifier>',
            f"<dc:title>{esc(self.title)}</dc:title>",
            f'<dc:creator id="creator">{esc(self.author)}</dc:creator>',
            f"<dc:language>{esc(self.language)}</dc:language>",
            f'<meta property="dcterms:modified">{modified}</meta>',
        ]
        if self.cover_image_id:
            meta.append(f'<meta name="cover" content="{self.cover_image_id}"/>')
        if self.fixed_layout:
            meta.append('<meta property="rendition:layout">pre-paginated</meta>')
            meta.append('<meta property="rendition:orientation">auto</meta>')
            meta.append('<meta property="rendition:spread">auto</meta>')
        manifest = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        ]
        for it in self.items:
            props = f' properties="{it.properties}"' if it.properties else ""
            manifest.append(f'<item id="{it.id}" href="{esc(it.href)}" media-type="{it.media_type}"{props}/>')
        nonlinear = ' linear="no"'
        spine = [f'<itemref idref="{i}"{"" if lin else nonlinear}/>' for i, lin in self.spine]
        nl = "\n    "
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
            f'xml:lang="{esc(self.language)}" prefix="rendition: http://www.idpf.org/vocab/rendition/#">\n'
            f'  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n    {nl.join(meta)}\n  </metadata>\n'
            f"  <manifest>\n    {nl.join(manifest)}\n  </manifest>\n"
            f'  <spine toc="ncx">\n    {nl.join(spine)}\n  </spine>\n'
            "</package>\n"
        )

    def write(self, path: str) -> None:
        container = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
            "</container>\n"
        )
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/content.opf", self._opf(), compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/nav.xhtml", self._nav_doc(), compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/toc.ncx", self._ncx(), compress_type=zipfile.ZIP_DEFLATED)
            for it in self.items:
                packed = it.media_type in ("image/jpeg", "image/png")
                z.writestr(f"OEBPS/{it.href}", it.data,
                           compress_type=zipfile.ZIP_STORED if packed else zipfile.ZIP_DEFLATED)
