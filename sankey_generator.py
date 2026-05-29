"""
Sankey chart generator — Apache ECharts backend.  SankeyArt-style visual design.

Flow (left → right):
  [Sub-segments] → Segments → Total Revenue
    → Gross Profit  (green)  → Operating Income → Net Income / Tax / Other Loss
    → Cost of Revenue (red)  → Product Costs / Service Costs
    → Operating Expenses     → R&D / S&M / G&A

Colour conventions (SankeyArt style)
  Charcoal   = segment / revenue nodes (neutral)
  Dark green = profit nodes + profit flows (light green ribbon)
  Pink/red   = cost nodes + cost flows (salmon pink ribbon)
  Gray       = sub-segment → segment flows and segment → revenue flows
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# ── Node colours ─────────────────────────────────────────────────────────────
_SEG_COLOR  = "#424242"   # reportable segment nodes — near-black charcoal
_SSEG_COLOR = "#9e9e9e"   # sub-segment nodes — medium gray

_CHARCOAL  = "#424242"   # revenue / neutral nodes
_DK_GREEN  = "#388e3c"   # profit nodes (Gross Profit, Operating Income, Net Income)
_DK_RED    = "#d81b60"   # cost nodes — pink-red

# All cost categories share one color (R&D / S&M / G&A / etc.)
_DK_PURPLE = _DK_RED
_DK_ORANGE = _DK_RED
_DK_MGNTA  = _DK_RED

# ── Link colours (rgba — stored in _prepare output, converted in _render) ─────
_L_GRAY    = "rgba(180,180,180,0.65)"
_L_GREEN   = "rgba(129,199,132,0.78)"
_L_RED     = "rgba(240,98,146,0.62)"
_L_PURPLE  = _L_RED
_L_ORANGE  = _L_RED
_L_MGNTA   = _L_RED

# ── ECharts link color mapping: rgba string → (hex, base_opacity) ─────────────
# Sub-segment links override to 0.25 opacity regardless of base.
_RGBA_TO_HEX = {
    "rgba(180,180,180,0.65)": ("#aaaaaa", 0.50),   # gray  (seg / sub-seg)
    "rgba(129,199,132,0.78)": ("#66bb6a", 0.62),   # green (profit flows)
    "rgba(240,98,146,0.62)":  ("#f48fb1", 0.56),   # pink  (cost flows)
}


# ── Public entry point ────────────────────────────────────────────────────────

def build_sankey_chart(segment_data: dict, output_path: str | None = None) -> str:
    html = _render(segment_data)
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")
        print(f"  Sankey saved: {output_path}")
    return html


# ── Sub-segment name cleanup ─────────────────────────────────────────────────
_SS_SUFFIXES = [
    (" and partner services",         10),
    (" products and cloud services",   8),
    (" services",                     12),
]

def _clean_subseg_name(name: str) -> str:
    nl = name.lower()
    for suffix, min_len in _SS_SUFFIXES:
        if nl.endswith(suffix) and len(name) - len(suffix) >= min_len:
            return name[: len(name) - len(suffix)]
    return name


# ── Data preparation ──────────────────────────────────────────────────────────

def _prepare(sd: dict) -> dict:
    ticker   = sd.get("ticker",       "???")
    company  = sd.get("company",      "Unknown")
    rdate    = sd.get("report_date",  "")
    period   = sd.get("fiscal_period","")
    fy       = sd.get("fiscal_year",  "")
    currency = sd.get("currency",     "USD")
    unit     = sd.get("unit",         "billions")
    ul       = "B" if "billion" in unit.lower() else "M"

    segs   = sd.get("segments", []) or []
    inc    = sd.get("income_statement", {}) or {}
    opex   = inc.get("operating_expenses", {}) or {}
    cor_bk = inc.get("cost_of_revenue_breakdown", {}) or {}

    total_rev    = inc.get("total_revenue")
    cost_of_rev  = inc.get("cost_of_revenue")
    gross_profit = inc.get("gross_profit")
    op_income    = inc.get("operating_income")
    total_opex   = inc.get("total_operating_expenses")
    net_income   = inc.get("net_income")
    tax          = inc.get("tax_expense")
    other_ie     = inc.get("other_income_expense")

    rd  = opex.get("research_and_development")
    sga = opex.get("selling_general_administrative")   # combined S&M + G&A (new format)
    sm  = opex.get("sales_and_marketing")              # legacy separate field
    ga  = opex.get("general_and_administrative")       # legacy separate field
    oo  = opex.get("other_operating")

    product_costs = cor_bk.get("product_costs")
    service_costs = cor_bk.get("service_costs")

    # Derive missing figures
    if gross_profit is None and total_rev and cost_of_rev:
        gross_profit = round(total_rev - cost_of_rev, 2)
    if cost_of_rev is None and total_rev and gross_profit:
        cost_of_rev = round(total_rev - gross_profit, 2)
    if op_income is None and gross_profit and total_opex:
        op_income = round(gross_profit - total_opex, 2)
    if total_opex is None and gross_profit and op_income:
        total_opex = round(gross_profit - op_income, 2)
    if total_opex is None:
        parts = [v for v in (rd, sm, ga, oo) if v]
        if parts:
            total_opex = round(sum(parts), 2)

    has_sub_vals = any(
        ss.get("revenue") and ss["revenue"] > 0
        for seg in segs
        for ss in (seg.get("sub_segments") or [])
    )
    has_segs = any(seg.get("revenue") for seg in segs)

    # Layer indices
    if has_sub_vals:
        L0, L1, L2, L3, L4, L5 = 0, 1, 2, 3, 4, 5
        num_layers = 6
    elif has_segs:
        L0, L1 = None, 0
        L2, L3, L4, L5 = 1, 2, 3, 4
        num_layers = 5
    else:
        L0 = L1 = None
        L2, L3, L4, L5 = 0, 1, 2, 3
        num_layers = 4

    nodes: list[dict] = []
    links: list[dict] = []
    key2idx: dict[str, int] = {}

    def node(key, name, value, display_value, yoy, layer, color,
             sort_order, ntype="default") -> int:
        idx = len(nodes)
        nodes.append({
            "id":           idx,
            "name":         name,
            "value":        float(value),
            "displayValue": float(display_value),
            "yoy":          yoy,
            "layer":        layer,
            "color":        color,
            "sortOrder":    sort_order,
            "type":         ntype,
        })
        key2idx[key] = idx
        return idx

    def link(src_key, tgt_key, value, color):
        if value and value > 0 and src_key in key2idx and tgt_key in key2idx:
            links.append({
                "source": key2idx[src_key],
                "target": key2idx[tgt_key],
                "value":  float(value),
                "color":  color,
            })

    # ── Segments + sub-segments ───────────────────────────────────────────────
    for i, seg in enumerate(segs):
        seg_rev = seg.get("revenue")
        seg_yoy = seg.get("yoy_growth_pct")
        seg_key = f"seg{i}"

        if L1 is not None and seg_rev is not None and seg_rev > 0:
            node(seg_key, seg["name"], seg_rev, seg_rev, seg_yoy,
                 L1, _SEG_COLOR, i, "segment")

        if L0 is not None and has_sub_vals:
            for j, ss in enumerate(seg.get("sub_segments") or []):
                ss_rev = ss.get("revenue")
                if ss_rev and ss_rev > 0:
                    ss_key = f"ss{i}_{j}"
                    ss_name = _clean_subseg_name(ss["name"])
                    node(ss_key, ss_name, ss_rev, ss_rev,
                         ss.get("yoy_growth_pct"),
                         L0, _SSEG_COLOR, i * 100 + j, "subsegment")
                    link(ss_key, seg_key, ss_rev, _L_GRAY)

    # ── Total revenue ─────────────────────────────────────────────────────────
    eff_rev = total_rev
    if eff_rev is None:
        seg_sum = sum(s.get("revenue") or 0 for s in segs if s.get("revenue"))
        eff_rev = seg_sum if seg_sum > 0 else None

    if eff_rev:
        node("rev", "Total Revenue", eff_rev, eff_rev,
             inc.get("total_revenue_yoy_pct"), L2, _CHARCOAL, 0, "revenue")

        if has_segs and L1 is not None:
            seg_sum = sum(s.get("revenue") or 0 for s in segs if s.get("revenue"))
            for i, seg in enumerate(segs):
                rev = seg.get("revenue")
                if rev and rev > 0:
                    link(f"seg{i}", "rev", rev, _L_GRAY)
            gap = round(eff_rev - seg_sum, 2) if total_rev else 0
            if gap > 0.05:
                node("gap", "Other Revenue", gap, gap, None,
                     L1, _CHARCOAL, 999, "other")
                link("gap", "rev", gap, _L_GRAY)

        # Revenue → Gross Profit + Cost of Revenue
        # If no gross_profit (banks, energy), skip L3 and link rev → oi directly.
        _has_gp = gross_profit and gross_profit > 0
        _oi_source = "gp" if _has_gp else "rev"

        if _has_gp:
            node("gp", "Gross Profit", gross_profit, gross_profit,
                 inc.get("gross_profit_yoy_pct"), L3, _DK_GREEN, 0, "profit")
            link("rev", "gp", gross_profit, _L_GREEN)

        if cost_of_rev and cost_of_rev > 0:
            node("cor", "Cost of Revenue", cost_of_rev, cost_of_rev,
                 inc.get("cost_of_revenue_yoy_pct"), L3, _DK_RED, 1, "cost")
            link("rev", "cor", cost_of_rev, _L_RED)

        # Gross Profit (or Revenue) → Operating Income + Operating Expenses
        if op_income and op_income > 0:
            node("oi", "Operating Income", op_income, op_income,
                 inc.get("operating_income_yoy_pct"), L4, _DK_GREEN, 0, "profit")
            link(_oi_source, "oi", op_income, _L_GREEN)

        if total_opex and total_opex > 0:
            node("opex", "Operating Expenses", total_opex, total_opex,
                 None, L4, _DK_RED, 1, "cost")
            link(_oi_source, "opex", total_opex, _L_RED)

        # Banks / no-cost-structure: no GP, no CoR, no OI from XBRL.
        # Build a meaningful cascade using income_before_tax (derived as ni + tax
        # by validate_data) so we show at least two meaningful intermediate nodes.
        if not _has_gp and not (cost_of_rev and cost_of_rev > 0):
            if not (op_income and op_income > 0) and net_income:
                ni_act  = float(net_income)
                tax_act = float(tax) if tax else 0.0

                # Prefer the derived income_before_tax; fall back to ni + tax
                ibt_raw = inc.get("income_before_tax")
                ibt_val = float(ibt_raw) if ibt_raw else (
                    round(ni_act + tax_act, 1) if tax_act else None
                )

                if ibt_val and ibt_val > 0 and eff_rev and eff_rev > ibt_val:
                    # Revenue → Operating Costs (gap) + Pre-Tax Income
                    total_costs = round(eff_rev - ibt_val, 1)
                    node("bank_costs", "Operating Costs", total_costs, total_costs,
                         None, L3, _DK_RED, 1, "cost")
                    link("rev", "bank_costs", total_costs, _L_RED)

                    node("ibt_b", "Pre-Tax Income", ibt_val, ibt_val,
                         None, L3, _DK_GREEN, 0, "profit")
                    link("rev", "ibt_b", ibt_val, _L_GREEN)

                    # Pre-Tax Income → Net Income + Income Tax
                    node("ni", "Net Income", ni_act, ni_act,
                         inc.get("net_income_yoy_pct"), L4, _DK_GREEN, 0, "profit")
                    link("ibt_b", "ni", ni_act, _L_GREEN)

                    if tax_act > 0:
                        node("tax", "Income Tax", tax_act, tax_act,
                             None, L4, _DK_RED, 1, "cost")
                        link("ibt_b", "tax", tax_act, _L_RED)
                else:
                    # Last resort: Revenue → Net Income directly
                    node("ni", "Net Income", ni_act, ni_act,
                         inc.get("net_income_yoy_pct"), L4, _DK_GREEN, 0, "profit")
                    link("rev", "ni", ni_act, _L_GREEN)

        # Operating Income → Net Income + Income Tax  (+ optional Other Income/Loss)
        if op_income and net_income:
            ni_act  = float(net_income)
            tax_act = float(tax) if tax else 0.0
            oie_val = float(other_ie) if other_ie else 0.0

            if oie_val > 0:
                # ── Positive other income (e.g. interest income) ──────────────
                # Structure: OI → Tax, OI → (NI contribution), Other Income → NI
                # "Interest & Other Income" is a root node at L4 (no source)
                node("oie_pos", "Interest & Other Income", oie_val, oie_val,
                     None, L4, _DK_GREEN, 5, "profit")

                if tax_act > 0:
                    node("tax", "Income Tax", tax_act, tax_act,
                         None, L5, _DK_RED, 1, "cost")
                    link("oi", "tax", tax_act, _L_RED)

                # OI contributes (OI - tax) toward NI; Other Income adds the rest
                oi_to_ni = max(round(op_income - tax_act, 1), 0.0)
                node("ni", "Net Income", ni_act, ni_act,
                     inc.get("net_income_yoy_pct"), L5, _DK_GREEN, 0, "profit")
                if oi_to_ni > 0:
                    link("oi", "ni", oi_to_ni, _L_GREEN)
                link("oie_pos", "ni", oie_val, _L_GREEN)

            else:
                # ── Negative or zero other income (loss / provision) ──────────
                other_act = abs(oie_val) if oie_val < 0 else 0.0

                if tax_act == 0 and other_act == 0:
                    implied = round(op_income - ni_act, 1)
                    if implied > 0.1:
                        tax_act = implied
                        _show_tax_as = "Tax & Other"
                    else:
                        _show_tax_as = "Income Tax"
                else:
                    _show_tax_as = "Income Tax"

                known_sum = ni_act + tax_act + other_act
                scale = (op_income / known_sum) if known_sum > 0 else 1.0

                if ni_act > 0:
                    node("ni", "Net Income", ni_act * scale, ni_act,
                         inc.get("net_income_yoy_pct"), L5, _DK_GREEN, 0, "profit")
                    link("oi", "ni", ni_act * scale, _L_GREEN)

                if tax_act > 0:
                    node("tax", _show_tax_as, tax_act * scale, tax_act,
                         None, L5, _DK_RED, 1, "cost")
                    link("oi", "tax", tax_act * scale, _L_RED)

                if other_act > 0:
                    node("other", "Other Loss", other_act * scale, other_act,
                         None, L5, _DK_RED, 2, "cost")
                    link("oi", "other", other_act * scale, _L_RED)

        # Cost of Revenue → Product Costs + Service Costs (at L5)
        if cost_of_rev and cost_of_rev > 0:
            _bk_valid = (
                product_costs and product_costs > 0 and
                service_costs and service_costs > 0 and
                abs((product_costs + service_costs) - cost_of_rev) / cost_of_rev < 0.05
            )
            if _bk_valid:
                node("pc", "Product costs", product_costs, product_costs,
                     None, L5, _DK_RED, 100, "cost")
                link("cor", "pc", product_costs, _L_RED)
                node("sc", "Service costs", service_costs, service_costs,
                     None, L5, _DK_RED, 101, "cost")
                link("cor", "sc", service_costs, _L_RED)

        # Operating Expenses → R&D + SG&A (or S&M + G&A separately) + residual
        if total_opex and total_opex > 0:
            opex_items: list[tuple] = []
            if rd and rd > 0:
                opex_items.append(("rd", "R&D", rd, _DK_PURPLE, _L_PURPLE, 10))
            if sga and sga > 0:
                # Combined selling_general_administrative (new extraction format)
                opex_items.append(("sga", "SG&A", sga, _DK_ORANGE, _L_ORANGE, 11))
            else:
                # Legacy separate fields
                if sm and sm > 0:
                    opex_items.append(("sm", "Sales & Marketing", sm, _DK_ORANGE, _L_ORANGE, 11))
                if ga and ga > 0:
                    opex_items.append(("ga", "G&A", ga, _DK_MGNTA, _L_MGNTA, 12))
            if oo and oo > 0:
                opex_items.append(("oo", "Other OpEx", oo, _DK_MGNTA, _L_MGNTA, 13))
            breakdown_sum = sum(t[2] for t in opex_items)
            residual = round(total_opex - breakdown_sum, 2)
            if residual > 0.15:
                opex_items.append(("oo_res", "Other OpEx", residual, _DK_MGNTA, _L_MGNTA, 14))
            for key, name, val, col, lcol, sord in opex_items:
                node(key, name, val, val, None, L5, col, sord, "cost")
                link("opex", key, val, lcol)

    gm = round(gross_profit / total_rev * 100, 1) if (gross_profit and total_rev) else None
    om = round(op_income    / total_rev * 100, 1) if (op_income    and total_rev) else None
    nm = round(net_income   / total_rev * 100, 1) if (net_income   and total_rev) else None

    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "company":          company,
            "ticker":           ticker,
            "period":           f"{fy} {period}".strip(),
            "fy":               fy,
            "fiscal_period":    period,
            "report_date":      rdate,
            "currency":         currency,
            "unit":             ul,
            "total_revenue":    total_rev,
            "gross_margin":     gm,
            "operating_margin": om,
            "net_margin":       nm,
            "has_sub_values":   has_sub_vals,
            "num_layers":       num_layers,
        },
    }


# ── HTML renderer ─────────────────────────────────────────────────────────────

def _fmt_date(date_str: str, period: str) -> str:
    """'2025-06-30' + 'FY' → 'Year ended 30 Jun 2025'"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        d = dt.strftime("%-d %b %Y")
        return f"Year ended {d}" if period == "FY" else f"Quarter ended {d}"
    except Exception:
        return date_str


