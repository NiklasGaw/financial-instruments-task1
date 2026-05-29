"""
PDF annual report handler for international stocks that don't file on SEC EDGAR.

Workflow:
1. PDFs with known URLs are auto-downloaded into data/annual_reports/<TICKER>/
2. For others, user places PDFs there manually.
3. This module extracts text and sends it to the LLM for extraction.

For 7 stocks: Samsung, SK Hynix, LVMH, Nestlé, Allianz, Siemens, CBA
"""
import os
import re
import time
import requests
from pathlib import Path
from config import DATA_DIR, CACHE_DIR

REPORTS_DIR = os.path.join(DATA_DIR, "annual_reports")


def get_report_dir(ticker: str) -> Path:
    """Get/create the directory where PDF reports should be placed."""
    safe_ticker = ticker.replace(".", "_")
    report_dir = Path(REPORTS_DIR) / safe_ticker
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir


def auto_download_pdfs(ticker: str, years: int = 3) -> None:
    """
    Download annual + quarterly report PDFs for tickers that have known URLs.

    pdf_urls keys can be:
      - "2024"      → annual,    saved as {ticker}_annual_2024.pdf
      - "2024-Q1"   → quarterly, saved as {ticker}_Q1_2024.pdf
      - "2024-H1"   → half-year, saved as {ticker}_H1_2024.pdf
      - "2024-9M"   → 9-month trading update, saved as {ticker}_9M_2024.pdf
    Skips files already on disk. Limits annuals to most-recent `years`,
    quarterlies/interims to most-recent `years * 4`.
    """
    from intl_mapping import NO_EDGAR_STOCKS
    info = NO_EDGAR_STOCKS.get(ticker, {})
    pdf_urls: dict[str, str] = info.get("pdf_urls", {})
    if not pdf_urls:
        return

    report_dir = get_report_dir(ticker)
    safe_ticker = ticker.replace(".", "_")

    # Split annual vs quarterly/half-year/9M keys
    annual_keys, period_keys = [], []
    period_pat = re.compile(r"^(\d{4})-(Q[1-4]|H[12]|9M)$", re.IGNORECASE)
    for key in pdf_urls:
        if period_pat.match(key):
            period_keys.append(key)
        elif re.fullmatch(r"\d{4}", key):
            annual_keys.append(key)

    targets = []  # list of (label, dest_filename, url)
    for key in sorted(annual_keys, reverse=True)[:years]:
        targets.append((f"{key} annual",
                        f"{safe_ticker}_annual_{key}.pdf",
                        pdf_urls[key]))
    for key in sorted(period_keys, reverse=True)[:years * 4]:
        year, period = period_pat.match(key).group(1, 2)
        period = period.upper()
        targets.append((f"{year} {period}",
                        f"{safe_ticker}_{period}_{year}.pdf",
                        pdf_urls[key]))

    for label, filename, url in targets:
        dest = report_dir / filename
        if dest.exists() and dest.stat().st_size > 10_000:
            continue
        print(f"    Downloading {ticker} {label} report…")
        try:
            resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            print(f"    Saved {dest.name} ({len(resp.content):,} bytes)")
            time.sleep(0.5)
        except Exception as e:
            print(f"    WARNING: could not download {label} report: {e}")


def find_pdf_reports(ticker: str) -> list[dict]:
    """
    Find PDF annual/quarterly reports in the data/annual_reports/<TICKER>/ folder.
    
    Expected naming convention:
      <TICKER>_annual_2024.pdf
      <TICKER>_annual_2023.pdf
      <TICKER>_annual_2022.pdf
      <TICKER>_Q1_2024.pdf  (optional quarterly)
    
    But also accepts any PDF in the folder.
    """
    report_dir = get_report_dir(ticker)
    pdfs = sorted(report_dir.glob("*.pdf"), reverse=True)
    
    reports = []
    for pdf_path in pdfs:
        name = pdf_path.stem.lower()
        
        # Try to parse period info from filename
        is_annual = "annual" in name or "10-k" in name or "20-f" in name or "yearly" in name
        year_match      = re.search(r"20\d{2}", name)
        quarter_match   = re.search(r"_q([1-4])_", name, re.IGNORECASE) or re.search(r"q([1-4])", name, re.IGNORECASE)
        halfyear_match  = re.search(r"_h([12])_", name, re.IGNORECASE)
        ninemonth_match = re.search(r"_9m_|_9-month_|_nine-month_", name, re.IGNORECASE)

        report = {
            "path": str(pdf_path),
            "filename": pdf_path.name,
            "is_annual": is_annual or (not quarter_match and not halfyear_match and not ninemonth_match),
            "year": year_match.group() if year_match else None,
            "quarter": quarter_match.group(1) if quarter_match else None,
        }

        if report["is_annual"]:
            report["form"] = "Annual Report"
            report["fiscal_period"] = "FY"
        elif ninemonth_match:
            # 9-month YTD update (typical for European Q3 sales press releases)
            report["form"]          = "9-Month Trading Update"
            report["fiscal_period"] = "9M"
        elif halfyear_match:
            # Half-year report: H1 = cumulative Jan-Jun, H2 = cumulative Jul-Dec (or fiscal equiv)
            # Kept as a distinct fiscal_period so synthesize_missing_periods can later
            # compute standalone Q2 = H1 − Q1 (when Q1 exists).
            half = halfyear_match.group(1)
            report["form"]          = "Half-Year Report"
            report["fiscal_period"] = f"H{half}"
        else:
            report["form"] = "Quarterly Report"
            report["fiscal_period"] = f"Q{report['quarter']}"
        
        report["fiscal_year"] = f"FY{report['year']}" if report["year"] else "Unknown"
        reports.append(report)
    
    return reports


