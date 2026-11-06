"""OCR support through OCRmyPDF + Tesseract."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from typing import Callable


def available() -> bool:
    return bool(shutil.which("ocrmypdf") and shutil.which("tesseract"))


@lru_cache(maxsize=1)
def languages() -> tuple[str, ...]:
    if not shutil.which("tesseract"):
        return ()
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
    langs = [l.strip() for l in out.splitlines()[1:] if l.strip() and l.strip() != "osd"]
    return tuple(sorted(langs))


def normalize_languages(spec: str) -> tuple[str, list[str]]:
    """Return a Tesseract language string ("eng+deu") and the requested codes that are not installed."""
    wanted = [l for l in re.split(r"[+,\s]+", spec or "") if l]
    installed = set(languages())
    ok = [l for l in wanted if re.fullmatch(r"[A-Za-z_]+", l) and l in installed]
    missing = [l for l in wanted if l not in ok]
    if not ok:
        ok = ["eng"] if "eng" in installed or not installed else [sorted(installed)[0]]
    return "+".join(dict.fromkeys(ok)), missing


def _page_ranges(pages: list[int]) -> str:
    """0-based page list -> OCRmyPDF 1-based range string ("1,3-5")."""
    nums = sorted(set(p + 1 for p in pages))
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ",".join(out)


def run(src: str, dst: str, pages: list[int], langs: str,
        progress: Callable[[int, int], None] | None = None) -> None:
    """OCR the given 0-based pages of src into dst. Other pages pass through untouched."""
    cmd = [
        "ocrmypdf",
        "--force-ocr",            # rasterize + OCR only the selected pages (their text layer is unusable)
        "--pages", _page_ranges(pages),
        "--rotate-pages",
        "--deskew",
        "--oversample", "300",
        "--output-type", "pdf",
        "--optimize", "0",
        "--invalidate-digital-signatures",
        "--jobs", str(max(1, os.cpu_count() or 1)),
        "-l", langs,
        src, dst,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    seen: set[str] = set()
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line.rstrip()])[-30:]
        m = re.match(r"^\s*(\d+)\s", line)
        if m and progress:
            seen.add(m.group(1))
            progress(len(seen), len(pages))
    code = proc.wait()
    # exit 4 = output failed strict validation but is still readable, which is all we need
    if code not in (0, 4) or not os.path.exists(dst):
        raise RuntimeError(f"OCRmyPDF failed (exit {code}): " + " | ".join(tail[-8:]))
