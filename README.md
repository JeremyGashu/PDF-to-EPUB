# PDF → EPUB

A local, private PDF-to-EPUB converter with a web UI. Runs entirely in Docker on your machine.

## Start

```bash
./start.sh
```

That builds the image (first run only, a few minutes), starts the app and opens **http://localhost:8080**.
Drop one or more PDFs, wait for the progress bar, click **Download**.

Stop it with `docker compose down`. Use another port with `PORT=9000 ./start.sh`.

## What it does

| Feature | How |
|---|---|
| **Chapters & table of contents** | Built from the PDF's bookmarks (outline). Without bookmarks, from detected headings. Nested TOC in both EPUB 3 nav and EPUB 2 NCX. |
| **Headings** | Detected from font size/weight relative to the body text and mapped to `h1`–`h4`. |
| **Paragraphs** | Re-flowed: lines are joined, hyphenated words rejoined, and paragraphs continued across page and column breaks. |
| **Formatting** | Bold, italic, inline code, superscripts (footnote markers), centered/right-aligned text, block quotes, code blocks with indentation. |
| **Lists** | Bulleted and numbered lists, including PDF symbol-font bullets. |
| **Tables** | Ruled tables (cell text rebuilt glyph-by-glyph) and borderless tables (column alignment) become real HTML tables, with captions. Option to keep them as images instead. |
| **Images & figures** | Raster images and vector charts/diagrams are rendered at their native resolution, with their captions (`Figure 3…`). Repeated logos/decorations are dropped. |
| **Multi-column layouts** | Columns are read in order; full-width elements split the column bands. |
| **Running headers, footers, page numbers** | Removed from the text (option to keep). |
| **Page numbers** | Kept as invisible EPUB page markers + a page list, so readers can show "page 42" matching the print book (uses PDF page labels). |
| **Links** | Web links and internal links (e.g. "see page 12") keep working. |
| **OCR** | Pages without a usable text layer (scans, broken fonts) are OCR'd with Tesseract via OCRmyPDF (auto-rotate, deskew). Pictures on scanned pages are recovered as images. |
| **Cover** | First page becomes the cover image (and is dropped from the text if it's artwork). |
| **Fixed layout** | Optional "Page images" mode: every page as an image, for comics, magazines and heavily designed PDFs. |
| **Encrypted PDFs** | Supply the password in Options. |

OCR languages bundled: English, German, French, Spanish, Italian, Portuguese, Dutch. Add more
(Tesseract codes) and rebuild:

```bash
OCR_LANGS="eng jpn chi-sim rus" docker compose build && ./start.sh
```

## Command line

Inside the container (files in the current folder):

```bash
docker run --rm -v "$PWD:/work" -w /work pdf2epub:latest python -m app.cli book.pdf -o book.epub
```

Options: `--ocr auto|force|off`, `--ocr-lang eng+deu`, `--layout reflow|fixed`, `--tables html|image`,
`--title`, `--author`, `--language`, `--no-cover`, `--keep-headers`, `--password`.

## Settings (environment)

| Variable | Default | |
|---|---|---|
| `PORT` | 8080 | Port on localhost |
| `MAX_CONCURRENT_JOBS` | 2 | Conversions in parallel |
| `JOB_TTL_HOURS` | 24 | Finished jobs are deleted after this |
| `MAX_UPLOAD_MB` | 1024 | Upload limit |

## Layout

```
app/converter.py   conversion engine (analysis, layout, grouping, chapters)
app/epub.py        EPUB 3 writer (nav, page-list, landmarks, NCX)
app/ocr.py         OCRmyPDF/Tesseract integration
app/server.py      FastAPI web app; each job runs in its own process
app/cli.py         command line
app/static/        web UI and the book stylesheet
tests/             sample PDF generator and a structural EPUB validator
```

## Tests

```bash
docker run --rm -v "$PWD/tests:/tests" -v /tmp/out:/out pdf2epub:latest sh -c \
  'python /tests/make_samples.py /out && python -m app.cli /out/book.pdf -o /out/book.epub \
   && python -m app.cli /out/scanned.pdf -o /out/scanned.epub && python /tests/validate_epub.py /out/*.epub'
```

## Limits

PDF is a print format. The converter rebuilds structure from positions and fonts, so results are excellent for
books, reports and papers. Very designed layouts (magazines, comics, posters) come out better in "Page images"
mode. Math formulas are reflowed as text rather than typeset, and complex tables with merged cells may need
the "Images" table mode.
