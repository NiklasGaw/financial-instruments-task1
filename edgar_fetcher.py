"""
SEC EDGAR data fetcher.
Handles: ticker -> CIK mapping, filing discovery, and filing text download.
"""
import requests
import json
import time
import os
import re
from pathlib import Path
from config import SEC_EDGAR_USER_AGENT, CACHE_DIR

HEADERS = {"User-Agent": SEC_EDGAR_USER_AGENT}


def get_cik_from_ticker(ticker: str) -> str:
    """Map a US ticker to its SEC CIK number."""
    cache_path = Path(CACHE_DIR) / "company_tickers.json"
    
    if cache_path.exists():
        with open(cache_path) as f:
            data = json.load(f)
    else:
        url = "https://www.sec.gov/files/company_tickers.json"
        resp = requests.get(url, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(data, f)
    
    # Build ticker -> CIK map. SEC's company_tickers.json uses inconsistent
    # delimiters (e.g. BRK-B with dash), so try both dash and dot variants.
    t = ticker.upper()
    ticker_variants = {t, t.replace("-", "."), t.replace(".", "-")}
    for entry in data.values():
        if entry["ticker"].upper() in ticker_variants:
            return str(entry["cik_str"]).zfill(10)
    
    raise ValueError(f"Ticker '{ticker}' not found in SEC EDGAR")


def get_filings_metadata(cik: str) -> dict:
    """Get all filing submissions for a CIK."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    time.sleep(0.11)  # SEC rate limit: 10 requests/sec
    return resp.json()


def _extract_filings_from_block(block: dict, cik: str, all_forms: set) -> list[dict]:
    """Pull annual/quarterly filings out of one SEC submissions block (recent or chunk)."""
    out: list[dict] = []
    if not block or "form" not in block:
        return out
    annual_forms = {"10-K", "20-F", "40-F"}
    for i in range(len(block["form"])):
        form = block["form"][i]
        if form not in all_forms:
            continue
        filing = {
            "form": form,
            "filing_date": block["filingDate"][i],
            "accession_number": block["accessionNumber"][i],
            "primary_document": block["primaryDocument"][i],
            "report_date": block.get("reportDate", [None] * len(block["form"]))[i],
            # The CIK this filing was actually filed under (matters for tickers
            # like BLK that reorganized: the SEC URL and XBRL companyfacts JSON
            # are both keyed on the original filer's CIK, not the successor's).
            "cik": cik,
        }
        filing["is_annual"] = form in annual_forms
        acc_no_dash = filing["accession_number"].replace("-", "")
        filing["url"] = (
            f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/"
            f"{acc_no_dash}/{filing['primary_document']}"
        )
        filing["index_url"] = (
            f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/"
            f"{acc_no_dash}/"
        )
        out.append(filing)
    return out


# Map current-entity CIK → list of predecessor-entity CIKs whose historical
# 10-K / 10-Q filings should also be considered the "same company" for
# multi-year output. Add new entries here as restructurings are discovered.
_PREDECESSOR_CIKS: dict[str, list[str]] = {
    # BlackRock restructured in 2024: the listed entity "BlackRock, Inc." became
    # a holding above the operating company "BlackRock Finance, Inc." (formerly
    # "BlackRock Inc."). Historical 10-K/10-Q filings live under the old CIK.
    "0002012383": ["0001364742"],
}


def _fetch_older_submissions_chunk(filename: str) -> dict:
    """Fetch one of the older paginated submission files referenced in filings.files[]."""
    url = f"https://data.sec.gov/submissions/{filename}"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    time.sleep(0.11)
    return resp.json()


def find_annual_and_quarterly_filings(cik: str, years: int = 3) -> list[dict]:
    """
    Find annual and quarterly filings for the last N years.
    Supports:
      - US companies: 10-K (annual), 10-Q (quarterly)
      - Foreign filers: 20-F (annual), 6-K (quarterly equivalent)
      - Canadian filers: 40-F (annual), 6-K (quarterly)

    SEC's submissions API returns the most recent ~1000 filings in
    `filings.recent` plus references to older paginated chunks in `filings.files`.
    For large filers (banks like BAC, JPM, MS, GS, C) the recent block is dominated
    by 8-K/13G/etc., so we paginate into `filings.files` until we satisfy the
    requested `years` × (1 annual + 4 quarterly) target.
    """
    ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
    QUARTERLY_FORMS = {"10-Q", "6-K"}
    ALL_FORMS = ANNUAL_FORMS | QUARTERLY_FORMS

    # Build the list of CIKs to walk: the requested one plus any predecessor
    # entities. Predecessor filings are URL-keyed by their original CIK so each
    # filing carries its own `cik` field.
    all_ciks = [cik] + _PREDECESSOR_CIKS.get(cik, [])

    need_annuals    = years
    need_quarterlies = years * 4

    def have_enough(fs):
        a = sum(1 for f in fs if f["is_annual"])
        q = sum(1 for f in fs if not f["is_annual"])
        return a >= need_annuals and q >= need_quarterlies

    filings: list[dict] = []
    for c in all_ciks:
        meta = get_filings_metadata(c)
        filings.extend(_extract_filings_from_block(meta["filings"]["recent"], c, ALL_FORMS))

        # Paginate into older filings if we still need more
        if not have_enough(filings):
            older = meta.get("filings", {}).get("files", [])
            for ref in older:
                try:
                    chunk = _fetch_older_submissions_chunk(ref["name"])
                except Exception as e:
                    print(f"  WARNING: could not fetch older filings chunk {ref['name']}: {e}")
                    continue
                filings.extend(_extract_filings_from_block(chunk, c, ALL_FORMS))
                if have_enough(filings):
                    break
        if have_enough(filings):
            break

    # Sort by date descending
    filings.sort(key=lambda x: x["filing_date"], reverse=True)

    # Filter: N annual + N*4 quarterly (last N years)
    annuals = [f for f in filings if f["is_annual"]][:years]
    quarterlies = [f for f in filings if not f["is_annual"]][:years * 4]

    result = annuals + quarterlies
    result.sort(key=lambda x: x["filing_date"], reverse=True)
    
    forms_found = set(f["form"] for f in result)
    print(f"  Filing types found: {forms_found}")
    
    return result


def _find_best_document_url(filing: dict) -> str | None:
    """
    When the primary_document is a cover page or XBRL viewer stub (i.e. the
    downloaded text is too short), scan the filing directory for the largest
    non-exhibit HTM file and return its URL.

    Handles two common SEC patterns:
      WFC: primary = wfc-20251231_d2.htm (cover), full 10-K = wfc-20251231.htm
      IBM: primary = ibm-20251231.htm (XBRL stub), full 10-K = ibm-20251231_d2.htm
    """
    try:
        index_url = filing.get("index_url", "")
        if not index_url:
            return None
        resp = requests.get(index_url, headers=HEADERS, timeout=10)
        # Pull all .htm filenames from the directory listing
        names = re.findall(r'([^\s\"<>/]+\.htm)', resp.text, re.IGNORECASE)
        # Exclude obvious exhibit/XBRL/viewer files
        _EXCLUDE = ("ex", "xbrl", "r1.", "r2.", "r3.", "r4.", "r5.", "defr", "def14")
        candidates = [
            n for n in set(names)
            if not any(x in n.lower() for x in _EXCLUDE)
            and n.lower() != filing.get("primary_document", "").lower()
        ]
        if not candidates:
            return None
        # Pick the largest by Content-Length
        best_name, best_size = None, 0
        for name in candidates:
            url = index_url + name
            try:
                h = requests.head(url, headers=HEADERS, timeout=5)
                size = int(h.headers.get("content-length", 0))
                if size > best_size:
                    best_size, best_name = size, name
            except Exception:
                pass
            time.sleep(0.05)
        if best_name and best_size > 80_000:
            return index_url + best_name
    except Exception:
        pass
    return None


def download_filing_text(filing: dict, cache_dir: str = CACHE_DIR) -> str:
    """
    Download the filing HTML/text and extract readable text.
    Caches to disk to avoid re-downloading.

    Falls back to the largest non-exhibit HTM in the filing directory when the
    primary_document turns out to be a cover page or XBRL viewer stub (< 80 000
    chars after html_to_text).  Handles WFC (_d2 = cover) and IBM (_d2 = full
    10-K) patterns without special-casing individual tickers.
    """
    safe_name = filing["accession_number"].replace("-", "_")
    form_type = filing["form"].replace("-", "")
    cache_path = Path(cache_dir) / f"{safe_name}_{form_type}.txt"

    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_text(encoding="utf-8")

    def _fetch(url: str) -> str:
        r = requests.get(url, headers=HEADERS)
        r.raise_for_status()
        time.sleep(0.11)
        return html_to_text(r.text)

    text = _fetch(filing["url"])

    # Check if document is a cover page / XBRL stub rather than the full filing.
    # Heuristics: too short (<150 K chars) OR very few financial numbers
    # (comma-formatted integers like 1,234 indicate actual data tables).
    def _looks_like_stub(t: str) -> bool:
        if len(t) < 150_000:
            return True
        num_count = len(re.findall(r'\b\d{1,3}(?:,\d{3})+', t))
        return num_count < 50  # real 10-K has hundreds of financial numbers

    # If result is suspiciously short, try the largest alternative document
    if _looks_like_stub(text):
        alt_url = _find_best_document_url(filing)
        if alt_url:
            print(f"    Primary doc too short ({len(text):,} chars) — trying {alt_url.split('/')[-1]}")
            alt_text = _fetch(alt_url)
            if len(alt_text) > len(text):
                text = alt_text

    os.makedirs(cache_dir, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")
    return text


def html_to_text(html: str) -> str:
    """Simple HTML to text conversion for SEC filings."""
    # Remove script and style tags
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Replace common HTML entities
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&#160;', ' ').replace('&rsquo;', "'").replace('&ldquo;', '"').replace('&rdquo;', '"')
    text = text.replace('&#8364;', '€').replace('&#163;', '£').replace('&#165;', '¥').replace('&#8361;', '₩')
    text = text.replace('&#8212;', '—').replace('&#8211;', '–').replace('&#8226;', '•')
    # Replace <br>, <p>, <div>, <tr> with newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(p|div|tr|li|h[1-6])>', '\n', text, flags=re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Clean up whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


def _score_windows(
    filing_text: str,
    keywords: tuple[str, ...],
    window: int,
    step: int,
    target: int,
    bonus_per_kw: int = 20,
) -> str:
    """
    Generic hybrid-scoring window extractor.  Shared by extract_is_section()
    and extract_segment_sections().

    score = number_density + keyword_bonus
      number_density — comma-formatted numbers per window (financial tables
                       have many; prose and TOC sections have few).
      keyword_bonus  — bonus per keyword hit inside the window.

    Algorithm: O(n) prefix sums → score every window → greedy non-overlapping
    selection by score (highest first) until `target` chars are accumulated →
    re-sort by document position → join with section-break markers.
    """
    import re as _re

    n = len(filing_text)
    if n == 0:
        return ""

    text_lower = filing_text.lower()

    # ── Number-density prefix sum ─────────────────────────────────────────────
    num_hits = [m.start() for m in _re.finditer(r"\b\d{1,3}(?:,\d{3})+", filing_text)]
    if not num_hits:
        return filing_text[:target]

    nd_prefix = [0] * (n + 1)
    for p in num_hits:
        if p < n:
            nd_prefix[p + 1] += 1
    for i in range(1, n + 1):
        nd_prefix[i] += nd_prefix[i - 1]

    # ── Keyword-bonus prefix sum ──────────────────────────────────────────────
    kw_pos: list[int] = []
    for kw in keywords:
        pos = 0
        while True:
            idx = text_lower.find(kw, pos)
            if idx == -1:
                break
            kw_pos.append(idx)
            pos = idx + len(kw)

    kw_prefix = [0] * (n + 1)
    for p in kw_pos:
        if p < n:
            kw_prefix[p + 1] += 1
    for i in range(1, n + 1):
        kw_prefix[i] += kw_prefix[i - 1]

    # ── Score + greedy selection ──────────────────────────────────────────────
    scored: list[tuple[int, int]] = []
    for start in range(0, max(1, n - window + 1), step):
        nd  = nd_prefix[min(n, start + window)] - nd_prefix[start]
        kwb = (kw_prefix[min(n, start + window)] - kw_prefix[start]) * bonus_per_kw
        scored.append((nd + kwb, start))

    scored.sort(reverse=True)
    selected: list[int] = []
    total_chars = 0
    for score, start in scored:
        if score == 0:
            break
        if not any(abs(start - s) < window for s in selected):
            selected.append(start)
            total_chars += window
            if total_chars >= target:
                break

    if not selected:
        return filing_text[:target]

    selected.sort()
    chunks = [filing_text[s: min(n, s + window)] for s in selected]
    return "\n\n---SECTION BREAK---\n\n".join(chunks)


# ── IS-specific keywords (tighter set → finds the P&L table, not TOC) ────────
_IS_KEYWORDS = (
    "consolidated statements of operations",
    "consolidated statements of income",
    "consolidated statement of earnings",
    "total net sales",       # AAPL
    "total net revenue",     # banks
    "total revenues",
    "net revenues",
    "gross profit",
    "gross margin",          # AAPL
    "cost of revenue",
    "cost of sales",         # AAPL
    "income from operations",
    "noninterest expense",   # banks
    "net interest income",   # banks
    "in millions",           # table unit header
    "in billions",
)

# ── Segment-specific keywords (broad → catches any revenue breakdown) ─────────
_SEG_KEYWORDS = (
    "in millions",
    "in billions",
    "€ millions",
    "disaggregated",
    "disaggregation of revenue",
    "reportable segment",
    "segment information",
    "significant product",
    "operating income",
    "net income",
    "total revenue",
    "total net revenue",
    "total net sales",
    "revenue by",
    "revenue by type",
    "net sales by",
    "net system sales",
    "segment result",
    "contracts with customers",
    "products and services",
    "product line",
    "business unit",
    "business segment",
)


# Exact section headers that mark the start of a consolidated income statement.
# Listed roughly most→least common.  Matched case-insensitively, first hit wins.
_IS_HEADERS = (
    "consolidated statements of operations",
    "consolidated statement of operations",
    "consolidated statements of income",
    "consolidated statement of income",
    "consolidated statements of earnings",
    "consolidated statement of earnings",
    "consolidated income statements",       # some 20-F filers (ASML, Philips)
    "consolidated income statement",
    "condensed consolidated statements of operations",
    "condensed consolidated statements of income",
    "group income statement",               # UK filers (Shell, HSBC)
    "group consolidated income statement",
    "statements of consolidated income",    # some bank filers (TD)
    "consolidated statements of net income",
)


def extract_is_section(filing_text: str) -> str:
    """
    Extract the consolidated income statement (≤ 10 000 chars).

    Strategy 1 — Header search (preferred):
      Scan for the exact section header (e.g. "Consolidated Statements of
      Operations") and return the 10 000 chars starting just before it.
      This is unambiguous: the header appears exactly once in any 10-K/20-F
      and marks the precise start of the IS table.

    Strategy 2 — Scoring fallback:
      If no header is found (unusual filing layouts), fall back to the
      keyword-scoring window extractor with a larger 10 000-char budget.
    """
    # Fix word-split artifacts from HTML/XBRL table-cell rendering in SEC filings
    # (e.g. "Stat ements" → "Statements", "Oper ations" → "Operations")
    filing_text = re.sub(r'\bStat\s+ements\b', 'Statements', filing_text, flags=re.IGNORECASE)
    filing_text = re.sub(r'\bOper\s+ations\b', 'Operations', filing_text, flags=re.IGNORECASE)

    text_lower = filing_text.lower()
    n = len(filing_text)

    # IS-specific content keywords used for scoring occurrences.
    # These appear in the actual IS table but not in auditor reports or
    # footnote references to the IS.
    _IS_CONTENT_KWS = (
        "total revenue", "total revenues", "net revenues", "net revenue",
        "total net sales", "total net revenue",
        "gross profit", "gross margin",
        "operating income", "income from operations",
        "net income", "net interest income", "noninterest expense",
        "cost of revenue", "cost of sales",
        "in millions", "in billions",
    )

    # Strategy 1: direct header search
    for header in _IS_HEADERS:
        pos, candidates = 0, []
        while True:
            hit = text_lower.find(header, pos)
            if hit == -1:
                break
            window = text_lower[hit: min(n, hit + 4000)]
            # Heavily weight IS content keywords; also count comma-numbers
            kw_score  = sum(window.count(kw) for kw in _IS_CONTENT_KWS) * 15
            num_score = len(re.findall(r'\b\d{1,3}(?:,\d{3})+',
                                       filing_text[hit: min(n, hit + 4000)]))
            raw_score = kw_score + num_score
            # A real section title starts at the beginning of a line.
            # Mid-sentence references (footnotes, prose) are preceded by word chars.
            # Apply 80% penalty so they can't outscore the actual IS table.
            before = text_lower[max(0, hit - 30): hit]
            if not re.search(r'\n\s*$', before):
                raw_score = int(raw_score * 0.2)
            candidates.append((raw_score, hit))
            pos = hit + len(header)
        if not candidates:
            continue
        candidates.sort(reverse=True)
        best_score, best_idx = candidates[0]
        if best_score < 5:
            continue  # header only in references/footnotes, no actual IS content
        start = max(0, best_idx - 100)
        return filing_text[start: min(n, start + 10000)]

    # Strategy 2: scoring fallback (wider budget than before to avoid MD&A hits)
    return _score_windows(
        filing_text,
        keywords=_IS_KEYWORDS,
        window=4000,
        step=600,
        target=10000,
        bonus_per_kw=50,   # very high bonus so IS keywords dominate over MD&A prose
    )


def _extract_table_blocks(text: str, keywords: tuple, max_chars: int = 10000) -> str:
    """
    Find financial table blocks in converted-HTML text and return those near
    segment keywords.

    Detection logic:
      - "numeric" line: ≥ 1 comma-formatted number (e.g. 47,529)
      - "strongly numeric": ≥ 2 comma-formatted numbers
      - Table block: run where up to 2 consecutive non-numeric gap lines are
        tolerated (handles rows like "Professional Viz  384  409  544" whose
        values are all < 1 000 and lack commas), plus:
          ≥ 4 numeric lines in the block AND ≥ 2 strongly numeric lines.
      - Only kept when any segment keyword falls within 2 000 chars of the block.
    """
    import re as _re
    _num_pat = _re.compile(r'\b\d{1,3}(?:,\d{3})+')
    text_lower = text.lower()

    # Collect keyword character positions
    kw_positions: list[int] = []
    for kw in keywords:
        p = 0
        while True:
            idx = text_lower.find(kw, p)
            if idx == -1:
                break
            kw_positions.append(idx)
            p = idx + 1

    if not kw_positions:
        return ""

    # Precompute line start character positions
    lines = text.split('\n')
    line_starts: list[int] = []
    pos = 0
    for line in lines:
        line_starts.append(pos)
        pos += len(line) + 1

    counts = [len(_num_pat.findall(line)) for line in lines]
    is_numeric        = [c >= 1 for c in counts]
    is_strongly_num   = [c >= 2 for c in counts]

    collected: list[str] = []
    total_chars = 0
    i = 0
    while i < len(lines) and total_chars < max_chars:
        if not is_numeric[i]:
            i += 1
            continue

        # Extend run allowing up to 2 consecutive non-numeric gap lines
        j, gap = i, 0
        while j < len(lines):
            if is_numeric[j]:
                gap = 0
                j += 1
            elif gap < 2:
                gap += 1
                j += 1
            else:
                break

        # Trim trailing gap lines back to last numeric line
        while j > i and not is_numeric[j - 1]:
            j -= 1

        numeric_count = sum(1 for k in range(i, j) if is_numeric[k])
        strong_count  = sum(1 for k in range(i, j) if is_strongly_num[k])

        if numeric_count >= 4 and strong_count >= 2:
            block_start = max(0, i - 5)
            block_end   = min(len(lines), j)
            char_lo = line_starts[block_start]
            char_hi = line_starts[block_end - 1] + len(lines[block_end - 1])
            if any(char_lo - 2000 <= kp <= char_hi + 2000 for kp in kw_positions):
                block = '\n'.join(lines[block_start:block_end])
                collected.append(block)
                total_chars += len(block)

        i = j if j > i else i + 1

    return ("\n\n---TABLE BREAK---\n\n".join(collected))[:max_chars]


# ── MD&A / management commentary keywords ────────────────────────────────────
# Used to pull Management's Discussion and Analysis (10-K Item 7, 10-Q Item 2,
# 20-F Item 5, 6-K interim equivalent, IFRS annual "Operating and Financial
# Review"). The MD&A is the narrative section where management explains
# strategy, market dynamics, capital allocation, and outlook — i.e. the
# qualitative companion to the IS / segment tables.
_MDA_KEYWORDS = (
    "management's discussion and analysis",
    "managements discussion and analysis",
    "management discussion and analysis",
    "operating and financial review",
    "financial review",
    "results of operations",
    "business overview",
    "strategy",
    "outlook",
    "business strategy",
    "competitive position",
    "operating environment",
    "capital allocation",
    "key drivers",
    "growth strategy",
    "we believe",       # common MD&A voice marker
    "our strategy",
    "we expect",
)


# Section-title anchors for MD&A. First-hit wins, case-insensitive.
_MDA_HEADERS = (
    "item 7. management's discussion and analysis",
    "item 7 management's discussion and analysis",
    "item 7. managements discussion and analysis",
    "item 2. management's discussion and analysis",
    "item 2 management's discussion and analysis",
    "item 5. operating and financial review",
    "item 5 operating and financial review",
    "item 5: operating and financial review",
    "operating and financial review and prospects",
    "management's discussion and analysis",
    "operating and financial review",
)


def extract_mda_section(filing_text: str, target: int = 30000) -> str:
    """
    Extract the Management's Discussion and Analysis section (~30 K chars).

    Strategy parallels extract_is_section():
      1. Try the exact section-title anchors first (most reliable).
      2. Fall back to keyword-scored windows.
    """
    text_lower = filing_text.lower()
    n = len(filing_text)

    # Strategy 1: exact MD&A title match
    for header in _MDA_HEADERS:
        hit = text_lower.find(header)
        if hit == -1:
            continue
        # Make sure it's not just a TOC reference. Real MD&A sections are
        # followed by long body text (≥ 5 000 chars before the next "Item X").
        body_start = hit
        # Look for the NEXT "item N" header to bound the section
        item_pat = re.compile(r"\bitem\s+\d+[a-z]?\.", re.IGNORECASE)
        m = item_pat.search(filing_text, body_start + len(header))
        body_end = m.start() if m else min(n, body_start + target + 5000)
        body_len = body_end - body_start
        if body_len < 5000:
            continue   # TOC entry — keep searching
        return filing_text[body_start: min(n, body_start + target + 10000)]

    # Strategy 2: keyword-window scoring fallback
    return _score_windows(
        filing_text,
        keywords=_MDA_KEYWORDS,
        window=8000,
        step=1000,
        target=target,
        bonus_per_kw=30,
    )


def extract_segment_sections(filing_text: str, target: int = 20000) -> str:
    """
    Extract segment-relevant text for LLM Pass 2.

    Two-layer strategy (tables first, narrative second):
      1. Table blocks — runs of dense comma-number rows near segment keywords.
         These are the most reliable source of segment revenue figures.
      2. Keyword-scored narrative windows — MD&A and footnote discussion that
         fills the remaining budget after tables are placed.

    Total output ≤ target + 10 000 chars (tables are prepended on top of the
    keyword-window budget so neither crowds out the other).
    Pass `target=30000` on retry for empty-segment filings.
    """
    table_text = _extract_table_blocks(filing_text, _SEG_KEYWORDS, max_chars=10000)
    narrative  = _score_windows(
        filing_text,
        keywords=_SEG_KEYWORDS,
        window=8000,
        step=1000,
        target=target,
        bonus_per_kw=20,
    )
    if table_text:
        return (table_text + "\n\n---SECTION BREAK---\n\n" + narrative)[:target + 10000]
    return narrative


# ---- Quick test ----
if __name__ == "__main__":
    print("Testing with MSFT...")
    cik = get_cik_from_ticker("MSFT")
    print(f"CIK for MSFT: {cik}")
    
    filings = find_annual_and_quarterly_filings(cik, years=3)
    print(f"\nFound {len(filings)} filings:")
    for f in filings:
        print(f"  {f['form']:5s} | {f['filing_date']} | {f['report_date']}")
    
    # Download the most recent 10-K
    if filings:
        latest_10k = next((f for f in filings if f["form"] == "10-K"), None)
        if latest_10k:
            print(f"\nDownloading latest 10-K ({latest_10k['filing_date']})...")
            text = download_filing_text(latest_10k)
            print(f"Filing text length: {len(text):,} chars")
            
            segments = extract_segment_sections(text)
            print(f"Extracted segment sections: {len(segments):,} chars")
            print(f"\nFirst 500 chars of extracted segments:\n{segments[:500]}")
