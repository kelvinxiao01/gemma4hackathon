"""Fetch payer policy documents into be/corpus/.

Run once; the output JSON is committed so indexing needs no network. Every
document passes fetch and extraction gates before anything reaches disk, because
a WAF block page written to corpus/ becomes a citable "policy" document.

    uv run python scripts/fetch_corpus.py
"""

import hashlib
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pdfplumber
import trafilatura

CORPUS = Path(__file__).resolve().parent.parent / "corpus"

# Every row comes from the payer seed lists, restricted to rows that are
# clinical criteria, active, non-virtual, and carry a real URL. Do not add a row
# the seed data does not support.
#
# scope is the honest answer to "is this document about this drug?":
#   dedicated - the whole document covers this drug
#   omnibus   - a multi-drug policy where this drug is one entry among many, so
#               the drug label is document-level and NOT true of every chunk
#
# (payer, payer_name, drug, brand, plan_type, fmt, scope, url)
DOCS = [
    # Pembrolizumab / Keytruda. UHC publishes no pembrolizumab policy at all:
    # its seed rows are NO_DOCUMENT_AVAILABLE with index_virtual=TRUE.
    ("aetna", "Aetna", "pembrolizumab", "Keytruda", "commercial", "html", "dedicated",
     "https://www.aetna.com/cpb/medical/data/800_899/0890.html"),
    ("aetna", "Aetna", "pembrolizumab", "Keytruda", "medicare", "pdf", "dedicated",
     "https://www.aetna.com/content/dam/aetna/pdfs/aetnacom/healthcare-professionals/documents-forms/Keytruda-1980-A-Aetna-MedB.pdf"),
    ("anthem", "Anthem", "pembrolizumab", "Keytruda", "commercial", "pdf", "dedicated",
     "https://www.anthem.com/content/dam/digital/docs/pharmacy-information/clinical-criteria/Keytruda.pdf"),
    ("cigna", "Cigna", "pembrolizumab", "Keytruda", "commercial", "pdf", "omnibus",
     "https://static.cigna.com/assets/chcp/pdf/coveragePolicies/pharmacy/ph_1403_coveragepositioncriteria_oncology.pdf"),

    # Ustekinumab / Stelara. The only drug here with a dedicated document at all
    # four payers, so it carries the cross-payer comparison without dilution.
    ("aetna", "Aetna", "ustekinumab", "Stelara", "commercial", "html", "dedicated",
     "https://www.aetna.com/cpb/medical/data/900_999/0912.html"),
    ("anthem", "Anthem", "ustekinumab", "Stelara", "commercial", "pdf", "dedicated",
     "https://www.anthem.com/content/dam/digital/docs/pharmacy-information/clinical-criteria/ustekinumab.pdf"),
    ("cigna", "Cigna", "ustekinumab", "Stelara", "commercial", "pdf", "dedicated",
     "https://static.cigna.com/assets/chcp/pdf/coveragePolicies/pharmacy/ip_0686_coveragepositioncriteria_inflammatory_conditions_stelara_intravenous.pdf"),
    ("uhc", "UnitedHealthcare", "ustekinumab", "Stelara", "commercial", "pdf", "dedicated",
     "https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/ustekinumab.pdf"),

    # Nivolumab / Opdivo and atezolizumab / Tecentriq, for oncology depth.
    ("aetna", "Aetna", "nivolumab", "Opdivo", "commercial", "html", "dedicated",
     "https://www.aetna.com/cpb/medical/data/800_899/0892.html"),
    ("aetna", "Aetna", "nivolumab", "Opdivo", "medicare", "pdf", "dedicated",
     "https://www.aetna.com/content/dam/aetna/pdfs/aetnacom/healthcare-professionals/documents-forms/Opdivo-2345-A-Aetna-MedB.pdf"),
    ("aetna", "Aetna", "atezolizumab", "Tecentriq", "medicare", "pdf", "dedicated",
     "https://www.aetna.com/content/dam/aetna/pdfs/aetnacom/healthcare-professionals/documents-forms/Tecentriq-2132-A-Aetna-MedB.pdf"),
    ("anthem", "Anthem", "nivolumab", "Opdivo", "commercial", "pdf", "dedicated",
     "https://www.anthem.com/content/dam/digital/docs/pharmacy-information/clinical-criteria/Opdivo.pdf"),
    ("anthem", "Anthem", "atezolizumab", "Tecentriq", "commercial", "pdf", "dedicated",
     "https://www.anthem.com/content/dam/digital/docs/pharmacy-information/clinical-criteria/Atezolizumab.pdf"),

    # UHC's oncology policy. The seed maps it to trastuzumab, not pembrolizumab;
    # labelling it pembrolizumab would contradict the source data. Its text
    # covers 16 agents, so it is an omnibus regardless of the seed's URL sharing.
    ("uhc", "UnitedHealthcare", "trastuzumab", "Herceptin", "commercial", "pdf", "omnibus",
     "https://www.uhcprovider.com/content/dam/provider/docs/public/policies/comm-medical-drug/oncology-medication-clinical-coverage-policy.pdf"),
    ("uhc", "UnitedHealthcare", "trastuzumab", "Herceptin", "medicaid", "pdf", "omnibus",
     "https://www.uhcprovider.com/content/dam/provider/docs/public/policies/medicaid-comm-plan/oncology-medication-clinical-coverage-policy-cs.pdf"),
]

