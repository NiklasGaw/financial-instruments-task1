"""Streamlit frontend for the Financial Instruments pipeline.

Browse all 96 tickers' outputs:
- Per-period Sankey HTML charts
- Per-ticker business model markdown reports
- Per-period income statement + segment data tables

Pure reader — never writes to output/ or cache/. Run with:
    streamlit run app.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

OUTPUT_DIR = Path(__file__).parent / "output"

# Period sort order within a fiscal year: FY first, then half-years, then
# quarters in calendar order. Newer fiscal years come first.
_PERIOD_ORDER = {"FY": 0, "H1": 1, "H2": 2, "Q1": 3, "Q2": 4, "Q3": 5, "Q4": 6, "9M": 7}


# ────────────────────────────────────────────────────────────────────────────
# Discovery
# ────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def discover_tickers() -> dict[str, dict]:
    """Scan output/ once per session and return a {ticker: metadata} dict.

    metadata: {
        'company': str | None,
        'dir': Path,
        'all_data_path': Path | None,
        'summary_path': Path | None,
        'periods': [{'key', 'label', 'sankey_path'}],  # newest first
    }
    """
    tickers: dict[str, dict] = {}
    if not OUTPUT_DIR.is_dir():
        return tickers

    # Sort so letter-prefixed tickers (AAPL, ABNB, …) come first, alphabetically,
    # and number-prefixed tickers (005930.KS, 6758.T, …) sink to the end.
    def _ticker_sort_key(p: Path) -> tuple:
        name = p.name
        return (name[:1].isdigit(), name)

    for ticker_dir in sorted(OUTPUT_DIR.iterdir(), key=_ticker_sort_key):
        if not ticker_dir.is_dir():
            continue
        if ticker_dir.name.startswith("_"):
            continue   # skip backup dirs like _dedup_backup_GOOGL
        ticker = ticker_dir.name

        all_data = ticker_dir / f"{ticker}_all_data.json"
        summary = ticker_dir / f"{ticker}_business_summary.md"

        # Parse company name from the first all_data entry if available.
        company = None
        if all_data.exists():
            try:
                with open(all_data, encoding="utf-8") as fp:
                    entries = json.load(fp)
                if entries:
                    company = entries[0].get("company")
            except Exception:
                pass

        # Index Sankey HTMLs by period key (e.g. FY2025_FY, FY2025_Q3).
        periods: list[dict] = []
        for sankey in ticker_dir.glob("*_sankey.html"):
            m = re.match(
                rf"^{re.escape(ticker)}_(FY\d{{4}})_(FY|H1|H2|Q1|Q2|Q3|Q4|9M)(?:_(\d{{8}}))?_sankey\.html$",
                sankey.name,
            )
            if not m:
                continue
            fy, period, date_suffix = m.group(1), m.group(2), m.group(3)
            label = f"{fy} {period}"
            if date_suffix:
                # e.g. multiple Sankeys for the same period — disambiguate by date
                label += f" ({date_suffix[:4]}-{date_suffix[4:6]}-{date_suffix[6:8]})"
            periods.append({
                "key": f"{fy}_{period}" + (f"_{date_suffix}" if date_suffix else ""),
                "label": label,
                "sankey_path": sankey,
                "fy": fy,
                "period": period,
                "date_suffix": date_suffix or "",
            })

        # Sort: newest fiscal year first; within a year by _PERIOD_ORDER.
        def _sort_key(p: dict) -> tuple:
            fy_year = int(p["fy"][2:]) if p["fy"][2:].isdigit() else 0
            return (-fy_year, _PERIOD_ORDER.get(p["period"], 99), p["date_suffix"])
        periods.sort(key=_sort_key)

        # Only register tickers that have at least one Sankey OR a summary.
        if periods or summary.exists() or all_data.exists():
            tickers[ticker] = {
                "company": company,
                "dir": ticker_dir,
                "all_data_path": all_data if all_data.exists() else None,
                "summary_path": summary if summary.exists() else None,
                "periods": periods,
            }

    return tickers


@st.cache_data(show_spinner=False)
def load_all_data(path_str: str) -> list[dict]:
    """Load and cache a ticker's all_data.json."""
    with open(path_str, encoding="utf-8") as fp:
        return json.load(fp)


