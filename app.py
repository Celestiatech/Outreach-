"""
Outreach UI – Streamlit dashboard
----------------------------------
Wraps the existing scraper and bulk-emailer scripts in a browser-based UI.

Run:
    streamlit run app.py
"""

from __future__ import annotations

import csv
import imaplib
import io
import logging
import os
import queue
import smtplib
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pandas as pd
import streamlit as st

import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Outreach Dashboard",
    page_icon="📧",
    layout="wide",
)

# Initialise SQLite and migrate any legacy CSV files (runs once per process)
db.init_db()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LEADS_CSV = "leads.csv"           # staging / most-recent scrape
LIVE_LEADS_CSV = "live_leads.csv" # permanent cumulative store
SENT_LOG_CSV = "sent_log.csv"
REPLIES_LOG_CSV = "replies_log.csv"  # cumulative log of lead replies detected
PIPELINE_CSV = "pipeline.csv"        # CRM pipeline tracking per lead
UNSUBSCRIBE_TXT = "unsubscribe.txt"
EMAIL_TEMPLATE_TXT = "email_template.txt"

# Migrate any legacy CSV / txt files into SQLite on first run
for _legacy_csv in [LEADS_CSV, LIVE_LEADS_CSV, SENT_LOG_CSV, REPLIES_LOG_CSV, PIPELINE_CSV]:
    db.migrate_csv_if_exists(_legacy_csv)
db.migrate_unsubscribe_txt_if_exists(UNSUBSCRIBE_TXT)

# ---------------------------------------------------------------------------
# Pipeline / CRM constants
# ---------------------------------------------------------------------------

PIPELINE_STATUSES: list[str] = [
    "new",
    "contacted",
    "replied",
    "interested",
    "call_booked",
    "closed_won",
    "closed_lost",
]

PIPELINE_STATUS_EMOJI: dict[str, str] = {
    "new": "🆕",
    "contacted": "📤",
    "replied": "💬",
    "interested": "🔥",
    "call_booked": "📞",
    "closed_won": "🏆",
    "closed_lost": "❌",
}

# Simple keyword lists for reply classification
_REPLY_POSITIVE_KW: list[str] = [
    "yes", "interested", "sounds good", "let's chat", "book", "schedule",
    "when can", "tell me more", "how much", "price", "cost", "available",
    "love to", "would like", "happy to", "sure", "absolutely", "definitely",
    "please send", "great idea", "looks good", "sign me up",
]
_REPLY_NEGATIVE_KW: list[str] = [
    "not interested", "no thanks", "unsubscribe", "remove me",
    "please remove", "stop emailing", "don't contact", "do not contact",
    "not looking", "already have someone", "no need", "not relevant",
]

# ---------------------------------------------------------------------------
# Conversion copy — offers, CTAs, proof lines
# ---------------------------------------------------------------------------

# Pre-written offer strings mapped to readable labels.
# Each offer is concrete (specific outcome + timeframe).
OFFER_LIBRARY: dict[str, str] = {
    "🎁 Free website audit": (
        "I'd like to send you a free 10-point website audit — "
        "no strings attached, just a clear list of what's costing you customers."
    ),
    "⚡ Fix loading speed in 24h": (
        "I can fix your page-speed issues in 24 hours, "
        "often cutting load time by 50%+ with no redesign needed."
    ),
    "🖥 Free homepage redesign preview": (
        "I'll mock up a redesigned homepage for free — "
        "you keep it whether we work together or not."
    ),
    "📈 Increase conversions (no redesign)": (
        "I can increase your conversion rate in 7 days "
        "by fixing the UX friction points I spotted — no full redesign required."
    ),
    "🔍 Free SEO quick-win report": (
        "I'll put together a free SEO quick-win report for your site — "
        "the 3 easiest fixes that typically move rankings within a month."
    ),
    "💬 Quick 15-min strategy call": (
        "I'm offering a free 15-minute strategy call this week — "
        "I'll walk you through exactly what I'd fix and why."
    ),
    "(Custom — write your own)": "",
}

# Strong, outcome-focused CTAs (mapped label → full sentence).
CTA_OPTIONS: dict[str, str] = {
    "Should I send the audit?": "Should I send the audit over?",
    "Want me to fix this for you?": "Want me to fix this for you? I can usually turn it around in 24–48 hours.",
    "Can I show you exactly what's wrong?": "Can I show you exactly what's wrong? Happy to record a quick 2-min walkthrough.",
    "Shall I book us in for a quick call?": "Shall I book us in for a quick call this week?",
    "Can I send you a free preview?": "Can I send you a free preview of what the fix would look like?",
    "(Custom — write your own)": "",
}

# Credibility proof lines — one-liners that signal experience without a case study.
PROOF_EXAMPLES: list[str] = [
    "I recently fixed this exact issue for a {niche} business and their leads doubled within 30 days.",
    "I've helped {niche} businesses in {city} fix exactly this — usually takes less than a week.",
    "Just wrapped up a project for a {niche} owner where we cut their bounce rate by 40%.",
    "I've done this for a handful of small businesses and the results are always noticeable fast.",
    "(Custom — write your own)",
]


BING_FIELDNAMES = [
    "source", "keyword", "niche", "url", "title", "email", "phone",
    "whatsapp_number", "has_whatsapp",
    "linkedin", "twitter", "facebook", "instagram",
    "contact_page", "issues", "lead_score",
]
MAPS_FIELDNAMES = [
    "source", "keyword", "niche", "city", "name", "address", "phone",
    "whatsapp_number", "has_whatsapp", "website",
    "rating", "reviews", "category", "email", "linkedin", "twitter", "facebook",
    "instagram", "contact_page", "issues", "lead_score",
]

# ---------------------------------------------------------------------------
# Keyword library — categorised buyer-intent / niche search queries
# ---------------------------------------------------------------------------

KEYWORD_LIBRARY: dict[str, list[str]] = {
    "💰 Service Buyers": [
        "looking for web developer",
        "need SEO expert",
        "hire digital marketer",
        "website redesign needed",
        "looking for freelancer",
        "need website built",
        "web design needed",
        "need online marketing help",
    ],
    "⚡ Urgent / High Intent": [
        "need website urgently",
        "looking for developer asap",
        "project available freelance",
        "immediate requirement designer",
        "need developer immediately",
        "urgent web design needed",
        "hire developer now",
        "website help asap",
    ],
    "🏢 Local Business Niches": [
        "dentist",
        "gym personal trainer",
        "real estate agent",
        "restaurant",
        "plumber",
        "accountant small business",
        "law firm",
        "car dealership",
    ],
    "🌐 Service Businesses": [
        "digital agency",
        "marketing agency",
        "web design studio",
        "SEO company",
        "social media agency",
        "branding agency",
        "e-commerce store",
    ],
    "🎯 Problem-Based (High Conversion)": [
        "low website conversion",
        "slow website fix",
        "no online booking",
        "website not ranking Google",
        "need more customers online",
        "increase website traffic",
        "outdated website redesign",
        "poor website design help",
    ],
    "🔥 Freelance / Startup": [
        "freelance developer needed",
        "remote developer job",
        "contract web developer",
        "startup looking for developer",
        "small business website help",
        "part time SEO specialist",
    ],
}


def _read_csv(path: str) -> pd.DataFrame:
    """Read a data file — routes known CSV names to SQLite."""
    return db.read_csv_as_table(path)


def _write_csv(path: str, df: pd.DataFrame) -> None:
    """Write a data file — routes known CSV names to SQLite."""
    db.write_csv_as_table(path, df)