ALLOWED_HOSTS = {"www.aetna.com", "www.anthem.com", "static.cigna.com", "www.uhcprovider.com"}

# This venv has no brotli or zstd decoder installed, and httpx silently skips an
# encoding it cannot decode (SUPPORTED_DECODERS lookup, except KeyError: continue),
# so requesting "br" would hand back raw compressed bytes that read as a bad
# extraction rather than an error. Ask only for what can actually be decoded.
CHROME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

BLOCK_MARKERS = ("access denied", "request blocked", "incapsula incident id",
                 "captcha", "error 403", "page not found", "are you a human")

MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 200
AETNA_DELAY = 8  # Aetna tolerated this across two full runs; the envelope is ~5 req/min

# A duplicate key would silently overwrite an earlier document while both rows
# report PASS, and a fmt that disagrees with the URL means the row was mistyped.
assert len({(d[0], d[2], d[4]) for d in DOCS}) == len(DOCS), "duplicate payer/drug/plan_type"
assert all(d[7].rsplit(".", 1)[-1] == d[5] for d in DOCS), "fmt disagrees with URL extension"
assert all(d[6] in ("dedicated", "omnibus") for d in DOCS), "bad scope"


class Rejected(Exception):
    """A gate failed. The message is the reason shown in the summary table."""


def fetch(client: httpx.Client, url: str, fmt: str) -> bytes:
    """Gate A. Returns the body only if every check passes."""
    last = None
    for attempt in range(3):
        try:
            r = client.get(url)
            break
        # TransportError covers timeouts, connect/read errors AND ProtocolError.
        # RemoteProtocolError is what an HTTP/2 GOAWAY raises, which is the most
        # likely flaky failure against a WAF-fronted host and precisely what a
        # retry is for. DecodingError (corrupt gzip) is a sibling, not a subclass.
        # A status code is an answer, not a glitch, so it is never retried.
        except (httpx.TransportError, httpx.DecodingError) as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    else:
        raise Rejected(f"transport failed after 3 attempts: {type(last).__name__}: {last}")

    if r.status_code != 200:
        raise Rejected(f"HTTP {r.status_code}")
    # Check every hop, not just the final one. Client headers and the cookie jar
    # are replayed across redirects, so a chain that detours off-allowlist and
    # comes back would otherwise pass.
    for hop in (*r.history, r):
        if hop.url.host not in ALLOWED_HOSTS:
            raise Rejected(f"redirect hop off-allowlist: {hop.url.host}")

    body = r.content
    if len(body) > MAX_BYTES:
        raise Rejected(f"{len(body)} bytes exceeds 25MB ceiling")

    if fmt == "pdf":
        if body[:4] != b"%PDF":
            raise Rejected(f"not a PDF, starts {body[:8]!r}")
        if len(body) < 1024:
            raise Rejected(f"PDF only {len(body)} bytes")
    else:
        if len(body) < 5000:
            raise Rejected(f"HTML only {len(body)} bytes")
        head = body[:5000].lower()
        if b"<html" not in head and b"<!doctype" not in head:
            raise Rejected("no <html> or <!doctype> in first 5KB")

    sniff = body[:8192].lower()
    for marker in BLOCK_MARKERS:
        if marker.encode() in sniff:
            raise Rejected(f"block page detected: {marker!r}")

    return body


