# aws_docs.py

Mirror every PDF on `docs.aws.amazon.com` and convert each one to plain-text
Markdown. Pages without an embedded text layer are run through Tesseract OCR.

The pipeline is incremental: a `manifest.json` records ETag / Last-Modified for
downloads and PDF size + mtime for conversions, so reruns only touch what
actually changed upstream.

## What it does

1. **Discover** — crawls `main-landing-page.xml`, then service landing pages,
   then each guide's `meta-inf/guide-info.json` to enumerate every PDF AWS
   publishes.
2. **Download** — fetches PDFs in parallel using conditional GETs
   (`If-None-Match` / `If-Modified-Since`) so unchanged files cost one HEAD-like
   round-trip and zero bytes.
3. **Convert** — extracts native text with `pypdf`; for any page below the
   text-density threshold, rasterises with `pdf2image` and OCRs with
   `pytesseract`. OCR results are cached on disk keyed by
   `(sha256(pdf), page, dpi, lang)`, shared across conversion modes and
   surviving crashes/reruns.

## Install

```bash
# Python deps
pip install -r requirements.txt

# System deps (only required if you want OCR)
brew install poppler tesseract        # macOS
# apt-get install poppler-utils tesseract-ocr   # Debian/Ubuntu
```

## Usage

```bash
# Discover + download every PDF into ./documentation/
./aws_docs.py download --out documentation

# Just print the URLs that would be downloaded
./aws_docs.py download --discover-only --url-list urls.txt

# Convert PDFs to one .md per PDF (default mode)
./aws_docs.py convert --pdf-dir documentation --text-dir text

# One file per page
./aws_docs.py convert --pdf-dir documentation --text-dir text_pages --per-page

# 10 pages per file
./aws_docs.py convert --pdf-dir documentation --text-dir text_chunks --pages-per-file 10

# Full pipeline in one shot
./aws_docs.py all --out documentation --text-dir text

# Detailed help with every flag
./aws_docs.py help
```

## Conversion modes

| `--pages-per-file` | Layout |
|---|---|
| `0` (default) | One `.md` per PDF, all pages concatenated. |
| `1` (`--per-page`) | Directory per PDF with `page-NNN.md` files. |
| `N` | Directory per PDF with `pages-AAA-BBB.md` chunks of `N` pages each. |

Per-page and chunked output files start with a header recording the source
PDF, the page range, and whether each page came from native extraction or OCR.

Chunk state is cached per `N` in `manifest.json` under
`conversions_chunked["<N>"]`, so you can keep multiple chunk sizes side-by-side
without invalidating each other.

## Output layout

```
documentation/<service>/<version>/<guide>/<name>.pdf
text/<service>/<version>/<guide>/<name>.md                          # single-file
text_pages/<service>/<version>/<guide>/<name>/page-001.md           # per-page
text_chunks/<service>/<version>/<guide>/<name>/pages-001-010.md     # chunked
```

## Manifest

`manifest.json` is the source of truth for "what's already done":

```json
{
  "version": 1,
  "updated_at": "<ISO8601>",
  "downloads":           { "<url>":      { "etag": "...", "local_size": ..., ... } },
  "conversions":         { "<pdf path>": { "pdf_size": ..., "ocr_pages": ..., ... } },
  "conversions_chunked": {
    "1":  { "<pdf path>": { ... } },
    "10": { "<pdf path>": { ... } }
  }
}
```

Pass `--force` to bypass all skip checks, or `--manifest <path>` to use a
different manifest file.

## Notes

- AWS publishes thousands of PDFs totalling many GB. A first run with OCR
  enabled is CPU-bound and can take hours; subsequent runs are minutes.
- OCR is on by default. Disable it with `--no-ocr` if you only want native
  text extraction.
- Conversion uses process-based parallelism (`pypdf` and `pytesseract` are
  CPU-bound and GIL-locked). Tune with `--workers`.
