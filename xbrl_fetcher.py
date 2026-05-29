"""
SEC XBRL company facts extractor.
Replaces LLM Pass 1 (income statement) with free, structured SEC XBRL data.

The SEC XBRL endpoint returns every reported financial fact for a company.
We match by accession_number to get values for the exact filing, then
select the entry with the correct period length (annual ≈ 365 days,
quarterly ≈ 90 days) to avoid grabbing YTD entries from 10-Qs.
"""
import json
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from config import SEC_EDGAR_USER_AGENT, CACHE_DIR

HEADERS = {"User-Agent": SEC_EDGAR_USER_AGENT}

# ifrs-full concept aliases for the same IS fields. Used for foreign 20-F filers
# that report under IFRS (TSM, SHEL, SAP, SONY, etc.) and don't populate us-gaap
# facts. Note: ifrs-full values may be reported in the company's native currency
# (TWD, EUR, JPY) rather than USD; the unit selection logic handles both.
_IFRS_CONCEPTS: dict[str, list[str]] = {
    "total_revenue": [
        "Revenue",
        "RevenueFromContractsWithCustomers",
        "RevenueAndOperatingIncome",  # Shell uses a combined revenue concept
    ],
    "cost_of_revenue": [
        "CostOfSales",
    ],
    "gross_profit": ["GrossProfit"],
    "r_and_d": ["ResearchAndDevelopmentExpense"],
    "sga": [
        "SellingGeneralAndAdministrativeExpense",
    ],
    "sga_g_and_a": ["AdministrativeExpense"],
    "sga_s_and_m": ["SellingExpense", "DistributionCosts"],
    "total_op_exp": [
        "OperatingExpense",
    ],
    "operating_income": [
        "ProfitLossFromOperatingActivities",
    ],
    "tax_expense": [
        "IncomeTaxExpenseContinuingOperations",
        "TaxExpenseIncome",
    ],
    "net_income": [
        "ProfitLoss",
        "ProfitLossAttributableToOwnersOfParent",
    ],
    "income_before_tax": [
        "ProfitLossBeforeTax",
    ],
}

