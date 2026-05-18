#!/usr/bin/env python3
"""
aws_docs.py - Download every AWS documentation PDF and convert them to plain
text (using OCR for any page that has no extractable text).

Subcommands:
  download   Crawl docs.aws.amazon.com and download all PDFs.
  convert    Walk a directory of PDFs and write .md files (with OCR fallback).
  all        Run download then convert.

Examples:
  ./aws_docs.py download --out documentation
  ./aws_docs.py convert --pdf-dir documentation --text-dir text
  ./aws_docs.py all

A manifest.json is maintained for both phases:
  - downloads record remote ETag / Last-Modified / size plus local size + mtime
    so repeat runs use conditional GET and skip unchanged files.
  - conversions record the source PDF size + mtime so unchanged PDFs skip
    re-conversion.
"""

import argparse
import concurrent.futures as cf
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from tqdm.auto import tqdm

BASE_URL = "https://docs.aws.amazon.com"
LANDING_XML = f"{BASE_URL}/en_us/main-landing-page.xml"
USER_AGENT = "aws-docs-downloader/2.0"
DEFAULT_TIMEOUT = 60
DEFAULT_MANIFEST = "manifest.json"

log = logging.getLogger("aws_docs")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=32, pool_maxsize=32, max_retries=3
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class Manifest:
    """Thread-safe JSON-backed record of download + conversion state.

    Shape:
      {
        "version": 1,
        "updated_at": "ISO8601",
        "downloads":   {url: {etag, last_modified, content_length,
                              local_path, local_size, local_mtime,
                              downloaded_at, checked_at, status}},
        "conversions": {pdf_path: {text_path, pdf_size, pdf_mtime,
                                   text_mtime, converted_at, ocr, native_pages,
                                   ocr_pages, status}},
      }
    """

    VERSION = 1

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data = {
            "version": self.VERSION,
            "downloads": {},
            "conversions": {},
            "conversions_chunked": {},
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data["downloads"] = loaded.get("downloads", {}) or {}
                    self.data["conversions"] = loaded.get("conversions", {}) or {}
                    self.data["conversions_chunked"] = (
                        loaded.get("conversions_chunked", {}) or {}
                    )
                    # Backwards compat: old manifests stored pages-per-file=1
                    # conversions under "conversions_per_page".
                    legacy = loaded.get("conversions_per_page") or {}
                    if legacy:
                        bucket = self.data["conversions_chunked"].setdefault("1", {})
                        for k, v in legacy.items():
                            bucket.setdefault(k, v)
            except Exception as e:
                log.warning("manifest load failed (%s), starting fresh", e)

    def get_download(self, url):
        with self._lock:
            return dict(self.data["downloads"].get(url, {}))

    def set_download(self, url, entry):
        with self._lock:
            self.data["downloads"][url] = entry

    def get_conversion(self, pdf_path, mode="single", pages_per_file=None):
        with self._lock:
            if mode == "chunked":
                ppf = str(int(pages_per_file or 1))
                return dict(
                    self.data["conversions_chunked"].get(ppf, {}).get(
                        str(pdf_path), {}
                    )
                )
            return dict(self.data["conversions"].get(str(pdf_path), {}))

    def set_conversion(self, pdf_path, entry, mode="single", pages_per_file=None):
        with self._lock:
            if mode == "chunked":
                ppf = str(int(pages_per_file or 1))
                bucket = self.data["conversions_chunked"].setdefault(ppf, {})
                bucket[str(pdf_path)] = entry
            else:
                self.data["conversions"][str(pdf_path)] = entry

    def save(self):
        with self._lock:
            self.data["version"] = self.VERSION
            self.data["updated_at"] = _now_iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _fetch(session, url):
    r = session.get(url, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.content


def _normalize_path(href):
    """Return a docs.aws.amazon.com path from href, or None for external URLs."""
    if not href:
        return None
    parts = urlsplit(href)
    if parts.netloc and "docs.aws.amazon.com" not in parts.netloc:
        return None
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if path.startswith("/assets/") or path.startswith("/images/"):
        return None
    return path


def _guide_dir(path):
    """Given an arbitrary docs path, return the guide root directory candidate."""
    if not path or path == "/":
        return None
    if re.search(r"\.[a-zA-Z0-9]{2,5}$", path):
        path = path.rsplit("/", 1)[0] + "/"
    if not path.endswith("/"):
        path += "/"
    if "/latest/" in path or re.search(r"/\d{4}-\d{2}-\d{2}/", path):
        return path
    return None


def _absolute_pdf_url(guide_dir, pdf_field):
    if pdf_field.startswith("http"):
        return pdf_field
    if pdf_field.startswith("/"):
        return BASE_URL + pdf_field
    return urljoin(BASE_URL + guide_dir, pdf_field)


def _pdfs_from_guide_info(session, guide_dir, seen):
    if guide_dir in seen:
        return set()
    seen.add(guide_dir)
    url = BASE_URL + guide_dir + "meta-inf/guide-info.json"
    try:
        data = json.loads(_fetch(session, url))
    except Exception as e:
        log.debug("guide-info miss %s: %s", url, e)
        return set()
    pdfs = set()
    if data.get("pdf"):
        pdfs.add(_absolute_pdf_url(guide_dir, data["pdf"]))
    for key in ("sub-guides", "sections", "contents"):
        for entry in data.get(key, []) or []:
            if isinstance(entry, dict) and entry.get("pdf"):
                pdfs.add(_absolute_pdf_url(guide_dir, entry["pdf"]))
    return pdfs


def _guide_dirs_in_html(session, url):
    try:
        html = _fetch(session, url)
    except Exception as e:
        log.warning("HTML fetch failed %s: %s", url, e)
        return set()
    soup = BeautifulSoup(html, "html.parser")
    out = set()
    for link in soup.find_all("a"):
        path = _normalize_path(link.get("href"))
        if not path:
            continue
        d = _guide_dir(path)
        if d:
            out.add(d)
    return out


def discover_pdfs(session, landing_url=LANDING_XML):
    log.info("Fetching landing page %s", landing_url)
    root_xml = _fetch(session, landing_url)
    soup = BeautifulSoup(root_xml, "xml")

    raw_hrefs = set()
    for tag in soup.find_all(True):
        href = tag.get("href") if tag.has_attr("href") else None
        if href:
            raw_hrefs.add(href)
    log.info("main landing: %d href candidates", len(raw_hrefs))

    guide_dirs = set()
    service_landings = set()
    for href in raw_hrefs:
        path = _normalize_path(href)
        if not path:
            continue
        d = _guide_dir(path)
        if d:
            guide_dirs.add(d)
            continue
        segments = [s for s in path.split("/") if s]
        if 1 <= len(segments) <= 2 and "/latest/" not in path:
            service_landings.add(BASE_URL + ("/" + segments[0] + "/"))

    log.info("direct guides: %d | service landings: %d",
             len(guide_dirs), len(service_landings))

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for extra in pool.map(lambda u: _guide_dirs_in_html(session, u),
                              sorted(service_landings)):
            guide_dirs |= extra

    log.info("total unique guide roots: %d", len(guide_dirs))

    pdfs = set()
    seen = set()
    guide_list = sorted(guide_dirs)
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(_pdfs_from_guide_info, session, gd, seen): gd
                   for gd in guide_list}
        with tqdm(total=len(futures), desc="probe guides", unit="guide",
                  leave=False) as bar:
            for fut in cf.as_completed(futures):
                found = fut.result()
                pdfs |= found
                bar.set_postfix(pdfs=len(pdfs))
                bar.update(1)

    log.info("discovered %d PDFs", len(pdfs))
    return pdfs


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _local_path(out_dir, pdf_url):
    parts = urlsplit(pdf_url).path.lstrip("/").split("/")
    return Path(out_dir, *parts)


def _local_stat(path):
    try:
        st = path.stat()
        return st.st_size, st.st_mtime
    except FileNotFoundError:
        return None, None


def download_one(session, pdf_url, out_dir, manifest, force=False):
    """Download one PDF using a conditional GET backed by the manifest.

    Returns (url, dest, status). Status is one of:
      'ok'            - downloaded (new or changed)
      'skip-local'    - local file matches manifest; no network request needed
      'skip-304'      - server confirmed cached copy is up to date
      'error: ...'    - failure
    """
    if pdf_url.startswith("//"):
        pdf_url = "http:" + pdf_url
    dest = _local_path(out_dir, pdf_url)
    cached = manifest.get_download(pdf_url)
    local_size, local_mtime = _local_stat(dest)

    # Fast path: we've seen this URL before, the local file exists, and its
    # size + mtime match what we recorded. Skip without hitting the network.
    if (
        not force
        and cached
        and dest.exists()
        and local_size == cached.get("local_size")
        and local_mtime == cached.get("local_mtime")
    ):
        cached["checked_at"] = _now_iso()
        cached["status"] = "cached"
        manifest.set_download(pdf_url, cached)
        return (pdf_url, dest, "skip-local")

    headers = {}
    if not force and dest.exists() and cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with session.get(
            pdf_url, headers=headers, stream=True, timeout=DEFAULT_TIMEOUT
        ) as r:
            if r.status_code == 304 and dest.exists():
                entry = dict(cached)
                size, mtime = _local_stat(dest)
                entry["local_path"] = str(dest)
                entry["local_size"] = size
                entry["local_mtime"] = mtime
                entry["checked_at"] = _now_iso()
                entry["status"] = "not-modified"
                manifest.set_download(pdf_url, entry)
                return (pdf_url, dest, "skip-304")
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 15):
                    if chunk:
                        fh.write(chunk)
            remote_etag = r.headers.get("ETag")
            remote_last_modified = r.headers.get("Last-Modified")
            remote_content_length = r.headers.get("Content-Length")
        tmp.replace(dest)
        size, mtime = _local_stat(dest)
        entry = {
            "etag": remote_etag,
            "last_modified": remote_last_modified,
            "content_length": int(remote_content_length)
                if remote_content_length and remote_content_length.isdigit() else None,
            "local_path": str(dest),
            "local_size": size,
            "local_mtime": mtime,
            "downloaded_at": _now_iso(),
            "checked_at": _now_iso(),
            "status": "ok",
        }
        manifest.set_download(pdf_url, entry)
        return (pdf_url, dest, "ok")
    except Exception as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return (pdf_url, dest, f"error: {e}")


