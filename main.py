"""
Main pipeline for Task 1.
Orchestrates: stock list -> data fetching -> LLM extraction -> Sankey charts + summaries

Three data paths:
  1. US stocks (10-K / 10-Q on SEC EDGAR)
  2. International stocks that file on EDGAR (20-F / 40-F / 6-K)
  3. International stocks with NO EDGAR filing (PDF annual reports)
"""
import json
import os
import sys
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime

from config import DATA_DIR, CACHE_DIR, OUTPUT_DIR, STOCK_LIST_PATH
from edgar_fetcher import (
    get_cik_from_ticker,
    find_annual_and_quarterly_filings,
    download_filing_text,
    extract_is_section,
    extract_segment_sections,
)
from llm_extractor import (
    extract_segment_data,
    retry_segment_extraction,
    generate_business_summary,
    build_batch_requests,
    submit_batch_requests,
    wait_for_batch,
    parse_batch_results,
)
from validate_data import (
    validate_and_fix,
    validate_company_consistency,
    synthesize_missing_q4,
    derive_fiscal_period,
)
from sankey_generator import build_sankey_chart
from intl_mapping import (
    get_edgar_ticker,
    needs_pdf_download,
    get_ir_info,
    US_LISTED_FOREIGN,
    INTL_TO_EDGAR_TICKER,
    NO_EDGAR_STOCKS,
)
from pdf_handler import process_pdf_reports, print_download_instructions


# ============================================================
# Segment Template Cache
# ============================================================

_TEMPLATE_DIR = Path(CACHE_DIR) / "templates"


def _make_template(segments: list[dict]) -> list[dict]:
    """Reduce a full segment list to name-only structure for the quarterly prompt."""
    return [
        {
            "name": s["name"],
            "sub_segments": [
                ss["name"]
                for ss in (s.get("sub_segments") or [])
                if ss.get("name") and ss.get("revenue")
            ],
        }
        for s in segments
        if s.get("name") and s.get("revenue")
    ]


def _make_union_template(segments_lists: list[list[dict]]) -> list[dict]:
    """Union of segment names + sub-segment names across multiple annual extractions.
    First-seen ordering is preserved so the most-recent annual's segments come first.
    Case-insensitive de-dup, but the original casing of the first occurrence is kept."""
    seen_names: dict[str, dict] = {}
    for segments in segments_lists:
        for s in (segments or []):
            name = (s.get("name") or "").strip()
            if not name or not s.get("revenue"):
                continue
            key = name.lower()
            sub_names = [
                ss["name"] for ss in (s.get("sub_segments") or [])
                if ss.get("name") and ss.get("revenue")
            ]
            if key not in seen_names:
                seen_names[key] = {"name": name, "sub_segments": list(sub_names)}
            else:
                # Merge sub-segments by lowercase
                existing = {ss.lower(): ss for ss in seen_names[key]["sub_segments"]}
                for sn in sub_names:
                    if sn.lower() not in existing:
                        seen_names[key]["sub_segments"].append(sn)
    return list(seen_names.values())


def _save_segment_template(ticker: str, template_segments: list[dict], source_label: str) -> None:
    _TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    template = {
        "ticker":   ticker,
        "source":   source_label,
        "segments": template_segments,
    }
    path = _TEMPLATE_DIR / f"{ticker.replace('.', '_')}_template.json"
    path.write_text(json.dumps(template, indent=2))
    print(f"    Segment template saved ({len(template_segments)} segments, source: {source_label})")


def _load_segment_template(ticker: str) -> list[dict] | None:
    path = _TEMPLATE_DIR / f"{ticker.replace('.', '_')}_template.json"
    if path.exists():
        try:
            return json.loads(path.read_text()).get("segments")
        except Exception:
            pass
    return None


# ============================================================
# MD&A loader (for business_summary generation)
# ============================================================