def find_is_section_in_pdf(pdf_path: str) -> str:
    """
    Scan the PDF page-by-page to locate the consolidated income statement table
    and return its text (~10K chars).  Stops as soon as the IS is found so we
    don't read all 400+ pages for a single section.

    Detects the IS page by looking for IS-table keywords (Gross margin, Cost of
    sales, etc.) and then returns text from that page + the next two pages.
    """
    # Require an IS-section header OR at least 2 of these expense line keywords on the same page
    _IS_HEADERS = (
        "consolidated income statement",
        "consolidated statement of income",
        "consolidated statements of income",
        "consolidated statements of operations",
        "group income statement",
        "income statement",
    )
    _IS_EXPENSE_KWS = (
        "cost of sales",
        "cost of goods sold",
        "gross margin",
        "marketing and selling",
        "selling, general",
        "general and administrative",
        "total operating expenses",
        "operating expenses",
    )
    import re as _re
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            pages_text = [p.extract_text() or "" for p in pdf.pages]

        def _chunk(i: int) -> str:
            c = pages_text[i]
            for j in range(i + 1, min(n, i + 3)):
                c += "\n\n" + pages_text[j]
            return c[:15000]

        # Pass 1 — standalone IS section header (most reliable; skips MD&A commentary pages)
        for i, t in enumerate(pages_text):
            t_lower = t.lower()
            has_header = any(
                _re.search(r'(?:^|\n)\s*' + _re.escape(h), t_lower)
                for h in _IS_HEADERS
            )
            if has_header and "revenue" in t_lower:
                comma_nums = len(_re.findall(r'\b\d{1,3}(?:,\d{3})+', t))
                if comma_nums >= 5:
                    return _chunk(i)

        # Pass 2 — fallback: page with ≥2 expense line-item keywords
        for i, t in enumerate(pages_text):
            t_lower = t.lower()
            expense_hits = sum(1 for kw in _IS_EXPENSE_KWS if kw in t_lower)
            if expense_hits >= 2 and "revenue" in t_lower:
                comma_nums = len(_re.findall(r'\b\d{1,3}(?:,\d{3})+', t))
                if comma_nums >= 5:
                    return _chunk(i)
    except Exception:
        pass
    return ""


def extract_text_from_pdf(pdf_path: str, max_pages: int = 150) -> str:
    """
    Extract text from a PDF file.
    Uses pdfplumber (better for tables) with PyPDF2 fallback.
    """
    text = ""
    
    # Try pdfplumber first (better at table extraction)
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            pages = pdf.pages[:max_pages]
            for page in pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n\n"
                    
                # Also extract tables
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row:
                            text += " | ".join(str(cell) if cell else "" for cell in row) + "\n"
                    text += "\n"
        
        if text.strip():
            return text
    except ImportError:
        pass
    except Exception as e:
        print(f"  pdfplumber failed: {e}, trying PyPDF2...")
    
    # Fallback to PyPDF2
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        pages = reader.pages[:max_pages]
        for page in pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n\n"
    except ImportError:
        print("  ERROR: Neither pdfplumber nor PyPDF2 installed!")
        print("  Install with: pip install pdfplumber PyPDF2")
        return ""
    except Exception as e:
        print(f"  PyPDF2 also failed: {e}")
        return ""
    
    return text