def _push_to_live_leads(records: List[Dict]) -> Tuple[int, int]:
    """
    Append *records* to ``LIVE_LEADS_CSV``, deduplicating on all columns.

    Returns ``(new_count, duplicate_count)``.
    """
    new_df = pd.DataFrame(records)
    existing = _read_csv(LIVE_LEADS_CSV)

    if existing.empty:
        _write_csv(LIVE_LEADS_CSV, new_df)
        return len(new_df), 0

    combined = pd.concat([existing, new_df], ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates()
    after = len(combined)

    _write_csv(LIVE_LEADS_CSV, combined)

    new_count = max(after - len(existing), 0)
    dup_count = max(len(new_df) - new_count, 0)
    return new_count, dup_count


def _default_template() -> str:
    p = Path(EMAIL_TEMPLATE_TXT)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return (
        "Hi {name},\n\n"
        "I came across {url} and wanted to reach out.\n\n"
        "Best regards,\n{from_name}\n"
    )


# ---------------------------------------------------------------------------
# Pipeline / CRM helpers
# ---------------------------------------------------------------------------

def _classify_reply(subject: str) -> str:
    """Return ``'positive'``, ``'negative'``, or ``'neutral'`` from reply subject."""
    text = subject.lower()
    for kw in _REPLY_NEGATIVE_KW:
        if kw in text:
            return "negative"
    for kw in _REPLY_POSITIVE_KW:
        if kw in text:
            return "positive"
    return "neutral"


def _load_pipeline() -> pd.DataFrame:
    """Load pipeline table from SQLite."""
    return db.read_table("pipeline")


def _save_pipeline(df: pd.DataFrame) -> None:
    db.write_table("pipeline", df)


def _upsert_pipeline_entries(records: List[Dict]) -> int:
    """
    Insert new pipeline rows for *records* (list of lead dicts).
    Existing entries (matched on email) are left unchanged.
    Returns the number of new rows added.
    """
    pip_df = _load_pipeline()
    existing_emails: set[str] = set(pip_df["email"].str.lower().str.strip())
    now_ts = datetime.now(timezone.utc).isoformat()
    new_rows: List[Dict] = []
    for r in records:
        email = str(r.get("email", "")).lower().strip()
        if not email or email in existing_emails:
            continue
        new_rows.append({
            "email": email,
            "name": r.get("title") or r.get("name") or "",
            "status": "new",
            "deal_value": "0",
            "reply_tag": "",
            "source": r.get("source", ""),
            "keyword": r.get("keyword", ""),
            "niche": r.get("niche") or r.get("category", ""),
            "last_updated": now_ts,
        })
        existing_emails.add(email)
    if new_rows:
        pip_df = pd.concat([pip_df, pd.DataFrame(new_rows)], ignore_index=True)
        _save_pipeline(pip_df)
    return len(new_rows)


def _pipeline_promote(email: str, new_status: str, reply_tag: str = "") -> None:
    """
    Move a pipeline entry to *new_status* (only if it represents forward progress).
    Optionally set *reply_tag*.
    """
    pip_df = _load_pipeline()
    if pip_df.empty or "email" not in pip_df.columns:
        return
    mask = pip_df["email"].str.lower().str.strip() == email.lower().strip()
    if not mask.any():
        return
    current_status = pip_df.loc[mask, "status"].iloc[0]
    # Only promote forward (don't downgrade closed_won/closed_lost)
    if current_status in ("closed_won", "closed_lost"):
        return
    idx_current = PIPELINE_STATUSES.index(current_status) if current_status in PIPELINE_STATUSES else 0
    idx_new = PIPELINE_STATUSES.index(new_status) if new_status in PIPELINE_STATUSES else 0
    if idx_new > idx_current:
        pip_df.loc[mask, "status"] = new_status
        pip_df.loc[mask, "last_updated"] = datetime.now(timezone.utc).isoformat()
    if reply_tag:
        pip_df.loc[mask, "reply_tag"] = reply_tag
        pip_df.loc[mask, "last_updated"] = datetime.now(timezone.utc).isoformat()
    _save_pipeline(pip_df)


# ---------------------------------------------------------------------------
# Logging capture helper (routes stdlib logging into a queue)
# ---------------------------------------------------------------------------

class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(self.format(record))


# ---------------------------------------------------------------------------
# Custom CSS / visual polish
# ---------------------------------------------------------------------------

def _inject_css() -> None:
    st.markdown(
        """
        <style>
        /* ── Font imports ─────────────────────────────────────────── */
        /* Primary: THICCCBOI (Wix Fonts via Fontshare, free) */
        @import url('https://api.fontshare.com/v2/css?f[]=thicccboi@400,500,700,800&display=swap');
        /* Fallback: Space Grotesk (Google Fonts) */
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700;800&display=swap');

        /* Theme-aware tokens (respects Streamlit light/dark) */
        :root {
            --oa-bg: var(--background-color, #ffffff);
            --oa-secondary-bg: var(--secondary-background-color, #f3f4f6);
            --oa-text: var(--text-color, #111827);
            --oa-primary: var(--primary-color, #237FEA);
            --oa-border: rgba(17, 24, 39, 0.10);
            --oa-muted: rgba(17, 24, 39, 0.60);
            --oa-shadow: 0 1px 4px rgba(0,0,0,.12);
            --oa-tab-active-bg: rgba(17, 24, 39, 0.04);
            --oa-glass-bg: rgba(255, 255, 255, 0.55);
            --oa-glass-border: rgba(17, 24, 39, 0.10);
            --oa-glass-shadow: 0 10px 30px rgba(17, 24, 39, 0.12);
            --oa-glass-blur: 18px;
        }
        html[data-theme="dark"], .stApp[data-theme="dark"] {
            --oa-border: rgba(226, 232, 240, 0.14);
            --oa-muted: rgba(226, 232, 240, 0.65);
            --oa-shadow: 0 1px 4px rgba(0,0,0,.35);
            --oa-tab-active-bg: rgba(226, 232, 240, 0.06);
            --oa-glass-bg: rgba(8, 12, 22, 0.55);
            --oa-glass-border: rgba(226, 232, 240, 0.16);
            --oa-glass-shadow: 0 12px 36px rgba(0, 0, 0, 0.45);
        }

        /* ── Global font & background ─────────────────────────────── */
        html, body, [class*="css"], .stApp {
            font-family: "THICCCBOI", "Space Grotesk", "Inter", "Segoe UI", sans-serif;
            background-color: var(--oa-bg);
            color: var(--oa-text);
        }

        .main .block-container {
            background-color: var(--oa-bg);
        }


        /* ── Metric cards ─────────────────────────────────────────── */
        [data-testid="stMetric"] {
            background: var(--oa-glass-bg);
            border: 1px solid var(--oa-glass-border);
            border-radius: 12px;
            padding: 16px 20px;
            box-shadow: var(--oa-glass-shadow);
            backdrop-filter: blur(var(--oa-glass-blur));
        }
        [data-testid="stMetric"]:nth-child(1) { border-top: 4px solid #237FEA; }
        [data-testid="stMetric"]:nth-child(2) { border-top: 4px solid #08C7E1; }
        [data-testid="stMetric"]:nth-child(3) { border-top: 4px solid #08C7E1; }
        [data-testid="stMetric"]:nth-child(4) { border-top: 4px solid #FF7E54; }
        [data-testid="stMetric"]:nth-child(5) { border-top: 4px solid #237FEA; }

        [data-testid="stMetricLabel"] {
            font-size: 0.78rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: .05em;
            color: var(--oa-muted);
        }
        [data-testid="stMetricValue"] {
            font-size: 1.7rem;
            font-weight: 700;
            color: var(--oa-text);
        }

        /* ── Tabs ─────────────────────────────────────────────────── */
        .stTabs [data-baseweb="tab-list"] {
            gap: 4px;
            background: var(--oa-glass-bg);
            padding: 6px 6px 0;
            border-radius: 10px 10px 0 0;
            border: 1px solid var(--oa-glass-border);
            backdrop-filter: blur(var(--oa-glass-blur));
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 8px 8px 0 0;
            padding: 8px 18px;
            font-weight: 500;
            font-size: 0.85rem;
            color: var(--oa-muted);
            background: transparent;
        }
        .stTabs [aria-selected="true"] {
            background: var(--oa-tab-active-bg) !important;
            color: var(--oa-primary) !important;
            border-bottom: 3px solid #237FEA;
            font-weight: 700;
        }

        /* ── Buttons ─────────────────────────────────────────────── */
        .stButton > button[kind="primary"] {
            background: linear-gradient(135deg, #237FEA 0%, #08C7E1 100%);
            border: none;
            border-radius: 8px;
            padding: 8px 24px;
            font-weight: 600;
            color: #fff;
            box-shadow: 0 2px 8px rgba(35,127,234,.35);
            transition: all .2s;
        }
        .stButton > button[kind="primary"]:hover {
            filter: brightness(1.1);
            box-shadow: 0 4px 14px rgba(35,127,234,.5);
            transform: translateY(-1px);
        }
        .stButton > button[kind="secondary"] {
            border-radius: 8px;
            font-weight: 500;
        }

        /* ── Expander ─────────────────────────────────────────────── */
        [data-testid="stExpander"] {
            border: 1px solid var(--oa-glass-border);
            border-radius: 10px;
            overflow: hidden;
            background: var(--oa-glass-bg);
            box-shadow: var(--oa-glass-shadow);
            backdrop-filter: blur(var(--oa-glass-blur));
        }

        /* ── Success / Error / Warning / Info boxes ───────────────── */
        [data-testid="stAlert"] {
            border-radius: 10px;
        }

        /* ── Sidebar branding ─────────────────────────────────────── */
        [data-testid="stSidebar"] {
            background: linear-gradient(160deg, rgba(35,127,234,0.08), rgba(8,199,225,0.06)),
                        var(--oa-secondary-bg);
        }
        [data-testid="stSidebar"] hr {
            border-color: var(--oa-glass-border);
        }
        [data-testid="stSidebar"] [data-testid="stMetric"] {
            background: var(--oa-glass-bg);
            border-color: var(--oa-glass-border);
            border-top-color: #237FEA;
            backdrop-filter: blur(var(--oa-glass-blur));
        }

        /* ── Data tables ─────────────────────────────────────────── */
        [data-testid="stDataFrame"] {
            border-radius: 10px;
            overflow: hidden;
        }

        /* ── Glass panels for inputs/blocks ─────────────────────── */
        [data-testid="stForm"],
        [data-testid="stContainer"],
        [data-testid="stDataFrame"],
        [data-testid="stFileUploader"],
        [data-testid="stSelectbox"],
        [data-testid="stTextInput"],
        [data-testid="stTextArea"],
        [data-testid="stNumberInput"],
        [data-testid="stMultiSelect"] {
            background: var(--oa-glass-bg);
            border: 1px solid var(--oa-glass-border);
            border-radius: 12px;
            box-shadow: var(--oa-glass-shadow);
            backdrop-filter: blur(var(--oa-glass-blur));
        }

        .stTextInput input,
        .stNumberInput input,
        .stTextArea textarea,
        .stSelectbox > div,
        .stMultiSelect > div {
            background: transparent !important;
        }

        /* ── Divider ─────────────────────────────────────────────── */
        hr {
            border-color: var(--oa-border) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )





# ---------------------------------------------------------------------------
# Tab: Dashboard
# ---------------------------------------------------------------------------

# Colour constants reused across the dashboard widgets
_C_BLUE   = "#237FEA"
_C_TEAL   = "#08C7E1"
_C_ORANGE = "#FF7E54"
_C_CARD   = "var(--oa-secondary-bg)"
_C_BORDER = "var(--oa-border)"
_C_MUTED  = "var(--oa-muted)"
_C_WHITE  = "var(--oa-text)"

# Pipeline stage accent colours keyed by stage name (safe against order changes)
_STAGE_COLORS: dict[str, str] = {
    "new":         "#237FEA",
    "contacted":   "#08C7E1",
    "replied":     "#a78bfa",
    "interested":  "#FF7E54",
    "call_booked": "#f59e0b",
    "closed_won":  "#22c55e",
    "closed_lost": "#ef4444",
}


def _sparkline_svg(values: list[float], color: str,
                   width: int = 80, height: int = 32) -> str:
    """Return an inline SVG polyline sparkline from a sequence of float values."""
    if not values or len(values) < 2:
        return ""
    mn, mx = min(values), max(values)
    rng = (mx - mn) or 1
    pad = 3
    n = len(values)
    pts = " ".join(
        f"{pad + i * (width - 2 * pad) / (n - 1):.1f},"
        f"{pad + (1 - (v - mn) / rng) * (height - 2 * pad):.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )


def _kpi_card_html(icon: str, color: str, label: str, value: str,
                   delta: str, delta_up: bool = True,
                   sparkline: str = "") -> str:
    """Return a self-contained HTML KPI card string."""
    arrow = "↑" if delta_up else "↓"
    delta_color = _C_TEAL if delta_up else _C_ORANGE
    sparkline_block = (
        f'<div style="position:absolute;bottom:16px;right:16px;opacity:0.75;">'
        f'{sparkline}</div>'
        if sparkline else ""
    )
    return f"""
    <div style="background:{_C_CARD};border:1px solid {_C_BORDER};border-radius:14px;
                padding:20px 22px;height:100%;box-sizing:border-box;position:relative;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
        <div style="width:40px;height:40px;border-radius:10px;background:{color}22;
                    display:flex;align-items:center;justify-content:center;font-size:1.15rem;">{icon}</div>
        <div style="font-size:0.7rem;color:{_C_MUTED};font-weight:600;
                    text-transform:uppercase;letter-spacing:.06em;">{label}</div>
      </div>
      <div style="font-size:2rem;font-weight:800;color:{_C_WHITE};line-height:1;margin-bottom:8px;">{value}</div>
      <div style="font-size:0.78rem;font-weight:600;color:{delta_color};">{arrow} {delta}</div>
      {sparkline_block}
    </div>"""


def tab_dashboard() -> None:
    import plotly.graph_objects as go

    # ── Load all data sources ───────────────────────────────────
    leads_df    = _read_csv(LIVE_LEADS_CSV)
    staging_df  = _read_csv(LEADS_CSV)
    sent_df     = _read_csv(SENT_LOG_CSV)
    pip_df      = _load_pipeline()
    replies_df  = _read_csv(REPLIES_LOG_CSV)

    # ── Compute KPIs ────────────────────────────────────────────
    total_leads   = len(leads_df)
    staged        = len(staging_df)
    sent_count    = int((sent_df["status"] == "sent").sum())   if "status" in sent_df.columns   else 0
    failed_count  = int((sent_df["status"] == "failed").sum()) if "status" in sent_df.columns   else 0
    reply_count   = len(replies_df)
    closed_won    = int((pip_df["status"] == "closed_won").sum()) if "status" in pip_df.columns else 0
    revenue       = 0.0
    if "deal_value" in pip_df.columns and "status" in pip_df.columns:
        won_mask = pip_df["status"] == "closed_won"
        revenue  = pd.to_numeric(pip_df.loc[won_mask, "deal_value"], errors="coerce").fillna(0).sum()

    total_sent = sent_count + failed_count
    delivery_rate = (
        f"{sent_count / total_sent * 100:.1f}%"
        if total_sent > 0 else "—"
    )

    # ── Compute 14-day sparkline series ─────────────────────────
    today = datetime.now(timezone.utc).date()
    _14_days = [today - timedelta(days=d) for d in range(13, -1, -1)]

    def _daily_counts(df: pd.DataFrame, ts_col: str,
                      status_col: str = "", status_val: str = "") -> list[float]:
        """Return a 14-element list of daily row counts."""
        if df.empty or ts_col not in df.columns:
            return [0.0] * 14
        tmp = df.copy()
        if status_col and status_val and status_col in tmp.columns:
            tmp = tmp[tmp[status_col] == status_val]
        tmp["_d"] = pd.to_datetime(tmp[ts_col], errors="coerce", utc=True).dt.date
        cnts = tmp.dropna(subset=["_d"]).groupby("_d").size()
        return [float(cnts.get(d, 0)) for d in _14_days]

    leads_spark_vals   = [float(total_leads)] * 14   # static; no dated leads CSV
    sent_spark_vals    = _daily_counts(sent_df, "timestamp", "status", "sent")
    # delivery rate per day: sent / (sent+failed); 0.0 when no sends recorded
    sent_per_day  = _daily_counts(sent_df, "timestamp", "status", "sent")
    total_per_day = _daily_counts(sent_df, "timestamp")
    dr_spark_vals = [
        s / t * 100 if t > 0 else 0.0
        for s, t in zip(sent_per_day, total_per_day)
    ]
    rev_spark_vals = _daily_counts(pip_df, "last_updated", "status", "closed_won")

    # Choose sparkline colour: teal for positive metrics, orange for mixed
    leads_spark   = _sparkline_svg(leads_spark_vals,   _C_TEAL)
    sent_spark    = _sparkline_svg(sent_spark_vals,    _C_TEAL)
    dr_spark      = _sparkline_svg(dr_spark_vals,      _C_TEAL)
    rev_spark     = _sparkline_svg(rev_spark_vals,     _C_ORANGE)

    # ── Page header ─────────────────────────────────────────────
    st.markdown(
        f"""
        <div style="display:flex;align-items:flex-end;justify-content:space-between;
                    margin-bottom:28px;padding-bottom:16px;border-bottom:1px solid {_C_BORDER};">
          <div>
            <div style="font-size:1.7rem;font-weight:800;color:{_C_WHITE};
                        letter-spacing:-.02em;line-height:1.1;">Dashboard Overview</div>
            <div style="font-size:0.85rem;color:{_C_MUTED};margin-top:4px;">
              Track leads, campaigns and revenue in real time
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── KPI Cards Row ───────────────────────────────────────────
    k1, k2, k3, k4 = st.columns(4)
    k1.markdown(_kpi_card_html(
        "🔍", _C_BLUE,   "Live Leads",     f"{total_leads:,}",
        f"{staged:,} staged" if staged else "no leads staged",
        delta_up=total_leads > 0,
        sparkline=leads_spark,
    ), unsafe_allow_html=True)
    k2.markdown(_kpi_card_html(
        "📤", _C_TEAL,   "Emails Sent",    f"{sent_count:,}",
        f"{reply_count:,} repl{'y' if reply_count == 1 else 'ies'} detected"
        if reply_count else "awaiting replies",
        delta_up=reply_count > 0,
        sparkline=sent_spark,
    ), unsafe_allow_html=True)
    k3.markdown(_kpi_card_html(
        "✅", _C_TEAL,   "Delivery Rate",  delivery_rate,
        f"{failed_count:,} failed" if failed_count else "no failures",
        delta_up=failed_count == 0,
        sparkline=dr_spark,
    ), unsafe_allow_html=True)
    k4.markdown(_kpi_card_html(
        "🏆", _C_ORANGE, "Closed Revenue", f"${revenue:,.0f}",
        f"{closed_won:,} deal{'s' if closed_won != 1 else ''} won"
        if closed_won else "no closed deals yet",
        delta_up=closed_won > 0,
        sparkline=rev_spark,
    ), unsafe_allow_html=True)

    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

    # ── Middle row: Revenue Breakdown  +  Send Activity Heatmap ──
    left_col, right_col = st.columns([1.1, 0.9], gap="medium")

    # ── LEFT: Revenue Breakdown ──────────────────────────────────
    with left_col:
        # Compute total pipeline deal value and per-source breakdown
        if not pip_df.empty and "deal_value" in pip_df.columns:
            pip_df["_val"] = pd.to_numeric(pip_df["deal_value"], errors="coerce").fillna(0)
            total_pipeline_val = pip_df["_val"].sum()
        else:
            pip_df["_val"] = 0.0
            total_pipeline_val = 0.0

        # Format headline value
        if total_pipeline_val >= 1_000_000:
            total_val_str = f"${total_pipeline_val / 1_000_000:.2f}M"
        elif total_pipeline_val >= 1_000:
            total_val_str = f"${total_pipeline_val / 1_000:.1f}K"
        else:
            total_val_str = f"${total_pipeline_val:,.0f}"

        # Source/keyword breakdown
        group_col = (
            "source"   if "source"  in pip_df.columns and pip_df["source"].replace("", pd.NA).notna().any() else
            "keyword"  if "keyword" in pip_df.columns and pip_df["keyword"].replace("", pd.NA).notna().any() else
            None
        )
        if group_col:
            src_rev = (
                pip_df[pip_df[group_col].replace("", pd.NA).notna()]
                .groupby(group_col)["_val"]
                .sum()
                .sort_values(ascending=False)
                .head(5)
            )
        else:
            src_rev = pd.Series(dtype=float)

        breakdown_max = src_rev.max() if not src_rev.empty else 1.0

        # Build per-row breakdown HTML
        _CHANNEL_COLORS = ["#237FEA", "#08C7E1", "#a78bfa", "#FF7E54", "#f59e0b"]
        channel_rows = ""
        for ci, (ch, val) in enumerate(src_rev.items()):
            pct = val / breakdown_max * 100 if breakdown_max > 0 else 0
            col = _CHANNEL_COLORS[ci % len(_CHANNEL_COLORS)]
            if val >= 1_000_000:
                val_str = f"${val / 1_000_000:.2f}M"
            elif val >= 1_000:
                val_str = f"${val / 1_000:.1f}K"
            else:
                val_str = f"${val:,.0f}"
            channel_rows += f"""
            <div style="margin-bottom:12px;">
              <div style="display:flex;align-items:center;justify-content:space-between;
                          margin-bottom:4px;">
                <span style="font-size:0.82rem;color:{_C_WHITE};font-weight:600;">
                  <span style="display:inline-block;width:10px;height:10px;
                               border-radius:2px;background:{col};margin-right:6px;
                               vertical-align:middle;"></span>{ch}
                </span>
                <span style="font-size:0.82rem;color:{_C_MUTED};">{val_str}</span>
              </div>
              <div style="background:{_C_BORDER};border-radius:4px;height:5px;">
                <div style="background:{col};border-radius:4px;height:5px;
                            width:{pct:.1f}%;"></div>
              </div>
            </div>"""

        no_data_msg = (
            f"<div style='color:{_C_MUTED};font-size:0.82rem;margin-top:8px;'>"
            "No deal values recorded yet. Update deal values in the Pipeline tab."
            "</div>"
        ) if src_rev.empty else ""

        st.markdown(
            f"""
            <div style="background:{_C_CARD};border:1px solid {_C_BORDER};
                        border-radius:14px;padding:20px 22px;">
              <div style="display:flex;align-items:flex-start;justify-content:space-between;
                          margin-bottom:16px;">
                <div>
                  <div style="font-size:0.75rem;font-weight:600;color:{_C_MUTED};
                              text-transform:uppercase;letter-spacing:.06em;
                              margin-bottom:4px;">Revenue Breakdown</div>
                  <div style="font-size:2rem;font-weight:800;color:{_C_WHITE};
                              line-height:1;">{total_val_str}</div>
                  <div style="font-size:0.75rem;color:{_C_MUTED};margin-top:4px;">
                    Total pipeline value</div>
                </div>
              </div>
              {channel_rows}
              {no_data_msg}
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── RIGHT: Send Activity Heatmap ───────────────────────────
    with right_col:
        st.markdown(
            f"""<div style="font-size:1rem;font-weight:700;color:{_C_WHITE};
                            margin-bottom:4px;">Send Activity</div>
                <div style="font-size:0.8rem;color:{_C_MUTED};margin-bottom:12px;">
                  Sends by day &amp; hour</div>""",
            unsafe_allow_html=True,
        )

        days_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        # Business hours window shown in the heatmap (06:00 – 22:00)
        _HMAP_START_HOUR = 6
        _HMAP_END_HOUR   = 23
        hours_range = list(range(_HMAP_START_HOUR, _HMAP_END_HOUR))

        if (
            not sent_df.empty
            and "timestamp" in sent_df.columns
            and "status" in sent_df.columns
        ):
            sent_only = sent_df[sent_df["status"] == "sent"].copy()
            if not sent_only.empty:
                sent_only["dt"] = pd.to_datetime(
                    sent_only["timestamp"], errors="coerce", utc=True
                )
                sent_only.dropna(subset=["dt"], inplace=True)
                sent_only["day"]  = sent_only["dt"].dt.day_name().str[:3]
                sent_only["hour"] = sent_only["dt"].dt.hour
                heat = (
                    sent_only[
                        sent_only["day"].isin(days_order)
                        & sent_only["hour"].isin(hours_range)
                    ]
                    .groupby(["day", "hour"])
                    .size()
                    .unstack(fill_value=0)
                    .reindex(days_order, fill_value=0)
                    .reindex(columns=hours_range, fill_value=0)
                )
                z  = heat.values.tolist()
                xs = [f"{h:02d}:00" for h in hours_range]
                ys = days_order
            else:
                z  = [[0] * len(hours_range) for _ in range(len(days_order))]
                xs = [f"{h:02d}:00" for h in hours_range]
                ys = days_order
        else:
            z  = [[0] * len(hours_range) for _ in range(len(days_order))]
            xs = [f"{h:02d}:00" for h in hours_range]
            ys = days_order

        fig_heat = go.Figure(go.Heatmap(
            z=z, x=xs, y=ys,
            colorscale=[[0, _C_BORDER], [0.5, _C_BLUE], [1, _C_TEAL]],
            showscale=False,
            hovertemplate="<b>%{y} %{x}</b><br>Sends: %{z}<extra></extra>",
        ))
        fig_heat.update_layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=260,
            margin=dict(l=0, r=0, t=0, b=0),
            xaxis=dict(
                tickfont=dict(color=_C_MUTED, size=9),
                showgrid=False,
                tickangle=-45,
            ),
            yaxis=dict(
                tickfont=dict(color=_C_MUTED, size=10),
                showgrid=False,
                autorange="reversed",
            ),
            font=dict(color=_C_WHITE),
        )
        st.plotly_chart(fig_heat, use_container_width=True,
                        config={"displayModeBar": False})

        # -- Sends over time sparkline below heatmap --
        st.markdown(
            f"<div style='font-size:0.85rem;font-weight:700;color:{_C_WHITE};"
            f"margin:4px 0 8px;'>Sends over time</div>",
            unsafe_allow_html=True,
        )
        if not sent_df.empty and "timestamp" in sent_df.columns:
            sent_only2 = sent_df[sent_df["status"] == "sent"].copy() if "status" in sent_df.columns else sent_df.copy()
            if not sent_only2.empty:
                sent_only2["date"] = pd.to_datetime(
                    sent_only2["timestamp"], errors="coerce", utc=True
                ).dt.date
                daily = (
                    sent_only2.dropna(subset=["date"])
                    .groupby("date")
                    .size()
                    .reset_index(name="n")
                )
                daily["date"] = daily["date"].astype(str)
                fig_line = go.Figure(go.Scatter(
                    x=daily["date"], y=daily["n"],
                    mode="lines",
                    line=dict(color=_C_TEAL, width=2),
                    fill="tozeroy",
                    fillcolor=f"rgba(8,199,225,0.12)",
                    hovertemplate="%{x}: %{y} sends<extra></extra>",
                ))
                fig_line.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    height=110,
                    margin=dict(l=0, r=0, t=4, b=0),
                    xaxis=dict(visible=False),
                    yaxis=dict(visible=False, showgrid=False),
                )
                st.plotly_chart(fig_line, use_container_width=True,
                                config={"displayModeBar": False})
            else:
                st.markdown(
                    f"<div style='color:{_C_MUTED};font-size:0.82rem;'>No sends recorded yet.</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                f"<div style='color:{_C_MUTED};font-size:0.82rem;'>No send data available.</div>",
                unsafe_allow_html=True,
            )

    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

    # ── Deal Payment Tracker ────────────────────────────────────
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;justify-content:space-between;
                    margin-bottom:12px;">
          <div>
            <div style="font-size:1rem;font-weight:700;color:{_C_WHITE};">
              Deal Payment Tracker</div>
            <div style="font-size:0.8rem;color:{_C_MUTED};">Pipeline deals with payment status</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Map pipeline statuses → payment badge (bg colour, text colour, label)
    _DEAL_BADGE: dict[str, tuple[str, str, str]] = {
        "closed_won":  ("#08c96a22", "#22c55e", "Paid"),
        "interested":  ("#f59e0b22", "#f59e0b", "Pending"),
        "call_booked": ("#f59e0b22", "#f59e0b", "Pending"),
        "closed_lost": ("#ef444422", "#ef4444", "Reject"),
        "replied":     ("#237FEA22", "#237FEA", "Active"),
        "contacted":   ("#237FEA22", "#237FEA", "Active"),
        "new":         ("#8ba4c022", "#8ba4c0", "New"),
    }

    if not pip_df.empty:
        # Build rows from pipeline; most recent last_updated first
        deal_rows_df = pip_df.copy()
        if "last_updated" in deal_rows_df.columns:
            deal_rows_df["_ts"] = pd.to_datetime(
                deal_rows_df["last_updated"], errors="coerce", utc=True
            )
            deal_rows_df = deal_rows_df.sort_values("_ts", ascending=False)
        deal_rows_df = deal_rows_df.head(20).reset_index(drop=True)

        thead_cells = "".join(
            f"<th style='padding:10px 14px;text-align:left;font-size:0.75rem;"
            f"font-weight:600;text-transform:uppercase;letter-spacing:.05em;"
            f"color:{_C_MUTED};white-space:nowrap;'>{h}</th>"
            for h in ["Deal ID", "Name / Email", "Date", "Amount", "Status"]
        )

        rows_html = ""
        for idx, row in deal_rows_df.iterrows():
            deal_id  = f"DEAL-{int(idx) + 1:03d}"
            name_raw = str(row.get("name", "")).strip()
            email    = str(row.get("email", "")).strip()
            name_val = name_raw if name_raw else email if email else "—"
            deal_val = pd.to_numeric(row.get("deal_value", 0), errors="coerce") or 0.0
            if deal_val >= 1_000_000:
                amt_str = f"${deal_val / 1_000_000:.2f}M"
            elif deal_val >= 1_000:
                amt_str = f"${deal_val / 1_000:.1f}K"
            else:
                amt_str = f"${deal_val:,.0f}" if deal_val else "—"

            date_str = "—"
            ts_raw = str(row.get("last_updated", "")).strip()
            if ts_raw:
                try:
                    date_str = pd.to_datetime(ts_raw, utc=True).strftime("%d %b, %Y").lstrip("0")
                except Exception:
                    date_str = ts_raw[:10]

            status_key = str(row.get("status", "new")).strip()
            bg, fg, badge_label = _DEAL_BADGE.get(
                status_key, ("#8ba4c022", "#8ba4c0", status_key.replace("_", " ").title())
            )

            rows_html += f"""
            <tr style='border-bottom:1px solid {_C_BORDER};'>
              <td style='padding:10px 14px;font-size:0.8rem;color:{_C_MUTED};
                         white-space:nowrap;font-weight:600;'>{deal_id}</td>
              <td style='padding:10px 14px;font-size:0.82rem;color:{_C_WHITE};
                         max-width:200px;overflow:hidden;text-overflow:ellipsis;
                         white-space:nowrap;'>{name_val}</td>
              <td style='padding:10px 14px;font-size:0.82rem;color:{_C_MUTED};
                         white-space:nowrap;'>{date_str}</td>
              <td style='padding:10px 14px;font-size:0.82rem;font-weight:700;
                         color:{_C_WHITE};white-space:nowrap;'>{amt_str}</td>
              <td style='padding:10px 14px;'>
                <span style='background:{bg};color:{fg};border-radius:20px;
                             padding:3px 11px;font-size:0.73rem;font-weight:700;'>
                  {badge_label}</span>
              </td>
            </tr>"""

        table_html = f"""
        <div style="background:{_C_CARD};border:1px solid {_C_BORDER};border-radius:14px;
                    overflow:hidden;overflow-x:auto;">
          <table style="width:100%;border-collapse:collapse;">
            <thead style="background:#0a2240;">
              <tr>{thead_cells}</tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </div>
        """
        st.markdown(table_html, unsafe_allow_html=True)
    else:
        st.markdown(
            f"<div style='background:{_C_CARD};border:1px solid {_C_BORDER};"
            f"border-radius:14px;padding:24px;text-align:center;color:{_C_MUTED};"
            f"font-size:0.88rem;'>No deals in pipeline yet. Scrape leads and push them to "
            f"<strong>Live Leads</strong> to populate this tracker.</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)

    # ── Lead Source & Keyword charts ───────────────────────────
    if not leads_df.empty:
        chart_cols = st.columns(2)

        # Leads by keyword
        with chart_cols[0]:
            if "keyword" in leads_df.columns:
                st.markdown(
                    f"<div style='font-size:0.9rem;font-weight:700;color:{_C_WHITE};"
                    f"margin-bottom:8px;'>Leads by keyword</div>",
                    unsafe_allow_html=True,
                )
                kw_counts = (
                    leads_df["keyword"].replace("", pd.NA).dropna()
                    .value_counts().head(12)
                    .rename_axis("keyword").reset_index(name="count")
                )
                if not kw_counts.empty:
                    fig_kw = go.Figure(go.Bar(
                        x=kw_counts["count"],
                        y=kw_counts["keyword"],
                        orientation="h",
                        marker_color=_C_BLUE,
                        hovertemplate="%{y}: %{x} leads<extra></extra>",
                    ))
                    fig_kw.update_layout(
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(0,0,0,0)",
                        height=280,
                        margin=dict(l=0, r=10, t=0, b=0),
                        xaxis=dict(
                            showgrid=True,
                            gridcolor=_C_BORDER,
                            tickfont=dict(color=_C_MUTED, size=10),
                        ),
                        yaxis=dict(
                            tickfont=dict(color=_C_WHITE, size=10),
                            showgrid=False,
                            autorange="reversed",
                        ),
                        font=dict(color=_C_WHITE),
                    )
                    st.plotly_chart(fig_kw, use_container_width=True,
                                    config={"displayModeBar": False})

        # Leads by source
        with chart_cols[1]:
            if "source" in leads_df.columns:
                st.markdown(
                    f"<div style='font-size:0.9rem;font-weight:700;color:{_C_WHITE};"
                    f"margin-bottom:8px;'>Leads by source</div>",
                    unsafe_allow_html=True,
                )
                src_counts = (
                    leads_df["source"].replace("", pd.NA).dropna()
                    .value_counts()
                    .rename_axis("source").reset_index(name="count")
                )
                if not src_counts.empty:
                    fig_src = go.Figure(go.Bar(
                        x=src_counts["count"],
                        y=src_counts["source"],
                        orientation="h",
                        marker_color=_C_TEAL,
                        hovertemplate="%{y}: %{x} leads<extra></extra>",
                    ))
                    fig_src.update_layout(
                        paper_bgcolor="rgba(0,0,0,0)",
                        plot_bgcolor="rgba(0,0,0,0)",
                        height=280,
                        margin=dict(l=0, r=10, t=0, b=0),
                        xaxis=dict(
                            showgrid=True,
                            gridcolor=_C_BORDER,
                            tickfont=dict(color=_C_MUTED, size=10),
                        ),
                        yaxis=dict(
                            tickfont=dict(color=_C_WHITE, size=10),
                            showgrid=False,
                            autorange="reversed",
                        ),
                        font=dict(color=_C_WHITE),
                    )
                    st.plotly_chart(fig_src, use_container_width=True,
                                    config={"displayModeBar": False})



# ---------------------------------------------------------------------------
# Tab: Unsubscribe Manager
# ---------------------------------------------------------------------------

def tab_unsubscribe() -> None:
    st.header("🚫 Unsubscribe Manager")
    st.caption("Manage opt-out addresses. These are permanently skipped on every send.")
    st.divider()

    def _load() -> List[str]:
        return db.load_unsubscribe()

    def _save(addresses: List[str]) -> None:
        db.save_unsubscribe(addresses)

    addresses = _load()
    st.write(f"**{len(addresses)} unsubscribed address(es)** stored in database")

    # --- Add new address ---
    with st.form("add_unsub", clear_on_submit=True):
        new_addr = st.text_input("Add email address to unsubscribe list")
        if st.form_submit_button("➕ Add") and new_addr.strip():
            addr = new_addr.strip().lower()
            if addr not in addresses:
                addresses.append(addr)
                _save(addresses)
                st.success(f"Added `{addr}`")
                st.rerun()
            else:
                st.info(f"`{addr}` is already on the list.")

    # --- Bulk-add from CSV column ---
    with st.expander("Bulk-add from a CSV"):
        up = st.file_uploader("Upload CSV with an 'email' column", type="csv", key="unsub_upload")
        if up:
            bulk_df = pd.read_csv(up, dtype=str).fillna("")
            if "email" in bulk_df.columns:
                new_addrs = [e.strip().lower() for e in bulk_df["email"] if e.strip()]
                before = len(addresses)
                combined = list(set(addresses) | set(new_addrs))
                _save(combined)
                added = len(combined) - before
                st.success(f"Added {added} new address(es) from CSV.")
                st.rerun()
            else:
                st.error("The CSV must contain an 'email' column.")

    # --- Display and remove ---
    if addresses:
        st.subheader("Current list")
        remove_set: Set[str] = set()
        for addr in sorted(addresses):
            col_a, col_b = st.columns([5, 1])
            col_a.write(addr)
            if col_b.button("✕", key=f"rm_{addr}"):
                remove_set.add(addr)

        if remove_set:
            remaining = [a for a in addresses if a not in remove_set]
            _save(remaining)
            st.rerun()

        st.divider()
        if st.button("🗑 Clear entire list", type="secondary"):
            _save([])
            st.rerun()
    else:
        st.info("No unsubscribed addresses yet.")


# ---------------------------------------------------------------------------
# Tab: Scrape
# ---------------------------------------------------------------------------

def tab_scrape() -> None:
    st.markdown(
        """
        <div style="
            background: linear-gradient(135deg,#237FEA 0%,#08C7E1 100%);
            border-radius:14px;padding:22px 28px;margin-bottom:20px;color:#fff;
        ">
            <div style="font-size:1.5rem;font-weight:800;letter-spacing:-.03em;">🔍 Scrape Leads</div>
            <div style="font-size:.9rem;opacity:.85;margin-top:4px;">
                Search Google Maps or Bing · review the results · push good leads to your permanent Live Leads database.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Step 1: Keywords ─────────────────────────────────────────────────────
    st.subheader("Step 1 · Keywords")

    # Keyword library: clicking a button injects the keyword into the textarea
    with st.expander("📚 Keyword Library — click any keyword to add it to your list"):
        for cat, kws in KEYWORD_LIBRARY.items():
            st.markdown(f"**{cat}**")
            cols = st.columns(4)
            for i, kw in enumerate(kws):
                if cols[i % 4].button(kw, key=f"kwlib_{cat}_{i}", use_container_width=True):
                    existing = st.session_state.get("kw_textarea", "")
                    lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
                    if kw not in lines:
                        lines.append(kw)
                    st.session_state["kw_textarea"] = "\n".join(lines)
                    st.rerun()

    kw_col, opt_col = st.columns([3, 1])
    with kw_col:
        keywords_raw = st.text_area(
            "Search keywords — one per line",
            key="kw_textarea",
            placeholder="digital agency London\nweb design Birmingham\nmarketing agency UK",
            height=130,
            help="Each keyword is searched separately. Results from all keywords are combined.",
        )
    with opt_col:
        num_results = st.number_input(
            "Results per keyword", min_value=1, max_value=200, value=10, step=5
        )
        engine = st.radio("Search engine", ["Google Maps", "Bing"], horizontal=False)

    keywords = [k.strip() for k in keywords_raw.splitlines() if k.strip()]
    if keywords:
        badges = "  ·  ".join(f"`{k}`" for k in keywords)
        st.caption(f"📌 **{len(keywords)} keyword(s) queued:** {badges}")

    # ── Step 2: Qualification filter ─────────────────────────────────────────
    st.subheader("Step 2 · Qualification Filter")
    qual_filter = st.selectbox(
        "Keep which leads?",
        [
            "All leads",
            "🎯 Outreach targets only (businesses with website issues)",
            "✅ Healthy sites only (no detected issues)",
        ],
        help=(
            "**Outreach targets**: businesses whose websites have problems "
            "(no SSL, missing contact page, missing meta description) — "
            "ideal cold-outreach candidates.\n\n"
            "Tip: the detected issues are saved in the `issues` column and you can "
            "reference them in your email template with `{issues}`."
        ),
    )
    st.caption(
        "💡 Use **`{issues}`** in your email template to mention the specific "
        "problems you spotted — makes outreach much more personal."
    )

    # ── Step 3: Run ──────────────────────────────────────────────────────────
    st.subheader("Step 3 · Run")
    run_btn = st.button(
        "▶ Run Scraper",
        type="primary",
        disabled=not keywords,
        help="Scrape all keywords and collect leads into a preview table.",
    )

    log_placeholder = st.empty()

    if run_btn and keywords:
        # Clear any previously staged results so the UI is fresh
        st.session_state.pop("staged_records", None)
        st.session_state.pop("staged_engine", None)

        log_lines: List[str] = []
        log_q: queue.Queue = queue.Queue()

        q_handler = _QueueHandler(log_q)
        q_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(q_handler)

        records: List[Dict] = []
        error_holder: List[str] = []

        def _run() -> None:
            try:
                for kw in keywords:
                    logger.info("=== Scraping keyword: %s ===", kw)
                    if engine == "Bing":
                        from bing_email_scraper import scrape
                        records.extend(scrape(keyword=kw, num_results=int(num_results)))
                    else:
                        from google_maps_scraper import scrape as maps_scrape
                        records.extend(maps_scrape(keyword=kw, num_results=int(num_results)))

                # Apply qualification filter
                if "Outreach targets" in qual_filter:
                    filtered = [r for r in records if r.get("issues")]
                elif "Healthy sites" in qual_filter:
                    filtered = [r for r in records if not r.get("issues")]
                else:
                    filtered = records[:]

                records.clear()
                records.extend(filtered)

                # Save to staging CSV
                if records:
                    staging_df = pd.DataFrame(records)
                    _write_csv(LEADS_CSV, staging_df)
                    logger.info("Saved %d row(s) to staging CSV: %s", len(records), LEADS_CSV)

            except ImportError as exc:
                error_holder.append(
                    f"Missing dependency: {exc}. Run `pip install -r requirements.txt`."
                )
            except OSError as exc:
                error_holder.append(f"File error: {exc}")
            except Exception as exc:  # noqa: BLE001
                error_holder.append(f"Scraper error ({type(exc).__name__}): {exc}")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        progress = st.progress(0, text="Scraping…")
        tick = 0
        while thread.is_alive():
            while not log_q.empty():
                log_lines.append(log_q.get_nowait())
            log_placeholder.text_area(
                "Live log", "\n".join(log_lines[-60:]), height=200, key=f"log_{tick}"
            )
            tick += 1
            progress.progress(min(tick % 100, 99), text="Scraping…")
            time.sleep(0.5)

        while not log_q.empty():
            log_lines.append(log_q.get_nowait())
        root_logger.removeHandler(q_handler)
        progress.progress(100, text="Done")
        log_placeholder.text_area(
            "Live log", "\n".join(log_lines[-60:]), height=200, key="log_final"
        )

        if error_holder:
            st.error(f"Scraper error: {error_holder[0]}")
        elif records:
            st.session_state["staged_records"] = records
            st.session_state["staged_engine"] = engine
            st.success(
                f"✅ Scraped **{len(records)} row(s)** across **{len(keywords)} keyword(s)**. "
                "Review below, then push to Live Leads when ready."
            )
        elif engine == "Google Maps" and any(
            "Google Maps blocked our access" in line for line in log_lines
        ):
            st.error(
                "Google Maps is blocking automated access from this machine/network.\n\n"
                "- Wait a bit and retry, or try a different network/IP\n"
                "- Reduce `Results per keyword`\n"
                "- Or switch the engine to **Bing** (more reliable for scraping)\n\n"
                "For production reliability, consider the official Google Places API."
            )
        else:
            st.warning(
                "No records returned after applying the filter. "
                "Try a different keyword, engine, or filter setting."
            )

    # ── Step 4: Review & Push ────────────────────────────────────────────────
    staged: List[Dict] = st.session_state.get("staged_records", [])
    if staged:
        st.divider()
        st.subheader("Step 4 · Review Staged Leads")

        df_staged = pd.DataFrame(staged)

        # Summary metrics
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total rows", f"{len(df_staged):,}")

        email_count = (
            int(df_staged["email"].str.strip().replace("", pd.NA).dropna().count())
            if "email" in df_staged.columns else 0
        )
        m2.metric("With email", f"{email_count:,}")

        if "issues" in df_staged.columns:
            issues_series = df_staged["issues"].str.strip()
            has_issues = int((issues_series != "").sum())
        else:
            has_issues = 0
        m3.metric("With site issues", f"{has_issues:,}")

        kw_count = df_staged["keyword"].nunique() if "keyword" in df_staged.columns else 0
        m4.metric("Keywords", f"{kw_count:,}")

        st.dataframe(df_staged, use_container_width=True)

        dl_col, push_col, _ = st.columns([2, 2, 3])

        with dl_col:
            csv_bytes = df_staged.to_csv(index=False).encode()
            st.download_button(
                "⬇ Download staged CSV",
                data=csv_bytes,
                file_name="staged_leads.csv",
                mime="text/csv",
            )

        with push_col:
            push_btn = st.button(
                "🚀 Push to Live Leads",
                type="primary",
                help=(
                    f"Append these leads to `{LIVE_LEADS_CSV}` (the permanent store). "
                    "Exact duplicates are skipped automatically."
                ),
            )

        if push_btn:
            new_count, dup_count = _push_to_live_leads(staged)
            pip_new = _upsert_pipeline_entries(staged)
            st.session_state.pop("staged_records", None)
            st.session_state.pop("staged_engine", None)
            if new_count:
                st.success(
                    f"🎉 **{new_count} new lead(s)** added to `{LIVE_LEADS_CSV}`. "
                    f"{dup_count} duplicate(s) skipped. "
                    f"{pip_new} new pipeline entr{'y' if pip_new == 1 else 'ies'} created."
                )
            else:
                st.info(
                    f"All {dup_count} row(s) were already in `{LIVE_LEADS_CSV}` — nothing new added."
                )
            st.rerun()


# ---------------------------------------------------------------------------
# Tab: Leads
# ---------------------------------------------------------------------------

def tab_leads() -> None:
    st.header("📋 Leads")
    st.caption(
        f"Browse, filter, and download your leads. "
        f"The **Live Leads** tab (`{LIVE_LEADS_CSV}`) is your permanent database — "
        f"`{LEADS_CSV}` holds the most recent staging scrape."
    )
    st.divider()

    # Source selector
    source = st.radio(
        "Data source",
        [f"📦 Live Leads ({LIVE_LEADS_CSV})", f"🔬 Staging ({LEADS_CSV})", "⬆ Upload CSV"],
        horizontal=True,
    )

    if source.startswith("⬆"):
        uploaded = st.file_uploader("Upload a leads CSV", type="csv")
        if uploaded:
            df = pd.read_csv(uploaded, dtype=str).fillna("")
            st.session_state["leads_df"] = df
            st.success(f"Loaded {len(df)} rows from uploaded file.", icon="✅")
        df = st.session_state.get("leads_df", pd.DataFrame())
    elif source.startswith("🔬"):
        df = _read_csv(LEADS_CSV)
        if not df.empty:
            st.session_state["leads_df"] = df
    else:
        df = _read_csv(LIVE_LEADS_CSV)
        if not df.empty:
            st.session_state["leads_df"] = df

    if df.empty:
        if source.startswith("📦"):
            st.info(
                f"No live leads yet. Run **Scrape** and click **Push to Live Leads** "
                f"to populate `{LIVE_LEADS_CSV}`.",
                icon="📂",
            )
        else:
            st.info("No data found for the selected source.", icon="📂")
        return

    st.write(f"**{len(df)} total rows**")

    # Filters
    with st.expander("Filters", expanded=True):
        fcol1, fcol2, fcol3, fcol4, fcol5 = st.columns(5)
        with fcol1:
            has_email = st.checkbox("Only rows with email", value=False)
            only_whatsapp = st.checkbox("📱 Only WhatsApp leads", value=False)
        with fcol2:
            min_score: int = 0
            if "lead_score" in df.columns:
                scores = pd.to_numeric(df["lead_score"], errors="coerce").dropna()
                if not scores.empty:
                    min_score = st.slider(
                        "Minimum lead score",
                        int(scores.min()),
                        int(scores.max()),
                        int(scores.min()),
                    )
        with fcol3:
            category_col = "category" if "category" in df.columns else None
            category_filter = ""
            if category_col:
                cats = ["(all)"] + sorted(df[category_col].dropna().unique().tolist())
                category_filter = st.selectbox("Category", cats)
        with fcol4:
            tier_filter = st.selectbox(
                "Lead tier",
                ["All", "🔥 Hot (score ≥ 7)", "🟡 Warm (4–6)", "🧊 Cold (< 4)"],
                help=(
                    "🔥 Hot: score ≥ 7 — high urgency / many issues / contact info found.\n\n"
                    "🟡 Warm: score 4–6 — decent signals but not urgent.\n\n"
                    "🧊 Cold: score < 4 — limited contact info or few opportunity signals."
                ),
            )
        with fcol5:
            wa_sort_first = st.checkbox("🔥 Sort WhatsApp first", value=False,
                                        help="Move all leads with a WhatsApp number to the top.")

    filtered = df.copy()
    if has_email and "email" in filtered.columns:
        filtered = filtered[filtered["email"].str.strip() != ""]
    if only_whatsapp and "whatsapp_number" in filtered.columns:
        filtered = filtered[filtered["whatsapp_number"].str.strip() != ""]
    if "lead_score" in filtered.columns and min_score:
        filtered = filtered[pd.to_numeric(filtered["lead_score"], errors="coerce").fillna(0) >= min_score]
    if category_col and category_filter and category_filter != "(all)":
        filtered = filtered[filtered[category_col] == category_filter]
    if "lead_score" in filtered.columns and tier_filter != "All":
        _numeric_score = pd.to_numeric(filtered["lead_score"], errors="coerce").fillna(0)
        if "Hot" in tier_filter:
            filtered = filtered[_numeric_score >= 7]
        elif "Warm" in tier_filter:
            filtered = filtered[(_numeric_score >= 4) & (_numeric_score < 7)]
        elif "Cold" in tier_filter:
            filtered = filtered[_numeric_score < 4]

    # Sort: WhatsApp-first (optional) then by lead score descending.
    if not filtered.empty and "lead_score" in filtered.columns:
        filtered = filtered.copy()
        filtered["_sort_score"] = pd.to_numeric(filtered["lead_score"], errors="coerce").fillna(0)
        if wa_sort_first and "whatsapp_number" in filtered.columns:
            filtered["_has_wa"] = (filtered["whatsapp_number"].str.strip() != "").astype(int)
            filtered = filtered.sort_values(
                ["_has_wa", "_sort_score"], ascending=[False, False]
            ).drop(columns=["_has_wa", "_sort_score"])
        else:
            filtered = filtered.sort_values("_sort_score", ascending=False).drop(columns=["_sort_score"])

    # Tier + WhatsApp badge summary
    tier_counts: dict = {}
    wa_count = 0
    if "lead_score" in df.columns:
        all_scores = pd.to_numeric(df["lead_score"], errors="coerce").fillna(0)
        tier_counts = {
            "🔥 Hot": int((all_scores >= 7).sum()),
            "🟡 Warm": int(((all_scores >= 4) & (all_scores < 7)).sum()),
            "🧊 Cold": int((all_scores < 4).sum()),
        }
    if "whatsapp_number" in df.columns:
        wa_count = int((df["whatsapp_number"].str.strip() != "").sum())
    if tier_counts:
        badges = "  ·  ".join(f"{t} **{c:,}**" for t, c in tier_counts.items())
        if wa_count:
            badges += f"  ·  📱 WhatsApp **{wa_count:,}**"
        st.caption(f"Tier breakdown: {badges}")

    st.write(f"**{len(filtered)} rows after filters**")

    # Ensure notes column exists for editing
    if "notes" not in filtered.columns:
        filtered = filtered.copy()
        filtered["notes"] = ""

    # Build click-to-chat WhatsApp URL column if numbers are present
    if "whatsapp_number" in filtered.columns:
        filtered = filtered.copy()
        filtered["whatsapp_chat"] = filtered["whatsapp_number"].apply(
            lambda n: f"https://wa.me/{n}" if str(n).strip() else ""
        )

    # Column config for richer display
    col_cfg: dict = {
        "notes": st.column_config.TextColumn("📝 Notes", width="medium"),
        "lead_score": st.column_config.NumberColumn("⭐ Score"),
        "issues": st.column_config.TextColumn("⚠️ Issues", width="large"),
        "whatsapp_number": st.column_config.TextColumn("📱 WhatsApp"),
    }
    if "url" in filtered.columns:
        col_cfg["url"] = st.column_config.LinkColumn("🔗 URL", display_text="Visit")
    if "website" in filtered.columns:
        col_cfg["website"] = st.column_config.LinkColumn("🌐 Website", display_text="Visit")
    if "whatsapp_chat" in filtered.columns:
        col_cfg["whatsapp_chat"] = st.column_config.LinkColumn(
            "💬 Chat on WhatsApp", display_text="Open chat"
        )

    edited_df = st.data_editor(
        filtered,
        use_container_width=True,
        num_rows="fixed",
        column_config=col_cfg,
        disabled=[c for c in filtered.columns if c != "notes"],
        key="leads_editor",
    )

    btn_col1, btn_col2 = st.columns([2, 5])

    with btn_col1:
        csv_bytes = edited_df.to_csv(index=False).encode()
        st.download_button(
            "⬇ Download filtered CSV",
            data=csv_bytes,
            file_name="filtered_leads.csv",
            mime="text/csv",
        )

    if source.startswith("📦") and "notes" in edited_df.columns:
        with btn_col2:
            if st.button("💾 Save notes to Live Leads", type="secondary"):
                live_df = _read_csv(LIVE_LEADS_CSV)
                if "notes" not in live_df.columns:
                    live_df["notes"] = ""
                if "email" in edited_df.columns and "email" in live_df.columns:
                    notes_map = dict(zip(
                        edited_df["email"].str.lower().str.strip(),
                        edited_df["notes"],
                    ))
                    live_df["notes"] = live_df.apply(
                        lambda r: notes_map.get(r["email"].lower().strip(), r["notes"]),
                        axis=1,
                    )
                    _write_csv(LIVE_LEADS_CSV, live_df)
                    st.success("✅ Notes saved to Live Leads.")
                    st.rerun()

    # --- Charts ---
    if not filtered.empty:
        st.divider()
        chart_col1, chart_col2 = st.columns(2)

        with chart_col1:
            if "lead_score" in filtered.columns:
                scores = pd.to_numeric(filtered["lead_score"], errors="coerce").dropna()
                if not scores.empty:
                    st.subheader("Score distribution")
                    score_counts = (
                        scores.astype(int)
                        .value_counts()
                        .sort_index()
                        .rename_axis("score")
                        .reset_index(name="count")
                    )
                    st.bar_chart(score_counts.set_index("score")["count"])

        with chart_col2:
            if "category" in filtered.columns:
                cat_counts = (
                    filtered["category"]
                    .replace("", pd.NA)
                    .dropna()
                    .value_counts()
                    .head(10)
                    .rename_axis("category")
                    .reset_index(name="count")
                )
                if not cat_counts.empty:
                    st.subheader("Top categories")
                    st.bar_chart(cat_counts.set_index("category")["count"])


# ---------------------------------------------------------------------------
# Tab: Compose & Send
# ---------------------------------------------------------------------------

def _pre_substitute(template: str, static_values: dict[str, str]) -> str:
    """
    Replace a fixed set of placeholder keys in *template* with *static_values*.

    This implements a two-stage substitution strategy:

    1. **Static stage** (this function) – replaces operator-level copy that is
       the same for every recipient in a send batch: ``{offer}``, ``{cta}``,
       ``{proof}``, and ``{booking_url}``.  These are chosen once in the UI,
       not per row.

    2. **Dynamic stage** (``bulk_emailer._render_template``) – replaces
       per-row placeholders such as ``{name}``, ``{url}``, ``{issues}``,
       ``{niche}``, and ``{city}`` at send time using values from the leads CSV.

    Only keys present in *static_values* are replaced — per-row placeholders
    are left verbatim (e.g. ``{name}`` stays as-is) so that
    :func:`bulk_emailer._render_template` can fill them at send time.
    """
    import string
    formatter = string.Formatter()
    parts: list[str] = []
    for literal_text, field_name, format_spec, conversion in formatter.parse(template):
        parts.append(literal_text)
        if field_name is not None:
            if field_name in static_values:
                parts.append(static_values[field_name])
            else:
                # Put the placeholder back verbatim for bulk_emailer to fill.
                parts.append("{" + field_name + "}")
    return "".join(parts)


def tab_send() -> None:
    st.header("✉️ Compose & Send")
    st.caption("Personalise and send outreach emails to your leads via SMTP.")
    st.divider()

    with st.expander("🔐 SMTP Settings", expanded=True):
        sc1, sc2 = st.columns(2)
        with sc1:
            smtp_host = st.text_input("SMTP Host", value=os.environ.get("SMTP_HOST", "smtp-relay.brevo.com"))
            smtp_user = st.text_input("SMTP Username", value=os.environ.get("SMTP_USER", os.environ.get("MAIL_USERNAME", "")))
            from_email = st.text_input("From Email", value=os.environ.get("MAIL_FROM_ADDRESS", os.environ.get("EMAIL_ADDRESS", "")))
            reply_to = st.text_input("Reply-To Email", value=os.environ.get("MAIL_REPLY_TO", ""))
        with sc2:
            smtp_port = st.number_input("SMTP Port", value=int(os.environ.get("SMTP_PORT", "587")), step=1)
            password = st.text_input("Password / App Password", type="password",
                                     value=os.environ.get("SMTP_PASS", os.environ.get("MAIL_PASSWORD", os.environ.get("EMAIL_PASSWORD", ""))))
        use_ssl = st.checkbox("Use SSL (port 465)", value=False)
        from_name = st.text_input("Sender Display Name", value=os.environ.get("MAIL_FROM_NAME", ""))
        st.caption(
            "💡 **Gmail users:** Enable 2-Step Verification and create an "
            "[App Password](https://myaccount.google.com/apppasswords). "
            "Your regular password will not work."
        )

    with st.expander("🎯 Offer Engine — *what you're giving them*", expanded=True):
        st.caption(
            "Choose a concrete offer. People reply to **specific outcomes**, not generic services. "
            "This fills the `{offer}` placeholder in your template."
        )
        offer_label = st.selectbox(
            "Select offer",
            list(OFFER_LIBRARY.keys()),
            help="Pick the offer that best matches your outreach goal.",
        )
        default_offer_text = OFFER_LIBRARY[offer_label]
        offer_text = st.text_area(
            "Offer text (editable)",
            value=default_offer_text,
            height=80,
            help="This replaces `{offer}` in your email template.",
        )

    with st.expander("💬 CTA — *what you want them to do*", expanded=True):
        st.caption(
            "A clear, specific ask gets far more replies than 'let me know if you're interested'. "
            "This fills the `{cta}` placeholder."
        )
        cta_label = st.selectbox(
            "Select CTA",
            list(CTA_OPTIONS.keys()),
        )
        default_cta_text = CTA_OPTIONS[cta_label]
        cta_text = st.text_input(
            "CTA text (editable)",
            value=default_cta_text,
            help="This replaces `{cta}` in your email template.",
        )

    with st.expander("✅ Social Proof — *one-liner credibility*", expanded=True):
        st.caption(
            "One sentence of proof closes the credibility gap without a full case study. "
            "Supports `{niche}` and `{city}` placeholders (filled per-row at send time). "
            "Fills the `{proof}` placeholder."
        )
        proof_choice = st.selectbox(
            "Proof example",
            PROOF_EXAMPLES,
        )
        proof_text = st.text_input(
            "Proof line (editable)",
            value="" if proof_choice == "(Custom — write your own)" else proof_choice,
            help="Replaces `{proof}` in your email template.",
        )

    with st.expander("🔗 Booking Link — *deal-closing layer*", expanded=False):
        st.caption(
            "Add a calendar / booking link so a warm prospect can book directly. "
            "Leave blank to omit. Fills the `{booking_url}` placeholder."
        )
        booking_url = st.text_input(
            "Booking / calendar URL",
            placeholder="https://calendly.com/yourname/15min",
            help="Replaces `{booking_url}` in your template. Leave blank to remove the line.",
        )
        booking_line = f"👉 Book a quick call: {booking_url}" if booking_url.strip() else ""

    with st.expander("📝 Email Content", expanded=True):
        subject = st.text_input("Subject line", value="Quick question for {name}",
                                help="Supports placeholders: {name}, {url}, {phone}, or any CSV column name.")
        template_body = st.text_area("Email body template", value=_default_template(), height=220,
                                     help="Use {name}, {url}, {issues}, {offer}, {cta}, {proof}, {booking_url} etc.")
        is_html = st.checkbox("HTML email", value=False)

        # Show a live preview of the static substitutions
        static_values = {
            "offer": offer_text,
            "cta": cta_text,
            "proof": proof_text,
            "booking_url": booking_line,
        }
        preview_body = _pre_substitute(template_body, static_values)
        with st.expander("👁 Template preview (static placeholders resolved)"):
            st.code(preview_body, language="text")

    with st.expander("⚙️ Sending Options", expanded=False):
        leads_csv = st.text_input("Leads CSV path", value=LIVE_LEADS_CSV)
        sent_log = st.text_input("Sent log CSV path", value=SENT_LOG_CSV)
        unsub_file = st.text_input("Unsubscribe list path", value=UNSUBSCRIBE_TXT)
        delay = st.slider("Delay between sends (seconds)", 0.5, 10.0, 2.0, 0.5)

    dry_run = st.checkbox("Dry run (preview only – no emails sent)", value=True)

    send_btn = st.button(
        "▶ Send Emails" if not dry_run else "👁 Preview Recipients",
        type="primary",
        disabled=not (smtp_host and from_email and (dry_run or password)),
    )

    if send_btn:
        import argparse
        import importlib
        be = importlib.import_module("bulk_emailer")

        # Pre-substitute static values (offer, cta, proof, booking_url) into
        # the template.  Per-row placeholders ({name}, {url}, …) remain for
        # bulk_emailer to fill at send time.
        resolved_template = _pre_substitute(template_body, static_values)

        # Build an args namespace matching bulk_emailer.cmd_send expectations
        ns = argparse.Namespace(
            csv=leads_csv,
            template=None,
            subject=subject,
            from_name=from_name,
            smtp_host=smtp_host,
            smtp_user=smtp_user,
            smtp_port=int(smtp_port),
            ssl=use_ssl,
            email=from_email,
            reply_to=reply_to,
            password=password,
            log=sent_log,
            unsubscribe=unsub_file,
            delay=float(delay),
            html=is_html,
            dry_run=dry_run,
        )

        # Write resolved template to a cross-platform temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="outreach_template_", delete=False, encoding="utf-8"
        ) as tmp_f:
            tmp_f.write(resolved_template)
            tmp_template_path = tmp_f.name
        ns.template = tmp_template_path

        # Export leads from SQLite to a temp CSV that bulk_emailer can read
        _table_name = db.CSV_TO_TABLE.get(Path(leads_csv).name, "live_leads")
        _leads_export = db.read_table(_table_name)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", prefix="outreach_leads_", delete=False, encoding="utf-8"
        ) as _tmp_leads:
            _leads_export.to_csv(_tmp_leads.name, index=False)
            ns.csv = _tmp_leads.name

        # Export unsubscribe list to a temp file for bulk_emailer
        _unsub_emails = db.load_unsubscribe()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="outreach_unsub_", delete=False, encoding="utf-8"
        ) as _tmp_unsub:
            _tmp_unsub.write("\n".join(_unsub_emails))
            ns.unsubscribe = _tmp_unsub.name

        log_lines: List[str] = []
        log_q: queue.Queue = queue.Queue()
        q_handler = _QueueHandler(log_q)
        q_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(q_handler)

        error_holder: List[str] = []

        def _run_send() -> None:
            try:
                be.cmd_send(ns)
            except SystemExit as exc:
                if str(exc) != "0":
                    error_holder.append(f"Send failed (exit {exc}). Check your credentials and CSV path.")
            except smtplib.SMTPAuthenticationError:
                error_holder.append(
                    "SMTP authentication failed. For Gmail, use an App Password "
                    "(myaccount.google.com/apppasswords)."
                )
            except (smtplib.SMTPConnectError, OSError) as exc:
                error_holder.append(f"Cannot connect to SMTP server {smtp_host}:{smtp_port} – {exc}")
            except FileNotFoundError as exc:
                error_holder.append(f"File not found: {exc}")
            except Exception as exc:  # noqa: BLE001
                error_holder.append(f"Unexpected error ({type(exc).__name__}): {exc}")

        thread = threading.Thread(target=_run_send, daemon=True)
        thread.start()

        log_box = st.empty()
        tick = 0
        while thread.is_alive():
            while not log_q.empty():
                log_lines.append(log_q.get_nowait())
            log_box.text_area("Log", "\n".join(log_lines[-60:]), height=220, key=f"send_log_{tick}")
            tick += 1
            time.sleep(0.4)

        while not log_q.empty():
            log_lines.append(log_q.get_nowait())
        root_logger.removeHandler(q_handler)

        log_box.text_area("Log", "\n".join(log_lines[-60:]), height=220, key="send_log_final")

        if error_holder:
            st.error(error_holder[0])
        else:
            label = "Dry-run complete." if dry_run else "Send complete."
            st.success(f"✅ {label}")


# ---------------------------------------------------------------------------
# Tab: Sent Log
# ---------------------------------------------------------------------------

def tab_sent_log() -> None:
    st.header("📑 Sent Log")
    st.caption("Track every email send attempt – successes, failures, and trends.")

    col_refresh, _ = st.columns([1, 5])
    with col_refresh:
        if st.button("🔄 Refresh"):
            pass  # simply re-render

    df = _read_csv(SENT_LOG_CSV)
    if df.empty:
        st.info(f"No sent log found at `{SENT_LOG_CSV}` yet. Send some emails first.", icon="📭")
        return

    # --- Summary metrics ---
    sent_n = int((df["status"] == "sent").sum()) if "status" in df.columns else 0
    failed_n = int((df["status"] == "failed").sum()) if "status" in df.columns else 0
    total_n = len(df)
    rate = f"{sent_n / total_n * 100:.1f}%" if total_n else "—"

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total entries", f"{total_n:,}")
    m2.metric("Sent", f"{sent_n:,}")
    m3.metric("Failed", f"{failed_n:,}")
    m4.metric("Success rate", rate)

    st.divider()

    # --- Sends over time chart ---
    if "timestamp" in df.columns and "status" in df.columns:
        sent_only = df[df["status"] == "sent"].copy()
        if not sent_only.empty:
            sent_only["date"] = pd.to_datetime(sent_only["timestamp"], errors="coerce", utc=True).dt.date
            daily = sent_only.groupby("date").size().reset_index(name="emails_sent")
            daily["date"] = daily["date"].astype(str)
            st.subheader("Sends per day")
            st.line_chart(daily.set_index("date")["emails_sent"])
            st.divider()

    st.write(f"**{total_n} total entries**")

    status_filter = st.selectbox("Filter by status", ["(all)", "✅ sent", "❌ failed"])
    filter_val = status_filter.split()[-1] if status_filter != "(all)" else None
    display_df = df.copy()
    if filter_val and "status" in display_df.columns:
        display_df = display_df[display_df["status"] == filter_val]

    st.dataframe(display_df, use_container_width=True)

    csv_bytes = display_df.to_csv(index=False).encode()
    st.download_button("⬇ Download log", data=csv_bytes, file_name="sent_log.csv", mime="text/csv")


# ---------------------------------------------------------------------------
# Tab: Replies
# ---------------------------------------------------------------------------

def tab_replies() -> None:
    st.header("💬 Replies")
    st.caption("Connect to your inbox and surface replies from known leads.")
    st.divider()

    with st.expander("🔐 IMAP Settings", expanded=True):
        rc1, rc2 = st.columns(2)
        with rc1:
            imap_host = st.text_input("IMAP Host", value=os.environ.get("IMAP_HOST", "imap.gmail.com"))
            imap_email = st.text_input("Email Address", value=os.environ.get("EMAIL_ADDRESS", ""),
                                       key="replies_email")
        with rc2:
            imap_port = st.number_input("IMAP Port", value=993, step=1)
            imap_pass = st.text_input("Password / App Password", type="password",
                                      value=os.environ.get("EMAIL_PASSWORD", ""), key="replies_pass")
        folder = st.text_input("Folder", value="INBOX")
        since_days = st.number_input("Look back (days)", min_value=1, max_value=365, value=30)

    sent_log_path = st.text_input("Sent log CSV (to match replies)", value=SENT_LOG_CSV)

    check_btn = st.button(
        "🔍 Check for Replies",
        type="primary",
        disabled=not (imap_host and imap_email and imap_pass),
    )

    if check_btn:
        import argparse
        import importlib
        be = importlib.import_module("bulk_emailer")

        ns = argparse.Namespace(
            imap_host=imap_host,
            imap_port=int(imap_port),
            email=imap_email,
            password=imap_pass,
            log=sent_log_path,
            folder=folder,
            since=int(since_days),
        )

        lead_replies: List[Dict] = []
        other_msgs: List[Dict] = []
        error_holder: List[str] = []

        import email as email_lib
        from email.utils import parseaddr

        def _fetch_replies() -> None:
            try:
                since_date = (
                    datetime.now(timezone.utc) - timedelta(days=int(since_days))
                ).strftime("%d-%b-%Y")

                sent_emails: Set[str] = set()
                # Read sent emails from SQLite (primary) with CSV fallback
                _sent_df = db.read_table("sent_log")
                if not _sent_df.empty and "status" in _sent_df.columns:
                    sent_emails = set(
                        _sent_df.loc[_sent_df["status"] == "sent", "to_email"]
                        .str.lower().str.strip()
                    )
                elif Path(sent_log_path).exists():
                    with open(sent_log_path, newline="", encoding="utf-8") as fh:
                        for row in csv.DictReader(fh):
                            if row.get("status") == "sent":
                                sent_emails.add(row["to_email"].lower().strip())

                with imaplib.IMAP4_SSL(imap_host, int(imap_port)) as imap:
                    imap.login(imap_email, imap_pass)
                    imap.select(folder, readonly=True)
                    _, data = imap.search(None, f'SINCE "{since_date}"')
                    msg_ids = data[0].split()

                    for msg_id in msg_ids:
                        _, msg_data = imap.fetch(msg_id, "(RFC822)")
                        raw = msg_data[0][1]
                        if not isinstance(raw, bytes):
                            continue
                        parsed = email_lib.message_from_bytes(raw)
                        fname, faddr = parseaddr(parsed.get("From", ""))
                        faddr = faddr.lower().strip()
                        entry = {
                            "from": faddr,
                            "name": fname or faddr,
                            "subject": parsed.get("Subject", "(no subject)"),
                            "date": parsed.get("Date", ""),
                        }
                        if faddr in sent_emails:
                            lead_replies.append(entry)
                        else:
                            other_msgs.append(entry)
            except imaplib.IMAP4.error as exc:
                msg = str(exc).lower()
                if "authenticate" in msg or "login" in msg or "auth" in msg:
                    error_holder.append(
                        "IMAP authentication failed. For Gmail, use an App Password "
                        "(myaccount.google.com/apppasswords)."
                    )
                elif "select" in msg or "doesn't exist" in msg:
                    error_holder.append(f"Folder '{folder}' not found on the server.")
                else:
                    error_holder.append(f"IMAP error: {exc}")
            except (OSError, TimeoutError) as exc:
                error_holder.append(f"Cannot connect to IMAP server {imap_host}:{imap_port} – {exc}")
            except Exception as exc:  # noqa: BLE001
                error_holder.append(f"Unexpected error ({type(exc).__name__}): {exc}")

        with st.spinner("Connecting to IMAP…"):
            t = threading.Thread(target=_fetch_replies, daemon=True)
            t.start()
            t.join(timeout=60)

        if error_holder:
            st.error(f"IMAP error: {error_holder[0]}")
            return

        # --- Persist lead replies to replies_log.csv and update pipeline ---
        if lead_replies:
            replies_log_path = Path(REPLIES_LOG_CSV)
            now_ts = datetime.now(timezone.utc).isoformat()
            log_exists = replies_log_path.exists() and replies_log_path.stat().st_size > 0
            with replies_log_path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=["logged_at", "from_email", "name", "subject", "date", "reply_tag"],
                )
                if not log_exists:
                    writer.writeheader()
                for r in lead_replies:
                    tag = _classify_reply(r["subject"])
                    r["reply_tag"] = tag
                    writer.writerow({
                        "logged_at": now_ts,
                        "from_email": r["from"],
                        "name": r["name"],
                        "subject": r["subject"],
                        "date": r["date"],
                        "reply_tag": tag,
                    })
                    # Promote pipeline: all replies → "replied"; positive → "interested"
                    new_pipeline_status = "interested" if tag == "positive" else "replied"
                    _pipeline_promote(r["from"], new_pipeline_status, reply_tag=tag)

        st.subheader(f"Replies from leads ({len(lead_replies)})")
        if lead_replies:
            display_replies = pd.DataFrame(lead_replies)
            # Add human-readable tag labels for display only (raw tags used for counts)
            if "reply_tag" in display_replies.columns:
                tag_labels = {"positive": "✅ Positive", "negative": "❌ Negative", "neutral": "😐 Neutral"}
                display_replies["reply_tag"] = display_replies["reply_tag"].map(
                    lambda t: tag_labels.get(t, t)
                )
            st.dataframe(display_replies, use_container_width=True)

            # Classification summary — count raw tags before label mapping
            raw_tags = [r.get("reply_tag", "neutral") for r in lead_replies]
            pos = sum(1 for t in raw_tags if t == "positive")
            neg = sum(1 for t in raw_tags if t == "negative")
            neu = sum(1 for t in raw_tags if t == "neutral")
            sc1, sc2, sc3 = st.columns(3)
            sc1.metric("✅ Positive replies", pos)
            sc2.metric("😐 Neutral replies", neu)
            sc3.metric("❌ Negative replies", neg)

            st.success(
                f"✅ {len(lead_replies)} lead reply(ies) logged to `{REPLIES_LOG_CSV}` "
                "and pipeline statuses updated.",
                icon="📝",
            )
        else:
            st.info("No replies from known leads found.")

        with st.expander(f"Other inbox messages ({len(other_msgs)})"):
            if other_msgs:
                st.dataframe(pd.DataFrame(other_msgs), use_container_width=True)
            else:
                st.write("None.")


