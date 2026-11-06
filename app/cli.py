"""Command line interface: python -m app.cli input.pdf [-o output.epub] [options]."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from .converter import ConversionError, Options, convert


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pdf2epub", description="Convert a PDF into a reflowable (or fixed-layout) EPUB 3.")
    p.add_argument("input", help="PDF file")
    p.add_argument("-o", "--output", help="EPUB file (default: next to the input)")
    p.add_argument("--title")
    p.add_argument("--author")
    p.add_argument("--language", default="en", help="Book language, BCP 47 (default: en)")
    p.add_argument("--ocr", choices=["auto", "force", "off"], default="auto",
                   help="auto: OCR pages without a usable text layer; force: OCR every page")
    p.add_argument("--ocr-lang", default="eng", help="Tesseract languages, e.g. eng+deu (default: eng)")
    p.add_argument("--layout", choices=["reflow", "fixed"], default="reflow",
                   help="reflow: real text that adapts to the screen; fixed: one image per page")
    p.add_argument("--tables", choices=["html", "image"], default="html")
    p.add_argument("--no-cover", action="store_true", help="Do not use the first page as the cover")
    p.add_argument("--keep-headers", action="store_true", help="Keep running headers, footers and page numbers")
    p.add_argument("--password", help="Password for encrypted PDFs")
    p.add_argument("--source-name", help=argparse.SUPPRESS)
    p.add_argument("--json-progress", action="store_true", help="Emit progress as JSON lines on stdout")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(message)s")
    out = a.output or str(Path(a.input).with_suffix(".epub"))
    opts = Options(title=a.title or None, author=a.author or None, language=a.language, ocr=a.ocr,
                   ocr_languages=a.ocr_lang, layout=a.layout, tables=a.tables, cover=not a.no_cover,
                   keep_headers=a.keep_headers, password=a.password,
                   source_name=a.source_name)

    def emit(obj: dict) -> None:
        print(json.dumps(obj), flush=True)

    last = [-1]

    def progress(frac: float, msg: str) -> None:
        if a.json_progress:
            emit({"progress": round(frac, 4), "message": msg})
        elif int(frac * 100) != last[0]:
            last[0] = int(frac * 100)
            print(f"\r[{last[0]:3d}%] {msg:<60}", end="", file=sys.stderr, flush=True)

    try:
        result = convert(a.input, out, opts, progress)
    except ConversionError as e:
        if a.json_progress:
            emit({"error": str(e)})
        else:
            print(f"\nError: {e}", file=sys.stderr)
        return 2
    if a.json_progress:
        emit({"result": asdict(result), "output": out})
    else:
        print(file=sys.stderr)
        print(f"Wrote {out}")
        print(f"  {result.pages} pages -> {result.chapters} chapters, {result.images} images, "
              f"{result.tables} tables, {result.ocr_pages} OCR'd pages")
        for w in result.warnings:
            print(f"  warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