def download_all(pdf_urls, out_dir, manifest, force=False, workers=8):
    session = make_session()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = skip = err = 0
    total_bytes = 0
    urls = sorted(pdf_urls)
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(download_one, session, u, out_dir, manifest, force)
            for u in urls
        ]
        with tqdm(total=len(futures), desc="download", unit="pdf") as bar:
            for fut in cf.as_completed(futures):
                url, dest, status = fut.result()
                if status == "ok":
                    ok += 1
                    size = manifest.get_download(url).get("local_size") or 0
                    total_bytes += size
                    log.info("[%d/%d] OK   %s", ok + skip + err,
                             len(futures), dest)
                elif status.startswith("skip"):
                    skip += 1
                    log.info("[%d/%d] %s %s", ok + skip + err,
                             len(futures), status.upper(), dest)
                else:
                    err += 1
                    log.warning("[%d/%d] FAIL %s (%s)", ok + skip + err,
                                len(futures), url, status)
                bar.set_postfix(ok=ok, skip=skip, err=err,
                                mb=f"{total_bytes / 1e6:.1f}")
                bar.update(1)
                if (ok + skip + err) % 25 == 0:
                    manifest.save()
    manifest.save()
    log.info("download summary: ok=%d skip=%d err=%d bytes=%d",
             ok, skip, err, total_bytes)
    return ok, skip, err


