"""
LLM-based segment data extractor using an OpenAI-compatible API endpoint.
Two-pass extraction: Pass 1 = income statement, Pass 2 = segments.
"""
import json
import uuid
from openai import OpenAI
from config import LLM_API_BASE, LLM_API_KEY, LLM_MODEL

client = OpenAI(
    base_url=LLM_API_BASE + "/v1",
    api_key=LLM_API_KEY,
)

# Cache for sequential "batch" results (keyed by fake batch_id)
_BATCH_RESULTS_CACHE: dict[str, dict[str, str]] = {}

# ── System prompts ─────────────────────────────────────────────────────────────

_SYSTEM_IS = (
    "You are an expert financial analyst who extracts structured data from SEC filings. "
    "Return ONLY valid JSON — no markdown fences, no commentary."
)

_SYSTEM_SEG = (
    "You are an expert financial analyst who extracts segment revenue data from SEC filings. "
    "Return ONLY valid JSON — no markdown fences, no commentary."
)

# ── Pass 1: Income statement ───────────────────────────────────────────────────

_IS_PROMPT = """Analyze this {form_type} filing excerpt for {company} ({ticker}), report date {report_date}.

Extract ONLY the consolidated income statement. Return JSON with EXACTLY this structure
(all monetary values in BILLIONS rounded to 1 decimal; null if not stated):

{{
  "fiscal_year": "FY2025",
  "fiscal_period": "FY",
  "currency": "USD",
  "exchange_rate_to_usd": null,
  "total_revenue": 281.7,
  "total_revenue_yoy_pct": 14.9,
  "cost_of_revenue": 87.8,
  "cost_of_revenue_yoy_pct": null,
  "cost_of_revenue_breakdown": {{"product_costs": 13.5, "service_costs": 74.3}},
  "gross_profit": 193.9,
  "gross_profit_yoy_pct": 13.4,
  "operating_expenses": {{
    "research_and_development": 32.5,
    "selling_general_administrative": 32.9
  }},
  "total_operating_expenses": 65.4,
  "operating_income": 128.5,
  "operating_income_yoy_pct": 17.4,
  "other_income_expense": -4.9,
  "income_before_tax": 123.6,
  "tax_expense": 21.8,
  "net_income": 101.8,
  "net_income_yoy_pct": 15.5
}}

EXTRACTION RULES:
1. fiscal_period: "FY" for annual, "Q1"/"Q2"/"Q3"/"Q4" for quarterly.
2. selling_general_administrative: sum of Sales & Marketing + General & Administrative if reported separately.
   If only one is reported (e.g. only G&A), use that. If reported as a combined line, use that.
3. other_income_expense: POSITIVE = income (interest income, investment gains).
   NEGATIVE = expense (provision for credit losses, interest expense, losses).
4. cost_of_revenue_breakdown: include only if filing explicitly shows separate Product and Service cost lines.
   Otherwise set to null.
5. YoY percentages: include if explicitly stated in the filing; otherwise null.
6. exchange_rate_to_usd: if the filing reports in a non-USD currency (e.g. NT$, EUR, GBP, JPY),
   set this to the number of local currency units per 1 USD as stated in the filing
   (e.g. 31.11 for NT$31.11 = US$1). Set null if the filing reports in USD.
   All monetary values must still be expressed in the local currency (in billions), NOT converted.
   IMPORTANT: Report the PRIMARY local currency even if the filing also shows a USD conversion
   column. For example, TSMC's 20-F shows both NT$ and USD columns — report currency="TWD" with
   the NT$ values (in billions) and set exchange_rate_to_usd. Do NOT report currency="USD" just
   because a USD conversion column is present alongside the primary local-currency column.
7. UNITS: All monetary values must be in BILLIONS of the filing's primary currency.
   If the filing's table header says "(in millions)" or "(NT$ in millions)", divide the raw
   numbers by 1,000 to express in billions. Example: NT$3,809,054 millions → 3,809.1 billions.
   If the header says "(in thousands)", divide by 1,000,000.

COMPANY-SPECIFIC ALIASES:
- Apple (AAPL): "Total net sales" → total_revenue | "Cost of sales" → cost_of_revenue |
  "Gross margin" → gross_profit | "Income from operations" → operating_income.
- Banks / Financials (JPM, GS, BAC, etc.): cost_of_revenue = null, gross_profit = null.
  "Net revenue" or "Net interest income + Noninterest revenue" → total_revenue.
  "Provision for credit losses" → other_income_expense as a NEGATIVE number.
  "Noninterest expense" → total_operating_expenses.
- Energy (Shell, BP, XOM): "Total revenues and other income" → total_revenue.
  "Purchases and other costs" or production costs → cost_of_revenue.

Filing text:
{filing_text}"""