def extract_mda_sections_from_pdf(full_text: str, target: int = 30000) -> str:
    """
    Extract MD&A / management commentary sections from an international PDF
    annual or interim report. International filers use varied terminology;
    we pick up multiple phrasings and dedupe overlapping windows.
    """
    text_lower = full_text.lower()
    keywords = [
        # SEC-style equivalents that some IFRS filers also use
        "management's discussion and analysis", "management discussion and analysis",
        "operating and financial review", "results of operations",
        # IFRS / European narrative anchors
        "business review", "operating review", "strategic report",
        "ceo's review", "chief executive's review", "management report",
        "letter to shareholders", "year in review",
        # Strategy / outlook commentary
        "our strategy", "business strategy", "growth strategy",
        "capital allocation", "competitive position", "market position",
        "outlook", "guidance", "key drivers",
        # Per-segment narrative
        "segment overview", "performance review",
    ]
    chunks: list[str] = []
    seen_offsets: list[int] = []
    for keyword in keywords:
        start = 0
        while True:
            idx = text_lower.find(keyword, start)
            if idx == -1:
                break
            # Skip if we already grabbed a window near this offset
            if any(abs(idx - so) < 4000 for so in seen_offsets):
                start = idx + len(keyword)
                continue
            seen_offsets.append(idx)
            chunk_start = max(0, idx - 300)
            chunk_end = min(len(full_text), idx + 6000)
            chunks.append(full_text[chunk_start:chunk_end])
            start = idx + len(keyword)
            if sum(len(c) for c in chunks) >= target:
                break
        if sum(len(c) for c in chunks) >= target:
            break
    if chunks:
        return "\n\n---SECTION BREAK---\n\n".join(chunks)[:target + 10000]
    # Fallback: middle slice of the document (TOC + cover often live at the
    # front, financial statements at the back — narrative tends to be middle).
    mid = len(full_text) // 2
    return full_text[max(0, mid - target // 2): mid + target // 2]


def extract_segment_sections_from_pdf(full_text: str) -> str:
    """
    Extract the most relevant sections for segment analysis from PDF text.
    International reports use different terminology than SEC filings.
    """
    text_lower = full_text.lower()
    
    keywords = [
        # Segment reporting
        "segment information", "operating segments", "business segments",
        "segment reporting", "reportable segments", "segment results",
        "divisional performance", "business divisions",
        # Revenue breakdown
        "revenue by segment", "revenue by division", "revenue by business",
        "disaggregated revenue", "revenue breakdown", "revenue analysis",
        "sales by segment", "sales by division", "net sales by",
        # Results discussion
        "results of operations", "financial review", "operating review",
        "business review", "management discussion", "performance overview",
        # Specific to some companies
        "income by segment", "profit by segment", "contribution by segment",
        "divisional results", "group results",
    ]
    
    chunks = []
    for keyword in keywords:
        start = 0
        while True:
            idx = text_lower.find(keyword, start)
            if idx == -1:
                break
            chunk_start = max(0, idx - 300)
            chunk_end = min(len(full_text), idx + 6000)
            chunks.append(full_text[chunk_start:chunk_end])
            start = idx + len(keyword)
    
    if chunks:
        combined = "\n\n---SECTION BREAK---\n\n".join(chunks)
        return combined[:60000]
    
    # Fallback: return first 40k chars
    return full_text[:40000]


def process_pdf_reports(ticker: str, company: str, years: int = 3) -> list[dict]:
    """
    Process PDF annual reports for a non-EDGAR international stock.
    Auto-downloads PDFs when known URLs are configured in intl_mapping.
    Returns list of extracted segment data dicts.
    """
    from llm_extractor import extract_segment_data, extract_revenue_only_data
    from intl_mapping import NO_EDGAR_STOCKS

    auto_download_pdfs(ticker, years=years)
    reports = find_pdf_reports(ticker)

    if not reports:
        print(f"  No PDF reports found in {get_report_dir(ticker)}")
        print(f"  Please download annual reports and place them there.")
        print(f"  Naming convention: {ticker.replace('.','_')}_annual_2024.pdf")
        return []

    print(f"  Found {len(reports)} PDF reports")

    # Build segment template from the most-recent annual report so that
    # revenue-only extractions reuse the same segment names.
    revenue_only_periods = set(
        p.upper() for p in NO_EDGAR_STOCKS.get(ticker, {}).get("revenue_only_periods", [])
    )

    all_extracted = []

    for report in reports:
        print(f"\n  Processing {report['filename']}...")
        
        # Check cache
        cache_key = f"{ticker.replace('.','_')}_{report['filename'].replace('.pdf','')}_extracted.json"
        cache_path = Path(CACHE_DIR) / "extractions" / cache_key
        
        if cache_path.exists():
            import json
            print(f"    Using cached extraction")
            with open(cache_path) as f:
                extracted = json.load(f)
            # Override fiscal_period from filename — authoritative for PDF reports
            if report.get("fiscal_period"):
                extracted["fiscal_period"] = report["fiscal_period"]
            if report.get("fiscal_year") and not extracted.get("fiscal_year"):
                extracted["fiscal_year"] = report["fiscal_year"]
            # Enforce accounting identities (parallels the EDGAR path in main.py).
            # Without this, bank IS structures (no gross profit, OpEx-only TOE)
            # never get the no-gp reconciliation that backs out the missing ECL
            # line from TR − OI = TOE.
            from validate_data import validate_and_fix
            extracted = validate_and_fix(extracted)
            all_extracted.append(extracted)
            continue
        
        # Extract text from PDF (first ~150 pages — covers business review + segment tables)
        print(f"    Extracting text from PDF...")
        full_text = extract_text_from_pdf(report["path"])

        if not full_text:
            print(f"    WARNING: Could not extract text from {report['filename']}")
            continue

        print(f"    Full text: {len(full_text):,} chars")

        # Extract IS section by scanning all pages for the IS table
        is_text = find_is_section_in_pdf(report["path"])
        if is_text:
            print(f"    IS section: {len(is_text):,} chars (page-scan)")
        else:
            print(f"    IS section: not found via page-scan, using full text for Pass 1")

        # Extract segment-relevant sections
        segment_text = extract_segment_sections_from_pdf(full_text)
        print(f"    Segment sections: {len(segment_text):,} chars")

        # LLM extraction
        report_date = f"{report['year']}-12-31" if report["year"] else "Unknown"

        # Decide path: full extraction or lightweight revenue-only
        report_period = (report.get("fiscal_period") or "").upper()
        use_rev_only = report_period in revenue_only_periods

        try:
            if use_rev_only:
                # Pull the segment-name template from an already-extracted annual (if any)
                template_seg_names = None
                for d in all_extracted:
                    if (d.get("fiscal_period") or "").upper() == "FY" and d.get("segments"):
                        template_seg_names = [
                            {"name": s["name"], "sub_segments": []}
                            for s in d["segments"] if s.get("name")
                        ]
                        break
                extracted = extract_revenue_only_data(
                    filing_text=segment_text,
                    ticker=ticker, company=company,
                    form_type=report["form"], report_date=report_date,
                    template_segments=template_seg_names,
                )
            else:
                extracted = extract_segment_data(
                    filing_text=segment_text,
                    is_text=is_text if is_text else None,
                    ticker=ticker,
                    company=company,
                    form_type=report["form"],
                    report_date=report_date,
                )

            # Override fiscal_period from filename — authoritative for PDF reports
            if report.get("fiscal_period"):
                extracted["fiscal_period"] = report["fiscal_period"]
            if report.get("fiscal_year") and not extracted.get("fiscal_year"):
                extracted["fiscal_year"] = report["fiscal_year"]

            # Cache
            import json
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump(extracted, f, indent=2)

            all_extracted.append(extracted)
            print(f"    Extracted {len(extracted.get('segments', []))} segments")
            
        except Exception as e:
            print(f"    ERROR during LLM extraction: {e}")
    
    return all_extracted


def print_download_instructions():
    """Print instructions for downloading annual reports for non-EDGAR stocks."""
    from intl_mapping import NO_EDGAR_STOCKS
    
    print("\n" + "=" * 70)
    print("MANUAL DOWNLOAD REQUIRED: International Annual Reports")
    print("=" * 70)
    print()
    print("The following stocks do not file on SEC EDGAR.")
    print("Please download their English annual reports as PDFs")
    print("and place them in the specified folders.")
    print()
    print("Download the last 3 years of annual reports for each company.")
    print("Naming convention: <TICKER>_annual_<YEAR>.pdf")
    print()
    
    for ticker, info in NO_EDGAR_STOCKS.items():
        safe_ticker = ticker.replace(".", "_")
        report_dir = os.path.join(REPORTS_DIR, safe_ticker)
        print(f"  {ticker} ({info['company']}):")
        print(f"    Download from: {info['ir_url']}")
        print(f"    Place PDFs in: {report_dir}/")
        print(f"    Example files:")
        print(f"      {safe_ticker}_annual_2024.pdf")
        print(f"      {safe_ticker}_annual_2023.pdf")
        print(f"      {safe_ticker}_annual_2022.pdf")
        if info.get("notes"):
            print(f"    Note: {info['notes']}")
        print()


if __name__ == "__main__":
    print_download_instructions()