# us-gaap concept aliases for each IS field — first hit that returns a value wins.
_CONCEPTS: dict[str, list[str]] = {
    "total_revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenuesNetOfInterestExpense",  # Banks/broker-dealers (GS, MS, etc.)
        "InterestAndDividendIncomeOperating",  # Some banks if RevenuesNetOf… absent
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueGoodsNet",
    ],
    "cost_of_revenue": [
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
        # "Excluding D&A" variants used by telecoms (T, VZ), industrials (LIN),
        # marketplaces (UBER), etc. Depreciation is reported as a separate line
        # in their IS, so the COR line excludes it.
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization",
        "CostOfServicesExcludingDepreciationDepletionAndAmortization",
    ],
    # Concepts that may appear alongside the primary cost concept and need to be
    # summed (e.g. RTX, TMO, VZ split into Goods + Services).
    "cost_of_services_supplement": ["CostOfServices"],
    "gross_profit": ["GrossProfit"],
    "r_and_d": ["ResearchAndDevelopmentExpense"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    "sga_g_and_a": ["GeneralAndAdministrativeExpense"],
    "sga_s_and_m": ["SellingAndMarketingExpense", "MarketingExpense"],
    "total_op_exp": [
        "OperatingExpenses",
        "CostsAndExpenses",
        "NoninterestExpense",   # Banks / broker-dealers (GS, JPM, MS, etc.)
    ],
    "operating_income": [
        "OperatingIncomeLoss",
        "OperatingIncome",
        # Bank fallback: banks rarely report a separate operating income; their
        # pre-tax income line is effectively "operating income" because there
        # are no non-operating items between OpEx and IBT.
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "tax_expense": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss"],
    "income_before_tax": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
}

_CACHE_TTL_DAYS = 7


def fetch_company_facts(cik: str) -> dict:
    """
    Fetch and cache the full XBRL company facts JSON for a CIK.
    Cache is refreshed after 7 days to pick up newly filed data.
    Returns {} on any error.
    """
    cache_dir = Path(CACHE_DIR) / "xbrl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"CIK{cik}.json"

    # Return cached copy if fresh enough
    if cache_path.exists():
        age_days = (time.time() - cache_path.stat().st_mtime) / 86400
        if age_days < _CACHE_TTL_DAYS:
            try:
                return json.loads(cache_path.read_text())
            except Exception:
                pass

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        cache_path.write_text(json.dumps(data))
        time.sleep(0.11)
        return data
    except Exception as e:
        print(f"    XBRL fetch failed for CIK {cik}: {e}")
        return {}


def _entries_for_accn(taxonomy: dict, concept: str, accn: str, unit: str = "USD") -> list[dict]:
    """Return all entries (in the given currency unit) for a concept filtered to a
    specific accession number. Works for both us-gaap and ifrs-full taxonomies."""
    try:
        return [
            e for e in taxonomy[concept]["units"][unit]
            if e.get("accn") == accn
        ]
    except (KeyError, TypeError):
        return []


def _pick_ifrs_unit(ifrs: dict, revenue_aliases: list[str], accn: str) -> str | None:
    """For an IFRS filer, pick the currency unit that actually has data for this
    accession. Prefer USD if it has entries for the accn; otherwise return the
    first non-empty native currency (TWD, EUR, JPY, …)."""
    candidates: list[str] = []
    for concept in revenue_aliases:
        if concept not in ifrs:
            continue
        units = ifrs[concept].get("units", {})
        if "USD" in units and any(e.get("accn") == accn for e in units["USD"]):
            return "USD"
        for u, entries in units.items():
            if u == "USD":
                continue
            if any(e.get("accn") == accn for e in entries) and u not in candidates:
                candidates.append(u)
    return candidates[0] if candidates else None


def _period_days(entry: dict) -> int:
    """Return the length of the reporting period in days."""
    try:
        start = datetime.strptime(entry["start"], "%Y-%m-%d")
        end   = datetime.strptime(entry["end"],   "%Y-%m-%d")
        return (end - start).days
    except Exception:
        return 0


def _pick_period_value(entries: list[dict], is_annual: bool) -> tuple[int | None, dict]:
    """
    Select the entry matching the desired period length:
      - Annual:    340–400 days  (full fiscal year, not YTD)
      - Quarterly:  75–105 days  (single quarter, not YTD)

    Returns (value_in_dollars, entry_dict).  Value is None if no match.
    """
    lo, hi = (340, 400) if is_annual else (75, 105)
    matches = [e for e in entries if lo <= _period_days(e) <= hi]
    if not matches:
        return None, {}
    # Among matches prefer the entry with the latest end date (current period,
    # not a prior-year comparative entry filed in the same 10-K/10-Q)
    best = max(matches, key=lambda e: e.get("end", ""))
    return best["val"], best


def _prior_year_value(taxonomy: dict, concept: str, fp: str, fy: int, is_annual: bool, unit: str = "USD") -> int | None:
    """
    Find the value for the same period one year ago (fy-1, same fp).
    Used to compute YoY growth %.
    """
    try:
        entries = taxonomy[concept]["units"][unit]
    except (KeyError, TypeError):
        return None

    candidates = [
        e for e in entries
        if e.get("fp") == fp and e.get("fy") == fy - 1
    ]
    val, _ = _pick_period_value(candidates, is_annual)
    return val


def _resolve_concept(taxonomy: dict, aliases: list[str], accn: str, is_annual: bool, unit: str = "USD") -> tuple[int | None, dict]:
    """Try each alias in order; return (value, entry) for the first hit."""
    for concept in aliases:
        entries = _entries_for_accn(taxonomy, concept, accn, unit)
        val, entry = _pick_period_value(entries, is_annual)
        if val is not None:
            return val, entry
    return None, {}


def _resolve_concept_named(taxonomy: dict, aliases: list[str], accn: str, is_annual: bool, unit: str = "USD") -> tuple[int | None, str | None]:
    """Same as _resolve_concept but returns the matching concept name (for callers
    that need to know which alias hit, e.g. to apply concept-specific supplements)."""
    for concept in aliases:
        entries = _entries_for_accn(taxonomy, concept, accn, unit)
        val, _entry = _pick_period_value(entries, is_annual)
        if val is not None:
            return val, concept
    return None, None


def _b(val: int | None) -> float | None:
    """Convert absolute dollar amount to billions, rounded to 1 decimal."""
    if val is None:
        return None
    return round(val / 1e9, 1)


def _yoy(current: int | None, prior: int | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    return round((current / prior - 1) * 100, 1)


def _build_is_from_taxonomy(
    taxonomy: dict,
    concepts: dict[str, list[str]],
    accn: str,
    is_annual: bool,
    unit: str,
    has_cost_split: bool = True,
) -> dict | None:
    """
    Build the IS dict from a single taxonomy (us-gaap or ifrs-full) and a chosen
    currency unit. `has_cost_split` controls the CostOfGoodsSold + CostOfServices
    summing logic, which only applies to us-gaap. Returns None if no revenue
    concept matches for this accession.
    """
    rev_raw, rev_entry = _resolve_concept(taxonomy, concepts["total_revenue"], accn, is_annual, unit)
    if rev_raw is None:
        return None

    fp = rev_entry.get("fp", "FY")
    fy = rev_entry.get("fy", 0)

    cogs_raw, cogs_concept = _resolve_concept_named(taxonomy, concepts["cost_of_revenue"], accn, is_annual, unit)
    if has_cost_split and cogs_concept == "CostOfGoodsSold" and cogs_raw is not None:
        services_raw, _ = _resolve_concept(taxonomy, concepts.get("cost_of_services_supplement", []), accn, is_annual, unit)
        if services_raw is not None:
            cogs_raw = cogs_raw + services_raw

    gp_raw,  _ = _resolve_concept(taxonomy, concepts["gross_profit"], accn, is_annual, unit)
    rd_raw,  _ = _resolve_concept(taxonomy, concepts["r_and_d"],      accn, is_annual, unit)
    sga_raw, _ = _resolve_concept(taxonomy, concepts["sga"],          accn, is_annual, unit)

    if sga_raw is None:
        ga_raw, _ = _resolve_concept(taxonomy, concepts["sga_g_and_a"], accn, is_annual, unit)
        sm_raw, _ = _resolve_concept(taxonomy, concepts["sga_s_and_m"], accn, is_annual, unit)
        if ga_raw is not None or sm_raw is not None:
            sga_raw = (ga_raw or 0) + (sm_raw or 0) or None

    oi_raw,  _ = _resolve_concept(taxonomy, concepts["operating_income"],  accn, is_annual, unit)
    ibt_raw, _ = _resolve_concept(taxonomy, concepts["income_before_tax"], accn, is_annual, unit)
    tax_raw, _ = _resolve_concept(taxonomy, concepts["tax_expense"],       accn, is_annual, unit)
    ni_raw,  _ = _resolve_concept(taxonomy, concepts["net_income"],        accn, is_annual, unit)

    rev_prior = None
    for alt in concepts["total_revenue"]:
        rev_prior = _prior_year_value(taxonomy, alt, fp, fy, is_annual, unit)
        if rev_prior is not None:
            break

    ni_prior = None
    for alt in concepts["net_income"]:
        ni_prior = _prior_year_value(taxonomy, alt, fp, fy, is_annual, unit)
        if ni_prior is not None:
            break

    total_opex_raw, _ = _resolve_concept(taxonomy, concepts["total_op_exp"], accn, is_annual, unit)
    if total_opex_raw is None:
        if rd_raw is not None and sga_raw is not None:
            total_opex_raw = rd_raw + sga_raw
        elif rd_raw is not None or sga_raw is not None:
            total_opex_raw = (rd_raw or 0) + (sga_raw or 0)

    fy_label = f"FY{fy}" if fy else ""
    if fp and fp != "FY":
        fiscal_period = fp
        fiscal_year_label = f"FY{fy}" if fy else ""
    else:
        fiscal_period = "FY"
        fiscal_year_label = fy_label

    return {
        "fiscal_year":          fiscal_year_label,
        "fiscal_period":        fiscal_period,
        "currency":             unit,
        "exchange_rate_to_usd": None,
        "total_revenue":              _b(rev_raw),
        "total_revenue_yoy_pct":      _yoy(rev_raw, rev_prior),
        "cost_of_revenue":            _b(cogs_raw),
        "cost_of_revenue_yoy_pct":    None,
        "cost_of_revenue_breakdown":  None,
        "gross_profit":               _b(gp_raw),
        "gross_profit_yoy_pct":       None,
        "operating_expenses": {
            "research_and_development":    _b(rd_raw),
            "selling_general_administrative": _b(sga_raw),
        },
        "total_operating_expenses":   _b(total_opex_raw),
        "operating_income":           _b(oi_raw),
        "operating_income_yoy_pct":   None,
        "other_income_expense":       None,
        "income_before_tax":          _b(ibt_raw),
        "tax_expense":                _b(tax_raw),
        "net_income":                 _b(ni_raw),
        "net_income_yoy_pct":         _yoy(ni_raw, ni_prior),
    }


def get_xbrl_income_statement(
    cik: str,
    accession_number: str,
    is_annual: bool = True,
) -> dict | None:
    """
    Build an income_statement dict for a specific SEC filing (identified by
    accession_number) using XBRL company facts data.

    Tries us-gaap first; falls back to ifrs-full for foreign 20-F filers
    (TSM, SHEL, SAP, SONY, etc.). For IFRS filers, picks USD unit if available;
    otherwise pulls native-currency values and sets `currency` accordingly so
    validate_data.py can convert downstream.

    Returns None if neither taxonomy yields a revenue value for this accession.
    """
    facts = fetch_company_facts(cik)
    accn = accession_number  # XBRL stores accn with dashes: "0001193125-26-191507"

    # ── us-gaap path ──────────────────────────────────────────────────────────
    gaap: dict = facts.get("facts", {}).get("us-gaap", {})
    if gaap:
        result = _build_is_from_taxonomy(gaap, _CONCEPTS, accn, is_annual, unit="USD", has_cost_split=True)
        if result is not None:
            return result

    # ── ifrs-full fallback ────────────────────────────────────────────────────
    ifrs: dict = facts.get("facts", {}).get("ifrs-full", {})
    if ifrs:
        unit = _pick_ifrs_unit(ifrs, _IFRS_CONCEPTS["total_revenue"], accn)
        if unit:
            return _build_is_from_taxonomy(ifrs, _IFRS_CONCEPTS, accn, is_annual, unit=unit, has_cost_split=False)

    return None
