"""
Post-extraction validation and auto-fix for financial data.
Called after LLM extraction, before Sankey generation.
Fixes math inconsistencies, fills derivable nulls, detects banks.
"""
from __future__ import annotations
import re as _re
from pathlib import Path

# Normalize currency codes that LLMs may use non-standardly
_CURRENCY_ALIASES: dict[str, str] = {
    "RMB": "CNY",   # Alibaba / Chinese companies often say "RMB"
    "¥":   "JPY",
    "€":   "EUR",
    "£":   "GBP",
    "元":  "CNY",
}


def _lookup_fx_rate(currency: str, report_date: str) -> float | None:
    """
    Look up historical exchange rate. Returns 'local currency units per 1 USD'
    (e.g., 149.4 for JPY: ¥149.4 = $1; 0.92 for EUR: €0.92 = $1).

    Primary source: Frankfurter (ECB data, free). Covers ~30 currencies.
    Fallback: Yahoo Finance (for TWD and other non-ECB currencies).
    Falls back up to 7 prior days to handle weekends/holidays.
    """
    if not report_date or not currency or currency == "USD":
        return None
    canon = _CURRENCY_ALIASES.get(currency.upper(), currency.upper())

    try:
        import requests as _req
        from datetime import datetime as _dt, timedelta as _td
        ref = _dt.strptime(report_date[:10], "%Y-%m-%d")

        # ── Primary: Frankfurter (ECB) ────────────────────────────────────────
        for offset in range(8):
            date_str = (ref - _td(days=offset)).strftime("%Y-%m-%d")
            url = f"https://api.frankfurter.app/{date_str}?from=USD&to={canon}"
            try:
                resp = _req.get(url, timeout=5)
                if resp.status_code == 200:
                    rates = resp.json().get("rates", {})
                    if canon in rates:
                        return round(float(rates[canon]), 4)
            except Exception:
                pass

        # ── Fallback: Yahoo Finance (for TWD and other non-ECB currencies) ────
        try:
            import yfinance as _yf
            start = (ref - _td(days=14)).strftime("%Y-%m-%d")
            end   = (ref + _td(days=3)).strftime("%Y-%m-%d")
            # USDTWD=X gives TWD per 1 USD directly
            hist = _yf.Ticker(f"USD{canon}=X").history(start=start, end=end)
            if not hist.empty:
                return round(float(hist["Close"].iloc[-1]), 4)
        except Exception:
            pass

    except Exception:
        pass
    return None

# Technology-classification segment names (nanometer nodes, process nodes, etc.)
# These are low-value for business analysis and removed first when dedup fires.
_TECH_NODE_RE = _re.compile(
    r'\b\d+\.?\d*\s*[-–]?\s*(nm|nanometer|micron|node|angstrom)\b'
    r'|\b(leading[- ]edge|mature[- ]node|advanced[- ]node|legacy)\b',
    _re.IGNORECASE,
)

def _is_tech_classification(name: str) -> bool:
    return bool(_TECH_NODE_RE.search(name))

# Exact geographic labels (lowercased). Prefix-match also applied below.
_GEO_NAMES = {
    # Macro regions
    "north america", "south america", "central america", "latin america",
    "latin america & caribbean", "americas",
    "europe", "western europe", "eastern europe", "northern europe", "southern europe",
    "europe middle east africa", "europe, middle east & africa",
    "emea", "apac", "latam",
    "asia pacific", "asia-pacific", "asia", "greater asia",
    "greater china", "japan and asia pacific",
    "middle east", "middle east & africa", "africa",
    "rest of world", "rest of asia", "rest of europe", "rest of america",
    "international", "worldwide", "global", "domestic", "foreign",
    # Market-type labels
    "emerging markets", "developed markets", "frontier markets",
    "other geographies", "other regions", "other countries", "other territories",
    "other markets",
    # Countries
    "united states", "u.s.", "usa",
    "china", "greater china",
    "japan",
    "germany", "france", "united kingdom", "uk", "spain", "italy",
    "canada", "mexico", "brazil", "india", "australia", "south korea", "korea",
    "russia", "netherlands", "switzerland", "sweden", "norway",
}

# Cardinal-direction + region combos (e.g. "South Asia", "North Africa")
_GEO_CARDINAL = _re.compile(
    r"^(north|south|east|west|central|greater|upper|lower)\s+"
    r"(america|americas|asia|africa|europe|pacific|atlantic|china|korea|india|europe)s?$"
)

# Phrases that always indicate geography regardless of surrounding words
_GEO_PATTERNS = _re.compile(
    r"\b(rest\s+of(\s+the)?\s+(world|asia|europe|america|africa|pacific))\b"
    r"|"
    r"\b(other\s+(countries|geographies|regions|territories|markets))\b"
    r"|"
    r"\b(emerging|developed|frontier)\s+markets?\b",
    _re.IGNORECASE,
)