# ── Pass 2a: Template-guided quarterly segments ────────────────────────────────

_QUARTERLY_TEMPLATE_PROMPT = """Analyze this {form_type} filing for {company} ({ticker}), report date {report_date}.

This company uses the following segment structure (established from the annual report):
{template_json}

Extract the QUARTERLY (three-month) revenue figures for THESE EXACT segments from the filing below.

CRITICAL — STANDALONE QUARTER, NOT YTD:
  10-Q filings typically show TWO side-by-side columns for the current period:
    • "Three Months Ended <date>"  ← USE THIS  (standalone quarter, e.g. ~3 months of revenue)
    • "Six/Nine Months Ended <date>"  ← DO NOT USE  (cumulative year-to-date)
  ALWAYS extract from the THREE-MONTH column. NEVER extract YTD/cumulative values.

  Sanity check the result: the sum of all segment revenues for the quarter MUST be approximately
  equal to the company's total revenue for the quarter (within ~5%). If your extracted sum is
  noticeably larger than total_revenue, you almost certainly grabbed the YTD column — go back
  and use the three-month column instead.

OTHER RULES:
- Use the EXACT SAME segment names and sub-segment names as in the template above.
- Do NOT add new segments, rename segments, or remove segments.
- Fill in the revenue value for EVERY segment listed in the template. Search the filing
  carefully — segment tables in 10-Qs usually report all segments together.
- Only set revenue to null if the segment is genuinely not disclosed for this quarter.
- Fill in yoy_growth_pct if stated; otherwise null.
- operating_income: fill if stated per segment; otherwise null.

Return JSON with EXACTLY this structure (values in BILLIONS, rounded to 1 decimal):
{{
  "segments": [
    {{
      "name": "Exact Segment Name From Template",
      "revenue": 45.6,
      "operating_income": null,
      "yoy_growth_pct": 12.3,
      "sub_segments": [
        {{"name": "Sub-Segment Name", "revenue": 30.1, "yoy_growth_pct": null}}
      ]
    }}
  ]
}}

UNITS: All monetary values in BILLIONS of the filing's primary currency, rounded to 1 decimal.
If table header says "(in millions)", divide by 1,000.

Filing text:
{filing_text}"""


# ── Pass 2b: Revenue-only trading-update prompt ───────────────────────────────
# Used for European Q1/Q3 trading updates (LVMH, Nestlé) that report revenue by
# segment ONLY — no cost, gross profit, operating income, or net income.

_REV_ONLY_PROMPT = """Analyze this {form_type} trading update for {company} ({ticker}), report date {report_date}.

This is a REVENUE-ONLY interim release. It reports total and per-segment revenue
but does NOT contain a full income statement. Only extract what is genuinely
disclosed; do NOT fabricate cost or profit numbers.

The company uses the following segment structure (established from the annual report):
{template_json}

Return JSON with EXACTLY this structure (values in BILLIONS, rounded to 1 decimal):
{{
  "total_revenue": 22.3,
  "currency": "EUR",
  "segments": [
    {{
      "name": "Exact Segment Name From Template",
      "revenue": 8.5,
      "yoy_growth_pct": 4.2,
      "sub_segments": []
    }}
  ]
}}

Rules:
- Use EXACT segment names from the template above (do not invent new ones).
- Fill `revenue` for every segment disclosed; set to null if not separately reported.
- `total_revenue` is the headline group-level revenue figure.
- `currency`: use the report's primary currency (EUR, CHF, USD, etc.).
- `yoy_growth_pct`: organic / reported growth percentage if explicitly stated.
- Extract STANDALONE values when possible; if only cumulative YTD figures are
  given (e.g. a 9-month update), use those values — the pipeline downstream
  subtracts cumulative siblings to recover standalone quarters.

Filing text:
{filing_text}"""