def _load_mda_for_ticker(ticker: str, valid_data: list[dict]) -> str:
    """Pull the Management's Discussion and Analysis text from this ticker's
    most-recent annual filing source (cached EDGAR text or PDF). Returns an
    empty string if the source can't be located.

    Picks the most recent FY entry as the anchor — its source filing is the
    annual (10-K / 20-F / annual PDF) which contains the richest MD&A.
    """
    from edgar_fetcher import extract_mda_section
    # Prefer the most recent annual (FY) entry as the MD&A source
    annuals = [d for d in valid_data if (d.get("fiscal_period") or "").upper() == "FY"]
    if not annuals:
        return ""
    annuals.sort(key=lambda d: d.get("report_date", ""), reverse=True)
    anchor = annuals[0]
    form = (anchor.get("form_type") or "").lower()

    # PDF path: report dict had a "path" key pointing to the PDF file
    if "pdf" in form or "(comparative)" in form or "annual report" in form:
        from pdf_handler import find_pdf_reports, extract_text_from_pdf, extract_mda_sections_from_pdf
        target_year = (anchor.get("report_date", "") or "")[:4]
        for r in find_pdf_reports(ticker):
            if r.get("fiscal_period", "").upper() != "FY":
                continue
            if str(r.get("year") or "") == target_year:
                txt = extract_text_from_pdf(r["path"])
                return extract_mda_sections_from_pdf(txt) if txt else ""
        return ""

    # EDGAR path: look up the cached filing text by accession number, which is
    # embedded in the extraction cache filename. The valid_data dicts don't
    # carry their accession, but the cached extraction filename does.
    from config import CACHE_DIR
    from pathlib import Path
    ext_dir = Path(CACHE_DIR) / "extractions"
    cache_dir = Path(CACHE_DIR)
    safe_t = ticker.replace(".", "_")
    rd = anchor.get("report_date", "")
    # Walk extractions matching this ticker + FY date; find which accession was used
    for f in ext_dir.glob(f"{safe_t}_*_extracted.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if d.get("report_date") == rd and (d.get("fiscal_period") or "").upper() == "FY":
            # Derive accession from filename: TICKER_<accn-parts>_extracted.json
            stem = f.stem.replace("_extracted", "").replace(f"{safe_t}_", "", 1)
            for ext in ("_10K", "_20F", "_40F", "_10Q", "_6K"):
                src = cache_dir / f"{stem}{ext}.txt"
                if src.exists():
                    return extract_mda_section(src.read_text(errors="ignore"))
    return ""


# ============================================================
# Stock Classification
# ============================================================

def classify_stock(ticker: str) -> str:
    """
    Classify a stock into one of three data paths:
      'edgar_us'    -> US company, files 10-K / 10-Q
      'edgar_intl'  -> International company that files 20-F / 40-F / 6-K on EDGAR
      'pdf'         -> International company with NO EDGAR filing, needs PDF reports
    """
    if needs_pdf_download(ticker):
        return "pdf"
    
    if get_edgar_ticker(ticker) is not None:
        return "edgar_intl"
    
    # Check for exchange suffix → if present and not mapped, it's an edge case
    if "." in ticker:
        suffix = ticker.split(".")[-1]
        if suffix not in ("A", "B"):  # BRK.B is US
            # Unknown international stock — default to PDF path
            return "pdf"
    
    return "edgar_us"


# ============================================================
# EDGAR Processing (US + International filers)
# ============================================================

def process_edgar_stock(ticker: str, company: str, edgar_ticker: str = None, test_mode: bool = False, years: int = 1) -> list[dict]:
    """
    Process a stock via SEC EDGAR (works for 10-K, 10-Q, 20-F, 40-F, 6-K).
    """
    lookup = edgar_ticker or ticker
    path_type = "20-F/40-F" if edgar_ticker and edgar_ticker != ticker else "10-K/10-Q"
    
    print(f"\n{'='*60}")
    print(f"Processing {ticker} ({company}) via SEC EDGAR [{path_type}]")
    if edgar_ticker and edgar_ticker != ticker:
        print(f"  EDGAR lookup ticker: {edgar_ticker}")
    print(f"{'='*60}")
    
    # Step 1: Get CIK
    try:
        cik_ticker = lookup.replace("-", ".")
        cik = get_cik_from_ticker(cik_ticker)
        print(f"  CIK: {cik}")
    except ValueError as e:
        print(f"  ERROR: {e}")
        print(f"  Falling back to Yahoo Finance for basic data...")
        return process_yahoo_fallback(ticker, company)
    
    # Step 2: Find filings
    filings = find_annual_and_quarterly_filings(cik, years=years)
    print(f"  Found {len(filings)} filings")
    
    if not filings:
        print("  WARNING: No filings found! Falling back to Yahoo Finance...")
        return process_yahoo_fallback(ticker, company)
    
    # Step 3: Process each filing
    if test_mode:
        # In test mode: only the most recent 10-K (saves API cost during development)
        filings = [f for f in filings if f["form"] in ("10-K", "20-F", "40-F")][:1]
        print(f"  TEST MODE: limited to {len(filings)} annual filing(s)")

    # Annual-first: ensure annuals are processed before quarterlies so the
    # segment template is ready when quarterly extraction runs.
    annuals     = [f for f in filings if f.get("is_annual")]
    quarterlies = [f for f in filings if not f.get("is_annual")]
    ordered_filings = annuals + quarterlies

    # Fiscal year end date — derived from the most recent annual filing; used to label
    # quarterly periods deterministically (overrides the LLM's fiscal_period guess).
    fy_end_date = annuals[0].get("report_date") or annuals[0].get("filing_date") if annuals else None

    # Load any previously saved template (from an earlier pipeline run); will be
    # overwritten as each annual is processed to keep the template = union(all annuals).
    template_segments = _load_segment_template(ticker)
    if template_segments:
        print(f"  Loaded segment template ({len(template_segments)} segments)")

    all_extracted = []
    all_annual_segments: list[list[dict]] = []  # accumulator for the union template

    def _update_template_from_annual(segs: list[dict], source: str) -> None:
        """Add this annual's segments to the union accumulator and rewrite the template."""
        nonlocal template_segments
        if not segs:
            return
        all_annual_segments.append(segs)
        template_segments = _make_union_template(all_annual_segments)
        _save_segment_template(ticker, template_segments, source)

    for filing in ordered_filings:
        is_annual_filing = filing.get("is_annual", False)
        print(f"\n  Processing {filing['form']} ({filing['filing_date']})...")

        # Skip small 6-Ks (non-financial press releases / regulatory notices)
        # Foreign filers file many 6-Ks for drug approvals, dividends, etc.
        # Real financial 6-Ks (interim results, trading updates) are >40K chars.
        if filing["form"] == "6-K":
            safe_name = filing["accession_number"].replace("-", "_")
            src_path = Path(CACHE_DIR) / f"{safe_name}_6K.txt"
            if src_path.exists() and src_path.stat().st_size < 40_000:
                print(f"    Skipping 6-K — {src_path.stat().st_size:,} bytes (non-financial)")
                continue

        # Check cache
        cache_key = f"{ticker.replace('.','_')}_{filing['accession_number'].replace('-','_')}_extracted.json"
        cache_path = Path(CACHE_DIR) / "extractions" / cache_key

        if cache_path.exists():
            with open(cache_path) as f:
                extracted = json.load(f)
            cached_score = extracted.get("_quality_score", 100)
            print(f"    Using cached extraction (quality: {cached_score}/100)")
            extracted = validate_and_fix(extracted)
            # Override fiscal_period deterministically from form_type + dates
            derived_fp = derive_fiscal_period(
                form_type=filing["form"],
                report_date=filing.get("report_date"),
                fy_end_date=fy_end_date,
                filing_date=filing.get("filing_date"),
            )
            if derived_fp and extracted.get("fiscal_period") != derived_fp:
                extracted["fiscal_period"] = derived_fp
            # Build union template across all annuals (so quarter prompts see every segment ever reported)
            if is_annual_filing and extracted.get("segments"):
                _update_template_from_annual(
                    extracted["segments"],
                    source=f"cache: {filing['form']} {filing.get('report_date', filing['filing_date'])}",
                )
            all_extracted.append(extracted)
            continue

        # Download and extract
        try:
            text = download_filing_text(filing)
            # Foreign filers spam 6-K for non-financial events (drug approvals,
            # dividends, voting rights). Real financial 6-Ks (interim results,
            # trading updates) are always >40K chars. Skip the rest.
            if filing["form"] == "6-K" and len(text) < 40_000:
                print(f"    Skipping 6-K — {len(text):,} chars, likely non-financial (press release / regulatory notice)")
                continue
            is_text  = extract_is_section(text)
            seg_text = extract_segment_sections(text)
            print(
                f"    Filing: {len(text):,} chars → "
                f"IS: {len(is_text):,} chars / seg: {len(seg_text):,} chars"
            )

            # Try XBRL first for the income statement (free, accurate, no LLM tokens).
            # Use the per-filing CIK so historical filings from a predecessor
            # entity (e.g. BLK pre-2024 restructure) hit the correct facts JSON.
            from xbrl_fetcher import get_xbrl_income_statement
            xbrl_is = get_xbrl_income_statement(
                cik=filing.get("cik", cik),
                accession_number=filing["accession_number"],
                is_annual=is_annual_filing,
            )
            if xbrl_is:
                print(f"    XBRL IS: rev={xbrl_is.get('total_revenue')}B, "
                      f"ni={xbrl_is.get('net_income')}B")

            # Quarterly filings use the template when available (consistent names,
            # cheaper prompt — LLM fills numbers into known structure)
            use_tmpl = template_segments if not is_annual_filing else None

            extracted = extract_segment_data(
                filing_text=seg_text,
                is_text=is_text,
                xbrl_is=xbrl_is,
                ticker=ticker,
                company=company,
                form_type=filing["form"],
                report_date=filing.get("report_date", filing["filing_date"]),
                is_annual=is_annual_filing,
                template_segments=use_tmpl,
            )

            # Validate and auto-fix math / missing fields
            extracted = validate_and_fix(extracted)

            # Override fiscal_period deterministically from form_type + dates
            derived_fp = derive_fiscal_period(
                form_type=filing["form"],
                report_date=filing.get("report_date"),
                fy_end_date=fy_end_date,
                filing_date=filing.get("filing_date"),
            )
            if derived_fp:
                extracted["fiscal_period"] = derived_fp

            # Retry with larger window if no segments found
            if extracted.get("_needs_retry"):
                print("    No segments found — retrying with 30K-char window (Sonnet)…",
                      end=" ", flush=True)
                larger_seg = extract_segment_sections(text, target=30000)
                retry_segs = retry_segment_extraction(
                    larger_seg, ticker, company,
                    filing["form"],
                    filing.get("report_date", filing["filing_date"]),
                )
                if retry_segs:
                    extracted["segments"] = retry_segs
                    extracted = validate_and_fix(extracted)
                    print(f"retry found {len(retry_segs)} segments")
                else:
                    print("retry also empty")

            # Build union template across all annuals (so quarter prompts see every segment ever reported)
            if is_annual_filing and extracted.get("segments"):
                _update_template_from_annual(
                    extracted["segments"],
                    source=f"{filing['form']} {filing.get('report_date', filing['filing_date'])}",
                )

            # Cache
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump(extracted, f, indent=2)

            all_extracted.append(extracted)
            print(f"    Extracted {len(extracted.get('segments', []))} segments "
                  f"(quality: {extracted['_quality_score']}/100)")

        except Exception as e:
            print(f"    ERROR processing filing: {e}")
            continue

    # Pick up any hand-built "synthetic" extractions (e.g. carve-out years for a
    # spinoff that pre-date the company's first standalone 10-K). Files matching
    # `{TICKER}_synthetic_*_extracted.json` are merged in alongside EDGAR-sourced
    # extractions and feed into Sankey generation + Q4 synthesis like any other.
    safe_ticker = ticker.replace(".", "_")
    synth_dir = Path(CACHE_DIR) / "extractions"
    for synth_path in sorted(synth_dir.glob(f"{safe_ticker}_synthetic_*_extracted.json")):
        try:
            with open(synth_path) as f:
                synth = json.load(f)
            synth = validate_and_fix(synth)
            all_extracted.append(synth)
            print(f"  Loaded synthetic entry {synth_path.name} "
                  f"({synth.get('fiscal_year')} {synth.get('fiscal_period')})")
        except Exception as e:
            print(f"  WARNING: could not load synthetic entry {synth_path.name}: {e}")

    # Cross-quarter consistency check (printed warnings, not blocking)
    if len(all_extracted) > 1:
        consistency = validate_company_consistency(all_extracted)
        if consistency.get("issues"):
            print(f"\n  Consistency check — grade {consistency['grade']}:")
            for issue in consistency["issues"]:
                print(f"    ⚠ {issue}")

    return all_extracted


def collect_edgar_pending(
    ticker: str,
    company: str,
    edgar_ticker: str = None,
    test_mode: bool = False,
    years: int = 1,
) -> tuple[list[dict], list[dict]]:
    """
    Batch-mode counterpart to process_edgar_stock().
    Returns:
      already_done  — list of extracted dicts loaded from cache (no LLM needed)
      pending       — list of dicts describing filings that need LLM calls:
                      {ticker, company, accession_key, filing, segments_text, is_annual}
    """
    lookup = edgar_ticker or ticker
    path_type = "20-F/40-F" if edgar_ticker and edgar_ticker != ticker else "10-K/10-Q"

    print(f"\n{'='*60}")
    print(f"Collecting {ticker} ({company}) [{path_type}]")
    print(f"{'='*60}")

    try:
        cik_ticker = lookup.replace("-", ".")
        cik = get_cik_from_ticker(cik_ticker)
        print(f"  CIK: {cik}")
    except ValueError as e:
        print(f"  ERROR: {e}")
        return [], []

    filings = find_annual_and_quarterly_filings(cik, years=years)
    print(f"  Found {len(filings)} filings")
    if not filings:
        return [], []

    if test_mode:
        filings = [f for f in filings if f["form"] in ("10-K", "20-F", "40-F")][:1]

    already_done: list[dict] = []
    pending: list[dict] = []

    for filing in filings:
        # Skip small 6-Ks (non-financial press releases / regulatory notices)
        if filing["form"] == "6-K":
            safe_name = filing["accession_number"].replace("-", "_")
            src_path = Path(CACHE_DIR) / f"{safe_name}_6K.txt"
            if src_path.exists() and src_path.stat().st_size < 40_000:
                print(f"    Skipping 6-K — {src_path.stat().st_size:,} bytes (non-financial)")
                continue

        cache_key = (
            f"{ticker.replace('.','_')}_{filing['accession_number'].replace('-','_')}_extracted.json"
        )
        cache_path = Path(CACHE_DIR) / "extractions" / cache_key
        accession_key = (
            f"{ticker.replace('.','_')}__{filing['accession_number'].replace('-','_')}"
        )

        if cache_path.exists():
            with open(cache_path) as f:
                extracted = json.load(f)
            cached_score = extracted.get("_quality_score", 100)
            print(f"    Cached (quality {cached_score}/100)")
            already_done.append(validate_and_fix(extracted))
            continue

        try:
            text          = download_filing_text(filing)
            # 6-K size check (in case source wasn't cached yet at the pre-check above)
            if filing["form"] == "6-K" and len(text) < 40_000:
                print(f"    Skipping 6-K — {len(text):,} chars (non-financial)")
                continue
            is_text       = extract_is_section(text)        # ~6K chars for LLM Pass 1 fallback
            segments_text = extract_segment_sections(text)  # ~20K chars for Pass 2
            print(
                f"    Downloaded: {len(text):,} chars → "
                f"IS: {len(is_text):,} / seg: {len(segments_text):,} chars"
            )
            # Try XBRL for IS (free, skips LLM IS batch request).
            # Use per-filing CIK for predecessor-entity historical filings.
            from xbrl_fetcher import get_xbrl_income_statement
            xbrl_is = get_xbrl_income_statement(
                cik=filing.get("cik", cik),
                accession_number=filing["accession_number"],
                is_annual=filing.get("is_annual", False),
            )
            if xbrl_is:
                print(f"    XBRL IS: rev={xbrl_is.get('total_revenue')}B")
            pending.append({
                "ticker":        ticker,
                "company":       company,
                "accession_key": accession_key,
                "cache_key":     cache_key,
                "filing":        filing,
                "is_text":       is_text,
                "segments_text": segments_text,
                "is_annual":     filing.get("is_annual", False),
                "xbrl_is":       xbrl_is,
            })
        except Exception as e:
            print(f"    ERROR downloading filing: {e}")

    return already_done, pending


def process_batch_pending(pending: list[dict]) -> list[tuple[dict, dict]]:
    """
    Submit all pending filings as one batch, wait, parse results.
    Returns list of (pending_item, extracted_dict) tuples.
    """
    if not pending:
        return []

    # Build all batch requests (IS request omitted when xbrl_is is available)
    all_requests: list[dict] = []
    for item in pending:
        all_requests.extend(build_batch_requests(
            filing_text=item["segments_text"],
            is_text=item.get("is_text"),
            xbrl_is=item.get("xbrl_is"),
            ticker=item["ticker"],
            company=item["company"],
            form_type=item["filing"]["form"],
            report_date=item["filing"].get("report_date", item["filing"]["filing_date"]),
            accession_key=item["accession_key"],
            is_annual=item["is_annual"],
        ))
    xbrl_count = sum(1 for item in pending if item.get("xbrl_is"))
    print(f"\nSubmitting batch: {len(all_requests)} requests "
          f"({len(pending)} filings, {xbrl_count} with XBRL IS)")

    batch_id = submit_batch_requests(all_requests)
    batch_results = wait_for_batch(batch_id)

    out: list[tuple[dict, dict]] = []
    for item in pending:
        extracted = parse_batch_results(
            batch_results=batch_results,
            accession_key=item["accession_key"],
            ticker=item["ticker"],
            company=item["company"],
            form_type=item["filing"]["form"],
            report_date=item["filing"].get("report_date", item["filing"]["filing_date"]),
            is_annual=item["is_annual"],
            xbrl_is=item.get("xbrl_is"),
        )
        extracted = validate_and_fix(extracted)

        # Cache result
        cache_path = Path(CACHE_DIR) / "extractions" / item["cache_key"]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(extracted, f, indent=2)

        out.append((item, extracted))

    return out


# ============================================================
# Yahoo Finance Fallback (basic income statement, no segments)
# ============================================================

def process_yahoo_fallback(ticker: str, company: str) -> list[dict]:
    """
    Fallback: use Yahoo Finance for basic income statement data.
    No segment breakdown available — just top-level financials.
    """
    print(f"\n  Using Yahoo Finance fallback for {ticker}...")
    
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        
        all_extracted = []
        
        # Annual statements
        income_stmt = stock.income_stmt
        if income_stmt is not None and not income_stmt.empty:
            for col in income_stmt.columns[:3]:
                data = income_stmt[col].to_dict()
                year = col.year if hasattr(col, 'year') else str(col)[:4]
                
                extracted = {
                    "company": company,
                    "ticker": ticker,
                    "report_date": str(col)[:10],
                    "form_type": "Annual",
                    "fiscal_year": f"FY{str(year)[2:]}",
                    "fiscal_period": "FY",
                    "currency": info.get("currency", "USD"),
                    "unit": "billions",
                    "segments": [],
                    "income_statement": _parse_yahoo_income(data),
                    "notes": "Yahoo Finance only — no segment breakdown. Download annual report PDFs for segments.",
                }
                all_extracted.append(extracted)
                print(f"    Annual data for {year}")
        
        # Quarterly statements
        quarterly = stock.quarterly_income_stmt
        if quarterly is not None and not quarterly.empty:
            for col in quarterly.columns[:12]:
                data = quarterly[col].to_dict()
                
                extracted = {
                    "company": company,
                    "ticker": ticker,
                    "report_date": str(col)[:10],
                    "form_type": "Quarterly",
                    "fiscal_year": f"FY{str(col.year)[2:]}",
                    "fiscal_period": f"Q{(col.month-1)//3 + 1}",
                    "currency": info.get("currency", "USD"),
                    "unit": "billions",
                    "segments": [],
                    "income_statement": _parse_yahoo_income(data),
                    "notes": "Yahoo Finance only — no segment breakdown.",
                }
                all_extracted.append(extracted)
        
        print(f"  Total Yahoo extractions: {len(all_extracted)}")
        return all_extracted
        
    except Exception as e:
        print(f"  ERROR with Yahoo Finance: {e}")
        return []


def _parse_yahoo_income(data: dict) -> dict:
    """Parse Yahoo Finance income statement into our standard format."""
    def get_val(keys):
        for k in keys:
            if k in data and data[k] is not None:
                try:
                    return round(float(data[k]) / 1e9, 1)  # Convert to billions
                except (ValueError, TypeError):
                    pass
        return None
    
    return {
        "total_revenue": get_val(["Total Revenue", "TotalRevenue"]),
        "cost_of_revenue": get_val(["Cost Of Revenue", "CostOfRevenue"]),
        "gross_profit": get_val(["Gross Profit", "GrossProfit"]),
        "operating_expenses": {
            "research_and_development": get_val(["Research Development", "ResearchAndDevelopment", "Research And Development"]),
            "sales_and_marketing": get_val(["Selling General Administrative", "Selling And Marketing Expense"]),
            "general_and_administrative": get_val(["General And Administrative Expense"]),
            "other_operating": None,
        },
        "total_operating_expenses": get_val(["Total Operating Expenses", "OperatingExpense"]),
        "operating_income": get_val(["Operating Income", "OperatingIncome"]),
        "other_income_expense": get_val(["Other Income Expense Net", "Total Other Income Expense Net"]),
        "income_before_tax": get_val(["Income Before Tax", "Pretax Income"]),
        "tax_expense": get_val(["Income Tax Expense", "Tax Provision"]),
        "net_income": get_val(["Net Income", "NetIncome"]),
    }


# ============================================================
# Market Data Enrichment
# ============================================================

def enrich_with_market_data(ticker: str, extracted_data: list[dict]) -> list[dict]:
    """Add current price, shares outstanding, market cap from Yahoo Finance."""
    try:
        yf_ticker = ticker  # Use original ticker for Yahoo Finance
        stock = yf.Ticker(yf_ticker)
        info = stock.info
        
        market_data = {
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "market_cap": info.get("marketCap"),
            "currency": info.get("currency", "USD"),
        }
        
        # Format market cap
        if market_data["market_cap"]:
            market_data["market_cap_billions"] = round(market_data["market_cap"] / 1e9, 1)
        if market_data["shares_outstanding"]:
            market_data["shares_outstanding_millions"] = round(market_data["shares_outstanding"] / 1e6, 1)
        
        # Add to each extraction
        for d in extracted_data:
            d["market_data"] = market_data
        
    except Exception as e:
        print(f"  Warning: Could not fetch market data from Yahoo: {e}")
    
    return extracted_data


# ============================================================
# Output Generation
# ============================================================

def generate_outputs(ticker: str, company: str, all_extracted: list[dict], generate_summary: bool = False):
    """Generate Sankey charts and optional business model summary."""
    output_dir = Path(OUTPUT_DIR) / ticker.replace(".", "_")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Sankey for each period (track seen names to avoid overwriting duplicates)
    seen_names: dict[str, int] = {}
    for data in all_extracted:
        if "error" in data:
            continue

        # Skip charts with no meaningful financial data to display
        inc = data.get("income_statement", {}) or {}
        segs = data.get("segments", []) or []
        has_rev = inc.get("total_revenue") is not None
        has_seg_rev = any(s.get("revenue") is not None for s in segs)
        has_inc = inc.get("net_income") is not None or inc.get("operating_income") is not None
        if not has_rev and not has_seg_rev and not has_inc:
            period_id = f"{data.get('fiscal_year')} {data.get('fiscal_period')}"
            print(f"  Skipping Sankey for {period_id} — no financial data extracted")
            continue

        period = data.get("fiscal_period", "unknown")
        fy = data.get("fiscal_year", "unknown")
        report_date = data.get("report_date", "")

        base_name = f"{ticker.replace('.','_')}_{fy}_{period}"
        if base_name in seen_names:
            seen_names[base_name] += 1
            # Use report_date to disambiguate (e.g. Q1_2025-01-29 vs Q1_2025-04-30)
            safe_date = report_date.replace("-", "") if report_date else str(seen_names[base_name])
            filename = f"{base_name}_{safe_date}_sankey.html"
        else:
            seen_names[base_name] = 0
            filename = f"{base_name}_sankey.html"

        output_path = str(output_dir / filename)

        try:
            build_sankey_chart(data, output_path=output_path)
        except Exception as e:
            print(f"  ERROR generating Sankey for {fy} {period}: {e}")
    
    # Business model summary (opt-in via --summary flag).
    # LLM API is flaky — retry up to 3× with backoff so the batch run survives
    # transient connection errors.
    if generate_summary:
        valid_data = [d for d in all_extracted if "error" not in d]
        if valid_data:
            mda_text = _load_mda_for_ticker(ticker, valid_data)
            import time as _time
            for attempt in range(3):
                try:
                    summary = generate_business_summary(ticker, company, valid_data, mda_text=mda_text)
                    summary_path = output_dir / f"{ticker.replace('.','_')}_business_summary.md"
                    summary_path.write_text(summary, encoding="utf-8")
                    print(f"  Summary saved: {summary_path} (MD&A: {len(mda_text):,} chars)")
                    break
                except Exception as e:
                    if attempt < 2:
                        print(f"  Summary attempt {attempt + 1}/3 failed ({e}); retrying…")
                        _time.sleep(5)
                    else:
                        print(f"  ERROR generating summary after 3 tries: {e}")
    
    # Raw data JSON
    json_path = output_dir / f"{ticker.replace('.','_')}_all_data.json"
    with open(json_path, "w") as f:
        json.dump(all_extracted, f, indent=2, default=str)
    print(f"  Raw data saved: {json_path}")


# ============================================================
# Main Pipeline
# ============================================================

def run_pipeline(tickers: list[str] = None, test_mode: bool = False, generate_summary: bool = False, years: int = 1, batch_mode: bool = False, first_n: int = None, no_output: bool = False):
    """Run the full pipeline for specified tickers or all 98 stocks."""
    # Load stock list
    df = pd.read_excel(STOCK_LIST_PATH)
    print(f"Loaded {len(df)} stocks from {STOCK_LIST_PATH}")

    if tickers:
        df = df[df["Ticker"].isin(tickers)]
        if df.empty:
            print(f"No matching tickers found for: {tickers}")
            return
    elif first_n is not None:
        df = df.head(first_n)
    
    # Classify all stocks
    classifications = {}
    for _, row in df.iterrows():
        t = row["Ticker"]
        classifications[t] = classify_stock(t)
    
    pdf_stocks = [t for t, c in classifications.items() if c == "pdf"]
    edgar_us = [t for t, c in classifications.items() if c == "edgar_us"]
    edgar_intl = [t for t, c in classifications.items() if c == "edgar_intl"]
    
    print(f"\nStock classification:")
    print(f"  US EDGAR (10-K/10-Q):         {len(edgar_us)} stocks")
    print(f"  International EDGAR (20-F):   {len(edgar_intl)} stocks")
    print(f"  PDF annual reports needed:    {len(pdf_stocks)} stocks")
    
    # Check if PDF stocks have their reports downloaded
    if pdf_stocks:
        missing_pdfs = []
        for t in pdf_stocks:
            from pdf_handler import find_pdf_reports
            reports = find_pdf_reports(t)
            if not reports:
                missing_pdfs.append(t)
        
        if missing_pdfs:
            print(f"\n  WARNING: {len(missing_pdfs)} stocks need PDF downloads: {missing_pdfs}")
            print("  Run: python pdf_handler.py  for download instructions")
    
    print(f"\nProcessing {len(df)} stocks... (batch_mode={batch_mode})")
    print(f"Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}

    if batch_mode:
        # ── Batch path: collect → submit one batch → process all at once ──────
        # Phase A: download filings + build prompt texts; collect pending items
        all_pending: list[dict] = []
        cached_by_ticker: dict[str, list[dict]] = {}
        pdf_by_ticker: dict[str, list[dict]] = {}

        for _, row in df.iterrows():
            ticker  = row["Ticker"]
            company = row["Company"]
            stock_type = classifications[ticker]
            try:
                if stock_type in ("edgar_us", "edgar_intl"):
                    et = get_edgar_ticker(ticker) if stock_type == "edgar_intl" else None
                    done, pending = collect_edgar_pending(
                        ticker, company, edgar_ticker=et,
                        test_mode=test_mode, years=years,
                    )
                    cached_by_ticker[ticker] = done
                    all_pending.extend(pending)
                elif stock_type == "pdf":
                    pdf_results = process_pdf_reports(ticker, company)
                    if not pdf_results:
                        pdf_results = process_yahoo_fallback(ticker, company)
                    pdf_by_ticker[ticker] = pdf_results
            except Exception as e:
                print(f"\nFATAL ERROR collecting {ticker}: {e}")
                import traceback; traceback.print_exc()
                results[ticker] = f"ERROR: {e}"

        # Phase B: submit one batch for all uncached EDGAR filings
        if all_pending:
            batch_out = process_batch_pending(all_pending)
            # Group results by ticker
            new_by_ticker: dict[str, list[dict]] = {}
            for item, extracted in batch_out:
                new_by_ticker.setdefault(item["ticker"], []).append(extracted)
        else:
            new_by_ticker = {}

        # Phase C: generate outputs per ticker
        for _, row in df.iterrows():
            ticker  = row["Ticker"]
            company = row["Company"]
            if ticker in results:
                continue  # already failed during collection
            stock_type = classifications[ticker]
            try:
                if stock_type == "pdf":
                    all_extracted = pdf_by_ticker.get(ticker, [])
                else:
                    all_extracted = (cached_by_ticker.get(ticker, [])
                                     + new_by_ticker.get(ticker, []))
                    # Sort by report_date descending to restore filing order
                    all_extracted.sort(
                        key=lambda x: x.get("report_date", ""), reverse=True
                    )
                if all_extracted:
                    all_extracted = synthesize_missing_q4(all_extracted, ticker)
                    if not no_output:
                        all_extracted = enrich_with_market_data(ticker, all_extracted)
                        generate_outputs(ticker, company, all_extracted, generate_summary=generate_summary)
                    results[ticker] = "SUCCESS"
                else:
                    results[ticker] = "NO DATA"
            except Exception as e:
                print(f"\nFATAL ERROR generating outputs for {ticker}: {e}")
                import traceback; traceback.print_exc()
                results[ticker] = f"ERROR: {e}"

    else:
        # ── Synchronous path (unchanged) ──────────────────────────────────────
        for _, row in df.iterrows():
            ticker  = row["Ticker"]
            company = row["Company"]
            stock_type = classifications[ticker]

            try:
                if stock_type == "edgar_us":
                    all_extracted = process_edgar_stock(ticker, company, test_mode=test_mode, years=years)
                elif stock_type == "edgar_intl":
                    edgar_ticker = get_edgar_ticker(ticker)
                    all_extracted = process_edgar_stock(ticker, company, edgar_ticker=edgar_ticker, test_mode=test_mode, years=years)
                elif stock_type == "pdf":
                    all_extracted = process_pdf_reports(ticker, company, years=years)
                    if not all_extracted:
                        print(f"  No PDFs found, using Yahoo Finance fallback...")
                        all_extracted = process_yahoo_fallback(ticker, company)

                if all_extracted:
                    all_extracted = synthesize_missing_q4(all_extracted, ticker)
                    if not no_output:
                        all_extracted = enrich_with_market_data(ticker, all_extracted)
                        generate_outputs(ticker, company, all_extracted, generate_summary=generate_summary)
                    results[ticker] = "SUCCESS"
                else:
                    results[ticker] = "NO DATA"

            except Exception as e:
                print(f"\nFATAL ERROR for {ticker}: {e}")
                import traceback
                traceback.print_exc()
                results[ticker] = f"ERROR: {e}"
    
    # Summary
    print(f"\n{'='*60}")
    print(f"PIPELINE COMPLETE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    success = sum(1 for v in results.values() if v == "SUCCESS")
    no_data = sum(1 for v in results.values() if v == "NO DATA")
    errors = sum(1 for v in results.values() if v.startswith("ERROR"))
    
    print(f"  Success:  {success}/{len(results)}")
    print(f"  No data:  {no_data}/{len(results)}")
    print(f"  Errors:   {errors}/{len(results)}")
    
    failures = {k: v for k, v in results.items() if v != "SUCCESS"}
    if failures:
        print(f"\nFailed stocks:")
        for t, err in failures.items():
            print(f"  {t}: {err}")


if __name__ == "__main__":
    # Parse flags and positional ticker arguments separately
    raw_args = sys.argv[1:]
    test_mode        = "--test"      in raw_args
    generate_summary = "--summary"   in raw_args
    batch_mode       = "--batch"     in raw_args
    no_output        = "--no-output" in raw_args
    # --years N  (default 1 to keep costs low; use --years 3 for full history)
    years = 1
    first_n = None
    for i, arg in enumerate(raw_args):
        if arg.startswith("--years="):
            years = int(arg.split("=", 1)[1])
        elif arg == "--years" and i + 1 < len(raw_args):
            years = int(raw_args[i + 1])
        elif arg.startswith("--first="):
            first_n = int(arg.split("=", 1)[1])
        elif arg == "--first" and i + 1 < len(raw_args):
            first_n = int(raw_args[i + 1])
    ticker_args = [a for a in raw_args if not a.startswith("--") and not a.lstrip("-").isdigit()]

    if "--download-instructions" in raw_args:
        print_download_instructions()
    elif ticker_args:
        # e.g. "python main.py MSFT --test" or "python main.py MSFT AAPL --batch --years=3"
        print(f"Tickers: {ticker_args} | test_mode={test_mode} | years={years} | summary={generate_summary} | batch={batch_mode} | no_output={no_output}")
        run_pipeline(ticker_args, test_mode=test_mode, generate_summary=generate_summary, years=years, batch_mode=batch_mode, no_output=no_output)
    elif first_n is not None:
        # e.g. "python main.py --first 10 --test"
        print(f"Tickers: FIRST {first_n} | test_mode={test_mode} | years={years} | summary={generate_summary} | batch={batch_mode} | no_output={no_output}")
        run_pipeline(test_mode=test_mode, generate_summary=generate_summary, years=years, batch_mode=batch_mode, first_n=first_n, no_output=no_output)
    else:
        # e.g. "python main.py" or "python main.py --years=3 --summary"
        print(f"Tickers: ALL | test_mode={test_mode} | years={years} | summary={generate_summary} | batch={batch_mode} | no_output={no_output}")
        run_pipeline(test_mode=test_mode, generate_summary=generate_summary, years=years, batch_mode=batch_mode, no_output=no_output)