@st.cache_data(show_spinner=False)
def load_text(path_str: str) -> str:
    with open(path_str, encoding="utf-8") as fp:
        return fp.read()


def find_entry_for_period(
    entries: list[dict], fy: str, period: str, date_suffix: str = ""
) -> dict | None:
    """Pick the all_data entry that matches a (fy, period[, date]) selection."""
    candidates = [
        e for e in entries
        if e.get("fiscal_year") == fy
        and (e.get("fiscal_period") or "").upper() == period
    ]
    if not candidates:
        return None
    if date_suffix:
        target = f"{date_suffix[:4]}-{date_suffix[4:6]}-{date_suffix[6:8]}"
        for e in candidates:
            if e.get("report_date") == target:
                return e
    return candidates[0]


# ────────────────────────────────────────────────────────────────────────────
# Rendering helpers
# ────────────────────────────────────────────────────────────────────────────

def _fmt_money(v, unit: str = "B") -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):,.1f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def render_metrics_row(entry: dict) -> None:
    """Show 4-cell metrics row above the Sankey."""
    inc = entry.get("income_statement") or {}
    unit = "B" if "billion" in (entry.get("unit") or "").lower() else "M"
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Revenue",     _fmt_money(inc.get("total_revenue"), unit))
    c2.metric("Operating Expenses", _fmt_money(inc.get("total_operating_expenses"), unit))
    c3.metric("Operating Income",  _fmt_money(inc.get("operating_income"), unit))
    c4.metric("Net Income",        _fmt_money(inc.get("net_income"), unit))


def _shrink_sankey_html(html: str, min_width: int) -> str:
    """Rewrite the Sankey's hardcoded 1420-px chart-width floor.

    The Sankey HTML template uses `Math.max(window.innerWidth, 1420)` for both
    initial sizing and the resize handler, so any iframe narrower than 1420 px
    clips the right edge (the body has overflow-x:hidden). Rewriting the
    literal to a smaller floor lets the chart fit a narrower iframe cleanly.
    """
    return html.replace("1420", str(min_width))


_SANKEY_BASE_W = 1000
_SANKEY_BASE_H = 640


def render_sankey_tab(entry: dict | None, period: dict) -> None:
    if entry is not None:
        render_metrics_row(entry)
        st.divider()
    raw_html = load_text(str(period["sankey_path"]))

    # Zoom slider — scales the chart visually (everything: bars, links, text)
    # via a CSS transform injected into the iframe's <body>. The iframe is
    # sized to the scaled dimensions so the browser shows scrollbars when the
    # chart overflows.
    zoom = st.slider(
        "🔍 Zoom",
        min_value=0.6, max_value=2.0, value=1.0, step=0.1,
        key=f"zoom_{period['key']}",
        help="Drag to make the Sankey bigger or smaller. Scroll inside the chart if it overflows.",
    )

    # Render the chart at 970 px inside a 1000-px body, then let CSS scale it.
    scaled_html = _shrink_sankey_html(raw_html, 970)
    if zoom != 1.0:
        zoom_css = (
            "<style>"
            f"body{{transform:scale({zoom});transform-origin:top left;"
            f"width:{_SANKEY_BASE_W}px;height:{_SANKEY_BASE_H}px;}}"
            "</style>"
        )
        scaled_html = scaled_html.replace("</head>", f"{zoom_css}</head>", 1)

    iframe_w = int(_SANKEY_BASE_W * zoom)
    iframe_h = int(_SANKEY_BASE_H * zoom)
    st.markdown('<div class="sankey-frame-wrap">', unsafe_allow_html=True)
    components.html(scaled_html, height=iframe_h, width=iframe_w, scrolling=True)
    st.markdown("</div>", unsafe_allow_html=True)