def _is_geographic(name: str) -> bool:
    nl = name.lower().strip()
    # Exact match against the geo-name list
    if nl in _GEO_NAMES:
        return True
    # Starts-with match — but only when followed by another geographic word OR
    # by region/segment/area/markets (so "Greater China Region" → strip; but
    # "Global Banking and Markets" and "International Wealth and Premier
    # Banking" survive because "Banking" / "Wealth" aren't geo words).
    for t in _GEO_NAMES:
        if nl == t:
            return True
        if nl.startswith(t + " "):
            tail = nl[len(t) + 1:]
            if _re.match(
                r"^(region|segment|area|"
                r"and\s+(other|america|asia|europe|africa|pacific|china|"
                r"middle\s+east|emea|apac|latam)|"
                r"&\s+(americas?|asia|europe|africa|pacific)|"
                r"(north|south|east|west|central)|"
                r"speaking\s+countries)\b",
                tail,
            ):
                return True
    # Cardinal-direction + region pattern (South Asia, North Africa, …)
    if _GEO_CARDINAL.match(nl):
        return True
    # Phrase patterns anywhere in the name
    if _GEO_PATTERNS.search(nl):
        return True
    # Insurance/financial multi-region labels (Allianz, AXA, etc.)
    # e.g. "German Speaking Countries", "Iberia & Latin America",
    # "Western & Southern Europe", "Central and Eastern Europe"
    if _re.search(
        r"\b(german[\s\-]speaking|iberia|nordics?|benelux|dach|"
        r"central[\s\&]+(and\s+)?eastern\s+europe|"
        r"western[\s\&]+(and\s+)?southern\s+europe|"
        r"northern[\s\&]+(and\s+)?eastern\s+europe|"
        r"asia[\s\&]+(pacific|africa)|"
        r"middle\s+east\s+(and|&)\s+africa|"
        r"european\s+(union|markets?)|"
        r"sub[\s\-]saharan|trans[\s\-]atlantic)\b",
        nl,
    ):
        return True
    # Compound names where the WHOLE name is a region/country reference, OR
    # a brand qualifier preceding a region word (e.g. "Allianz Asia Pacific",
    # "Greater China Region", "AXA Europe"). We deliberately do NOT match
    # business-segment names where geography is just a sub-qualifier, e.g.
    # PEP's "Frito-Lay North America" (the segment IS Frito-Lay; the region is
    # a scope marker, not the segment identity) or HSBC's "International Wealth
    # and Premier Banking" (the segment is the wealth business, "International"
    # is a scope marker — see _GEO_NAMES exclusions earlier).
    geo_words = (
        r"(north\s+america|south\s+america|latin\s+america|americas?|"
        r"europe|africa|asia|pacific|"
        r"germany|france|united\s+kingdom|spain|italy|china|japan|india|"
        r"canada|mexico|brazil|australia|korea|"
        r"emea|apac|latam|dach|nordic)"
    )
    # Strict whole-name geo: "north america", "asia pacific", "greater china",
    # "europe & americas", optional company prefix word (e.g. "AXA Europe").
    # Names with 3+ non-geographic words preceding the region word stay (they
    # are business-line + region labels, not pure geographic segments).
    geo_re = _re.compile(rf"^\s*([a-z]+\s+)?{geo_words}(\s+(region|segment|area|markets?))?\s*$")
    if geo_re.match(nl):
        return True
    return False


def _round1(v: float) -> float:
    return round(v, 1)