def _render(sd: dict) -> str:
    data      = _prepare(sd)
    meta      = data["meta"]
    raw_nodes = data["nodes"]
    raw_links = data["links"]

    # ── Header text ───────────────────────────────────────────────────────────
    fy       = meta.get("fy", "")
    fp       = meta.get("fiscal_period", "")
    fy_short = fy.replace("FY20", "FY") if fy else ""

    if fp == "FY":
        period_html = f'<span class="period-label">{fy_short}</span> Income statement'
    else:
        period_html = f'<span class="period-label">{fy_short} {fp}</span> Income statement'

    sub_subtitle = _fmt_date(meta["report_date"], fp) if meta["report_date"] else ""

    margin_parts = []
    if meta.get("gross_margin")     is not None:
        margin_parts.append(f"Gross margin {meta['gross_margin']:.1f}%")
    if meta.get("operating_margin") is not None:
        margin_parts.append(f"Operating margin {meta['operating_margin']:.1f}%")
    if meta.get("net_margin")       is not None:
        margin_parts.append(f"Net margin {meta['net_margin']:.1f}%")
    margin_line = "   ·   ".join(margin_parts)

    # ── Convert internal nodes → ECharts format ───────────────────────────────
    has_sub   = meta["has_sub_values"]
    left_lyrs = {0, 1} if has_sub else {0}

    # Deduplicate node names (ECharts uses name as ID)
    name_seen: dict[str, int] = {}
    unique_name: list[str] = []
    for n in raw_nodes:
        base = n["name"]
        cnt  = name_seen.get(base, 0)
        unique_name.append(base if cnt == 0 else f"{base} ({cnt})")
        name_seen[base] = cnt + 1

    echarts_nodes = []
    for i, n in enumerate(raw_nodes):
        uname    = unique_name[i]
        is_left  = n["layer"] in left_lyrs
        echarts_nodes.append({
            "name":         uname,
            "value":        n["displayValue"],
            "depth":        n["layer"],
            "itemStyle":    {"color": n["color"], "borderWidth": 0},
            "label":        {"position": "left" if is_left else "right"},
            # custom fields for JS formatter
            "displayValue": n["displayValue"],
            "yoy":          n["yoy"],
            "nodeType":     n["type"],
        })

    # ── Convert internal links → ECharts format ───────────────────────────────
    echarts_links = []
    for lk in raw_links:
        src_idx  = lk["source"]
        tgt_idx  = lk["target"]
        src_name = unique_name[src_idx]
        tgt_name = unique_name[tgt_idx]

        rgba     = lk["color"]
        hex_col, base_op = _RGBA_TO_HEX.get(rgba, ("#aaaaaa", 0.45))
        # Sub-segment links rendered extra-light to appear visually thin
        is_subseg = raw_nodes[src_idx]["type"] == "subsegment"
        opacity   = 0.25 if is_subseg else base_op

        echarts_links.append({
            "source": src_name,
            "target": tgt_name,
            "value":  lk["value"],
            "lineStyle": {"color": hex_col, "opacity": opacity},
        })

    echarts_data = {
        "nodes": echarts_nodes,
        "links": echarts_links,
        "meta":  meta,
    }

    data_json = json.dumps(echarts_data, ensure_ascii=False, separators=(",", ":"))

    html = _HTML_TEMPLATE
    html = html.replace("__PAGE_TITLE__",   f"{meta['company']} {meta['period']}")
    html = html.replace("__COMPANY__",      meta["company"])
    html = html.replace("__SUBTITLE__",     period_html)
    html = html.replace("__SUB_SUBTITLE__", sub_subtitle)
    html = html.replace("__TICKER_INFO__",  f"{meta['ticker']} · {meta['currency']}")
    html = html.replace("__MARGIN_LINE__",  margin_line)
    html = html.replace("__CHART_DATA__",   data_json)
    return html


