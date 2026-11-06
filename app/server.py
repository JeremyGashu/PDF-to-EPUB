"""Web app: upload PDFs, convert them in isolated worker processes, download EPUBs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from . import ocr

DATA = Path(os.environ.get("DATA_DIR", "/data")) / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
JOB_TTL = float(os.environ.get("JOB_TTL_HOURS", "24")) * 3600
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "1024")) * 1024 * 1024
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="PDF to EPUB")
slots = threading.BoundedSemaphore(MAX_CONCURRENT)
lock = threading.Lock()


@dataclass
class Job:
    id: str
    name: str
    args: list[str]
    status: str = "queued"       # queued | running | done | error | cancelled
    progress: float = 0.0
    message: str = "Waiting in queue"
    created: float = field(default_factory=time.time)
    error: str | None = None
    result: dict | None = None
    out_name: str = "book.epub"
    proc: subprocess.Popen | None = None

    @property
    def dir(self) -> Path:
        return DATA / self.id

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "name", "status", "progress", "message", "error", "result", "out_name")}


jobs: dict[str, Job] = {}


def safe_name(s: str) -> str:
    s = re.sub(r"[^\w\s.\-()\[\]]+", "", s, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", s)[:120] or "book"


def run_job(job: Job) -> None:
    with slots:
        if job.status == "cancelled":
            return
        job.status, job.message = "running", "Starting"
        cmd = [sys.executable, "-m", "app.cli", str(job.dir / "input.pdf"), "-o", str(job.dir / "output.epub"),
               "--json-progress", *job.args]
        stderr_path = job.dir / "stderr.log"
        with open(stderr_path, "w") as err:
            job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                                        cwd=str(Path(__file__).parent.parent))
            assert job.proc.stdout is not None
            for line in job.proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "progress" in msg:
                    job.progress, job.message = msg["progress"], msg["message"]
                elif "error" in msg:
                    job.error = msg["error"]
                elif "result" in msg:
                    job.result = msg["result"]
            code = job.proc.wait()
        if job.status == "cancelled":
            return
        if code == 0 and (job.dir / "output.epub").exists():
            job.status, job.progress, job.message = "done", 1.0, "Done"
            title = (job.result or {}).get("title") or Path(job.name).stem
            job.out_name = safe_name(title) + ".epub"
        else:
            job.status = "error"
            if not job.error:
                tail = stderr_path.read_text(errors="replace").strip().splitlines()[-5:]
                job.error = "Conversion crashed: " + (" ".join(tail) or f"exit code {code}")
            job.message = "Failed"
        (job.dir / "input.pdf").unlink(missing_ok=True)


def janitor() -> None:
    while True:
        time.sleep(600)
        now = time.time()
        with lock:
            old = [j for j in jobs.values() if now - j.created > JOB_TTL and j.status not in ("running", "queued")]
            for j in old:
                jobs.pop(j.id, None)
        for j in old:
            shutil.rmtree(j.dir, ignore_errors=True)


threading.Thread(target=janitor, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/info")
def info() -> dict:
    return {"ocr": ocr.available(), "ocr_languages": list(ocr.languages())}


@app.post("/api/jobs")
def create_job(
    file: UploadFile = File(...),
    title: str = Form(""),
    author: str = Form(""),
    language: str = Form("en"),
    ocr_mode: str = Form("auto"),
    ocr_languages: str = Form("eng"),
    layout: str = Form("reflow"),
    tables: str = Form("html"),
    cover: bool = Form(True),
    keep_headers: bool = Form(False),
    password: str = Form(""),
) -> dict:
    if ocr_mode not in ("auto", "force", "off") or layout not in ("reflow", "fixed") or tables not in ("html", "image"):
        raise HTTPException(400, "Invalid option")
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", language):
        raise HTTPException(400, "Invalid language code")
    if not re.fullmatch(r"[A-Za-z_+]{1,200}", ocr_languages):
        raise HTTPException(400, "Invalid OCR languages")
    job = Job(id=uuid.uuid4().hex, name=file.filename or "document.pdf", args=[])
    job.dir.mkdir(parents=True)
    dest = job.dir / "input.pdf"
    size = 0
    with open(dest, "wb") as f:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                shutil.rmtree(job.dir, ignore_errors=True)
                raise HTTPException(413, f"File larger than {MAX_UPLOAD // 1024 // 1024} MB")
            f.write(chunk)
    with open(dest, "rb") as f:
        if b"%PDF" not in f.read(1024):
            shutil.rmtree(job.dir, ignore_errors=True)
            raise HTTPException(400, "That file is not a PDF")
    args = ["--source-name", job.name, "--language", language, "--ocr", ocr_mode, "--ocr-lang", ocr_languages, "--layout", layout, "--tables", tables]
    if title.strip():
        args += ["--title", title.strip()]
    if author.strip():
        args += ["--author", author.strip()]
    if not cover:
        args.append("--no-cover")
    if keep_headers:
        args.append("--keep-headers")
    if password:
        args += ["--password", password]
    job.args = args
    with lock:
        jobs[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job.public()


def get_job(job_id: str) -> Job:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    return get_job(job_id).public()


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    job = get_job(job_id)
    path = job.dir / "output.epub"
    if job.status != "done" or not path.exists():
        raise HTTPException(409, "Not ready")
    return FileResponse(path, media_type="application/epub+zip", filename=job.out_name)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = get_job(job_id)
    job.status = "cancelled" if job.status in ("queued", "running") else job.status
    if job.proc and job.proc.poll() is None:
        job.proc.kill()
    with lock:
        jobs.pop(job_id, None)
    shutil.rmtree(job.dir, ignore_errors=True)
    return {"ok": True}