# ---------------------------------------------------------------------------
# Tab: Pipeline (CRM)
# ---------------------------------------------------------------------------

def tab_pipeline() -> None:
    st.markdown(
        """
        <div style="
            background: linear-gradient(135deg,#08C7E1 0%,#237FEA 100%);
            border-radius:14px;padding:22px 28px;margin-bottom:20px;color:#fff;
        ">
            <div style="font-size:1.5rem;font-weight:800;letter-spacing:-.03em;">🎯 Pipeline & Deal Tracking</div>
            <div style="font-size:.9rem;opacity:.85;margin-top:4px;">
                Track every lead from first contact to closed deal — and see which keywords make you money.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    pip_df = _load_pipeline()

    if pip_df.empty:
        st.info(
            "No pipeline entries yet. Scrape leads and click **Push to Live Leads** "
            "to auto-populate the pipeline.",
            icon="🎯",
        )
        return

    # --- Funnel KPIs ---
    status_counts: dict[str, int] = {s: 0 for s in PIPELINE_STATUSES}
    if "status" in pip_df.columns:
        for s, cnt in pip_df["status"].value_counts().items():
            if s in status_counts:
                status_counts[s] = int(cnt)

    # Active = any status between 'contacted' and 'call_booked' (inclusive)
    _active_statuses = [s for s in PIPELINE_STATUSES if s not in ("new", "closed_won", "closed_lost")]
    # Contacted = any status that is not 'new'
    _contacted_statuses = [s for s in PIPELINE_STATUSES if s != "new"]

    revenue = 0.0
    pipeline_value = 0.0
    if "deal_value" in pip_df.columns and "status" in pip_df.columns:
        won_mask = pip_df["status"] == "closed_won"
        active_mask = pip_df["status"].isin(_active_statuses)
        revenue = pd.to_numeric(pip_df.loc[won_mask, "deal_value"], errors="coerce").fillna(0).sum()
        pipeline_value = pd.to_numeric(pip_df.loc[active_mask, "deal_value"], errors="coerce").fillna(0).sum()

    total_pip = len(pip_df)
    contacted = sum(status_counts[s] for s in _contacted_statuses)
    replied_pct = f"{status_counts['replied'] / contacted * 100:.0f}%" if contacted > 0 else "—"
    close_rate = f"{status_counts['closed_won'] / contacted * 100:.0f}%" if contacted > 0 else "—"

    k_cols = st.columns(6)
    k_cols[0].metric("Total in Pipeline", f"{total_pip:,}")
    k_cols[1].metric("Contacted", f"{contacted:,}")
    k_cols[2].metric("Replied", f"{status_counts['replied']:,}", delta=replied_pct)
    k_cols[3].metric("Interested 🔥", f"{status_counts['interested']:,}")
    k_cols[4].metric("Calls Booked 📞", f"{status_counts['call_booked']:,}")
    k_cols[5].metric("Closed Won 🏆", f"{status_counts['closed_won']:,}", delta=close_rate)

    rv1, rv2 = st.columns(2)
    rv1.metric("💰 Revenue (closed)", f"${revenue:,.0f}")
    rv2.metric("🔮 Pipeline Value (active)", f"${pipeline_value:,.0f}")

    st.divider()

    # --- Funnel chart ---
    funnel_df = pd.DataFrame({
        "stage": [
            f"{PIPELINE_STATUS_EMOJI.get(s, '')} {s.replace('_', ' ').title()}"
            for s in PIPELINE_STATUSES
        ],
        "count": [status_counts[s] for s in PIPELINE_STATUSES],
    })
    st.subheader("Conversion Funnel")
    st.bar_chart(funnel_df.set_index("stage")["count"])

    # --- Revenue attribution ---
    won_df = pip_df[pip_df["status"] == "closed_won"].copy() if "status" in pip_df.columns else pd.DataFrame()
    if not won_df.empty and "deal_value" in won_df.columns:
        won_df["_val"] = pd.to_numeric(won_df["deal_value"], errors="coerce").fillna(0)
        st.divider()
        ra_col1, ra_col2 = st.columns(2)
        with ra_col1:
            st.subheader("💰 Revenue by keyword")
            if "keyword" in won_df.columns:
                kw_rev = (
                    won_df.groupby("keyword")["_val"]
                    .sum()
                    .sort_values(ascending=False)
                    .head(15)
                    .rename_axis("keyword")
                    .reset_index(name="revenue ($)")
                )
                if not kw_rev.empty:
                    st.bar_chart(kw_rev.set_index("keyword")["revenue ($)"])
                else:
                    st.info("No keyword revenue data yet.", icon="📊")
        with ra_col2:
            st.subheader("💰 Revenue by source")
            if "source" in won_df.columns:
                src_rev = (
                    won_df.groupby("source")["_val"]
                    .sum()
                    .sort_values(ascending=False)
                    .rename_axis("source")
                    .reset_index(name="revenue ($)")
                )
                if not src_rev.empty:
                    st.bar_chart(src_rev.set_index("source")["revenue ($)"])
                else:
                    st.info("No source revenue data yet.", icon="📊")

    st.divider()

    # --- Editable pipeline table ---
    st.subheader("📋 Manage Pipeline")
    st.caption(
        "Update **Status** and **Deal Value** inline. "
        "Click **💾 Save Changes** to persist. "
        "Positive replies auto-promote to 'interested'; all replies promote to 'replied'."
    )

    # Status filter
    status_filter = st.multiselect(
        "Filter by status",
        options=PIPELINE_STATUSES,
        default=PIPELINE_STATUSES,
        format_func=lambda s: f"{PIPELINE_STATUS_EMOJI.get(s, '')} {s.replace('_', ' ').title()}",
    )
    display_df = pip_df[pip_df["status"].isin(status_filter)].copy() if status_filter else pip_df.copy()

    # Column config
    status_options = [
        f"{PIPELINE_STATUS_EMOJI.get(s, '')} {s}" for s in PIPELINE_STATUSES
    ]
    col_cfg = {
        "status": st.column_config.SelectboxColumn(
            "Status",
            options=PIPELINE_STATUSES,
            required=True,
        ),
        "deal_value": st.column_config.NumberColumn(
            "💰 Deal Value ($)",
            min_value=0,
            format="$%d",
        ),
        "reply_tag": st.column_config.SelectboxColumn(
            "Reply Tag",
            options=["", "positive", "neutral", "negative"],
        ),
        "last_updated": st.column_config.TextColumn("Last Updated", disabled=True),
    }

    # Convert deal_value to numeric for the editor
    display_df["deal_value"] = pd.to_numeric(display_df["deal_value"], errors="coerce").fillna(0)

    edited_pip = st.data_editor(
        display_df,
        use_container_width=True,
        num_rows="fixed",
        column_config=col_cfg,
        disabled=[c for c in display_df.columns if c not in ("status", "deal_value", "reply_tag")],
        key="pipeline_editor",
    )

    save_col, dl_col, _ = st.columns([2, 2, 4])

    with save_col:
        if st.button("💾 Save Changes", type="primary"):
            # Merge edited rows back into full pipeline
            now_ts = datetime.now(timezone.utc).isoformat()
            full_pip = _load_pipeline()
            full_pip["deal_value"] = pd.to_numeric(full_pip["deal_value"], errors="coerce").fillna(0)

            for _, row in edited_pip.iterrows():
                email = str(row.get("email", "")).lower().strip()
                mask = full_pip["email"].str.lower().str.strip() == email
                if mask.any():
                    full_pip.loc[mask, "status"] = row["status"]
                    full_pip.loc[mask, "deal_value"] = str(round(float(row["deal_value"]), 2))
                    if row.get("reply_tag"):
                        full_pip.loc[mask, "reply_tag"] = row["reply_tag"]
                    full_pip.loc[mask, "last_updated"] = now_ts

            _save_pipeline(full_pip)
            st.success("✅ Pipeline saved.")
            st.rerun()

    with dl_col:
        csv_bytes = edited_pip.to_csv(index=False).encode()
        st.download_button(
            "⬇ Download pipeline CSV",
            data=csv_bytes,
            file_name="pipeline.csv",
            mime="text/csv",
        )

    # --- Add leads from Live Leads that aren't in pipeline ---
    st.divider()
    with st.expander("➕ Add missing Live Leads to Pipeline"):
        live_df = _read_csv(LIVE_LEADS_CSV)
        if live_df.empty:
            st.info("No live leads found.", icon="📂")
        else:
            existing_emails = set(pip_df["email"].str.lower().str.strip())
            if "email" in live_df.columns:
                normalised = live_df["email"].str.lower().str.strip()
                missing = live_df[normalised.str.len() > 0 & ~normalised.isin(existing_emails)]
            else:
                missing = pd.DataFrame()

            st.write(f"**{len(missing)} lead(s) not yet in pipeline**")
            if not missing.empty:
                if st.button("➕ Add all missing leads to pipeline", type="secondary"):
                    added = _upsert_pipeline_entries(missing.to_dict("records"))
                    st.success(f"✅ Added {added} lead(s) to pipeline.")
                    st.rerun()


# ---------------------------------------------------------------------------
# Follow-up helpers
# ---------------------------------------------------------------------------

def _render_follow_up_send_form(
    due_df: pd.DataFrame,
    sequence_num: int,
    key_prefix: str,
) -> None:
    """Render an SMTP send form for one follow-up batch."""
    import importlib

    default_templates = {
        1: (Path("follow_up_1_template.txt"), "Quick follow-up — {name}"),
        2: (Path("follow_up_2_template.txt"), "Last message from me — {name}"),
    }
    tmpl_path, default_subject = default_templates.get(
        sequence_num, (Path("email_template.txt"), "Following up — {name}")
    )
    default_body = (
        tmpl_path.read_text(encoding="utf-8")
        if tmpl_path.exists()
        else _default_template()
    )

    with st.form(f"{key_prefix}_form"):
        fc1, fc2 = st.columns(2)
        with fc1:
            smtp_host = st.text_input(
                "SMTP Host", value=os.environ.get("SMTP_HOST", "smtp-relay.brevo.com"),
                key=f"{key_prefix}_host",
            )
            smtp_user = st.text_input(
                "SMTP Username", value=os.environ.get("SMTP_USER", os.environ.get("MAIL_USERNAME", "")),
                key=f"{key_prefix}_smtp_user",
            )
            from_email = st.text_input(
                "From Email", value=os.environ.get("MAIL_FROM_ADDRESS", os.environ.get("EMAIL_ADDRESS", "")),
                key=f"{key_prefix}_email",
            )
            reply_to = st.text_input(
                "Reply-To Email", value=os.environ.get("MAIL_REPLY_TO", ""),
                key=f"{key_prefix}_reply_to",
            )
            from_name = st.text_input("Sender Name", value=os.environ.get("MAIL_FROM_NAME", ""), key=f"{key_prefix}_fname")
        with fc2:
            smtp_port = st.number_input("SMTP Port", value=int(os.environ.get("SMTP_PORT", "587")), key=f"{key_prefix}_port")
            password = st.text_input(
                "Password / App Password", type="password",
                value=os.environ.get("SMTP_PASS", os.environ.get("MAIL_PASSWORD", os.environ.get("EMAIL_PASSWORD", ""))),
                key=f"{key_prefix}_pass",
            )
            use_ssl = st.checkbox("Use SSL (port 465)", value=False, key=f"{key_prefix}_ssl")

        subject = st.text_input("Subject", value=default_subject, key=f"{key_prefix}_subj")
        body = st.text_area("Email body", value=default_body, height=180, key=f"{key_prefix}_body")

        # Conversion copy fields
        offer_label_fu = st.selectbox(
            "Offer", list(OFFER_LIBRARY.keys()), key=f"{key_prefix}_offer_label"
        )
        offer_fu = st.text_input(
            "Offer text", value=OFFER_LIBRARY[offer_label_fu], key=f"{key_prefix}_offer_text",
            help="Fills `{offer}` in the template.",
        )
        cta_label_fu = st.selectbox(
            "CTA", list(CTA_OPTIONS.keys()), key=f"{key_prefix}_cta_label"
        )
        cta_fu = st.text_input(
            "CTA text", value=CTA_OPTIONS[cta_label_fu], key=f"{key_prefix}_cta_text",
            help="Fills `{cta}` in the template.",
        )
        proof_fu = st.text_input(
            "Proof line", value="", key=f"{key_prefix}_proof",
            help="Fills `{proof}` — leave blank to remove that line.",
        )
        booking_fu = st.text_input(
            "Booking URL", value="", key=f"{key_prefix}_booking",
            placeholder="https://calendly.com/yourname/15min",
            help="Fills `{booking_url}` — leave blank to omit.",
        )

        dry_run = st.checkbox("Dry run (preview only)", value=True, key=f"{key_prefix}_dry")

        submit = st.form_submit_button(
            f"▶ Send Follow-up #{sequence_num} ({len(due_df)} recipients)",
            type="primary",
        )

    if submit:
        be = importlib.import_module("bulk_emailer")
        import argparse as _ap

        static_fu = {
            "offer": offer_fu,
            "cta": cta_fu,
            "proof": proof_fu,
            "booking_url": f"👉 Book a quick call: {booking_fu}" if booking_fu.strip() else "",
        }
        resolved_body = _pre_substitute(body, static_fu)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="fu_tmpl_", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(resolved_body)
            tmp_path = tmp.name

        # Export leads + unsubscribe from SQLite to temp files for bulk_emailer
        _fu_leads_df = db.read_table("live_leads")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", prefix="fu_leads_", delete=False, encoding="utf-8"
        ) as _tmp_fu_leads:
            _fu_leads_df.to_csv(_tmp_fu_leads.name, index=False)
            _fu_leads_path = _tmp_fu_leads.name

        _fu_unsub = db.load_unsubscribe()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="fu_unsub_", delete=False, encoding="utf-8"
        ) as _tmp_fu_unsub:
            _tmp_fu_unsub.write("\n".join(_fu_unsub))
            _fu_unsub_path = _tmp_fu_unsub.name

        ns = _ap.Namespace(
            sequence_num=sequence_num,
            csv=_fu_leads_path,
            template=tmp_path,
            subject=subject,
            from_name=from_name,
            smtp_host=smtp_host,
            smtp_user=smtp_user,
            smtp_port=int(smtp_port),
            ssl=use_ssl,
            email=from_email,
            reply_to=reply_to,
            password=password,
            log=SENT_LOG_CSV,
            unsubscribe=_fu_unsub_path,
            delay=2.0,
            html=False,
            dry_run=dry_run,
        )

        error_holder: List[str] = []
        log_lines: List[str] = []
        log_q: queue.Queue = queue.Queue()

        q_handler = _QueueHandler(log_q)
        q_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(q_handler)

        def _run() -> None:
            try:
                be.cmd_follow_up(ns)
            except SystemExit as exc:
                if str(exc) != "0":
                    error_holder.append(f"Send failed (exit {exc}).")
            except Exception as exc:  # noqa: BLE001
                error_holder.append(f"Error ({type(exc).__name__}): {exc}")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        log_box = st.empty()
        tick = 0
        while thread.is_alive():
            while not log_q.empty():
                log_lines.append(log_q.get_nowait())
            log_box.text_area("Log", "\n".join(log_lines[-40:]), height=160, key=f"{key_prefix}_log_{tick}")
            tick += 1
            time.sleep(0.4)

        while not log_q.empty():
            log_lines.append(log_q.get_nowait())
        root_logger.removeHandler(q_handler)
        log_box.text_area("Log", "\n".join(log_lines[-40:]), height=160, key=f"{key_prefix}_log_final")

        if error_holder:
            st.error(error_holder[0])
        else:
            label = "Dry-run complete." if dry_run else f"✅ Follow-up #{sequence_num} sent!"
            st.success(label)
            if not dry_run:
                st.rerun()


# ---------------------------------------------------------------------------
# Tab: Follow-ups
# ---------------------------------------------------------------------------

def tab_follow_ups() -> None:
    st.markdown(
        """
        <div style="
            background: linear-gradient(135deg,#FF7E54 0%,#237FEA 100%);
            border-radius:14px;padding:22px 28px;margin-bottom:20px;color:#fff;
        ">
            <div style="font-size:1.5rem;font-weight:800;letter-spacing:-.03em;">📅 Follow-up Sequences</div>
            <div style="font-size:.9rem;opacity:.85;margin-top:4px;">
                Day-3 check-in &amp; Day-7 final message — most replies come from follow-ups.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.info(
        "**Schedule:** Day 1 → First email · Day 3 → Follow-up #1 · Day 7 → Follow-up #2 (final)\n\n"
        "The system reads your sent log and surfaces everyone who is now due for the next step.",
        icon="📆",
    )
    st.divider()

    sent_df = _read_csv(SENT_LOG_CSV)
    if sent_df.empty:
        st.info(
            "No sent emails found yet. Send your first outreach from the **✉️ Compose & Send** tab first.",
            icon="📭",
        )
        return

    if "sequence_num" not in sent_df.columns:
        sent_df["sequence_num"] = "0"
    sent_df["sequence_num"] = (
        pd.to_numeric(sent_df["sequence_num"], errors="coerce").fillna(0).astype(int)
    )

    sent_only = sent_df[sent_df["status"] == "sent"].copy()
    if sent_only.empty:
        st.info("No successfully sent emails recorded yet.")
        return

    sent_only["sent_at"] = pd.to_datetime(sent_only["timestamp"], errors="coerce", utc=True)
    now = datetime.now(timezone.utc)

    summary = (
        sent_only.groupby("to_email")
        .agg(
            max_seq=("sequence_num", "max"),
            last_sent=("sent_at", "max"),
            to_name=("to_name", "first"),
        )
        .reset_index()
    )
    summary["days_since"] = (
        (now - summary["last_sent"]).dt.total_seconds() / 86400
    ).fillna(0).apply(lambda x: int(x))

    # Follow-up #1: max_seq == 0 and days_since >= 3
    due1 = summary[(summary["max_seq"] == 0) & (summary["days_since"] >= 3)].copy()
    # Follow-up #2: max_seq == 1 and days_since >= 4
    due2 = summary[(summary["max_seq"] == 1) & (summary["days_since"] >= 4)].copy()
    # Still waiting (< 3 days since first email)
    pending = summary[(summary["max_seq"] == 0) & (summary["days_since"] < 3)].copy()
    # Completed full sequence
    completed = summary[summary["max_seq"] >= 2]

    # --- Summary metrics ---
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total contacted", f"{len(summary):,}")
    c2.metric("Due: Day-3 follow-up", f"{len(due1):,}")
    c3.metric("Due: Day-7 final", f"{len(due2):,}")
    c4.metric("Full sequence done", f"{len(completed):,}")

    st.divider()

    # --- Day-3 follow-up ---
    with st.expander(
        f"📬 Day-3 Follow-up — **{len(due1)} due**",
        expanded=len(due1) > 0,
    ):
        if due1.empty:
            st.info("No contacts due for the Day-3 follow-up yet. Check back in a couple of days.")
        else:
            st.dataframe(
                due1[["to_email", "to_name", "days_since", "max_seq"]].rename(columns={
                    "to_email": "Email", "to_name": "Name",
                    "days_since": "Days Since Last Send", "max_seq": "Sequence #",
                }),
                use_container_width=True,
            )
            _render_follow_up_send_form(due1, sequence_num=1, key_prefix="fu1")

    # --- Day-7 final ---
    with st.expander(
        f"📬 Day-7 Final — **{len(due2)} due**",
        expanded=len(due2) > 0,
    ):
        if due2.empty:
            st.info("No contacts due for the Day-7 final follow-up yet.")
        else:
            st.dataframe(
                due2[["to_email", "to_name", "days_since", "max_seq"]].rename(columns={
                    "to_email": "Email", "to_name": "Name",
                    "days_since": "Days Since Last Send", "max_seq": "Sequence #",
                }),
                use_container_width=True,
            )
            _render_follow_up_send_form(due2, sequence_num=2, key_prefix="fu2")

    # --- Awaiting ---
    if not pending.empty:
        with st.expander(f"⏳ Awaiting Day-3 window — {len(pending)} contact(s)"):
            st.dataframe(
                pending[["to_email", "to_name", "days_since"]].rename(columns={
                    "to_email": "Email", "to_name": "Name",
                    "days_since": "Days Since Send",
                }),
                use_container_width=True,
            )




_inject_css()

# ────────────────────────────────────────────────────────────────────────────
# Modern CRM Navigation - Session State & Sidebar
# ────────────────────────────────────────────────────────────────────────────

if "current_page" not in st.session_state:
    st.session_state.current_page = "Dashboard"

# Tab definitions: (emoji, label, function)
PAGES = [
    ("📊", "Dashboard", tab_dashboard),
    ("🔍", "Scrape", tab_scrape),
    ("📋", "Leads", tab_leads),
    ("✉️", "Compose & Send", tab_send),
    ("📅", "Follow-ups", tab_follow_ups),
    ("📑", "Sent Log", tab_sent_log),
    ("💬", "Replies", tab_replies),
    ("🎯", "Pipeline", tab_pipeline),
    ("🚫", "Unsubscribes", tab_unsubscribe),
]

# ────────────────────────────────────────────────────────────────────────────
# Sidebar Navigation
# ────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    # Navigation section only
    st.markdown(
        """
        <div style="margin-bottom:12px;">
          <div style="font-size:0.75rem;font-weight:700;text-transform:uppercase;
                      letter-spacing:.08em;color:var(--oa-muted);padding:0 4px;">
            🗂 Navigation
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Navigation buttons with active state
    for emoji, label, _ in PAGES:
        is_active = st.session_state.current_page == label
        btn_style = (
            "background:linear-gradient(135deg, #237FEA 0%, #08C7E1 100%);color:#fff;font-weight:700;"
            if is_active
            else "background:var(--oa-secondary-bg);color:var(--oa-text);border:1px solid var(--oa-border);"
        )
        if st.sidebar.button(
            f"{emoji}  {label}",
            use_container_width=True,
            key=f"nav_{label}",
            help=f"Go to {label}"
        ):
            st.session_state.current_page = label

# ────────────────────────────────────────────────────────────────────────────
# Render Active Page
# ────────────────────────────────────────────────────────────────────────────

for emoji, label, page_func in PAGES:
    if st.session_state.current_page == label:
        page_func()
        break