# ── HTML / ECharts template ───────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__PAGE_TITLE__</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#fff;font-family:'Segoe UI',system-ui,Arial,sans-serif;overflow-x:hidden}

#hdr{
  padding:16px 24px 10px;
  text-align:center;
  border-bottom:1px solid #e8e8e8;
  background:#fff;
  user-select:none;
}
#hdr h1{font-size:26px;font-weight:700;color:#212121;letter-spacing:.3px}
#hdr .subtitle{font-size:15px;font-weight:400;color:#424242;margin-top:3px}
#hdr .subtitle .period-label{font-weight:800;color:#388e3c}
#hdr .sub-subtitle{font-size:12px;color:#757575;margin-top:2px}
#hdr .ticker{font-size:11px;color:#9e9e9e;margin-top:2px}
#hdr .margins{font-size:12px;font-weight:600;color:#455a64;margin-top:4px;letter-spacing:.15px}

#chart{width:100%;overflow:hidden}
</style>
</head>
<body>

<div id="hdr">
  <h1>__COMPANY__</h1>
  <div class="subtitle">__SUBTITLE__</div>
  <div class="sub-subtitle">__SUB_SUBTITLE__</div>
  <div class="ticker">__TICKER_INFO__</div>
  <div class="margins">__MARGIN_LINE__</div>
