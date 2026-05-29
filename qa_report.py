"""
QA report: reads all cached extractions, groups by ticker, runs consistency
checks, and prints a summary table.

Usage:
    python qa_report.py                  # all tickers
    python qa_report.py MSFT AAPL AMZN  # specific tickers
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict
from pathlib import Path

from config import CACHE_DIR
from validate_data import validate_company_consistency, _is_annual, _num


_EXTRACT_DIR = Path(CACHE_DIR) / "extractions"


def _ticker_from_filename(name: str) -> str:
    """'MSFT_0000789019_25_100235_extracted' → 'MSFT'"""
    # Ticker is everything before the first run of digits after an underscore
    parts = name.split("_")
    ticker_parts = []
    for part in parts:
        if part.isdigit() and len(part) >= 7:
            break
        ticker_parts.append(part)
    return "_".join(ticker_parts)


def _load_extractions(ticker: str) -> list[dict]:
    prefix = ticker.replace(".", "_") + "_"
    files = sorted(_EXTRACT_DIR.glob(f"{prefix}*_extracted.json"))
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out


def _seg_count(d: dict) -> str:
    n = len(d.get("segments") or [])
    return str(n) if n else "-"


def _fp_label(d: dict) -> str:
    return (d.get("fiscal_period") or "?").upper()


def _names_match(annuals: list[dict], quarterlies: list[dict]) -> str:
    if not annuals or not annuals[0].get("segments"):
        return "n/a"
    annual_names = {s["name"].lower() for s in annuals[0]["segments"] if s.get("name")}
    mismatches = 0
    for q in quarterlies:
        q_names = {s["name"].lower() for s in (q.get("segments") or []) if s.get("name")}
        if q_names and q_names - annual_names:
            mismatches += 1
    return "Yes" if mismatches == 0 else f"No ({mismatches}q)"


def _rev_trend_ok(quarterlies: list[dict]) -> str:
    """Return 'OK' or 'FLAG' if any segment jumped >100% QoQ."""
    sorted_q = sorted(
        quarterlies,
        key=lambda d: d.get("report_date") or d.get("filing_date") or "",
    )
    for i in range(1, len(sorted_q)):
        prev_segs = {
            s["name"].lower(): _num(s.get("revenue"))
            for s in (sorted_q[i - 1].get("segments") or [])
        }
        curr_segs = {
            s["name"].lower(): _num(s.get("revenue"))
            for s in (sorted_q[i].get("segments") or [])
        }
        for name, cur_rev in curr_segs.items():
            prev_rev = prev_segs.get(name)
            if cur_rev and prev_rev and prev_rev > 0:
                if abs(cur_rev - prev_rev) / prev_rev > 1.0:
                    return "FLAG"
    return "OK"


def run_report(tickers: list[str]) -> None:
    # ── Header ────────────────────────────────────────────────────────────────
    col_w = [8, 8, 7, 7, 7, 7, 14, 13, 6]
    headers = ["Ticker", "Annual", "Q1", "Q2", "Q3", "Q4", "Names Match", "Rev Trend", "Grade"]
    sep = "  ".join("-" * w for w in col_w)
    header_row = "  ".join(h.ljust(w) for h, w in zip(headers, col_w))
    print()
    print(header_row)
    print(sep)

    grades_summary: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}

    for ticker in tickers:
        all_ext = _load_extractions(ticker)
        if not all_ext:
            row = [ticker, "NO DATA", "-", "-", "-", "-", "-", "-", "F"]
            print("  ".join(str(v).ljust(w) for v, w in zip(row, col_w)))
            grades_summary["F"] += 1
            continue

        annuals     = [d for d in all_ext if _is_annual(d)]
        quarterlies = [d for d in all_ext if not _is_annual(d)]

        # Segment counts: annual uses first annual; quarters by fiscal_period label
        annual_segs = _seg_count(annuals[0]) if annuals else "-"

        # Map quarterlies by fiscal_period → keep highest-quality entry if dupes
        by_fp: dict[str, dict] = {}
        for q in quarterlies:
            fp = _fp_label(q)
            if fp not in by_fp or (q.get("_quality_score", 0) > by_fp[fp].get("_quality_score", 0)):
                by_fp[fp] = q

        q1 = _seg_count(by_fp["Q1"]) if "Q1" in by_fp else "-"
        q2 = _seg_count(by_fp["Q2"]) if "Q2" in by_fp else "-"
        q3 = _seg_count(by_fp["Q3"]) if "Q3" in by_fp else "-"
        q4 = _seg_count(by_fp["Q4"]) if "Q4" in by_fp else "-"

        names_ok  = _names_match(annuals, quarterlies)
        rev_trend = _rev_trend_ok(quarterlies) if quarterlies else "n/a"

        result = validate_company_consistency(all_ext)
        grade  = result["grade"]
        grades_summary[grade] = grades_summary.get(grade, 0) + 1

        row = [ticker, annual_segs, q1, q2, q3, q4, names_ok, rev_trend, grade]
        print("  ".join(str(v).ljust(w) for v, w in zip(row, col_w)))

        # Print issues indented below the row (only for C/D/F)
        if grade in ("C", "D", "F") and result.get("issues"):
            for issue in result["issues"][:3]:  # cap at 3 per ticker to keep output readable
                print(f"         ↳ {issue}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    total = sum(grades_summary.values())
    summary_parts = [f"{g}={grades_summary[g]}" for g in ("A", "B", "C", "D", "F") if grades_summary.get(g)]
    print(f"Tickers: {total}  |  Grades: {', '.join(summary_parts)}")
    print()


def main() -> None:
    args = sys.argv[1:]

    if args:
        tickers = [a.upper() for a in args]
    else:
        # Discover all tickers from cache
        seen: set[str] = set()
        tickers = []
        for f in sorted(_EXTRACT_DIR.glob("*_extracted.json")):
            t = _ticker_from_filename(f.stem.replace("_extracted", ""))
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)

    print(f"QA Report — {len(tickers)} ticker(s)")
    run_report(tickers)


if __name__ == "__main__":
    main()