def validate_and_fix(data: dict, skip_fx_conversion: bool = False, skip_geo_strip: bool = False) -> dict:
    """
    Validate and auto-fix extracted financial data in-place.
    Returns the (modified) data dict.

    When skip_fx_conversion=True, the currency-conversion section is bypassed.
    Used by the synthesizer where the synthetic dict's values are already in the
    parent's currency, so any FX lookup would double-convert.

    When skip_geo_strip=True, the geographic-segment stripper is bypassed. Used
    by the synthesizer because the parent extraction's segments were already
    stripped — re-running can over-strip business-segment names that happen to
    contain geographic-looking words (e.g. HSBC's "UK", "Hong Kong",
    "International Wealth and Premier Banking" are real reportable segments).
    """
    inc = data.get("income_statement")
    if not inc:
        return data

    # ── Pull raw values ────────────────────────────────────────────────────────
    tr   = _num(inc.get("total_revenue"))
    cor  = _num(inc.get("cost_of_revenue"))
    gp   = _num(inc.get("gross_profit"))
    oi   = _num(inc.get("operating_income"))
    toe  = _num(inc.get("total_operating_expenses"))
    ni   = _num(inc.get("net_income"))
    tax  = _num(inc.get("tax_expense"))
    oie  = _num(inc.get("other_income_expense"))
    ibt  = _num(inc.get("income_before_tax"))

    opex = inc.get("operating_expenses") or {}
    rd   = _num(opex.get("research_and_development"))
    sga  = _num(opex.get("selling_general_administrative"))
    sm   = _num(opex.get("sales_and_marketing"))
    ga   = _num(opex.get("general_and_administrative"))
    oo   = _num(opex.get("other_operating"))

    # ── 1. Normalise OpEx sub-fields ──────────────────────────────────────────
    # Combine S&M + G&A → selling_general_administrative if not already combined
    if sga is None and (sm is not None or ga is not None):
        parts = [v for v in (sm, ga) if v is not None]
        sga = _round1(sum(parts)) if parts else None
        opex["selling_general_administrative"] = sga

    # ── 2. Derive total_operating_expenses ────────────────────────────────────
    if toe is None:
        parts = [v for v in (rd, sga, oo) if v is not None]
        if parts:
            toe = _round1(sum(parts))
            inc["total_operating_expenses"] = toe

    # ── 3. Derive gross_profit ────────────────────────────────────────────────
    if gp is None and tr is not None and cor is not None:
        gp = _round1(tr - cor)
        inc["gross_profit"] = gp

    # ── 4. Derive cost_of_revenue ─────────────────────────────────────────────
    if cor is None and tr is not None and gp is not None:
        cor = _round1(tr - gp)
        inc["cost_of_revenue"] = cor

    # ── 5. Derive operating_income ────────────────────────────────────────────
    if oi is None:
        if gp is not None and toe is not None:
            oi = _round1(gp - toe)
            inc["operating_income"] = oi
        elif tr is not None and toe is not None and gp is None:
            # Bank / no-gross-profit structure
            oi = _round1(tr - toe)
            inc["operating_income"] = oi

    # ── 6. Derive / reconcile total_operating_expenses against GP − OI ────────
    # Enforce the accounting identity: Gross Profit − Operating Expenses = Operating Income
    # If toe is missing OR disagrees with (gp − oi) by more than 0.05B, recompute it.
    if gp is not None and oi is not None:
        derived_toe = _round1(gp - oi)
        if toe is None or abs((toe or 0) - derived_toe) > 0.05:
            toe = derived_toe
            inc["total_operating_expenses"] = toe
    elif gp is None and tr is not None and oi is not None:
        # No-gross-profit structure (banks, online travel like BKNG, etc.):
        # TR − Operating Expenses = Operating Income. Reconcile if toe is missing
        # or significantly disagrees (e.g. XBRL pieces missed line items like
        # Marketing / Personnel that aren't in R&D or SG&A).
        derived_toe = _round1(tr - oi)
        if toe is None or abs((toe or 0) - derived_toe) > 0.05:
            toe = derived_toe
            inc["total_operating_expenses"] = toe

    # ── 7. Residual OpEx bucket ───────────────────────────────────────────────
    # If R&D + SGA < total_opex, put the remainder in other_operating
    if toe is not None:
        known = sum(v for v in (rd, sga, oo) if v is not None)
        residual = _round1(toe - known)
        if residual > 0.15:
            opex["other_operating"] = residual

    inc["operating_expenses"] = opex

    # ── 8. Derive other_income_expense ────────────────────────────────────────
    if oie is None and oi is not None and tax is not None and ni is not None:
        computed = _round1(ni - oi + tax)   # oie = NI - OI + tax  ↔  NI = OI + oie - tax
        if abs(computed) >= 0.1:
            inc["other_income_expense"] = computed

    # ── 9. Derive income_before_tax ───────────────────────────────────────────
    if ibt is None and oi is not None and oie is not None:
        inc["income_before_tax"] = _round1(oi + oie)
    elif ibt is None and ni is not None and tax is not None:
        inc["income_before_tax"] = _round1(ni + tax)

    # ── 10. Bank detection ────────────────────────────────────────────────────
    # Refresh after derivations
    cor2 = _num(inc.get("cost_of_revenue"))
    gp2  = _num(inc.get("gross_profit"))
    is_bank = (cor2 is None or cor2 == 0) and (gp2 is None or gp2 == 0)
    data["_is_bank"] = is_bank

    # ── 10a. Currency conversion to USD ──────────────────────────────────────
    # Normalize currency aliases (RMB→CNY etc.) before any FX work
    raw_cur = data.get("currency", "USD")
    canon_cur = _CURRENCY_ALIASES.get(raw_cur.upper(), raw_cur.upper()) if raw_cur else "USD"
    if canon_cur != raw_cur:
        data["currency"] = canon_cur

    # Auto-fill exchange rate from Yahoo Finance if LLM didn't extract one
    if (not skip_fx_conversion) and data.get("currency", "USD") != "USD" and not _num(data.get("exchange_rate_to_usd")):
        report_date = data.get("report_date", "")
        looked_up = _lookup_fx_rate(data["currency"], report_date)
        if looked_up:
            data["exchange_rate_to_usd"] = looked_up
            print(f"  FX lookup: {data['currency']}/USD = {looked_up} (report_date={report_date})")

    # If the filing reports in a non-USD currency, convert all monetary IS fields
    # and segment revenues to USD using the exchange rate stated in the filing.
    fx = _num(data.get("exchange_rate_to_usd"))
    if (not skip_fx_conversion) and fx and fx > 0 and data.get("currency", "USD") != "USD":
        print(f"  Currency: {data['currency']} → USD (÷ {fx})")
        _MONEY_KEYS = [
            "total_revenue", "cost_of_revenue", "gross_profit",
            "total_operating_expenses", "operating_income",
            "other_income_expense", "income_before_tax", "tax_expense", "net_income",
        ]
        for k in _MONEY_KEYS:
            v = _num(inc.get(k))
            if v is not None:
                inc[k] = _round1(v / fx)
        opex2 = inc.get("operating_expenses") or {}
        for k in list(opex2.keys()):
            v = _num(opex2.get(k))
            if v is not None:
                opex2[k] = _round1(v / fx)
        inc["operating_expenses"] = opex2
        # Convert segment revenues
        for seg in data.get("segments") or []:
            for fld in ("revenue", "operating_income"):
                v = _num(seg.get(fld))
                if v is not None:
                    seg[fld] = _round1(v / fx)
            for ss in seg.get("sub_segments") or []:
                v = _num(ss.get("revenue"))
                if v is not None:
                    ss["revenue"] = _round1(v / fx)
        data["currency"] = "USD"
        data["income_statement"] = inc

    # ── 10b. Strip geographic segments ────────────────────────────────────────
    segs_raw = data.get("segments") or []
    if skip_geo_strip:
        segs_filtered = segs_raw
    else:
        segs_filtered = [s for s in segs_raw if not _is_geographic(s.get("name", ""))]
        if len(segs_filtered) < len(segs_raw):
            removed = [s["name"] for s in segs_raw if _is_geographic(s.get("name", ""))]
            print(f"  Removed {len(segs_raw) - len(segs_filtered)} geographic segment(s): {removed}")
    data["segments"] = segs_filtered

    # ── 10c. Deduplicate overlapping segment breakdowns ───────────────────────
    # If segment sum > 1.2× total revenue the LLM mixed two orthogonal breakdowns
    # (e.g. TSMC: technology-node breakdown + end-market breakdown).
    # Strategy 1: drop all tech-classification segments (nm nodes) first — they are
    # always the less informative breakdown. If the remainder sums to ≈ total, done.
    # Strategy 2: fallback greedy removal if Strategy 1 doesn't resolve it.
    segs = data.get("segments") or []
    tr2  = _num(inc.get("total_revenue"))
    if segs and tr2:
        seg_sum = sum(_num(s.get("revenue")) or 0 for s in segs)
        if seg_sum > tr2 * 1.2:
            print(f"  Dedup: seg sum {seg_sum:.1f}B >> total {tr2:.1f}B — removing overlapping breakdown")
            # Strategy 1: remove tech-node segments (nanometer classifications)
            business_segs = [s for s in segs if not _is_tech_classification(s.get("name", ""))]
            biz_sum = sum(_num(s.get("revenue")) or 0 for s in business_segs)
            if business_segs and biz_sum > 0 and abs(biz_sum - tr2) / tr2 <= 0.20:
                removed = [s["name"] for s in segs if _is_tech_classification(s.get("name", ""))]
                if removed:
                    print(f"  Dedup: removed tech-node segments: {removed}")
                segs = business_segs
            else:
                # Strategy 2: greedy removal — drop whichever segment brings sum closest to total
                working = list(segs)
                while len(working) > 1:
                    cur_sum = sum(_num(s.get("revenue")) or 0 for s in working)
                    if cur_sum <= tr2 * 1.15:
                        break
                    best_i, best_dist = 0, float("inf")
                    for i, s in enumerate(working):
                        after = cur_sum - (_num(s.get("revenue")) or 0)
                        dist  = abs(after - tr2)
                        if dist < best_dist:
                            best_dist, best_i = dist, i
                    removed_seg = working.pop(best_i)
                    print(f"  Dedup: removed '{removed_seg['name']}'")
                segs = working
            data["segments"] = segs

    # ── 10d. Auto-correct segments still in local-currency trillions ─────────────
    # Happens when the filing shows values in NT$/EUR/etc. millions but the LLM
    # divided by 10^6 (→ trillions) instead of 10^3 (→ billions), AND the IS was
    # extracted from the USD column so no FX conversion was applied to segments.
    # Heuristic: currency=="USD", exchange_rate present, seg_sum << total_revenue,
    # AND seg_sum × exchange_rate ≈ total_revenue (within 30%).
    fx3  = _num(data.get("exchange_rate_to_usd"))
    segs_raw2 = data.get("segments") or []
    tr_now = _num(inc.get("total_revenue"))
    if fx3 and fx3 > 0 and data.get("currency") == "USD" and tr_now and segs_raw2:
        seg_sum_now = sum(_num(s.get("revenue")) or 0 for s in segs_raw2)
        if (seg_sum_now > 0
                and seg_sum_now < tr_now * 0.15
                and abs(seg_sum_now * fx3 - tr_now) / tr_now <= 0.30):
            print(f"  Auto-scale: seg sum {seg_sum_now:.2f} × {fx3} ≈ {tr_now:.1f} → multiplying segments by FX rate")
            for seg in segs_raw2:
                for fld in ("revenue", "operating_income"):
                    v = _num(seg.get(fld))
                    if v is not None:
                        seg[fld] = _round1(v * fx3)
                for ss in (seg.get("sub_segments") or []):
                    v = _num(ss.get("revenue"))
                    if v is not None:
                        ss["revenue"] = _round1(v * fx3)
            data["segments"] = segs_raw2

    # ── 11. Segment sum sanity check ──────────────────────────────────────────
    segs = data.get("segments") or []
    seg_sum = 0.0
    if segs and tr2:
        seg_sum = sum(_num(s.get("revenue")) or 0 for s in segs)
        if seg_sum > 0:
            deviation = abs(seg_sum - tr2) / tr2
            if deviation > 0.10:
                print(
                    f"  WARN: segment sum {seg_sum:.1f}B vs total_revenue {tr2:.1f}B "
                    f"({deviation*100:.0f}% off)"
                )
            if seg_sum < tr2 * 0.90:
                print(
                    f"  WARNING: Segments only cover {seg_sum/tr2*100:.0f}% of revenue"
                    f" ({seg_sum:.1f}B / {tr2:.1f}B) — possible missing segments"
                )

    # ── 11b. Sub-segment residual fill ───────────────────────────────────────
    # If a segment has sub_segments whose revenues don't sum to the segment total,
    # append an "Other <Segment Name>" entry to absorb the gap.
    # Only fires when:
    #   - segment has explicit revenue AND at least one sub_segment with revenue
    #   - residual > 0.05B AND > 2% of segment revenue (rules out rounding noise)
    # Sub-segments that *exceed* the segment total are left unchanged (LLM error;
    # dedup above handles segment-level over-counting).
    for seg in (data.get("segments") or []):
        seg_rev = _num(seg.get("revenue"))
        if seg_rev is None or seg_rev <= 0:
            continue
        sub_segs = seg.get("sub_segments") or []
        sub_revs = [_num(ss.get("revenue")) for ss in sub_segs]
        if not any(r is not None for r in sub_revs):
            continue  # no sub-segment has a revenue → nothing to reconcile
        sub_sum = sum(r for r in sub_revs if r is not None)
        residual = _round1(seg_rev - sub_sum)
        if residual > 0.05 and residual / seg_rev > 0.02:
            seg["sub_segments"] = list(sub_segs) + [{
                "name":    f"Other {seg['name']}",
                "revenue": residual,
                "yoy_growth_pct": None,
            }]

    # ── 11c. Retry flag ───────────────────────────────────────────────────────
    # Set when segments are empty so the pipeline can retry with a larger window.
    data["_needs_retry"] = not bool(data.get("segments"))

    # ── 12. Quality score (0–100) ─────────────────────────────────────────────
    score = 0
    if segs:
        score += 40  # segments found
        if any(
            ss.get("revenue")
            for s in segs
            for ss in (s.get("sub_segments") or [])
        ):
            score += 10  # has sub-segments with revenue
        if seg_sum > 0 and tr2 and abs(seg_sum - tr2) / tr2 <= 0.10:
            score += 20  # segments sum within 10% of total revenue
    tr3 = _num(inc.get("total_revenue"))
    oi3 = _num(inc.get("operating_income"))
    ni3 = _num(inc.get("net_income"))
    if tr3 and oi3 and ni3:
        score += 30  # income statement complete
    elif tr3 and ni3:
        score += 15
    data["_quality_score"] = score

    data["income_statement"] = inc
    return data