</div>

<div id="chart"></div>

<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script>
(function(){
'use strict';

const D    = __CHART_DATA__;
const meta = D.meta;

// ── helpers ───────────────────────────────────────────────────────────────────
function fmtV(v){
  if(v == null) return '?';
  return v >= 10 ? v.toFixed(1) : v.toFixed(2);
}

// Word-wrap: break at spaces to keep lines ≤ maxChars
function wrap(text, maxChars){
  if(!text || text.length <= maxChars) return [text];
  const words = text.split(' ');
  const lines = [];
  let cur = '';
  for(const w of words){
    const cand = cur ? cur + ' ' + w : w;
    if(cand.length <= maxChars){ cur = cand; }
    else { if(cur) lines.push(cur); cur = w; }
  }
  if(cur) lines.push(cur);
  return lines;
}

// ── chart sizing ──────────────────────────────────────────────────────────────
const hdrEl   = document.getElementById('hdr');
const chartEl = document.getElementById('chart');

function chartDims(){
  const hdrH = hdrEl.offsetHeight;
  return {
    w: Math.max(window.innerWidth,  1420),
    h: Math.max(window.innerHeight - hdrH, 520),
  };
}

// Set explicit px dimensions BEFORE echarts.init so it sees a non-zero container
const initDims = chartDims();
chartEl.style.width  = initDims.w + 'px';
chartEl.style.height = initDims.h + 'px';

// ── ECharts init ──────────────────────────────────────────────────────────────
// Must come AFTER the div has a non-zero size
const chart = echarts.init(chartEl, null, {renderer: 'canvas'});

function resize(){
  const {w, h} = chartDims();
  chartEl.style.width  = w + 'px';
  chartEl.style.height = h + 'px';
  chart.resize({width: w, height: h});
}

// ── label formatter (rich text) ───────────────────────────────────────────────
// Each node shows:  Name (bold)  /  $XX.XB (gray)  /  +XX% Y/Y (green or red)
function labelFmt(params){
  const d      = params.data;
  const isSub  = d.nodeType === 'subsegment';
  const isSeg  = d.nodeType === 'segment';

  const nStyle = isSub ? 'ns' : (isSeg ? 'nb' : 'nm');
  const vStyle = isSub ? 'vs' : 'vn';

  // Wrap name into lines of ≤ 24 chars (sub-segs) or ≤ 18 chars (others)
  const maxC   = isSub ? 24 : 18;
  const lines  = wrap(params.name, maxC);

  // Build rich-text string — each line is {style|text} separated by \n
  let out = lines.map(l => '{' + nStyle + '|' + l + '}').join('\n');
  out += '\n{' + vStyle + '|$' + fmtV(d.displayValue) + meta.unit + '}';

  if(d.yoy != null){
    const pos  = d.yoy >= 0;
    const ys   = isSub ? (pos ? 'yps' : 'yns') : (pos ? 'ypn' : 'ynn');
    const sign = pos ? '+' : '';
    out += '\n{' + ys + '|' + sign + Math.round(d.yoy) + '% Y/Y}';
  }
  return out;
}

// ── tooltip formatter ─────────────────────────────────────────────────────────
function tipFmt(params){
  if(params.dataType === 'node'){
    const d = params.data;
    let html = '<b>' + params.name + '</b><br/>$' + fmtV(d.displayValue) + meta.unit;
    if(d.yoy != null){
      const col  = d.yoy >= 0 ? '#388e3c' : '#d81b60';
      const sign = d.yoy >= 0 ? '+' : '';
      html += '<br/><span style="color:' + col + '">'
           + sign + Math.round(d.yoy) + '% Y/Y</span>';
    }
    return html;
  }
  // edge / link
  return params.data.source + '  →  ' + params.data.target
       + '<br/>$' + fmtV(params.value) + meta.unit;
}

// ── ECharts option ────────────────────────────────────────────────────────────
const leftPx  = meta.has_sub_values ? 295 : 210;
const rightPx = 190;

const option = {
  backgroundColor: '#ffffff',

  tooltip: {
    trigger: 'item',
    triggerOn: 'mousemove',
    backgroundColor: 'rgba(20,20,30,0.88)',
    borderWidth: 0,
    textStyle: {color: '#fff', fontSize: 12},
    formatter: tipFmt,
  },

  series: [{
    type: 'sankey',
    orient: 'horizontal',

    // chart area insets (px) — leaves room for left/right labels
    left:   leftPx,
    right:  rightPx,
    top:    18,
    bottom: 18,

    nodeWidth:        12,    // thin node bars
    nodeGap:          10,    // vertical gap between nodes in same column
    layoutIterations: 64,    // iterations for vertical ordering optimisation
    draggable:        true,  // nodes can be dragged vertically

    emphasis: {
      focus:      'adjacency',   // highlight connected flows on hover
      blurScope:  'coordinateSystem',
    },

    // Global link style — per-link lineStyle overrides these
    lineStyle: {
      curveness: 0.5,
      opacity:   0.5,
    },

    // Node + link data (injected from Python _prepare output)
    data:  D.nodes,
    links: D.links,

    // Node label — rich text with bold name, gray value, coloured YoY
    label: {
      show:      true,
      formatter: labelFmt,
      rich: {
        // Segment (bold 12px)
        nb:  {fontWeight:'bold', fontSize:12, color:'#212121', lineHeight:17},
        // Revenue / profit / cost right-side nodes (semi-bold 12px)
        nm:  {fontWeight:'600',  fontSize:12, color:'#212121', lineHeight:17},
        // Sub-segment (regular 10px)
        ns:  {fontWeight:'400',  fontSize:10, color:'#424242', lineHeight:14},
        // Value lines
        vn:  {fontWeight:'400',  fontSize:11, color:'#616161', lineHeight:15},
        vs:  {fontWeight:'400',  fontSize:10, color:'#757575', lineHeight:14},
        // YoY — normal nodes
        ypn: {fontWeight:'600',  fontSize:10, color:'#388e3c', lineHeight:14},
        ynn: {fontWeight:'600',  fontSize:10, color:'#d81b60', lineHeight:14},
        // YoY — sub-segment (slightly smaller)
        yps: {fontWeight:'600',  fontSize: 9, color:'#388e3c', lineHeight:13},
        yns: {fontWeight:'600',  fontSize: 9, color:'#d81b60', lineHeight:13},
      },
    },
  }],
};

chart.setOption(option);

// ── resize on window change ───────────────────────────────────────────────────
window.addEventListener('resize', resize, {passive: true});

})();
</script>
</body>
</html>"""


# ── Smoke test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os

    sample = {
        "company": "Microsoft Corp",  "ticker": "MSFT",
        "report_date": "2025-06-30",  "form_type": "10-K",
        "fiscal_year": "FY2025",      "fiscal_period": "FY",
        "currency": "USD",            "unit": "billions",
        "segments": [
            {
                "name": "Productivity and Business Processes",
                "revenue": 120.8, "operating_income": 69.8, "yoy_growth_pct": 13.0,
                "sub_segments": [
                    {"name": "Microsoft 365 Commercial products and cloud services", "revenue": 87.8, "yoy_growth_pct": 14.0},
                    {"name": "LinkedIn",                                              "revenue": 17.8, "yoy_growth_pct":  9.0},
                    {"name": "Dynamics products and cloud services",                  "revenue":  7.8, "yoy_growth_pct": 15.0},
                    {"name": "Microsoft 365 Consumer products and cloud services",    "revenue":  7.4, "yoy_growth_pct": 11.0},
                ],
            },
            {
                "name": "Intelligent Cloud",
                "revenue": 106.3, "operating_income": 44.6, "yoy_growth_pct": 21.0,
                "sub_segments": [
                    {"name": "Server products and cloud services", "revenue": 98.5, "yoy_growth_pct": 23.0},
                    {"name": "Enterprise and partner services",    "revenue":  7.8, "yoy_growth_pct":  2.0},
                ],
            },
            {
                "name": "More Personal Computing",
                "revenue": 54.6, "operating_income": 14.2, "yoy_growth_pct": 7.0,
                "sub_segments": [
                    {"name": "Gaming",                     "revenue": 23.5, "yoy_growth_pct":  9.0},
                    {"name": "Windows and Devices",        "revenue": 17.3, "yoy_growth_pct":  2.0},
                    {"name": "Search and news advertising","revenue": 13.9, "yoy_growth_pct": 13.0},
                ],
            },
        ],
        "income_statement": {
            "total_revenue": 281.7,    "total_revenue_yoy_pct": 15.0,
            "cost_of_revenue": 87.8,   "cost_of_revenue_yoy_pct": 19.0,
            "gross_profit": 193.9,     "gross_profit_yoy_pct": 13.0,
            "operating_expenses": {
                "research_and_development": 32.5,
                "sales_and_marketing": 25.7,
                "general_and_administrative": 7.2,
            },
            "total_operating_expenses": 65.4,
            "operating_income": 128.5, "operating_income_yoy_pct": 17.0,
            "other_income_expense": -4.9,
            "income_before_tax": 123.6,
            "tax_expense": 21.8,
            "net_income": 101.8,       "net_income_yoy_pct": 16.0,
            "cost_of_revenue_breakdown": {
                "product_costs": 13.5,
                "service_costs": 74.3,
            },
        },
    }

    os.makedirs("output", exist_ok=True)
    build_sankey_chart(sample, output_path="output/MSFT_FY25_sample_sankey.html")
    print("Open output/MSFT_FY25_sample_sankey.html in your browser.")