# ── Pass 2: Segments ───────────────────────────────────────────────────────────

_SEG_PROMPT = """Analyze this {form_type} filing excerpt for {company} ({ticker}), report date {report_date}.

Extract ALL levels of product/business revenue breakdown. Return JSON with EXACTLY this structure
(all monetary values in BILLIONS rounded to 1 decimal; null if not stated):

{{
  "segments": [
    {{
      "name": "Exact Segment Name From Filing",
      "revenue": 45.6,
      "operating_income": 12.3,
      "yoy_growth_pct": 15.2,
      "sub_segments": [
        {{"name": "Product/Service Line", "revenue": 30.1, "yoy_growth_pct": null}}
      ]
    }}
  ]
}}

UNITS: All monetary values must be in BILLIONS of the filing's primary currency, rounded to 1 decimal.
If the filing's table header says "(in millions)" or "(NT$ in millions)", divide raw numbers by
1,000 to express in billions. Example: NT$2,192,931 millions → output 2,192.9 (billions).
If the header says "(in thousands)", divide by 1,000,000. Never express values in trillions.

EXTRACTION LEVELS:
  LEVEL 1 — Reportable/Operating Segments: the official CODM segments the company reports
    (e.g. Microsoft: Productivity & Business Processes / Intelligent Cloud / More Personal Computing).
  LEVEL 2 — Product lines within segments: finer-grained lines disclosed within each Level 1 segment
    (e.g. within Intelligent Cloud: Azure / Server products / Enterprise services).
  Always capture BOTH levels when the filing discloses them.

COMPANY TYPE GUIDANCE — every industry has a standard breakdown; find it:
  Tech:       product/service lines (Azure, Office 365, iPhone, Mac, Services, Data Center, Gaming)
  Banks:      business lines (Consumer Banking, Investment Banking, Asset Management, Wealth Management,
              Commercial Banking, Markets / Trading, Card Services)
  Energy:     activity lines (Upstream / Exploration, Downstream / Refining, Chemicals, Renewables,
              LNG, Marketing & Trading)
  Pharma:     therapeutic area or drug franchise (Oncology, Immunology, Cardiovascular, Rare Disease,
              Neuroscience) — or by named drug (Humira, Keytruda, Eliquis)
  Retail:     format or banner (Walmart US, Sam's Club, International; or eCommerce vs Stores)
  Industrial: division or end market (Automation, Smart Infrastructure, Digital Industries, Mobility)

LOOK HARDER FOR DETAIL:
  - If the company reports only 1–2 broad segments, search the MD&A and footnotes for product-line
    revenue tables, disaggregation of revenue notes, or "Revenue by Type" schedules.
  - Look for percentage breakdowns in MD&A that, multiplied by total revenue, give implicit amounts.
  - Search for tables labeled "Disaggregation of Revenue", "Revenue by Product/Service",
    "Net Sales by Category", "Business Segment Results", or "Segment Information".

RULES:
1. Treat as a segment ANY named entity with an explicit revenue/net-sales dollar amount —
   whether labeled "segment", "category", "product line", "market", "division", or "business unit".
   Examples:
   - Apple: iPhone / Mac / iPad / Wearables / Services from "net sales by category"
   - JPMorgan: Consumer & Community Banking / Commercial & Investment Bank / Asset & Wealth
     Management from "Business Segment Results"
   - NVIDIA: Data Center / Gaming / Professional Visualization / Automotive / OEM & Other
2. PRIORITY TIEBREAKER: When the filing contains BOTH a geographic segment table (North America /
   International / AWS) AND a product/service line table (Online stores / Advertising / AWS),
   extract ONLY from the product/service table; ignore the geographic table.
3. sub_segments: include when the filing gives explicit dollar amounts for finer lines WITHIN a
   Level 1 segment (e.g. Azure / Office 365 within Intelligent Cloud). Use [] if none exist.
4. Do NOT include the consolidated company total as a segment.
5. If no named revenue breakdown exists at all, return {{"segments": []}}.
6. NEVER include geographic regions at any level — not as segments, not as sub_segments.
   Excluded labels: North America, International, Americas, Europe, Asia Pacific, EMEA, APAC,
   LATAM, Latin America, Greater China, China, Japan, United States, United Kingdom, Germany,
   Rest of World, domestic, foreign, worldwide, global, and any other geographic split.
   Use ONLY named business units, product lines, or market verticals.
7. Banks/financials: every business line with an explicit net revenue figure is a segment.

Filing text:
{filing_text}"""


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    """Strip optional markdown fences and parse JSON."""
    s = text.strip()
    if s.startswith("```"):
        # remove opening fence line
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        # remove closing fence
        if "```" in s:
            s = s.rsplit("```", 1)[0]
    return json.loads(s.strip())


