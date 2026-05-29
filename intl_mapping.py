"""
Mapping for international stocks.

Three categories:
1. FILES_ON_EDGAR: These companies file 20-F or 40-F on SEC EDGAR.
   We map their local ticker → EDGAR ticker/name for CIK lookup.
2. NO_EDGAR: These companies do NOT file on EDGAR.
   We provide their Investor Relations PDF URLs for annual reports.
3. US_LISTED_FOREIGN: Foreign companies with US ADR/listing that file on EDGAR.
"""

# --- Category 1: Map local exchange ticker → EDGAR ticker ---
# These file 20-F (or 40-F for Canadian) on SEC EDGAR
INTL_TO_EDGAR_TICKER = {
    # Asian
    "9988.HK":  "BABA",     # Alibaba files as BABA
    "6758.T":   "SONY",     # Sony files as SONY
    "8306.T":   "MUFG",     # Mitsubishi UFJ files as MUFG
    # European
    "SHEL.L":   "SHEL",     # Shell files as SHEL
    "SAP.DE":   "SAP",      # SAP files as SAP
    "SAN.MC":   "SAN",      # Santander files as SAN
    "NOVO-B.CO":"NVO",      # Novo Nordisk files as NVO
    "SHOP.TO":  "SHOP",     # Shopify files as SHOP (dual NYSE/TSX)
    # Canadian (file 40-F instead of 20-F)
    "RY.TO":    "RY",       # Royal Bank of Canada
    "TD.TO":    "TD",       # Toronto Dominion
}

# --- Category 2: US-listed ADRs that file on EDGAR directly ---
# These use their US ticker directly for EDGAR lookup
US_LISTED_FOREIGN = [
    "TSM",      # Taiwan Semiconductor - 20-F
    "ASML",     # ASML - 20-F
    "NVS",      # Novartis - 20-F
    "TM",       # Toyota - 20-F
    "ACN",      # Accenture - actually files 10-K (Irish but US-listed)
]

