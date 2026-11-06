"""Generate sample PDFs exercising the converter: python tests/make_samples.py OUTDIR"""
import io
import math
import sys
from pathlib import Path

import pymupdf

LOREM = ("The quick brown fox jumps over the lazy dog while the committee deliberates on matters of "
         "considerable importance. Every paragraph in this sample is long enough to wrap across several "
         "lines so that paragraph detection, hyphenation and justification can be exercised properly. ")


def chart_png() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 260), 0)
    pix.clear_with(255)
    for i in range(400):
        for j in range(260):
            if (i // 40 + j // 40) % 2 == 0:
                pix.set_pixel(i, j, (40 + i // 4, 90 + j // 4, 160))
    return pix.tobytes("png")


HTML = f"""
<h1>Chapter One: Beginnings</h1>
<p>{LOREM * 3}</p>
<p>{LOREM * 2} This sentence continues with a hyperlink to <a href="https://example.com">example.com</a>
and some <b>bold words</b> and <i>italic words</i> and <code>inline_code()</code> in the middle.</p>
<h2>1.1 A list of things</h2>
<ul><li>First bullet item that is quite long and should wrap onto a second line in the rendered PDF output for testing.</li>
<li>Second bullet item.</li><li>Third bullet item.</li></ul>
<ol><li>Numbered one.</li><li>Numbered two with more words in it.</li><li>Numbered three.</li></ol>
<h2>1.2 A table</h2>
<table border="1" style="border-collapse: collapse">
<tr><th>Name</th><th>Quantity</th><th>Price</th></tr>
<tr><td>Apples</td><td>12</td><td>$3.50</td></tr>
<tr><td>Oranges</td><td>7</td><td>$4.10</td></tr>
<tr><td>Pears</td><td>3</td><td>$1.20</td></tr>
</table>
<p>{LOREM * 2}</p>
<h2>1.3 A picture</h2>
<img src="chart.png" width="300"/>
<p>Figure 1. A checkered sample image.</p>
<p>{LOREM * 4}</p>
<h1>Chapter Two: Code and More</h1>
<p>{LOREM * 2}</p>
<pre>def hello(name):
    print(f"Hello, {{name}}")
    return 42</pre>
<p>{LOREM * 6}</p>
<h2>2.1 Closing thoughts</h2>
<p>{LOREM * 5}</p>
"""


def make_book(out: Path) -> None:
    arch = pymupdf.Archive()
    arch.add(chart_png(), "chart.png")
    story = pymupdf.Story(HTML, archive=arch, user_css="body{font-family:serif;font-size:11pt} h1{font-size:22pt} h2{font-size:15pt} pre{font-family:monospace;font-size:9.5pt}")
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    mediabox = pymupdf.paper_rect("letter")
    where = mediabox + (72, 72, -72, -72)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    body = pymupdf.open("pdf", buf.getvalue())

    doc = pymupdf.open()
    # cover page: big image + title
    cover = doc.new_page(width=612, height=792)
    cover.draw_rect(cover.rect, fill=(0.15, 0.25, 0.4))
    cover.insert_image(pymupdf.Rect(106, 150, 506, 410), stream=chart_png())
    cover.insert_text((106, 500), "The Sample Book", fontsize=36, color=(1, 1, 1))
    cover.insert_text((106, 540), "by Test Author", fontsize=18, color=(1, 1, 1))
    doc.insert_pdf(body)

    # two-column page with a vector chart
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 90), "Chapter Three: Columns", fontsize=22)
    col_text = (LOREM * 5)
    p.insert_textbox(pymupdf.Rect(72, 110, 296, 700), col_text + "The left column ends mid", fontsize=10.5, fontname="tiro")
    p.insert_textbox(pymupdf.Rect(316, 110, 540, 330), "sentence and continues here in the right column. " + LOREM * 2, fontsize=10.5, fontname="tiro")
    # vector bar chart in the right column
    shape = p.new_shape()
    for i, h in enumerate([60, 120, 90, 150, 40]):
        shape.draw_rect(pymupdf.Rect(340 + i * 38, 560 - h, 366 + i * 38, 560))
    shape.finish(fill=(0.2, 0.5, 0.7), color=(0, 0, 0))
    shape.draw_circle((480, 400), 20)
    shape.finish(color=(0.8, 0.2, 0.2), width=2)
    shape.draw_line((330, 560), (530, 560))
    shape.finish(color=(0, 0, 0))
    shape.commit()
    p.insert_text((330, 590), "Figure 2. A vector bar chart.", fontsize=9)

    # running headers / footers on all non-cover pages
    for page in doc:
        if page.number == 0:
            continue
        page.insert_text((72, 40), "The Sample Book", fontsize=8, color=(0.4, 0.4, 0.4))
        page.insert_text((300, 760), str(page.number + 1), fontsize=9)

    # outline: find headings
    toc = []
    for page in doc:
        for title, lvl in [("Chapter One: Beginnings", 1), ("1.1 A list of things", 2), ("1.2 A table", 2),
                           ("1.3 A picture", 2), ("Chapter Two: Code and More", 1), ("2.1 Closing thoughts", 2),
                           ("Chapter Three: Columns", 1)]:
            hits = page.search_for(title)
            if hits:
                toc.append([lvl, title, page.number + 1, {"kind": pymupdf.LINK_GOTO, "page": page.number, "to": pymupdf.Point(72, hits[0].y0)}])
    toc.sort(key=lambda e: (e[2], e[3]["to"].y))
    doc.set_toc(toc)
    # internal link on the last page back to chapter one
    last = doc[-1]
    last.insert_text((330, 640), "Back to chapter one.", fontsize=10.5, fontname="tiro", color=(0, 0, 1))
    last.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(330, 630, 440, 643), "page": 1, "to": pymupdf.Point(72, 72)})
    doc.set_metadata({"title": "The Sample Book", "author": "Test Author"})
    doc.save(out)


def make_scanned(src: Path, out: Path) -> None:
    doc = pymupdf.open(src)
    scan = pymupdf.open()
    for page in doc:
        if page.number == 0:
            continue
        pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
        p = scan.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=pix.tobytes("png"))
    scan.save(out)


if __name__ == "__main__":
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    outdir.mkdir(parents=True, exist_ok=True)
    make_book(outdir / "book.pdf")
    make_scanned(outdir / "book.pdf", outdir / "scanned.pdf")
    print("ok")