def _call_llm(prompt: str, system: str, max_tokens: int = 2048, model: str = LLM_MODEL) -> dict:
    """Call the LLM and return parsed JSON dict. Returns {} on error."""
    response = client.chat.completions.create(
        model=LLM_MODEL,  # always use the configured model regardless of argument
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        temperature=0,
        max_tokens=max_tokens,
    )
    raw = response.choices[0].message.content
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        print(f"  WARNING: JSON parse failed: {e}")
        print(f"  Raw (first 400 chars): {raw[:400]}")
        return {}


# ── Batch API helpers ──────────────────────────────────────────────────────────

def submit_batch_requests(requests: list[dict]) -> str:
    """
    Execute requests sequentially (the university endpoint has no batch API).
    Stores results in module-level cache; returns a fake batch_id to keep the
    calling code in main.py unchanged.
    """
    batch_id = str(uuid.uuid4())
    results: dict[str, str] = {}
    print(f"  Sequential execution: {len(requests)} requests…", flush=True)
    for req in requests:
        cid = req["custom_id"]
        system   = req["system"]
        user_msg = req["messages"][0]["content"]
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0,
                max_tokens=req["max_tokens"],
            )
            results[cid] = resp.choices[0].message.content
            print(f"    {cid}: OK", flush=True)
        except Exception as e:
            print(f"    {cid}: ERROR — {e}", flush=True)
    _BATCH_RESULTS_CACHE[batch_id] = results
    return batch_id


def wait_for_batch(batch_id: str, poll_interval: int = 30) -> dict[str, str]:
    """Return pre-computed results from the sequential submit (no polling needed)."""
    results = _BATCH_RESULTS_CACHE.pop(batch_id, {})
    print(f"  Sequential batch complete: {len(results)} results")
    return results


def build_batch_requests(
    filing_text: str,
    ticker: str,
    company: str,
    form_type: str,
    report_date: str,
    accession_key: str,
    is_annual: bool = False,
    is_text: str | None = None,
    xbrl_is: dict | None = None,
    template_segments: list[dict] | None = None,
) -> list[dict]:
    """
    Build batch request dicts for one filing.
    When xbrl_is is provided, only the seg request is returned (IS already covered).
    When template_segments is provided for a quarterly filing, uses the template prompt.
    """
    _base = dict(form_type=form_type, company=company, ticker=ticker, report_date=report_date)
    requests = []
    if xbrl_is is None:
        _is_input = is_text if is_text else filing_text
        requests.append({
            "custom_id": f"{accession_key}__is",
            "model":      LLM_MODEL,
            "max_tokens": 1536,
            "system":     _SYSTEM_IS,
            "messages":   [{"role": "user", "content": _IS_PROMPT.format(filing_text=_is_input, **_base)}],
        })
    use_template = template_segments and not is_annual
    if use_template:
        seg_prompt = _QUARTERLY_TEMPLATE_PROMPT.format(
            filing_text=filing_text,
            template_json=json.dumps(template_segments, indent=2),
            **_base,
        )
        seg_max_tokens = 1536
    else:
        seg_prompt = _SEG_PROMPT.format(filing_text=filing_text, **_base)
        seg_max_tokens = 2048
    requests.append({
        "custom_id": f"{accession_key}__seg",
        "model":      LLM_MODEL,
        "max_tokens": seg_max_tokens,
        "system":     _SYSTEM_SEG,
        "messages":   [{"role": "user", "content": seg_prompt}],
    })
    return requests


