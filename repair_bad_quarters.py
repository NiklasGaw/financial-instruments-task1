"""
Identify cached quarterly extractions with bad segment data (YTD-cumulative
instead of standalone-quarter, sparse, or with null revenues), delete those
caches, and print the affected tickers so the pipeline can re-extract them
with the improved quarterly template prompt.

Usage:
    python repair_bad_quarters.py                  # dry-run: list bad caches, no deletion
    python repair_bad_quarters.py --apply          # delete bad caches + print tickers to rerun
    python repair_bad_quarters.py --purge-empty    # also list extractions with no rev + no segs
    python repair_bad_quarters.py --purge-empty --apply
"""
from __future__ import annotations
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from config import CACHE_DIR


_EXTRACT_DIR = Path(CACHE_DIR) / "extractions"


def _ticker_from_data(d: dict, fallback_stem: str) -> str:
    """Prefer the JSON's own 'ticker' field; fall back to filename parsing."""
    t = (d.get("ticker") or "").strip()
    if t:
        return t
    name = fallback_stem.replace("_extracted", "")
    m = re.match(
        r"^(.+?)(?:_\d{7,}|_Q4_calc|_Q[1-4]_\d{4}|_H[12]_\d{4}|_annual_\d{4})",
        name,
    )
    return m.group(1) if m else name


def _is_bad_quarter(q: dict, annual_seg_count: int) -> list[str]:
    """Return list of problem labels, or empty list if quarter looks OK."""
    fp = (q.get("fiscal_period") or "").upper()
    if fp not in ("Q1", "Q2", "Q3"):
        return []  # don't repair Q4 (synthetic) or FY
    if q.get("form_type") == "Calculated":
        return []  # synthetic Q4 — handled by re-synthesis

    segs = q.get("segments") or []
    seg_count = len(segs)
    rev = (q.get("income_statement") or {}).get("total_revenue") or 0
    seg_sum = sum((s.get("revenue") or 0) for s in segs)
    nulls = sum(1 for s in segs if s.get("revenue") is None)

    problems: list[str] = []
    # Missing segments — any quarter with fewer segments than the annual is suspect
    if 0 < seg_count < annual_seg_count:
        problems.append(f"MISSING_SEG({seg_count}/{annual_seg_count})")
    # Segment sum way above total revenue — LLM picked YTD column
    if rev and seg_sum > rev * 1.15:
        problems.append(f"YTD({seg_sum:.1f}/{rev:.1f}={seg_sum/rev*100:.0f}%)")
    # Segment sum well below total revenue — a major segment got dropped
    if rev and seg_count > 0 and seg_sum < rev * 0.85:
        problems.append(f"LOW_SUM({seg_sum:.1f}/{rev:.1f}={seg_sum/rev*100:.0f}%)")
    # LLM matched names but missed values for many of them
    if seg_count >= 3 and nulls > seg_count * 0.4:
        problems.append(f"NULLS({nulls}/{seg_count})")
    return problems


def main() -> None:
    apply_deletes = "--apply" in sys.argv
    purge_empty   = "--purge-empty" in sys.argv

    by_ticker: dict[str, list[tuple[Path, dict]]] = defaultdict(list)
    empty_files: list[tuple[str, Path, str]] = []
    for f in sorted(_EXTRACT_DIR.glob("*_extracted.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        ticker = _ticker_from_data(d, f.stem)
        by_ticker[ticker].append((f, d))

        # Detect empty extractions: no rev and no segments (or just placeholder)
        rev = (d.get("income_statement") or {}).get("total_revenue")
        segs = d.get("segments") or []
        # An empty extraction is one with no rev AND segments are either [] or all None-revenue
        seg_useful = any(s.get("revenue") is not None for s in segs)
        if rev is None and not seg_useful:
            empty_files.append((ticker, f, "no rev + no useful segments"))

    bad_files: list[tuple[str, Path, str]] = []  # (ticker, path, problem-label)
    affected_tickers: set[str] = set()
    synthetic_q4_affected: set[tuple[str, str]] = set()  # (ticker, fy)

    for ticker, ds in by_ticker.items():
        annuals = [d for _f, d in ds if (d.get("fiscal_period") or "").upper() == "FY"]
        if not annuals:
            continue
        annual = sorted(annuals, key=lambda d: d.get("report_date", ""), reverse=True)[0]
        annual_seg_count = len(annual.get("segments") or [])
        if annual_seg_count < 2:
            continue

        for f, d in ds:
            problems = _is_bad_quarter(d, annual_seg_count)
            if problems:
                bad_files.append((ticker, f, ", ".join(problems)))
                affected_tickers.add(ticker)
                fy = d.get("fiscal_year")
                if fy:
                    synthetic_q4_affected.add((ticker, fy))

    # Find synthetic Q4 cache files that depend on the bad quarters
    synthetic_q4_files: list[Path] = []
    for ticker, fy in synthetic_q4_affected:
        safe_t = ticker.replace(".", "_")
        safe_fy = fy.replace("/", "_").replace(" ", "_")
        q4_path = _EXTRACT_DIR / f"{safe_t}_Q4_calc_{safe_fy}_extracted.json"
        if q4_path.exists():
            synthetic_q4_files.append(q4_path)

    print(f"Bad quarterly extractions: {len(bad_files)} across {len(affected_tickers)} ticker(s)")
    for t, f, p in bad_files:
        print(f"  {t:15s} {f.name}: {p}")
    print()
    print(f"Synthetic Q4 files that will be invalidated: {len(synthetic_q4_files)}")
    for f in synthetic_q4_files:
        print(f"  {f.name}")
    print()
    if purge_empty:
        print(f"Empty extractions (no rev + no useful segments): {len(empty_files)}")
        for t, f, _ in empty_files[:30]:
            print(f"  {t:15s} {f.name}")
        if len(empty_files) > 30:
            print(f"  … and {len(empty_files) - 30} more")
        empty_tickers = {t for t, _, _ in empty_files}
        print(f"  Empty-affected tickers ({len(empty_tickers)}): {' '.join(sorted(empty_tickers))}")
        print()
    print(f"Affected tickers ({len(affected_tickers)}):")
    print("  " + " ".join(sorted(affected_tickers)))

    if not apply_deletes:
        print("\nDRY RUN — no files deleted. Re-run with --apply to delete.")
        return

    # Delete bad files + dependent synthetic Q4s + (optionally) empty files
    for _, f, _ in bad_files:
        f.unlink()
    for f in synthetic_q4_files:
        if f.exists():
            f.unlink()
    empty_count = 0
    if purge_empty:
        for _, f, _ in empty_files:
            if f.exists():
                f.unlink()
                empty_count += 1
    msg = (f"\nDeleted {len(bad_files)} bad quarterly caches "
           f"+ {len(synthetic_q4_files)} dependent synthetic Q4 caches")
    if purge_empty:
        msg += f" + {empty_count} empty caches"
    print(msg + ".")
    all_affected = affected_tickers | ({t for t, _, _ in empty_files} if purge_empty else set())
    if all_affected:
        print(f"\nRerun with:\n  python main.py {' '.join(sorted(all_affected))} --years=3")


if __name__ == "__main__":
    main()