def render_summary_tab(meta: dict) -> None:
    if meta["summary_path"] is None:
        st.info("Business model summary has not been generated for this ticker.")
        st.markdown(
            "To generate one, run:\n```\n"
            f"python3 main.py {meta['dir'].name} --summary\n```"
        )
        return
    body = load_text(str(meta["summary_path"]))
    # The summaries are full of money figures like "$209.6B". Streamlit's
    # markdown renderer treats `$…$` as inline LaTeX math, which turns the text
    # between two dollar signs into italic garbage. Replacing `$` with its HTML
    # entity preserves the visible character without triggering MathJax.
    body = body.replace("$", "&#36;")
    # Wrap the rendered markdown in a max-width "card" so the long lines of
    # text don't span 1500px (uncomfortable line length for prose). Streamlit
    # renders `st.markdown` inside our wrapper div via unsafe_allow_html.
    st.markdown(
        f'<div class="bm-report">\n\n{body}\n\n</div>',
        unsafe_allow_html=True,
    )


def _entries_to_period_label(entry: dict) -> str:
    fy = entry.get("fiscal_year") or "?"
    fp = entry.get("fiscal_period") or "?"
    return f"{fy} {fp}"


def render_raw_data_tab(meta: dict) -> None:
    if meta["all_data_path"] is None:
        st.info("No structured data available for this ticker.")
        return
    entries = load_all_data(str(meta["all_data_path"]))

    # Sort entries newest first using the same ordering as the period dropdown.
    def _sort_key(e: dict) -> tuple:
        fy = e.get("fiscal_year") or ""
        fy_year = int(fy[2:]) if fy[2:].isdigit() else 0
        return (-fy_year, _PERIOD_ORDER.get((e.get("fiscal_period") or "").upper(), 99))
    entries = sorted(entries, key=_sort_key)

    period_cols = [_entries_to_period_label(e) for e in entries]

    # ── IS table ──────────────────────────────────────────────────────────
    is_fields = [
        ("Total revenue",       "total_revenue"),
        ("Cost of revenue",     "cost_of_revenue"),
        ("Gross profit",        "gross_profit"),
        ("Operating expenses",  "total_operating_expenses"),
        ("Operating income",    "operating_income"),
        ("Other income/expense","other_income_expense"),
        ("Income before tax",   "income_before_tax"),
        ("Tax expense",         "tax_expense"),
        ("Net income",          "net_income"),
    ]
    is_rows = []
    for label, key in is_fields:
        row = [label] + [
            (e.get("income_statement") or {}).get(key) for e in entries
        ]
        is_rows.append(row)
    is_df = pd.DataFrame(is_rows, columns=["Line item"] + period_cols)
    st.subheader("Income statement")
    st.dataframe(is_df, use_container_width=True, hide_index=True)

    # ── Segments table ────────────────────────────────────────────────────
    # Build a wide table where rows are segment names and columns are periods.
    seg_data: dict[str, dict[str, object]] = {}   # seg_name → {period_label: rev}
    for e, col in zip(entries, period_cols):
        for seg in e.get("segments") or []:
            name = seg.get("name") or ""
            if not name:
                continue
            seg_data.setdefault(name, {})[col] = seg.get("revenue")

    if seg_data:
        seg_rows = []
        for name in seg_data:   # preserve insertion order (matches first-seen-newest order)
            row = [name] + [seg_data[name].get(c) for c in period_cols]
            seg_rows.append(row)
        seg_df = pd.DataFrame(seg_rows, columns=["Segment"] + period_cols)
        st.subheader("Segments — revenue")
        st.dataframe(seg_df, use_container_width=True, hide_index=True)
    else:
        st.caption("No segment data extracted for this ticker.")


# ────────────────────────────────────────────────────────────────────────────
# Main app
# ────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Financial Instruments Browser",
    page_icon="📊",
    layout="wide",
)