def parse_batch_results(
    batch_results: dict[str, str],
    accession_key: str,
    ticker: str,
    company: str,
    form_type: str,
    report_date: str,
    is_annual: bool = False,
    xbrl_is: dict | None = None,
) -> dict:
    """
    Parse seg (and optionally IS) batch results for one filing.
    When xbrl_is is provided, it is used directly instead of parsing the __is batch result.
    """
    seg_raw = batch_results.get(f"{accession_key}__seg", "")

    if xbrl_is is not None:
        is_result = xbrl_is
        notes = "XBRL IS + LLM segments (batch)."
    else:
        is_raw = batch_results.get(f"{accession_key}__is", "")
        if not is_raw:
            print(f"  WARNING: batch result missing for {accession_key}__is")
        try:
            is_result = _parse_json(is_raw) if is_raw else {}
        except json.JSONDecodeError as e:
            print(f"  WARNING: JSON parse failed (IS): {e}")
            is_result = {}
        notes = "Two-pass LLM extraction (batch)."

    seg_result = {}
    if not seg_raw:
        print(f"  WARNING: batch result missing for {accession_key}__seg")
    else:
        try:
            seg_result = _parse_json(seg_raw)
        except json.JSONDecodeError as e:
            print(f"  WARNING: JSON parse failed (SEG): {e} | raw[:200]={seg_raw[:200]}")

    return {
        "company":              company,
        "ticker":               ticker,
        "report_date":          report_date,
        "form_type":            form_type,
        "fiscal_year":          is_result.get("fiscal_year", ""),
        "fiscal_period":        is_result.get("fiscal_period", "FY"),
        "currency":             is_result.get("currency", "USD"),
        "exchange_rate_to_usd": is_result.get("exchange_rate_to_usd"),
        "unit":                 "billions",
        "segments":             seg_result.get("segments", []),
        "income_statement":     _extract_is_fields(is_result),
        "notes":                notes,
    }


# ── IS field extraction ────────────────────────────────────────────────────────

_IS_KEYS = [
    "total_revenue", "total_revenue_yoy_pct",
    "cost_of_revenue", "cost_of_revenue_yoy_pct", "cost_of_revenue_breakdown",
    "gross_profit", "gross_profit_yoy_pct",
    "operating_expenses", "total_operating_expenses",
    "operating_income", "operating_income_yoy_pct",
    "other_income_expense",
    "income_before_tax",
    "tax_expense",
    "net_income", "net_income_yoy_pct",
]


def _extract_is_fields(is_result: dict) -> dict:
    inc = {k: is_result.get(k) for k in _IS_KEYS}
    # Guarantee operating_expenses sub-dict always exists
    if inc.get("operating_expenses") is None:
        inc["operating_expenses"] = {
            "research_and_development": None,
            "selling_general_administrative": None,
        }
    return inc


# ── Public API ─────────────────────────────────────────────────────────────────