# ---------------------------------------------------------------------------
# PDF to text (with OCR fallback)
# ---------------------------------------------------------------------------

def _lazy_import_converters():
    global pypdf, pdf2image, pytesseract
    import pypdf  # type: ignore
    import pdf2image  # type: ignore
    import pytesseract  # type: ignore


def _extract_native_pages(pdf_path):
    reader = pypdf.PdfReader(str(pdf_path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return pages


def _sha256_file(path, bufsize=1 << 20):
    """SHA-256 of a file's contents as a hex string."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _ocr_cache_path(cache_dir, pdf_sha, page_number, dpi, lang):
    """Disk-cache location for OCR output of a single page.

    Sharded by the first 2 hex chars of pdf_sha to avoid huge directories.
    Cache key includes dpi and lang so changes invalidate cleanly.
    """
    shard = pdf_sha[:2]
    key = f"{pdf_sha}-p{page_number:05d}-d{dpi}-{lang}.txt"
    return Path(cache_dir) / shard / key


def _ocr_page(
    pdf_path,
    page_number,
    dpi=200,
    lang="eng",
    cache_dir=None,
    pdf_sha=None,
    force_ocr=False,
):
    """OCR a single page, consulting/updating the on-disk OCR cache.

    The cache key is (sha256(pdf), page_number, dpi, lang). A hit returns
    the previously-OCR'd text without re-rendering or re-running tesseract.
    """
    cache_path = None
    if cache_dir and pdf_sha:
        cache_path = _ocr_cache_path(cache_dir, pdf_sha, page_number, dpi, lang)
        if cache_path.exists() and not force_ocr:
            try:
                return cache_path.read_text(encoding="utf-8"), True
            except OSError:
                pass

    images = pdf2image.convert_from_path(
        str(pdf_path), dpi=dpi, first_page=page_number, last_page=page_number
    )
    if not images:
        return "", False
    text = pytesseract.image_to_string(images[0], lang=lang) or ""

    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(cache_path.suffix + ".part")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(cache_path)
        except OSError as e:
            log.debug("ocr cache write failed for %s p%d: %s",
                      pdf_path, page_number, e)
    return text, False


def _page_file(text_dir_for_pdf, page_number, total_pages):
    """Return the path for a single page's .md file inside the per-PDF directory."""
    width = len(str(total_pages)) if total_pages > 0 else 1
    return text_dir_for_pdf / f"page-{page_number:0{width}d}.md"


def _chunk_file(text_dir_for_pdf, start_page, end_page, total_pages):
    """Return the path for a multi-page chunk file (inclusive page range)."""
    width = len(str(total_pages)) if total_pages > 0 else 1
    return text_dir_for_pdf / (
        f"pages-{start_page:0{width}d}-{end_page:0{width}d}.md"
    )


def _chunk_ranges(total_pages, pages_per_file):
    """Yield (start, end) inclusive 1-indexed page ranges of size pages_per_file."""
    for start in range(1, total_pages + 1, pages_per_file):
        end = min(start + pages_per_file - 1, total_pages)
        yield start, end


def convert_pdf_worker(
    pdf_path,
    target,
    ocr=True,
    ocr_threshold=40,
    dpi=200,
    lang="eng",
    pages_per_file=0,
    ocr_cache_dir=None,
    force_ocr=False,
):
    """Pure worker: extract text (and OCR where needed) from one PDF and
    write the result. Must be picklable, so it takes no manifest or live
    objects. Returns a dict the parent merges into the manifest.

    pages_per_file:
      0 -> single combined .md (target is a file path)
      1 -> one page-NNN.md per page (target is a directory)
      N -> pages-AAA-BBB.md chunks of N pages (target is a directory)

    ocr_cache_dir: directory holding a per-page OCR result cache keyed on
      (sha256(pdf), page_number, dpi, lang). Survives crashes and reruns,
      and is shared across pages_per_file settings.
    force_ocr: re-run tesseract even when a cached result exists.
    """
    _lazy_import_converters()
    pdf_path = Path(pdf_path)
    target = Path(target)
    chunked = pages_per_file and pages_per_file > 0

    pdf_size, pdf_mtime = _local_stat(pdf_path)
    if pdf_size is None:
        return {"status": "error", "error": "missing source PDF",
                "native_pages": 0, "ocr_pages": 0, "ocr_cache_hits": 0}

    if chunked:
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)

    try:
        pages = _extract_native_pages(pdf_path)
    except Exception as e:
        return {"status": "error", "error": f"native extract: {e}",
                "native_pages": 0, "ocr_pages": 0, "ocr_cache_hits": 0}

    pdf_sha = None
    if ocr and ocr_cache_dir:
        try:
            pdf_sha = _sha256_file(pdf_path)
        except OSError as e:
            log.debug("sha256 failed for %s: %s", pdf_path, e)

    total = len(pages)
    native = ocr_count = ocr_hits = 0
    bodies = []
    sources = []
    for idx_page, text in enumerate(pages, 1):
        stripped = (text or "").strip()
        if len(stripped) >= ocr_threshold or not ocr:
            native += 1
            bodies.append(text or "")
            sources.append("native")
        else:
            try:
                body, cache_hit = _ocr_page(
                    pdf_path, idx_page, dpi=dpi, lang=lang,
                    cache_dir=ocr_cache_dir, pdf_sha=pdf_sha,
                    force_ocr=force_ocr,
                )
                body = body or ""
                ocr_count += 1
                if cache_hit:
                    ocr_hits += 1
                bodies.append(body)
                sources.append("ocr-cached" if cache_hit else "ocr")
            except Exception as e:
                bodies.append(text or "")
                sources.append(f"ocr-failed:{e}")

    written_files = []
    stale_removed = 0
    text_size = text_mtime = None

    if not chunked:
        out_parts = [
            f"===== Page {i} ({sources[i - 1]}) =====\n"
            f"{bodies[i - 1].rstrip()}\n"
            for i in range(1, total + 1)
        ]
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_text("\n".join(out_parts), encoding="utf-8")
        tmp.replace(target)
        text_size, text_mtime = _local_stat(target)
    else:
        per_page = pages_per_file == 1
        for start, end in _chunk_ranges(total, pages_per_file):
            if per_page:
                out_path = _page_file(target, start, total)
                header = (
                    f"Source: {pdf_path}\n"
                    f"Page: {start}/{total}\n"
                    f"Extraction: {sources[start - 1]}\n"
                    f"{'-' * 40}\n"
                )
                body = bodies[start - 1].rstrip() + "\n"
            else:
                out_path = _chunk_file(target, start, end, total)
                unique = sorted(set(sources[start - 1:end]))
                extraction = unique[0] if len(unique) == 1 else ",".join(unique)
                header = (
                    f"Source: {pdf_path}\n"
                    f"Pages: {start}-{end}/{total}\n"
                    f"Extraction: {extraction}\n"
                    f"{'-' * 40}\n"
                )
                body_parts = []
                for i in range(start, end + 1):
                    body_parts.append(
                        f"===== Page {i} ({sources[i - 1]}) =====\n"
                        f"{bodies[i - 1].rstrip()}\n"
                    )
                body = "\n".join(body_parts)
            tmp = out_path.with_suffix(out_path.suffix + ".part")
            tmp.write_text(header + body, encoding="utf-8")
            tmp.replace(out_path)
            written_files.append(str(out_path))

        # Remove stale files matching our naming conventions that we did
        # not just write (handles shrinking PDFs and changes to pages_per_file).
        keep = {Path(p) for p in written_files}
        for pattern in ("page-*.md", "pages-*-*.md"):
            for f in sorted(target.glob(pattern)):
                if f not in keep:
                    try:
                        f.unlink()
                        stale_removed += 1
                    except OSError:
                        pass

    return {
        "status": "ok",
        "chunked": bool(chunked),
        "pages_per_file": int(pages_per_file) if chunked else 0,
        "target": str(target),
        "pdf_size": pdf_size,
        "pdf_mtime": pdf_mtime,
        "pdf_sha": pdf_sha,
        "text_size": text_size,
        "text_mtime": text_mtime,
        "page_count": total,
        "files_written": len(written_files),
        "native_pages": native,
        "ocr_pages": ocr_count,
        "ocr_cache_hits": ocr_hits,
        "stale_removed": stale_removed,
        "ocr": ocr,
        "ocr_threshold": ocr_threshold,
        "dpi": dpi,
        "lang": lang,
    }


def _make_manifest_entry(result):
    """Translate a worker result into a manifest entry dict."""
    base = {
        "pdf_size": result["pdf_size"],
        "pdf_mtime": result["pdf_mtime"],
        "pdf_sha": result.get("pdf_sha"),
        "converted_at": _now_iso(),
        "ocr": result["ocr"],
        "ocr_threshold": result["ocr_threshold"],
        "dpi": result["dpi"],
        "lang": result["lang"],
        "native_pages": result["native_pages"],
        "ocr_pages": result["ocr_pages"],
        "ocr_cache_hits": result.get("ocr_cache_hits", 0),
        "status": "ok",
    }
    if result["chunked"]:
        base.update({
            "text_dir": result["target"],
            "page_count": result["page_count"],
            "pages_per_file": result["pages_per_file"],
            "files_written": result["files_written"],
            "stale_removed": result["stale_removed"],
        })
    else:
        base.update({
            "text_path": result["target"],
            "text_size": result["text_size"],
            "text_mtime": result["text_mtime"],
        })
    return base


def convert_tree(
    pdf_dir,
    text_dir,
    manifest,
    ocr=True,
    ocr_threshold=40,
    dpi=200,
    lang="eng",
    force=False,
    workers=None,
    pages_per_file=0,
    ocr_cache_dir=".ocr_cache",
    force_ocr=False,
):
    """Convert a whole tree of PDFs using process-based parallelism.

    pypdf and pytesseract are CPU-bound pure-Python; threads share the GIL,
    so we fan out to processes. The parent owns manifest I/O so workers
    stay picklable and isolated.

    pages_per_file:
      0 -> one combined .md per PDF
      1 -> one page-NNN.md per page
      N -> pages-AAA-BBB.md chunks of N pages
    """
    pdf_dir = Path(pdf_dir)
    text_dir = Path(text_dir)
    pdfs = sorted(pdf_dir.rglob("*.pdf"))
    chunked = pages_per_file and pages_per_file > 0
    if chunked:
        mode_label = "per-page" if pages_per_file == 1 else f"chunk-{pages_per_file}"
        mode_key = "chunked"
    else:
        mode_label = "single-file"
        mode_key = "single"
    if workers is None or workers <= 0:
        workers = max(1, os.cpu_count() or 1)
    log.info(
        "converting %d PDFs from %s -> %s (%s) with %d workers",
        len(pdfs), pdf_dir, text_dir, mode_label, workers,
    )
    if ocr:
        cache_label = "disabled" if not ocr_cache_dir else str(ocr_cache_dir)
        log.info("ocr cache: %s (force_ocr=%s)", cache_label, bool(force_ocr))
        if ocr_cache_dir:
            Path(ocr_cache_dir).mkdir(parents=True, exist_ok=True)

    # Pre-skip unchanged PDFs on the parent side (cheap, no worker spawn).
    todo = []
    skipped_pages_native = skipped_pages_ocr = 0
    skipped_pdfs = set()
    for pdf in pdfs:
        rel = pdf.relative_to(pdf_dir)
        target = (
            text_dir / rel.with_suffix("") if chunked
            else text_dir / rel.with_suffix(".md")
        )
        pdf_size, pdf_mtime = _local_stat(pdf)
        cached = manifest.get_conversion(
            pdf, mode=mode_key, pages_per_file=pages_per_file,
        )
        target_exists = target.is_dir() if chunked else target.exists()
        if (
            not force
            and target_exists
            and cached
            and cached.get("pdf_size") == pdf_size
            and cached.get("pdf_mtime") == pdf_mtime
        ):
            skipped_pages_native += int(cached.get("native_pages", 0) or 0)
            skipped_pages_ocr += int(cached.get("ocr_pages", 0) or 0)
            skipped_pdfs.add(pdf)
            continue
        todo.append((pdf, target))

    skipped_count = len(skipped_pdfs)
    if skipped_count:
        log.info("skipping %d unchanged PDFs (cached)", skipped_count)

    ok = err = 0
    skip = skipped_count
    pages_done = skipped_pages_native + skipped_pages_ocr
    ocr_hits_total = 0
    mb_done = 0.0
    total_mb = sum((p.stat().st_size for p in pdfs), 0) / 1e6

    with tqdm(total=len(pdfs), desc=f"convert ({mode_label})", unit="pdf") as bar:
        if skipped_count:
            mb_done += sum(
                (p.stat().st_size for p in skipped_pdfs), 0,
            ) / 1e6
            bar.set_postfix(
                ok=ok, skip=skip, err=err,
                pages=pages_done,
                mb=f"{mb_done:.0f}/{total_mb:.0f}",
            )
            bar.update(skipped_count)

        if not todo:
            manifest.save()
            log.info(
                "convert summary: ok=%d skip=%d err=%d pages=%d ocr_cache_hits=0",
                ok, skip, err, pages_done,
            )
            return ok, skip, err

        with cf.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    convert_pdf_worker,
                    str(pdf), str(target),
                    ocr, ocr_threshold, dpi, lang, pages_per_file,
                    ocr_cache_dir, force_ocr,
                ): (pdf, target)
                for pdf, target in todo
            }
            done = 0
            for fut in cf.as_completed(futures):
                pdf, target = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    result = {
                        "status": "error", "error": str(e),
                        "native_pages": 0, "ocr_pages": 0,
                    }
                done += 1
                mb_done += pdf.stat().st_size / 1e6
                n_native = result.get("native_pages", 0)
                n_ocr = result.get("ocr_pages", 0)
                n_hits = result.get("ocr_cache_hits", 0)
                pages_done += n_native + n_ocr
                ocr_hits_total += n_hits
                if result["status"] == "ok":
                    ok += 1
                    manifest.set_conversion(
                        pdf, _make_manifest_entry(result),
                        mode=mode_key, pages_per_file=pages_per_file,
                    )
                    hit_note = f" cache_hits={n_hits}" if n_hits else ""
                    log.info(
                        "[%d/%d] OK   %s native=%d ocr=%d%s",
                        done, len(todo), target, n_native, n_ocr, hit_note,
                    )
                else:
                    err += 1
                    log.warning(
                        "[%d/%d] FAIL %s (%s)",
                        done, len(todo), pdf, result.get("error"),
                    )
                postfix = dict(
                    ok=ok, skip=skip, err=err,
                    pages=pages_done,
                    mb=f"{mb_done:.0f}/{total_mb:.0f}",
                )
                if ocr_hits_total:
                    postfix["ocr_cached"] = ocr_hits_total
                bar.set_postfix(**postfix)
                bar.update(1)
                if done % 25 == 0:
                    manifest.save()

    manifest.save()
    log.info(
        "convert summary: ok=%d skip=%d err=%d pages=%d ocr_cache_hits=%d",
        ok, skip, err, pages_done, ocr_hits_total,
    )
    return ok, skip, err


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--manifest", default=DEFAULT_MANIFEST,
                   help="Path to manifest.json (default: ./manifest.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="Download all AWS documentation PDFs.")
    d.add_argument("--out", default="documentation", help="Output directory")
    d.add_argument("--force", action="store_true",
                   help="Re-download files even if manifest/local state matches")
    d.add_argument("--workers", type=int, default=8)
    d.add_argument("--landing", default=LANDING_XML)
    d.add_argument("--url-list", help="Also write the discovered URLs to this file")
    d.add_argument("--discover-only", action="store_true",
                   help="Only discover PDF URLs, do not download them")

    c = sub.add_parser("convert", help="Convert PDFs to .md with OCR fallback.")
    c.add_argument("--pdf-dir", default="documentation")
    c.add_argument("--text-dir", default="text",
                   help="Output dir. Single-file mode: one .md per PDF. "
                        "Per-page / chunked modes: one directory per PDF "
                        "containing page-NNN.md or pages-AAA-BBB.md files.")
    group = c.add_mutually_exclusive_group()
    group.add_argument("--per-page", action="store_true",
                       help="Write one file per page (same as --pages-per-file 1).")
    group.add_argument("--pages-per-file", type=int, default=0,
                       help="Group N pages per .md (0 = single combined file, "
                            "1 = one file per page, N>1 = chunks of N pages).")
    c.add_argument("--no-ocr", action="store_true", help="Disable OCR fallback")
    c.add_argument("--ocr-threshold", type=int, default=40,
                   help="Minimum native-text chars per page before OCR kicks in")
    c.add_argument("--dpi", type=int, default=200, help="DPI for OCR rasterisation")
    c.add_argument("--lang", default="eng",
                   help="Tesseract language(s), e.g. 'eng' or 'eng+fra'")
    c.add_argument("--force", action="store_true")
    c.add_argument("--workers", type=int, default=0,
                   help="Number of worker processes (0 = os.cpu_count()).")
    c.add_argument("--ocr-cache", default=".ocr_cache",
                   help="Directory holding per-page OCR cache "
                        "(default: .ocr_cache). Pass '' to disable.")
    c.add_argument("--force-ocr", action="store_true",
                   help="Re-run tesseract even when a cached OCR result exists.")

    a = sub.add_parser("all", help="Run download then convert with default paths.")
    a.add_argument("--out", default="documentation")
    a.add_argument("--text-dir", default="text",
                   help="Output dir for single-file conversion. "
                        "Skipped entirely when --no-single is passed.")
    a.add_argument("--chunk-dir", default="text_pages",
                   help="Output dir for per-page / chunked conversion "
                        "(used when --per-page or --pages-per-file is set).")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--per-page", action="store_true",
                   help="Also produce per-page output "
                        "(same as --pages-per-file 1).")
    g.add_argument("--pages-per-file", type=int, default=0,
                   help="Also produce chunked output with N pages per file "
                        "(0 = disabled).")
    a.add_argument("--no-single", action="store_true",
                   help="Skip single-file conversion.")
    a.add_argument("--force", action="store_true")
    a.add_argument("--no-ocr", action="store_true")
    a.add_argument("--download-workers", type=int, default=8)
    a.add_argument("--convert-workers", type=int, default=0,
                   help="Conversion worker processes (0 = os.cpu_count()).")
    a.add_argument("--ocr-cache", default=".ocr_cache",
                   help="Directory holding per-page OCR cache "
                        "(default: .ocr_cache). Pass '' to disable.")
    a.add_argument("--force-ocr", action="store_true",
                   help="Re-run tesseract even when a cached OCR result exists.")

    sub.add_parser("help", help="Show the detailed usage guide and exit.")

    return p


