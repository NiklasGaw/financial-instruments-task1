# Financial Instruments — Task 1

Automated Sankey charts and business-model summaries for **96 of 98** stocks in the assigned universe, covering the **last 3 fiscal years** (12 quarterly + 3 annual reports per stock where issuer cadence allows).

## What's in the box

- **1,719 Sankey HTMLs** under `output/<TICKER>/<TICKER>_<FY>_<PERIOD>_sankey.html` — one per fiscal period, self-contained (no external JS/CSS), opens directly in any browser.
- **96 business-model summaries** under `output/<TICKER>/<TICKER>_business_summary.md` — 7-section structured reports written by an LLM grounded in the extracted income statement, segment data, and the filing's MD&A.
- **Per-ticker structured data** under `output/<TICKER>/<TICKER>_all_data.json` — every period's income statement + segment breakdown.
- **Streamlit browser** at [`app.py`](app.py) — interactive UI to browse tickers / periods / Sankeys / summaries / raw data.

## Quick view (no setup)

Open any HTML directly:

```
open output/AAPL/AAPL_FY2024_FY_sankey.html
open output/HSBC/HSBC_business_summary.md
```

## Interactive browser

```
pip install -r requirements.txt
streamlit run app.py
```

Sidebar lists 96 tickers (AAPL first; international tickers with numeric prefixes at the bottom). Three tabs:

1. **Sankey** — the chart for the selected period + metrics row (Revenue / OpEx / Operating Income / Net Income).
2. **Business Summary** — the 7-section markdown report.
3. **Raw Data** — wide tables of the income statement and segments across every period.

## Re-run the pipeline for a single ticker

The LLM key in [`config.py`](config.py) is already filled in; the SEC user-agent uses my real email. Cached SEC filings + XBRL JSONs live under `cache/`, so re-runs reuse them and only hit the LLM for any new periods that aren't already cached.

```
python3 main.py --years=3 AAPL              # one ticker
python3 main.py --years=3 AAPL MSFT JPM     # several tickers
python3 main.py --years=3 --summary AAPL    # also regenerate the business summary
```

Outputs land in `output/<TICKER>/`.

## Architecture

The pipeline is six stages, all driven from [`main.py`](main.py):

1. **Fetch** — [`edgar_fetcher.py`](edgar_fetcher.py) pulls 10-K / 10-Q / 20-F / 40-F / 6-K filings from SEC EDGAR; [`pdf_handler.py`](pdf_handler.py) downloads investor-relations PDFs for the 11 foreign issuers that don't file on EDGAR (HSBC, Samsung, LVMH, Nestlé, Allianz, Siemens, CBA, Tencent, AstraZeneca, Roche, MUFG — listed in [`intl_mapping.py`](intl_mapping.py)).
2. **Income-statement extraction** — [`xbrl_fetcher.py`](xbrl_fetcher.py) reads structured financial facts straight from SEC XBRL (both `us-gaap` and `ifrs-full` taxonomies, the latter covering TSM, SAP, Sony, Shell). Free, exact, no LLM tokens needed for the IS.
3. **Segment extraction** — [`llm_extractor.py`](llm_extractor.py) calls a Mistral-Large model on the segment-disclosure sections of each filing to pull per-segment revenue / operating income, with a template that quarter prompts reuse so segment names stay consistent.
4. **Validation & reconciliation** — [`validate_data.py`](validate_data.py) enforces accounting identities (TR − COR = GP, GP − OpEx = OI, …), converts non-USD filers to USD via Yahoo-Finance FX rates, strips pure-geographic "segments", and synthesizes Q2/Q4 from H1 / FY when the issuer doesn't report them standalone.
5. **Rendering** — [`sankey_generator.py`](sankey_generator.py) builds the per-period Sankey HTMLs.
6. **Summary** — [`llm_extractor.py::generate_business_summary`](llm_extractor.py) writes the 7-section business-model report per ticker, grounded in the validated data + MD&A text extracted by [`edgar_fetcher.py::extract_mda_section`](edgar_fetcher.py).

## Data sources

Strictly the three the syllabus allows:

- **SEC EDGAR** — filings and XBRL (`edgar_fetcher.py`, `xbrl_fetcher.py`).
- **Yahoo Finance** — historical FX rates + market data (`validate_data.py::_lookup_fx_rate`, fallback in `main.py`).
- **The companies' own annual / quarterly reports** — IR-page PDFs for the 11 issuers without EDGAR filings (`intl_mapping.py::NO_EDGAR_STOCKS`).

The Mistral-Large LLM is a processing tool (segment extraction + summary writing), not a data source.

## Universe note

- 97 tickers in [`data/stock_list.xlsx`](data/stock_list.xlsx).
- 96 fully processed (full Sankey + summary + raw data).
- **SK Hynix (000660.KS)** is the gap — its IR portal uses JS-gated downloads with no stable PDF URLs, so the pipeline cannot reach the annual reports without manual download. Documented in [`intl_mapping.py`](intl_mapping.py).

## Repository layout

```
Financial Instruments/
├── app.py                  # Streamlit browser
├── main.py                 # pipeline driver
├── edgar_fetcher.py        # SEC EDGAR filings + MD&A extraction
├── xbrl_fetcher.py         # SEC XBRL facts (us-gaap + ifrs-full)
├── pdf_handler.py          # PDF download + extraction for non-EDGAR filers
├── llm_extractor.py        # segment extraction + business-model summary
├── validate_data.py        # accounting reconciliation + FX + synth
├── sankey_generator.py     # ECharts-based Sankey HTMLs
├── intl_mapping.py         # foreign ticker → EDGAR ticker / IR-PDF URLs
├── config.py               # SEC user-agent + LLM credentials + paths
├── data/
│   ├── stock_list.xlsx     # universe (97 tickers)
│   └── annual_reports/     # manually-placed IR PDFs
├── cache/
│   ├── extractions/        # per-filing extraction JSONs (re-used across runs)
│   ├── filings/            # raw SEC filing text
│   └── xbrl/               # SEC XBRL company-facts JSONs
├── output/<TICKER>/        # Sankey HTMLs + business summary + all_data.json
└── scripts/                # one-off backfill utilities (e.g. IFRS XBRL backfill)
```

## API key

The `LLM_API_KEY` in [`config.py`](config.py) is my personal credential for the university LiteLLM endpoint. It works out of the box for re-runs; please don't share or commit it elsewhere.