def extract_segment_data(
    filing_text: str,
    ticker: str,
    company: str,
    form_type: str,
    report_date: str,
    is_annual: bool = False,
    is_text: str | None = None,
    xbrl_is: dict | None = None,
    template_segments: list[dict] | None = None,
) -> dict:
    """
    Two-pass extraction:
      Pass 1 → income statement: XBRL (free, accurate) when available, else LLM
      Pass 2 → business segments:
        - Quarterly with template: fills known segment names with quarterly numbers
          (cheaper, consistent naming across all quarters for the same company)
        - Annual or no template: free-form segment discovery

    When xbrl_is is provided, Pass 1 LLM call is skipped entirely.
    Pass 1 falls back to is_text (~6K chars) or full filing_text.
    All LLM calls use LLM_MODEL (university endpoint, single model).
    """
    if xbrl_is is not None:
        is_result = xbrl_is
        print("    XBRL IS: ✓", flush=True)
        notes = "XBRL IS + LLM segments."
    else:
        _is_input = is_text if is_text else filing_text
        print("    LLM Pass 1: income statement …", end=" ", flush=True)
        is_result = _call_llm(
            _IS_PROMPT.format(
                form_type=form_type, company=company,
                ticker=ticker, report_date=report_date,
                filing_text=_is_input,
            ),
            system=_SYSTEM_IS,
            max_tokens=1536,
        )
        print("done")
        notes = "Two-pass LLM extraction."

    use_template = template_segments and not is_annual

    if use_template:
        print("    LLM Pass 2: segments (template) …", end=" ", flush=True)
        seg_result = _call_llm(
            _QUARTERLY_TEMPLATE_PROMPT.format(
                form_type=form_type, company=company,
                ticker=ticker, report_date=report_date,
                template_json=json.dumps(template_segments, indent=2),
                filing_text=filing_text,
            ),
            system=_SYSTEM_SEG,
            max_tokens=1536,
        )
    else:
        print("    LLM Pass 2: segments …", end=" ", flush=True)
        seg_result = _call_llm(
            _SEG_PROMPT.format(
                form_type=form_type, company=company,
                ticker=ticker, report_date=report_date,
                filing_text=filing_text,
            ),
            system=_SYSTEM_SEG,
            max_tokens=2048,
        )
    print("done")

    return {
        "company":              company,
        "ticker":               ticker,
        "report_date":          report_date,
        "form_type":            form_type,
        "fiscal_year":          is_result.get("fiscal_year", ""),
        "fiscal_period":        is_result.get("fiscal_period", "FY"),
        "currency":             is_result.get("currency", "USD"),
        "exchange_rate_to_usd": is_result.get("exchange_rate_to_usd"),
        "unit":                 "billions",
        "segments":             seg_result.get("segments", []),
        "income_statement":     _extract_is_fields(is_result),
        "notes":                notes,
    }


def extract_revenue_only_data(
    filing_text: str,
    ticker: str,
    company: str,
    form_type: str,
    report_date: str,
    template_segments: list[dict] | None = None,
) -> dict:
    """
    Lightweight single-pass extraction for revenue-only trading updates
    (European Q1/Q3 / 3M / 9M sales press releases).

    Returns a dict with the same schema as extract_segment_data but with
    income_statement populated only for `total_revenue`; all other IS fields are None.
    """
    template_json = json.dumps(template_segments or [], indent=2)
    prompt = _REV_ONLY_PROMPT.format(
        form_type=form_type, company=company,
        ticker=ticker, report_date=report_date,
        template_json=template_json,
        filing_text=filing_text,
    )
    print("    LLM Revenue-only extraction …", end=" ", flush=True)
    result = _call_llm(prompt, system=_SYSTEM_SEG, max_tokens=1024)
    print("done")

    is_stub = _extract_is_fields({
        "total_revenue": result.get("total_revenue"),
        "currency":      result.get("currency", "USD"),
    })
    return {
        "company":              company,
        "ticker":               ticker,
        "report_date":          report_date,
        "form_type":            form_type,
        "fiscal_year":          "",
        "fiscal_period":        "",
        "currency":             result.get("currency", "USD"),
        "exchange_rate_to_usd": None,
        "unit":                 "billions",
        "segments":             result.get("segments", []),
        "income_statement":     is_stub,
        "notes":                "Revenue-only trading update (no full income statement).",
        "_revenue_only":        True,
    }


def retry_segment_extraction(
    filing_text: str,
    ticker: str,
    company: str,
    form_type: str,
    report_date: str,
) -> list[dict]:
    """Re-run only Pass 2 (segment extraction) with Sonnet for higher accuracy.
    Keeps Pass 1 (income statement) results unchanged.
    Returns the segments list (may be empty if still nothing found).
    """
    common_kwargs = dict(
        form_type=form_type,
        company=company,
        ticker=ticker,
        report_date=report_date,
        filing_text=filing_text,
    )
    print("    LLM Retry: segments …", end=" ", flush=True)
    result = _call_llm(
        _SEG_PROMPT.format(**common_kwargs),
        system=_SYSTEM_SEG,
        max_tokens=2048,
    )
    print("done")
    return result.get("segments", [])