# Global styling: shrink Streamlit's main-content side padding so the 1500-px
# Sankey iframe isn't pushed off the page, and add a readable card style for
# the business-summary markdown.
st.markdown(
    """
    <style>
      /* Reduce default Streamlit horizontal padding so wide Sankeys fit. */
      [data-testid="stMainBlockContainer"] {
          padding-left: 1.5rem;
          padding-right: 1.5rem;
          max-width: 100%;
      }

      /* Center all custom-component iframes (Sankeys etc.) inside their
         Streamlit wrapper. Two rules: one for the outer wrapper, one for
         the iframe itself, so we catch both modern and legacy DOMs. */
      [data-testid="stCustomComponentV1"],
      [data-testid="stIFrame"],
      .stCustomComponentV1Host {
          display: flex !important;
          justify-content: center !important;
          width: 100% !important;
      }
      [data-testid="stCustomComponentV1"] iframe,
      [data-testid="stIFrame"] iframe,
      .stCustomComponentV1Host iframe {
          margin-left: auto !important;
          margin-right: auto !important;
      }

      /* Business-summary card: comfortable reading width + soft background. */
      .bm-report {
          max-width: 820px;
          margin: 0 auto;
          padding: 2rem 2.4rem 2.4rem;
          background: #ffffff;
          border: 1px solid #e0e0e0;
          border-radius: 12px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.04);
          line-height: 1.65;
          color: #212121;
      }
      .bm-report h1 {
          font-size: 1.75rem;
          margin-bottom: 0.4rem;
          color: #1a1a1a;
          border-bottom: 2px solid #1a73e8;
          padding-bottom: 0.5rem;
      }
      .bm-report h2 {
          font-size: 1.3rem;
          margin-top: 2rem;
          margin-bottom: 0.6rem;
          color: #1a73e8;
          font-weight: 700;
      }
      .bm-report h3 {
          font-size: 1.05rem;
          margin-top: 1.4rem;
          margin-bottom: 0.4rem;
          color: #424242;
          font-weight: 600;
      }
      .bm-report p { margin-bottom: 0.9rem; }
      .bm-report ul, .bm-report ol { margin-left: 1.4rem; margin-bottom: 0.9rem; }
      .bm-report li { margin-bottom: 0.3rem; }
      .bm-report strong { color: #1a1a1a; }
      .bm-report hr {
          border: none;
          border-top: 1px solid #e0e0e0;
          margin: 1.8rem 0;
      }
      .bm-report code {
          background: #f5f5f5;
          padding: 0.1rem 0.35rem;
          border-radius: 3px;
          font-size: 0.92em;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

tickers = discover_tickers()

if not tickers:
    st.error(f"No ticker output found under {OUTPUT_DIR}. Run the pipeline first.")
    st.stop()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📊 Browser")
    st.caption(f"{len(tickers)} tickers loaded")

    # Ticker selector — show "TICKER — Company name" for clarity.
    def _ticker_label(t: str) -> str:
        company = tickers[t]["company"]
        return f"{t} — {company}" if company else t

    selected_ticker = st.selectbox(
        "Ticker",
        options=list(tickers.keys()),
        format_func=_ticker_label,
        index=0,
    )

    meta = tickers[selected_ticker]
    periods = meta["periods"]

    if periods:
        selected_period_key = st.selectbox(
            "Period",
            options=[p["key"] for p in periods],
            format_func=lambda k: next(p["label"] for p in periods if p["key"] == k),
            index=0,
        )
        selected_period = next(p for p in periods if p["key"] == selected_period_key)
    else:
        selected_period = None
        st.warning("No Sankey HTMLs found for this ticker.")

    st.divider()
    st.caption(
        f"**{len(periods)}** Sankeys · "
        f"{'✔️ summary' if meta['summary_path'] else '— no summary'} · "
        f"{'✔️ data' if meta['all_data_path'] else '— no data'}"
    )
    if st.button("🔄 Reload from disk"):
        discover_tickers.clear()
        load_all_data.clear()
        load_text.clear()
        st.rerun()

# ── Main area ────────────────────────────────────────────────────────────────
header = meta["company"] or selected_ticker
st.markdown(f"## {header}  \n*Ticker: `{selected_ticker}`*")

tab_sankey, tab_summary, tab_data = st.tabs(["Sankey", "Business summary", "Raw data"])

# Load all_data once if we need it.
all_data_entries: list[dict] = []
if meta["all_data_path"]:
    all_data_entries = load_all_data(str(meta["all_data_path"]))

with tab_sankey:
    if selected_period is None:
        st.info("No Sankey HTMLs available.")
    else:
        entry = find_entry_for_period(
            all_data_entries,
            selected_period["fy"],
            selected_period["period"],
            selected_period.get("date_suffix", ""),
        )
        render_sankey_tab(entry, selected_period)

with tab_summary:
    render_summary_tab(meta)

with tab_data:
    render_raw_data_tab(meta)
