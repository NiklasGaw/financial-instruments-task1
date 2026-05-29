"""
One-off backfill: patch cached extractions for the 4 IFRS-full SEC filers
(TSM, SHEL, SAP, SONY) by pulling IS fields from XBRL via the new IFRS path
added to xbrl_fetcher.py. The original LLM-only extractions left TR/COR/TOE/OI
as None for these tickers because they don't report us-gaap facts; the new
ifrs-full path in get_xbrl_income_statement() now resolves them.

For each cached extraction file in cache/extractions/:
  1. Derive accession_number from the filename
  2. Look up CIK for the ticker
  3. Call get_xbrl_income_statement()
  4. If it returns data, copy IS fields into the extraction
  5. Save back

Does not touch segments, just the IS scaffolding.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xbrl_fetcher import get_xbrl_income_statement  # noqa: E402

EXTRACTIONS_DIR = ROOT / "cache" / "extractions"

# ticker_prefix -> CIK (zero-padded to 10 digits)
TICKERS = {
    "TSM":    "0001046179",
    "SHEL_L": "0001306965",
    "SAP_DE": "0001000184",
    "6758_T": "0000313838",
}

IS_FIELDS = [
    "total_revenue", "total_revenue_yoy_pct",
    "cost_of_revenue", "cost_of_revenue_yoy_pct", "cost_of_revenue_breakdown",
    "gross_profit", "gross_profit_yoy_pct",
    "operating_expenses", "total_operating_expenses",
    "operating_income", "operating_income_yoy_pct",
    "other_income_expense", "income_before_tax", "tax_expense",
    "net_income", "net_income_yoy_pct",
]


def parse_accession_from_filename(fn: str, prefix: str) -> str:
    """SHEL_L_0001306965_25_000007_extracted.json -> 0001306965-25-000007"""
    stem = fn[len(prefix) + 1:].replace("_extracted.json", "")
    return "-".join(stem.split("_"))


def main() -> None:
    for prefix, cik in TICKERS.items():
        print(f"\n=== {prefix} (CIK {cik}) ===")
        files = sorted(EXTRACTIONS_DIR.glob(f"{prefix}_*_extracted.json"))
        patched = 0
        skipped = 0
        already = 0
        for fp in files:
            d = json.loads(fp.read_text())
            accn = parse_accession_from_filename(fp.name, prefix)
            is_annual = (d.get("fiscal_period") or "").upper() == "FY"
            xbrl = get_xbrl_income_statement(cik=cik, accession_number=accn, is_annual=is_annual)
            if xbrl is None:
                skipped += 1
                continue
            # Merge IS fields at BOTH the top level (preserved by the LLM
            # extraction format) and the nested income_statement dict — the
            # latter is what validate_and_fix() reads for FX conversion and
            # reconciliation. Without populating it, the downstream pipeline
            # operates on the LLM's stale None values and skips conversion.
            inc = d.get("income_statement") or {}
            for k in IS_FIELDS:
                d[k] = xbrl.get(k)
                inc[k] = xbrl.get(k)
            d["income_statement"] = inc
            d["currency"] = xbrl.get("currency", "USD")
            d["exchange_rate_to_usd"] = xbrl.get("exchange_rate_to_usd")
            d["accession_number"] = accn
            d["cik"] = cik
            # XBRL is authoritative for fiscal_year / fiscal_period — the LLM
            # sometimes mis-labels comparative-period 20-Fs as the prior year.
            if xbrl.get("fiscal_year"):
                d["fiscal_year"] = xbrl["fiscal_year"]
            if xbrl.get("fiscal_period"):
                d["fiscal_period"] = xbrl["fiscal_period"]
            fp.write_text(json.dumps(d, indent=2, default=str))
            patched += 1
            print(f"  {fp.name} [{d.get('fiscal_year')} {d.get('fiscal_period')}]"
                  f" cur={xbrl['currency']} TR={xbrl['total_revenue']} OI={xbrl['operating_income']}")
        print(f"  Summary: {patched} patched, {skipped} no-XBRL, {already} already-populated, {len(files)} total")


if __name__ == "__main__":
    main()