def validate_company_consistency(all_extracted: list[dict]) -> dict:
    """
    Cross-filing consistency checks for a single company.
    Expects all_extracted sorted annual-first (as produced by process_edgar_stock).

    Returns:
        {
            "grade":  "A" | "B" | "C" | "D" | "F",
            "issues": [str, ...]          # human-readable warnings
        }
    """
    issues: list[str] = []
    penalty = 0  # accumulates; grade thresholds below

    annuals     = [d for d in all_extracted if _is_annual(d)]
    quarterlies = [d for d in all_extracted if not _is_annual(d)]

    annual = annuals[0] if annuals else None

    # ── a) Segment name check ─────────────────────────────────────────────────
    if annual and annual.get("segments"):
        annual_names = {s["name"].lower() for s in annual["segments"] if s.get("name")}
        for q in quarterlies:
            q_segs = q.get("segments") or []
            if not q_segs:
                continue
            q_names = {s["name"].lower() for s in q_segs if s.get("name")}
            extra   = q_names - annual_names
            missing = annual_names - q_names
            label   = f"{q.get('fiscal_year','?')} {q.get('fiscal_period','?')}"
            if extra:
                issues.append(f"{label}: unexpected segment name(s): {sorted(extra)}")
                penalty += 5
            if missing:
                issues.append(f"{label}: missing annual segment(s): {sorted(missing)}")
                penalty += 3

    # ── b) Revenue trend check (>100% QoQ jump per segment) ──────────────────
    # Sort quarterlies by report_date for sequential comparison
    sorted_q = sorted(
        quarterlies,
        key=lambda d: d.get("report_date") or d.get("filing_date") or "",
    )
    for i in range(1, len(sorted_q)):
        prev, curr = sorted_q[i - 1], sorted_q[i]
        prev_segs = {s["name"].lower(): _num(s.get("revenue")) for s in (prev.get("segments") or [])}
        curr_segs = {s["name"].lower(): _num(s.get("revenue")) for s in (curr.get("segments") or [])}
        label = f"{curr.get('fiscal_year','?')} {curr.get('fiscal_period','?')}"
        for name, cur_rev in curr_segs.items():
            prev_rev = prev_segs.get(name)
            if cur_rev and prev_rev and prev_rev > 0:
                pct_chg = abs(cur_rev - prev_rev) / prev_rev * 100
                if pct_chg > 100:
                    issues.append(
                        f"{label}: segment '{name}' changed by {pct_chg:.0f}% QoQ "
                        f"({prev_rev:.1f}B → {cur_rev:.1f}B)"
                    )
                    penalty += 8

    # ── c) Quarterly sum vs annual total ──────────────────────────────────────
    if annual:
        annual_rev = _num((annual.get("income_statement") or {}).get("total_revenue"))
        if annual_rev and annual_rev > 0:
            # Group quarterlies by fiscal_year; require all 4 quarters present
            from collections import defaultdict
            by_fy: dict[str, list[dict]] = defaultdict(list)
            for q in quarterlies:
                fy = q.get("fiscal_year") or ""
                by_fy[fy].append(q)

            annual_fy = annual.get("fiscal_year") or ""
            qs_for_fy = by_fy.get(annual_fy, [])
            q_revs = [
                _num((q.get("income_statement") or {}).get("total_revenue"))
                for q in qs_for_fy
            ]
            q_revs_valid = [r for r in q_revs if r is not None]
            if len(q_revs_valid) == 4:
                q_sum = sum(q_revs_valid)
                deviation = abs(q_sum - annual_rev) / annual_rev
                if deviation > 0.05:
                    issues.append(
                        f"Q1+Q2+Q3+Q4 sum ({q_sum:.1f}B) vs annual ({annual_rev:.1f}B) "
                        f"— {deviation*100:.0f}% off (threshold 5%)"
                    )
                    penalty += 15 if deviation > 0.10 else 8

    # ── d) Income statement trend — IS field outliers within same filing type ──
    # Compare quarters to quarters and annuals to annuals separately to avoid
    # flagging the expected ~4× size difference between annual and quarterly totals.
    for field in ("total_revenue", "net_income"):
        for group in (annuals, sorted_q):
            vals: list[tuple[str, float]] = []
            for d in group:
                v = _num((d.get("income_statement") or {}).get(field))
                label = f"{d.get('fiscal_year','?')} {d.get('fiscal_period','?')}"
                if v is not None:
                    vals.append((label, v))
            if len(vals) >= 3:
                numbers = [v for _, v in vals]
                mean = sum(numbers) / len(numbers)
                if mean > 0:
                    for label, v in vals:
                        if abs(v - mean) / mean > 1.0:
                            issues.append(
                                f"IS outlier: {field} for {label} is {v:.1f}B "
                                f"vs peer mean {mean:.1f}B ({abs(v-mean)/mean*100:.0f}% off)"
                            )
                            penalty += 5

    # ── Grade ─────────────────────────────────────────────────────────────────
    if not annuals:
        grade = "F"
    elif penalty == 0:
        grade = "A"
    elif penalty <= 8:
        grade = "B"
    elif penalty <= 20:
        grade = "C"
    elif penalty <= 35:
        grade = "D"
    else:
        grade = "F"

    return {"grade": grade, "issues": issues}