# ── Business model report (structured, multi-section) ────────────────────────

_SUMMARY_PROMPT = """You are a senior equity research analyst writing a business model
report on {company} ({ticker}). The report below will be consumed by a portfolio
manager who is deciding whether to take a long-term position — write for someone
who needs to understand HOW the company makes money, WHICH parts of the business
are doing the heavy lifting, and WHERE the risks are.

Use BOTH inputs below:
  1. **Segment & income-statement data** ({n_periods} periods of structured numbers
     from the company's filings — revenue / operating income / segment breakdowns
     across multiple fiscal periods).
  2. **Management's Discussion and Analysis (MD&A)** — the narrative from the most
     recent annual / interim report. Quote management's own words selectively,
     especially around strategy, competitive position, and outlook.

Write the report in markdown with the EXACT section headers below. Be specific
with dollar amounts and percentages — if you can't quantify a claim, drop it.
Cite the period a number refers to (e.g. "FY2025: $24.1B, +18% YoY").

---

# {company} ({ticker}) — Business Model Analysis

## Executive Summary
2–3 sentences. What does this company do, how does it make money, and what's
the single most important thing to know about it right now?

## Business Model
1–2 paragraphs covering:
- What products / services they sell and to whom
- The unit economics — what's the typical transaction / contract / customer
- How they capture value (subscription / margin on goods / take rate / interest spread / etc.)
- Their economic moat (network effects, scale, switching costs, brand, regulation, IP)

## Segment Deep Dive
For EACH reportable segment (skip immaterial Corporate / Other lines unless they're
big enough to matter), write a `### Segment Name` subsection covering:
- What the segment actually does in 1–2 sentences
- Most-recent-period revenue, $ and % of total
- 3-year revenue trajectory (CAGR or YoY rates)
- Operating income / margin if available
- Key growth drivers (cite MD&A where possible)
- Key headwinds / risks for this segment

## Growth Trajectory
- Total revenue 3-year trend (with rates)
- Which segments drove the growth (or the decline)
- Is this growth structural (secular demand, share gains) or cyclical (recovery,
  rate cycle, commodity prices) — be explicit. Cite MD&A.
- Notable M&A, divestitures, or restructurings in the period

## Margin Profile
- Total gross margin and operating margin trend (if reported)
- Margin variation across segments — which segment carries the franchise?
- Operating leverage commentary — is the business getting more or less efficient?

## Capital Allocation & Strategy
Pull these from the MD&A and recent commentary:
- Priorities management has stated (organic investment / M&A / dividend / buyback)
- R&D intensity (R&D as % of revenue, trend)
- Major capex programs or strategic initiatives
- Any stated 3-year outlook or financial targets

## Key Risks
Top 3–5 risks management itself has named, OR that are obvious from the segment data
(e.g. customer concentration, geographic concentration, cyclicality, regulation, tech
disruption, FX exposure). One sentence each.

---

DATA INPUTS BEGIN BELOW.

## Segment & income-statement data (across periods):
{segment_data_json}

## Management's Discussion and Analysis (most recent annual filing):
{mda_text}
"""


def generate_business_summary(
    ticker: str,
    company: str,
    all_segment_data: list[dict],
    mda_text: str = "",
) -> str:
    """Produce a structured business-model markdown report.

    `all_segment_data` is the per-period extracted dicts (income statement +
    segments). `mda_text` is the optional Management's Discussion and Analysis
    text pulled from the most recent annual filing — when supplied, the LLM
    grounds its narrative in management's own words. Length-capped to fit in
    a single ~32k context window.
    """
    seg_json = json.dumps(all_segment_data, indent=2)[:50000]
    # Trim MD&A to leave room for segment data + prompt + response budget
    mda_trimmed = (mda_text or "(none supplied)")[:30000]
    prompt = _SUMMARY_PROMPT.format(
        company=company,
        ticker=ticker,
        n_periods=len(all_segment_data),
        segment_data_json=seg_json,
        mda_text=mda_trimmed,
    )
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=4096,
    )
    return response.choices[0].message.content