HELP_TEXT = """\
aws_docs.py - AWS documentation mirror and text extractor
=========================================================

OVERVIEW
  Two-phase pipeline against docs.aws.amazon.com:
    1. download  -> fetch every AWS doc PDF we can discover.
    2. convert   -> turn each PDF into plain text. Pages with no embedded
                    text layer are OCR'd with Tesseract (unless --no-ocr).

  State for both phases is recorded in manifest.json so reruns only touch
  what actually changed upstream:
    - downloads use conditional GET (If-None-Match / If-Modified-Since).
    - conversions compare the PDF's size + mtime against recorded values.

SUBCOMMANDS
  download   Crawl and download all PDFs.
  convert    Convert a tree of PDFs to .md (single file, per-page, or chunked).
  all        Run download, then single-file convert, then (optionally)
             per-page / chunked convert.
  help       Print this guide.

CONVERSION MODES (--pages-per-file / --per-page)
  pages-per-file 0 (default)    -> one .md per PDF, all pages concatenated.
  pages-per-file 1 / --per-page -> one page-NNN.md per page.
  pages-per-file N              -> pages-AAA-BBB.md chunks of N pages each.

  Chunk state is cached per N in manifest.json under
  conversions_chunked["<N>"], so you can keep multiple chunk sizes
  side by side without invalidating each other's cache.

COMMON EXAMPLES
  # Discover + download everything to ./documentation/
  ./aws_docs.py download --out documentation

  # Just see what URLs would be downloaded:
  ./aws_docs.py download --discover-only --url-list urls.txt

  # One .md per PDF (default), uses OCR where needed:
  ./aws_docs.py convert --pdf-dir documentation --text-dir text

  # One file per PDF page:
  ./aws_docs.py convert --pdf-dir documentation --text-dir text_pages --per-page

  # 10 pages per file:
  ./aws_docs.py convert --pdf-dir documentation --text-dir text_chunks --pages-per-file 10

  # Full pipeline, single + chunked output in one shot:
  ./aws_docs.py all --out documentation --text-dir text \\
                    --pages-per-file 10 --chunk-dir text_chunks

  # Chunked only, no combined .md files:
  ./aws_docs.py all --pages-per-file 10 --chunk-dir text_chunks --no-single

PROGRESS BARS
  download : bar per PDF; postfix shows ok/skip/err plus MB downloaded.
  convert  : bar per PDF; postfix shows ok/skip/err, total pages extracted,
             and MB processed out of the total MB of PDFs to convert.
  discover : bar per guide probed; postfix shows PDFs discovered so far.

  Use -v / --verbose for DEBUG logging. Log lines are routed through
  tqdm.write so they don't overwrite the progress bar.

OUTPUT LAYOUT
  Single-file mode:
    documentation/pdfs/<svc>/<ver>/<guide>/<name>.pdf
    text/pdfs/<svc>/<ver>/<guide>/<name>.md

  Per-page / chunked modes (directory per PDF):
    text_pages/pdfs/<svc>/<ver>/<guide>/<name>/page-001.md        (per-page)
    text_chunks/pdfs/<svc>/<ver>/<guide>/<name>/pages-001-010.md  (chunk 10)

  Per-page / chunk files start with a traceability header:
    Source: <pdf path>
    Page:  <n>/<total>            (per-page)
    Pages: <start>-<end>/<total>  (chunked)
    Extraction: native | ocr | ocr-failed:<err> [ | comma list if mixed ]
    ----------------------------------------
    <page text>

MANIFEST (manifest.json)
  {
    "version": 1,
    "updated_at": "<ISO8601>",
    "downloads":           { "<url>":     {...} },
    "conversions":         { "<pdf path>": {...} },    // single-file
    "conversions_chunked": {
      "1":  { "<pdf path>": {...} },                   // per-page
      "10": { "<pdf path>": {...} },                   // 10 pages/file
      ...
    }
  }

  --force bypasses all skip checks.
  --manifest <path> uses a different manifest file.

DEPENDENCIES
  Python (installed via `uv pip install -r requirements-aws-docs.txt`):
    requests, beautifulsoup4, lxml, pypdf, pdf2image, pytesseract,
    Pillow, tqdm

  System (only needed for OCR):
    poppler      (pdf2image backend)   -- macOS: brew install poppler
    tesseract    (pytesseract backend) -- macOS: brew install tesseract
"""