def _is_annual(d: dict) -> bool:
    """Return True if this extraction is from an annual report."""
    fp = (d.get("fiscal_period") or "").upper()
    ft = (d.get("form_type") or "").upper()
    return fp == "FY" or ft in ("10-K", "20-F", "40-F")


_ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
_QUARTERLY_FORMS = {"10-Q", "6-K"}


def derive_fiscal_period(form_type: str, report_date: str | None = None,
                         fy_end_date: str | None = None,
                         filing_date: str | None = None) -> str:
    """
    Compute fiscal_period deterministically from the filing form_type and dates.

    - 10-K / 20-F / 40-F  → 'FY'
    - 10-Q / 6-K          → 'Q1'/'Q2'/'Q3'/'Q4' based on report_date vs fy_end_date
                            (report_date is the period-end). If only filing_date is
                            available, subtract a 45-day lag to approximate period end.
    - anything else       → '' (let the LLM's value stand)
    """
    from datetime import datetime as _dt, timedelta as _td
    ft = (form_type or "").upper()
    if ft in _ANNUAL_FORMS:
        return "FY"
    if ft not in _QUARTERLY_FORMS:
        return ""

    # Pick the best available period-end date.
    # For 6-K filings, EDGAR's `reportDate` is frequently the filing date itself
    # (foreign filers don't report a separate period-end), so always back-shift by
    # 45 days to estimate the actual period end. 10-Qs have a reliable reportDate
    # and don't need this correction.
    rd_str = report_date if report_date else None
    if ft == "6-K" and filing_date:
        try:
            rd_str = (_dt.strptime(filing_date[:10], "%Y-%m-%d") - _td(days=45)).strftime("%Y-%m-%d")
        except Exception:
            pass
    if not rd_str and filing_date:
        try:
            rd_str = (_dt.strptime(filing_date[:10], "%Y-%m-%d") - _td(days=45)).strftime("%Y-%m-%d")
        except Exception:
            return ""
    if not rd_str:
        return ""
    try:
        rd = _dt.strptime(rd_str[:10], "%Y-%m-%d")
    except Exception:
        return ""

    # Fiscal year end month (defaults to Dec for calendar-year filers)
    fy_end_month = 12
    if fy_end_date:
        try:
            fy_end_month = _dt.strptime(fy_end_date[:10], "%Y-%m-%d").month
        except Exception:
            pass

    # Months from FY end to this report's period end (1..12)
    months_after_end = (rd.month - fy_end_month) % 12
    if months_after_end == 0:
        months_after_end = 12   # exactly one year on → Q4 close
    quarter = ((months_after_end - 1) // 3) + 1   # 1..4
    return f"Q{quarter}"


def _num(v) -> float | None:
    """Coerce to float or return None."""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None   # reject NaN
    except (TypeError, ValueError):
        return None


# ────────────────────────────────────────────────────────────────────────────
# Q4 synthesis
# US companies file 10-K (= FY) instead of a standalone Q4 10-Q. For any
# fiscal year that has FY + Q1 + Q2 + Q3 cached, compute Q4 by subtraction.
# ────────────────────────────────────────────────────────────────────────────

def _sub4(fy, q1, q2, q3) -> float | None:
    """Q4 = FY - Q1 - Q2 - Q3 for scalar numeric fields. None if any input is None."""
    fy_n, q1_n, q2_n, q3_n = _num(fy), _num(q1), _num(q2), _num(q3)
    if None in (fy_n, q1_n, q2_n, q3_n):
        return None
    return round(fy_n - q1_n - q2_n - q3_n, 1)


def _subtract_is(fy_is: dict, q1_is: dict, q2_is: dict, q3_is: dict) -> dict:
    """Compute Q4 income statement by per-field subtraction. YoY %s and breakdowns
    cannot be subtracted meaningfully and are set to None."""
    scalar_fields = (
        "total_revenue", "cost_of_revenue", "gross_profit",
        "total_operating_expenses", "operating_income", "other_income_expense",
        "income_before_tax", "tax_expense", "net_income",
    )
    out = {f: _sub4(fy_is.get(f), q1_is.get(f), q2_is.get(f), q3_is.get(f))
           for f in scalar_fields}
    for k in ("total_revenue_yoy_pct", "cost_of_revenue_yoy_pct",
              "cost_of_revenue_breakdown", "gross_profit_yoy_pct",
              "operating_income_yoy_pct", "net_income_yoy_pct"):
        out[k] = None
    fy_oe = fy_is.get("operating_expenses") or {}
    q1_oe = q1_is.get("operating_expenses") or {}
    q2_oe = q2_is.get("operating_expenses") or {}
    q3_oe = q3_is.get("operating_expenses") or {}
    keys = set(fy_oe) | set(q1_oe) | set(q2_oe) | set(q3_oe)
    out["operating_expenses"] = {
        k: _sub4(fy_oe.get(k), q1_oe.get(k), q2_oe.get(k), q3_oe.get(k))
        for k in keys
    }
    return out


def _find_by_name(items: list[dict], name: str) -> dict:
    target = (name or "").strip().lower()
    for item in items:
        if (item.get("name") or "").strip().lower() == target:
            return item
    return {}


def _subtract_segments(fy_segs: list, q1_segs: list, q2_segs: list, q3_segs: list) -> list:
    """Compute Q4 segments by matching names across periods and subtracting per-segment
    revenue + operating_income. Drops segments whose Q4 revenue can't be computed
    or would be negative (sign of segment-definition drift across periods)."""
    out: list[dict] = []
    for fy_seg in fy_segs or []:
        name = (fy_seg.get("name") or "").strip()
        if not name:
            continue
        q1 = _find_by_name(q1_segs or [], name)
        q2 = _find_by_name(q2_segs or [], name)
        q3 = _find_by_name(q3_segs or [], name)
        q4_rev = _sub4(fy_seg.get("revenue"), q1.get("revenue"),
                       q2.get("revenue"), q3.get("revenue"))
        if q4_rev is None or q4_rev < 0:
            continue
        q4_oi = _sub4(fy_seg.get("operating_income"), q1.get("operating_income"),
                      q2.get("operating_income"), q3.get("operating_income"))
        sub_q4 = []
        for fy_sub in fy_seg.get("sub_segments") or []:
            sub_name = (fy_sub.get("name") or "").strip()
            if not sub_name:
                continue
            q1s = _find_by_name(q1.get("sub_segments") or [], sub_name)
            q2s = _find_by_name(q2.get("sub_segments") or [], sub_name)
            q3s = _find_by_name(q3.get("sub_segments") or [], sub_name)
            sub_rev = _sub4(fy_sub.get("revenue"), q1s.get("revenue"),
                            q2s.get("revenue"), q3s.get("revenue"))
            if sub_rev is not None:
                sub_q4.append({"name": sub_name, "revenue": sub_rev, "yoy_growth_pct": None})
        out.append({
            "name": name,
            "revenue": q4_rev,
            "operating_income": q4_oi,
            "yoy_growth_pct": None,
            "sub_segments": sub_q4,
        })
    return out


def _sub2(a, b) -> float | None:
    """a - b for scalar numeric fields. None if either is None."""
    a_n, b_n = _num(a), _num(b)
    if None in (a_n, b_n):
        return None
    return round(a_n - b_n, 1)


def _subtract_is_pair(a_is: dict, b_is: dict) -> dict:
    """Compute period_a - period_b for income statement (per-field)."""
    scalar_fields = (
        "total_revenue", "cost_of_revenue", "gross_profit",
        "total_operating_expenses", "operating_income", "other_income_expense",
        "income_before_tax", "tax_expense", "net_income",
    )
    out = {f: _sub2(a_is.get(f), b_is.get(f)) for f in scalar_fields}
    for k in ("total_revenue_yoy_pct", "cost_of_revenue_yoy_pct",
              "cost_of_revenue_breakdown", "gross_profit_yoy_pct",
              "operating_income_yoy_pct", "net_income_yoy_pct"):
        out[k] = None
    a_oe = a_is.get("operating_expenses") or {}
    b_oe = b_is.get("operating_expenses") or {}
    keys = set(a_oe) | set(b_oe)
    out["operating_expenses"] = {k: _sub2(a_oe.get(k), b_oe.get(k)) for k in keys}
    return out


def _subtract_segments_pair(a_segs: list, b_segs: list) -> list:
    """Compute period_a - period_b for segment-level data, matching by name."""
    out: list[dict] = []
    for a_seg in a_segs or []:
        name = (a_seg.get("name") or "").strip()
        if not name:
            continue
        b = _find_by_name(b_segs or [], name)
        new_rev = _sub2(a_seg.get("revenue"), b.get("revenue"))
        if new_rev is None or new_rev < 0:
            continue
        new_oi = _sub2(a_seg.get("operating_income"), b.get("operating_income"))
        sub_out = []
        for a_sub in a_seg.get("sub_segments") or []:
            sub_name = (a_sub.get("name") or "").strip()
            if not sub_name:
                continue
            b_sub = _find_by_name(b.get("sub_segments") or [], sub_name)
            sub_rev = _sub2(a_sub.get("revenue"), b_sub.get("revenue"))
            if sub_rev is not None:
                sub_out.append({"name": sub_name, "revenue": sub_rev, "yoy_growth_pct": None})
        out.append({
            "name": name,
            "revenue": new_rev,
            "operating_income": new_oi,
            "yoy_growth_pct": None,
            "sub_segments": sub_out,
        })
    return out


def _make_synthetic(template_d: dict, period: str, ticker: str, fy: str,
                    is_calc: dict, seg_calc: list, notes: str) -> dict:
    """Helper to build a synthetic-period extraction dict in the standard schema."""
    d = {
        "company":              template_d.get("company"),
        "ticker":               template_d.get("ticker", ticker),
        "report_date":          template_d.get("report_date"),
        "form_type":            "Calculated",
        "fiscal_year":          fy,
        "fiscal_period":        period,
        "currency":             template_d.get("currency"),
        "exchange_rate_to_usd": template_d.get("exchange_rate_to_usd"),
        "unit":                 "billions",
        "segments":             seg_calc,
        "income_statement":     is_calc,
        "notes":                notes,
        "_quality_score":       80,
        "_synthetic_q4":        True,
    }
    # Enforce accounting identities (gp − total_opex = op_income, R&D + SG&A = total_opex, etc.)
    # Skip FX conversion (values already converted in parent) and skip geographic-segment
    # stripping (parent already stripped — re-running can over-strip business segments
    # whose names look geographic like HSBC's "UK" / "Hong Kong" / "International …").
    # FX lookup here would double-convert them (parent never went through FX conversion).
    return validate_and_fix(d, skip_fx_conversion=True, skip_geo_strip=True)


def _cache_synthetic(d: dict, ticker: str, period: str, fy: str) -> Path:
    """Write a synthetic extraction to disk and return its path."""
    import json
    from pathlib import Path
    from config import CACHE_DIR
    cache_root = Path(CACHE_DIR) / "extractions"
    cache_root.mkdir(parents=True, exist_ok=True)
    safe_fy = fy.replace("/", "_").replace(" ", "_")
    safe_p  = period.upper().replace("/", "_")
    cache_key = f"{ticker.replace('.', '_')}_{safe_p}_calc_{safe_fy}_extracted.json"
    cache_path = cache_root / cache_key
    cache_path.write_text(json.dumps(d, indent=2))
    return cache_path


def synthesize_missing_q4(all_extracted: list[dict], ticker: str) -> list[dict]:
    """
    Synthesize missing standalone periods by arithmetic across siblings:

      Q4 = FY  − Q1 − Q2 − Q3
      Q2 = H1  − Q1                (when standalone Q2 is absent but H1 exists)
      H2 = FY  − H1                (covers Q3+Q4 cumulative — useful for FY+H1-only filers)

    Appends each synthesized dict to all_extracted and writes a cache file
    under cache/extractions/{ticker}_{period}_calc_{fy}_extracted.json.
    """
    from collections import defaultdict

    by_fy: dict[str, dict[str, dict]] = defaultdict(dict)
    for d in all_extracted:
        fy = d.get("fiscal_year")
        fp = (d.get("fiscal_period") or "").upper()
        if not fy or fp not in ("FY", "Q1", "Q2", "Q3", "Q4", "H1", "H2", "9M"):
            continue
        prior = by_fy[fy].get(fp)
        if prior is None or d.get("_quality_score", 0) > prior.get("_quality_score", 0):
            by_fy[fy][fp] = d

    augmented = list(all_extracted)

    for fy, periods in by_fy.items():
        # ── 0. Q3 = 9M − H1 (when only 9-month YTD + H1 exist) ────────────────
        if "9M" in periods and "H1" in periods and "Q3" not in periods:
            ninem, h1 = periods["9M"], periods["H1"]
            is_q3  = _subtract_is_pair(ninem.get("income_statement") or {},
                                       h1.get("income_statement") or {})
            seg_q3 = _subtract_segments_pair(ninem.get("segments") or [],
                                             h1.get("segments") or [])
            rev = is_q3.get("total_revenue")
            if (rev is not None and rev >= 0) or seg_q3:
                d = _make_synthetic(
                    ninem, "Q3", ticker, fy, is_q3, seg_q3,
                    "Calculated as 9-month YTD minus H1 (standalone Q3)",
                )
                augmented.append(d)
                periods["Q3"] = d
                p = _cache_synthetic(d, ticker, "Q3", fy)
                print(f"  Synthesized Q3 for {fy} (rev={rev}B) → {p.name}")

        # ── 1. Q2 = H1 − Q1 (only when standalone Q2 is missing) ──────────────
        if "H1" in periods and "Q1" in periods and "Q2" not in periods:
            h1, q1 = periods["H1"], periods["Q1"]
            is_q2  = _subtract_is_pair(h1.get("income_statement") or {},
                                       q1.get("income_statement") or {})
            seg_q2 = _subtract_segments_pair(h1.get("segments") or [],
                                             q1.get("segments") or [])
            rev = is_q2.get("total_revenue")
            if rev is not None and rev >= 0 or seg_q2:
                d = _make_synthetic(
                    h1, "Q2", ticker, fy, is_q2, seg_q2,
                    "Calculated as H1 minus Q1 (standalone Q2 not separately filed)",
                )
                augmented.append(d)
                periods["Q2"] = d  # now available for Q4 computation below
                p = _cache_synthetic(d, ticker, "Q2", fy)
                print(f"  Synthesized Q2 for {fy} (rev={rev}B) → {p.name}")

        # ── 2. H2 = FY − H1 (covers Q3+Q4 for half-year-only filers) ──────────
        if "FY" in periods and "H1" in periods and "H2" not in periods:
            fy_d, h1 = periods["FY"], periods["H1"]
            is_h2  = _subtract_is_pair(fy_d.get("income_statement") or {},
                                       h1.get("income_statement") or {})
            seg_h2 = _subtract_segments_pair(fy_d.get("segments") or [],
                                             h1.get("segments") or [])
            rev = is_h2.get("total_revenue")
            if rev is not None and rev >= 0 or seg_h2:
                d = _make_synthetic(
                    fy_d, "H2", ticker, fy, is_h2, seg_h2,
                    "Calculated as FY minus H1 (second half-year)",
                )
                augmented.append(d)
                periods["H2"] = d
                p = _cache_synthetic(d, ticker, "H2", fy)
                print(f"  Synthesized H2 for {fy} (rev={rev}B) → {p.name}")

        # ── 3. Q4 = FY − Q1 − Q2 − Q3 (existing behavior) ─────────────────────
        if all(p in periods for p in ("FY", "Q1", "Q2", "Q3")) and "Q4" not in periods:
            fy_d, q1_d, q2_d, q3_d = (periods["FY"], periods["Q1"],
                                      periods["Q2"], periods["Q3"])
            is_q4 = _subtract_is(
                fy_d.get("income_statement") or {},
                q1_d.get("income_statement") or {},
                q2_d.get("income_statement") or {},
                q3_d.get("income_statement") or {},
            )
            seg_q4 = _subtract_segments(
                fy_d.get("segments") or [],
                q1_d.get("segments") or [],
                q2_d.get("segments") or [],
                q3_d.get("segments") or [],
            )
            rev = is_q4.get("total_revenue")
            if rev is not None and rev < 0:
                print(f"  Skipping synthetic Q4 for {fy}: implied revenue {rev}B is negative")
                continue
            if rev is None and not seg_q4:
                continue
            d = _make_synthetic(
                fy_d, "Q4", ticker, fy, is_q4, seg_q4,
                "Calculated as FY minus Q1+Q2+Q3 (no separate Q4 filing exists)",
            )
            augmented.append(d)
            periods["Q4"] = d
            p = _cache_synthetic(d, ticker, "Q4", fy)
            print(f"  Synthesized Q4 for {fy} (rev={rev}B) → {p.name}")

    return augmented