# --- Category 3: NO EDGAR filing — need PDF annual reports ---
# We store investor relations URLs for annual report downloads
NO_EDGAR_STOCKS = {
    "005930.KS": {
        "company": "Samsung Electronics",
        "ir_url": "https://www.samsung.com/global/ir/reports-disclosures/business-report/",
        "notes": (
            "Quarterly 'Business Report' PDFs: Samsung publishes 1Q/3Q/4Q standalone "
            "plus a Half-Year report (H1 = cumulative Q1+Q2). Q2 is synthesized from H1−Q1."
        ),
        "pdf_urls": {
            "2024":    "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2024_4Q_Interim_Report.pdf",
            "2023":    "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2023_4Q_Interim_Report.pdf",
            "2024-Q1": "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2024_1Q_Interim_Report.pdf",
            "2024-H1": "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2024_Half_Interim_Report.pdf",
            "2024-Q3": "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2024_3Q_Interim_Report.pdf",
            "2023-Q1": "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2023_1Q_Interim_Report.pdf",
            "2023-H1": "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2023_Half_Interim_Report.pdf",
            "2023-Q3": "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2023_3Q_Interim_Report.pdf",
        },
    },
    "000660.KS": {
        "company": "SK Hynix Inc.",
        "ir_url": "https://www.skhynix.com/ir/UI-FR-IR13/",
        "notes": (
            "No stable direct PDF URLs (IR portal uses JS gated downloads). "
            "Download Annual Business Report manually from the IR portal and "
            "place at data/annual_reports/000660_KS/000660_KS_annual_2024.pdf"
        ),
    },
    "MC.PA": {
        "company": "LVMH Moët Hennessy",
        "ir_url": "https://www.lvmh.com/investors/publications/",
        "notes": (
            "LVMUY trades OTC; does not file 20-F. Annual + half-year auto-downloaded. "
            "LVMH publishes Q1/Q3 revenue-only press releases, but the PDF URLs use "
            "opaque hashes (voda.akamaized.net/lvmh/<id>/files/…) that change per "
            "release and cannot be hard-coded. To add Q1/Q3 data: download manually "
            "from lvmh.com and place at data/annual_reports/MC_PA/MC_PA_Q1_2024.pdf, "
            "data/annual_reports/MC_PA/MC_PA_9M_2024.pdf, etc."
        ),
        "revenue_only_periods": ["Q1", "Q3", "9M"],
        "pdf_urls": {
            "2024": "https://lvmh-com.cdn.prismic.io/lvmh-com/Z-PY3HdAxsiBv6wN_UniversalRegistrationDocument2024.pdf",
            "2023": "https://urd.lvmh.com/en/urd-2023-va_vdef.pdf",
        },
    },
    "NESN.SW": {
        "company": "Nestlé S.A.",
        "ir_url": "https://www.nestle.com/investors/publications",
        "notes": (
            "NSRGY trades OTC. Reports: Annual (full IS+segments) + H1 (full) + "
            "Q1/3-month sales (revenue-only) + 9-month sales (revenue-only). "
            "Standalone Q2 = H1−Q1, Q3 = 9M−H1, Q4/H2 = FY−9M synthesized downstream."
        ),
        "revenue_only_periods": ["Q1", "9M"],
        "pdf_urls": {
            "2024":    "https://www.nestle.com/sites/default/files/2025-02/annual-review-2024-en.pdf",
            "2023":    "https://www.nestle.com/sites/default/files/2024-02/2023-annual-review-en.pdf",
            "2024-H1": "https://www.nestle.com/sites/default/files/2024-07/2024-half-year-report-en.pdf",
            "2023-H1": "https://www.nestle.com/sites/default/files/2023-07/2023-half-year-report-en.pdf",
            "2024-Q1": "https://www.nestle.com/sites/default/files/2024-04/three-month-sales-2024-press-release-en.pdf",
            "2024-9M": "https://www.nestle.com/sites/default/files/2024-10/2024-nine-month-sales-press-release-en.pdf",
            "2023-Q1": "https://www.nestle.com/sites/default/files/2023-04/three-month-sales-2023-press-release-en.pdf",
            "2023-9M": "https://www.nestle.com/sites/default/files/2023-10/2023-nine-month-sales-press-release-en.pdf",
        },
    },
    "ALV.DE": {
        "company": "Allianz SE",
        "ir_url": "https://www.allianz.com/en/investor_relations/results-reports/annual-reports.html",
        "notes": (
            "Allianz CDN is Cloudflare-protected and returns 403 to scripted requests; "
            "using Wayback Machine snapshots. Allianz discontinued standalone Q1/Q3 "
            "interim reports in 2016 — they only publish Annual + H1 (2Q Interim Report). "
            "Q1 and Q3 are released as earnings press releases on the Cloudflare-blocked "
            "press domain (not in Wayback). To add Q1/Q3 manually: download from "
            "https://www.allianz.com/en/investor_relations/results-reports/results.html "
            "and place as data/annual_reports/ALV_DE/ALV_DE_Q1_2024.pdf, "
            "ALV_DE_Q3_2024.pdf, etc. (revenue_only_periods config will apply)."
        ),
        "revenue_only_periods": ["Q1", "Q3"],
        "pdf_urls": {
            "2025":    "https://web.archive.org/web/20260528000616/https://www.allianz.com/content/dam/onemarketing/azcom/Allianz_com/investor-relations/en/results-reports/annual-report/ar-2025/en-allianz-group-annual-report-2025.pdf",
            "2024":    "https://web.archive.org/web/20250403170742/https://www.allianz.com/content/dam/onemarketing/azcom/Allianz_com/investor-relations/en/results-reports/annual-report/ar-2024/en-allianz-group-annual-report-2024.pdf",
            "2023":    "https://web.archive.org/web/20240520194118/https://www.allianz.com/content/dam/onemarketing/azcom/Allianz_com/investor-relations/en/results-reports/annual-report/ar-2023/en-Allianz-Group-Annual-Report-2023.pdf",
            "2025-H1": "https://web.archive.org/web/20260528000952/https://www.allianz.com/content/dam/onemarketing/azcom/Allianz_com/investor-relations/en/results/2025-2q/2q-2025-interim-report-allianz.pdf",
            "2024-H1": "https://web.archive.org/web/20260528000959/https://www.allianz.com/content/dam/onemarketing/azcom/Allianz_com/investor-relations/en/results/2024-2q/en-interim-report-2Q-2024.pdf",
            "2023-H1": "https://web.archive.org/web/20231029103329/https://www.allianz.com/content/dam/onemarketing/azcom/Allianz_com/investor-relations/en/results/2023-2q/en-interim-report-2q-2023.pdf",
        },
    },
    "AZN": {
        "company": "AstraZeneca PLC",
        "ir_url": "https://www.astrazeneca.com/investor-relations.html",
        "notes": (
            "Files 20-F annually on SEC EDGAR but no regular quarterly filings — "
            "switched to PDF path so we can pick up Q1 / H1 / Q3 interim reports "
            "from AZ's IR site. Q1 / H1 / Q3 reports are FULL interim filings "
            "(income statement + therapeutic-area revenue), not revenue-only press "
            "releases — leave revenue_only_periods empty. PDF URLs change per "
            "release; download manually from astrazeneca.com/investor-relations.html "
            "and place at data/annual_reports/AZN/AZN_annual_2024.pdf, "
            "AZN_Q1_2024.pdf, AZN_H1_2024.pdf, AZN_Q3_2024.pdf, etc."
        ),
        "revenue_only_periods": [],
        "pdf_urls": {},
    },
    "HSBC": {
        "company": "HSBC Holdings plc",
        "ir_url": "https://www.hsbc.com/investors/results-and-announcements",
        "notes": (
            "Files 20-F annually on SEC EDGAR. The dozens of 6-Ks HSBC files "
            "are regulatory notices (AGM statements, debt issuance, awards "
            "grants), not financial reports — switched to PDF path so we can "
            "pick up Q1 / H1 / Q3 from HSBC's IR site. H1 = Jan–Jun cumulative "
            "(FULL Interim Report). Q1 / Q3 are 3-month / 9-month trading "
            "updates (revenue-only press releases). The pipeline synthesizes "
            "Q2 = H1 − Q1 and Q4 = FY − Q1 − Q2 − Q3 downstream. PDF URLs "
            "below come from hsbc.com/investors/results-and-announcements/"
            "all-reporting/group — they're stable per release, but new "
            "quarters need to be appended here as HSBC publishes them."
        ),
        "revenue_only_periods": ["Q1", "Q3", "9M"],
        "pdf_urls": {
            # Annual Report and Accounts (full Annual Report = 20-F equivalent)
            "2023":    "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/annual/pdfs/hsbc-holdings-plc/240226-annual-report-and-accounts-2023.pdf",
            "2024":    "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2024/annual/pdfs/hsbc-holdings-plc/250219-annual-report-and-accounts-2024.pdf",
            "2025":    "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-annual-report-and-accounts-2025.pdf",
            # Interim Report (H1 = Jan-Jun, FULL report including IS + segments)
            "2023-H1": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/interim/pdfs/hsbc-holdings-plc/230801-interim-report-2023.pdf",
            "2024-H1": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2024/interim/pdfs/hsbc-holdings-plc/240731-interim-report-2024.pdf",
            "2025-H1": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/interim/pdfs/hsbc-holdings-plc/250730-hsbc-holdings-plc-interim-report-2025.pdf",
            # Q1 Earnings Release (revenue-only trading update)
            "2023-Q1": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/1q/pdfs/hsbc-holdings-plc/230502-1q-2023-earnings-release.pdf",
            "2024-Q1": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2024/1q/pdfs/hsbc-holdings-plc/240430-1q-2024-earnings-release.pdf",
            "2025-Q1": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/1q/pdfs/hsbc-holdings-plc/250429-1q-2025-earnings-release.pdf",
            "2026-Q1": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2026/1q/pdfs/hsbc-holdings-plc/260505-1q-2026-earnings-release.pdf",
            # Q3 Earnings Release (revenue-only)
            "2023-Q3": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/3q/pdfs/hsbc-holdings-plc/231030-3q-2023-earnings-release.pdf",
            "2024-Q3": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2024/3q/pdfs/hsbc-holdings-plc/sea-241029-e-3q-2024-earnings-release.pdf",
            "2025-Q3": "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/3q/pdfs/hsbc-holdings-plc/251028-3q-2025-earnings-release.pdf",
        },
    },
    "SIE.DE": {
        "company": "Siemens AG",
        "ir_url": "https://www.siemens.com/investor/en/",
        "notes": (
            "SIEGY trades OTC. Annual Financial Report + Q1/Q3 press / earnings releases "
            "auto-downloaded. Q2 earnings release URL not consistently exposed."
        ),
        "pdf_urls": {
            "2024":    "https://assets.new.siemens.com/siemens/assets/api/uuid:ae46683e-14dd-4455-a882-09d4184457c7/Annual-Financial-Report-FY2024.pdf",
            "2023":    "https://assets.new.siemens.com/siemens/assets/api/uuid:bb51b804-05aa-4590-84e3-0cad12783255/Annual-Financial-Report-FY2023.pdf",
            "2024-Q1": "https://assets.new.siemens.com/siemens/assets/api/uuid:311a8f40-b968-429f-8b8e-94ea513b991b/2024-q1-press-release-en.pdf",
            "2024-Q3": "https://assets.new.siemens.com/siemens/assets/api/uuid:4091122e-baa2-4c30-bf53-b6ff2b5e898b/2024-q3-earnings-release-en.pdf",
            "2023-Q1": "https://assets.new.siemens.com/siemens/assets/api/uuid:23ad8781-9e93-4f84-987d-6a185a8521e1/HQCOPR202302086646EN.pdf",
            "2023-Q3": "https://assets.new.siemens.com/siemens/assets/api/uuid:2fbfb90c-9337-4588-87b2-ffbaf23aab6d/2023-q3-earnings-release-en.pdf",
        },
    },
    "CBA.AX": {
        "company": "Commonwealth Bank of Australia",
        "ir_url": "https://www.commbank.com.au/about-us/investors/annual-reports.html",
        "notes": (
            "No US listing. Annual + half-year profit announcements only — no standalone "
            "quarterlies. CBA fiscal year ends June 30; '1H24' = Jul-Dec 2023."
        ),
        "pdf_urls": {
            "2024":    "https://www.commbank.com.au/content/dam/commbank-assets/investors/docs/results/fy24/2024-Annual-Report_spreads.pdf",
            "2023":    "https://www.commbank.com.au/content/dam/commbank/about-us/shareholders/us-investors/docs/2023-US-Financial-Report.pdf",
            "2024-H1": "https://www.commbank.com.au/content/dam/commbank-assets/investors/docs/results/1h24/CBA-1H24-Profit-Announcement.pdf",
            "2023-H1": "https://www.commbank.com.au/content/dam/commbank-assets/investors/docs/results/1h23/CBA-1H23-Profit-Announcement.pdf",
        },
    },
    "TCEHY": {
        "company": "Tencent Holdings Ltd.",
        "ir_url": "https://www.tencent.com/en-us/investors/financial-reports.html",
        "notes": (
            "Hong Kong listed (700.HK); OTC ADR TCEHY does not file with SEC. "
            "Annual reports + quarterly results press releases auto-downloaded."
        ),
        "pdf_urls": {
            "2024":    "https://static.www.tencent.com/uploads/2025/04/08/1132b72b565389d1b913aea60a648d73.pdf",
            "2023":    "https://static.www.tencent.com/uploads/2024/04/08/e95c902973fc282be3b3e285c6245281.pdf",
            "2024-Q1": "https://static.www.tencent.com/uploads/2024/05/14/207c400f3d6e2d9894c0b9b778507cf1.pdf",
            "2024-Q2": "https://static.www.tencent.com/uploads/2024/08/14/027889ef78b4ed2b83337dd4a7c2ffef.pdf",
            "2024-Q3": "https://static.www.tencent.com/uploads/2024/11/13/fc23e847ab5be9093587be6b7b01c115.pdf",
            "2023-Q1": "https://static.www.tencent.com/uploads/2023/05/17/7b07c1a2b0befc1a89a6fc4219ed6cae.pdf",
            "2023-Q2": "https://static.www.tencent.com/uploads/2023/08/16/fd005676b39a09da4ac60be5889b6ba0.pdf",
            "2023-Q3": "https://static.www.tencent.com/uploads/2023/11/15/9e4da3187104bbdf04e2cbe491b75147.pdf",
        },
    },
    "RHHBY": {
        "company": "Roche Holding AG",
        "ir_url": "https://www.roche.com/investors/annualreport24",
        "notes": (
            "Swiss listed (ROG.SW); OTC ADR RHHBY does not file with SEC. "
            "Annual + half-year reports only — no standalone quarterly disclosure. "
            "Q2 and Q4/H2 are synthesized from the H1 + FY data."
        ),
        "pdf_urls": {
            "2024":    "https://assets.roche.com/f/176343/x/09457b2a19/ar24e.pdf",
            "2023":    "https://assets.roche.com/f/176343/x/98b8e2ba9d/ar23e.pdf",
            "2024-H1": "https://assets.roche.com/f/176343/x/b4e99fb76c/hy24e.pdf",
            "2023-H1": "https://assets.roche.com/f/176343/x/40d59063c5/hy23e.pdf",
        },
    },
}


def get_edgar_ticker(ticker: str) -> str | None:
    """
    Given a local exchange ticker, return the EDGAR ticker if the company files on EDGAR.
    Returns None if the company doesn't file on EDGAR.
    """
    # Direct US-listed foreign companies
    if ticker in US_LISTED_FOREIGN:
        return ticker
    
    # Mapped international tickers
    if ticker in INTL_TO_EDGAR_TICKER:
        return INTL_TO_EDGAR_TICKER[ticker]
    
    return None


def needs_pdf_download(ticker: str) -> bool:
    """Check if this stock requires manual PDF annual report download."""
    return ticker in NO_EDGAR_STOCKS


def get_ir_info(ticker: str) -> dict | None:
    """Get investor relations info for stocks that need PDF downloads."""
    return NO_EDGAR_STOCKS.get(ticker)