class TqdmLoggingHandler(logging.Handler):
    """Route log records through tqdm.write so they don't break progress bars."""

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)
        except Exception:
            self.handleError(record)


def _resolve_pages_per_file(args):
    """Translate --per-page / --pages-per-file into a single int."""
    if getattr(args, "per_page", False):
        return 1
    return int(getattr(args, "pages_per_file", 0) or 0)


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.cmd == "help":
        print(HELP_TEXT)
        return 0

    handler = TqdmLoggingHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    manifest = Manifest(args.manifest)

    if args.cmd == "download":
        session = make_session()
        urls = discover_pdfs(session, args.landing)
        if args.url_list:
            Path(args.url_list).write_text(
                "\n".join(sorted(urls)), encoding="utf-8"
            )
            log.info("wrote %d URLs to %s", len(urls), args.url_list)
        if not args.discover_only:
            download_all(urls, args.out, manifest,
                         force=args.force, workers=args.workers)

    elif args.cmd == "convert":
        convert_tree(
            args.pdf_dir, args.text_dir, manifest,
            ocr=not args.no_ocr,
            ocr_threshold=args.ocr_threshold,
            dpi=args.dpi, lang=args.lang,
            force=args.force, workers=args.workers,
            pages_per_file=_resolve_pages_per_file(args),
            ocr_cache_dir=(args.ocr_cache or None),
            force_ocr=args.force_ocr,
        )

    elif args.cmd == "all":
        session = make_session()
        urls = discover_pdfs(session)
        download_all(urls, args.out, manifest,
                     force=args.force, workers=args.download_workers)
        ocr_cache_dir = args.ocr_cache or None
        if not args.no_single:
            convert_tree(
                args.out, args.text_dir, manifest,
                ocr=not args.no_ocr,
                force=args.force, workers=args.convert_workers,
                pages_per_file=0,
                ocr_cache_dir=ocr_cache_dir,
                force_ocr=args.force_ocr,
            )
        ppf = _resolve_pages_per_file(args)
        if ppf > 0:
            convert_tree(
                args.out, args.chunk_dir, manifest,
                ocr=not args.no_ocr,
                force=args.force, workers=args.convert_workers,
                pages_per_file=ppf,
                ocr_cache_dir=ocr_cache_dir,
                force_ocr=args.force_ocr,
            )

    manifest.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