def extract(body: bytes, fmt: str, drug: str, brand: str) -> list[dict]:
    """Gate B. Returns page records only if the text looks like the real document."""
    if fmt == "pdf":
        pages = []
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            # pdf.pages is eager, so the slice bounds the extract_text() calls
            # (the expensive part) rather than page construction. The 25MB
            # ceiling is what bounds the parse itself.
            total = len(pdf.pages)
            for n, page in enumerate(pdf.pages[:MAX_PAGES], start=1):
                pages.append({"page": n, "text": page.extract_text() or ""})
                page.close()  # release pdfplumber's per-page cache
        if total > MAX_PAGES:
            print(f"    note: {total} pages, kept first {MAX_PAGES}")
    else:
        # A web page has no pagination, so it is one page and the citation is
        # the URL. Inventing page numbers would break source verification.
        # Pass raw bytes: trafilatura runs its own encoding detection on them.
        # Pre-decoding as UTF-8 skips that and turns any non-UTF-8 page into
        # U+FFFD replacement characters, which no gate here would catch.
        text = trafilatura.extract(body, include_tables=True, include_links=False,
                                   include_comments=False, output_format="txt")
        if not text:
            raise Rejected("trafilatura extracted nothing")
        pages = [{"page": 1, "text": text}]

    if not pages:
        raise Rejected("no pages")
    joined = "\n".join(p["text"] for p in pages)
    if len(joined) < 2000:
        raise Rejected(f"only {len(joined)} chars, likely scanned or blocked")
    if not any(len(p["text"]) >= 200 for p in pages):
        raise Rejected("no page has 200+ chars")
    # Proves the bytes are the document we think they are. Catches a redirect to
    # an index page, a retired-policy notice, and a mislabelled row.
    low = joined.lower()
    if drug.lower() not in low and brand.lower() not in low:
        raise Rejected(f"neither {drug!r} nor {brand!r} appears in the text")

    return pages


def main() -> int:
    CORPUS.mkdir(parents=True, exist_ok=True)
    results = []

    with httpx.Client(http2=True, headers=CHROME_HEADERS,
                      follow_redirects=True, timeout=30) as client:
        for payer, payer_name, drug, brand, plan_type, fmt, scope, url in DOCS:
            key = f"{payer}_{drug}_{plan_type}"
            out = CORPUS / f"{key}.json"
            print(f"--> {key} ({fmt})")
            if payer == "aetna":
                time.sleep(AETNA_DELAY)

            stage, reason = "fetch", None
            try:
                body = fetch(client, url, fmt)
                stage = "extract"
                pages = extract(body, fmt, drug, brand)
            except Rejected as exc:
                reason = str(exc)
            # A malformed PDF raises out of pdfminer, and an unretried transport
            # class raises out of httpx. Without this, one bad document aborts
            # the run and the remaining documents are never fetched.
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"

            if reason is not None:
                print(f"[{stage} error] {key}: {reason}")
                # Drop any file from an earlier run. Leaving it would ship a
                # stale document under a fresh-looking corpus.
                out.unlink(missing_ok=True)
                results.append((key, payer, 0, 0, f"FAIL: {reason}"))
                continue

            chars = sum(len(p["text"]) for p in pages)
            payload = json.dumps({
                "payer": payer, "payer_name": payer_name,
                "drug": drug, "brand_name": brand, "plan_type": plan_type,
                "doc_url": url,  # seed URL: stable across re-runs, unlike the redirect target
                "fmt": fmt,  # html is unpaginated, so its page 1 is not a citable page
                "scope": scope,  # omnibus means the drug label is not true of every chunk
                "source_sha256": hashlib.sha256(body).hexdigest(),
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "n_pages": len(pages), "pages": pages,
            }, indent=1, ensure_ascii=False)
            # Write-then-replace: a truncated JSON file in the committed corpus
            # would pass every gate here because no gate re-reads it.
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(out)
            results.append((key, payer, len(pages), chars, "PASS"))

    print(f"\n{'document':<40} {'pages':>6} {'chars':>9}  status")
    print("-" * 78)
    for key, _payer, pages, chars, status in results:
        print(f"{key:<40} {pages:>6} {chars:>9}  {status}")

    passed = [r for r in results if r[4] == "PASS"]
    payers = {r[1] for r in passed}
    print(f"\n{len(passed)}/{len(DOCS)} passed, {len(payers)} payers: {sorted(payers)}")

    if len(passed) < 8 or len(payers) < 2:
        print("[fetch error] below acceptance: needs 8+ documents and 2+ payers")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
