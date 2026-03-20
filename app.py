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

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Outreach Dashboard",
    page_icon="📧",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LEADS_CSV = "leads.csv"           # staging / most-recent scrape
LIVE_LEADS_CSV = "live_leads.csv" # permanent cumulative store
SENT_LOG_CSV = "sent_log.csv"
UNSUBSCRIBE_TXT = "unsubscribe.txt"
EMAIL_TEMPLATE_TXT = "email_template.txt"

BING_FIELDNAMES = [
    "source", "keyword", "url", "title", "email", "phone",
    "linkedin", "twitter", "facebook", "instagram",
    "contact_page", "issues", "lead_score",
]
MAPS_FIELDNAMES = [
    "source", "keyword", "name", "address", "phone", "website", "rating", "reviews",
    "category", "email", "linkedin", "twitter", "facebook", "instagram",
    "contact_page", "issues", "lead_score",
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
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(p, dtype=str).fillna("")


def _write_csv(path: str, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False)


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
        /* ── Global font & background ─────────────────────────────── */
        html, body, [class*="css"] {
            font-family: "Inter", "Segoe UI", sans-serif;
        }

        /* ── Metric cards ─────────────────────────────────────────── */
        [data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px 20px;
            box-shadow: 0 1px 4px rgba(0,0,0,.06);
        }
        [data-testid="stMetric"]:nth-child(1) { border-top: 4px solid #6366f1; }
        [data-testid="stMetric"]:nth-child(2) { border-top: 4px solid #0ea5e9; }
        [data-testid="stMetric"]:nth-child(3) { border-top: 4px solid #22c55e; }
        [data-testid="stMetric"]:nth-child(4) { border-top: 4px solid #f97316; }
        [data-testid="stMetric"]:nth-child(5) { border-top: 4px solid #a855f7; }

        [data-testid="stMetricLabel"] {
            font-size: 0.78rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: .05em;
            color: #6b7280;
        }
        [data-testid="stMetricValue"] {
            font-size: 1.7rem;
            font-weight: 700;
            color: #111827;
        }

        /* ── Tabs ─────────────────────────────────────────────────── */
        .stTabs [data-baseweb="tab-list"] {
            gap: 4px;
            background: #f9fafb;
            padding: 6px 6px 0;
            border-radius: 10px 10px 0 0;
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 8px 8px 0 0;
            padding: 8px 18px;
            font-weight: 500;
            font-size: 0.85rem;
            color: #6b7280;
            background: transparent;
        }
        .stTabs [aria-selected="true"] {
            background: #ffffff !important;
            color: #6366f1 !important;
            border-bottom: 3px solid #6366f1;
            font-weight: 700;
        }

        /* ── Buttons ─────────────────────────────────────────────── */
        .stButton > button[kind="primary"] {
            background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
            border: none;
            border-radius: 8px;
            padding: 8px 24px;
            font-weight: 600;
            color: #fff;
            box-shadow: 0 2px 8px rgba(99,102,241,.35);
            transition: all .2s;
        }
        .stButton > button[kind="primary"]:hover {
            filter: brightness(1.1);
            box-shadow: 0 4px 14px rgba(99,102,241,.5);
            transform: translateY(-1px);
        }
        .stButton > button[kind="secondary"] {
            border-radius: 8px;
            font-weight: 500;
        }

        /* ── Expander ─────────────────────────────────────────────── */
        [data-testid="stExpander"] {
            border: 1px solid #e5e7eb;
            border-radius: 10px;
            overflow: hidden;
        }

        /* ── Success / Error / Warning / Info boxes ───────────────── */
        [data-testid="stAlert"] {
            border-radius: 10px;
        }

        /* ── Sidebar branding ─────────────────────────────────────── */
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #0f172a 0%, #1e293b 100%);
        }
        [data-testid="stSidebar"] * {
            color: #e2e8f0 !important;
        }
        [data-testid="stSidebar"] hr {
            border-color: #334155;
        }
        [data-testid="stSidebar"] [data-testid="stMetric"] {
            background: #1e293b;
            border-color: #334155;
            border-top-color: #6366f1;
        }
        [data-testid="stSidebar"] [data-testid="stMetricValue"] {
            color: #f8fafc !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown(
            """
            <div style="text-align:center;padding:8px 0 4px">
              <div style="font-size:2.2rem">📧</div>
              <div style="font-size:1.25rem;font-weight:800;letter-spacing:-.02em;
                          color:#f8fafc;margin-top:4px">Outreach</div>
              <div style="font-size:0.75rem;color:#94a3b8;margin-top:2px">
                Lead generation &amp; email outreach
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.divider()

        # Quick stats from disk
        live_df = _read_csv(LIVE_LEADS_CSV)
        staging_df = _read_csv(LEADS_CSV)
        sent_df = _read_csv(SENT_LOG_CSV)

        live_total = len(live_df)
        live_emails = (
            live_df["email"].str.strip().replace("", pd.NA).dropna().nunique()
            if "email" in live_df.columns else 0
        )
        staged = len(staging_df)
        sent_n = int((sent_df["status"] == "sent").sum()) if "status" in sent_df.columns else 0

        st.caption("📊 QUICK STATS")
        s1, s2 = st.columns(2)
        s1.metric("Live Leads", f"{live_total:,}")
        s2.metric("Sent", f"{sent_n:,}")
        s1.metric("Emails", f"{live_emails:,}")
        s2.metric("Staged", f"{staged:,}")

        st.divider()
        st.caption("🗂 TABS")
        st.markdown(
            """
            | Tab | Purpose |
            |---|---|
            | 📊 Dashboard | KPIs & charts |
            | 🔍 Scrape | Collect leads |
            | 📋 Leads | Browse & filter |
            | ✉️ Send | Compose emails |
            | 📅 Follow-ups | Day-3 & Day-7 sequences |
            | 📑 Sent Log | Track sends |
            | 💬 Replies | Inbox check |
            | 🚫 Unsub | Opt-out list |
            """,
            unsafe_allow_html=False,
        )

        st.divider()
        st.caption("⚡ TIPS")
        st.info(
            "1️⃣ **Scrape** leads → review the preview\n\n"
            "2️⃣ Click **Push to Live Leads** to save them permanently\n\n"
            "3️⃣ Go to **Compose & Send** to send outreach emails",
            icon="💡",
        )


# ---------------------------------------------------------------------------
# Tab: Dashboard
# ---------------------------------------------------------------------------

def tab_dashboard() -> None:
    # Hero banner
    st.markdown(
        """
        <div style="
            background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 60%, #0ea5e9 100%);
            border-radius: 14px;
            padding: 28px 32px;
            margin-bottom: 24px;
            color: #fff;
        ">
            <div style="font-size:1.8rem;font-weight:800;letter-spacing:-.03em;">
                📊 Outreach Dashboard
            </div>
            <div style="font-size:1rem;opacity:.85;margin-top:6px;">
                Your leads, sends, and results — all in one place.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    leads_df = _read_csv(LIVE_LEADS_CSV)
    staging_df = _read_csv(LEADS_CSV)
    sent_df = _read_csv(SENT_LOG_CSV)

    # --- KPI row ---
    k1, k2, k3, k4, k5 = st.columns(5)

    total_leads = len(leads_df)
    unique_emails = leads_df["email"].str.strip().replace("", pd.NA).dropna().nunique() if "email" in leads_df.columns else 0
    staged = len(staging_df)

    sent_count = int((sent_df["status"] == "sent").sum()) if "status" in sent_df.columns else 0
    failed_count = int((sent_df["status"] == "failed").sum()) if "status" in sent_df.columns else 0
    success_rate = f"{sent_count / (sent_count + failed_count) * 100:.0f}%" if (sent_count + failed_count) > 0 else "—"

    k1.metric("Live leads", f"{total_leads:,}")
    k2.metric("Unique emails", f"{unique_emails:,}")
    k3.metric("Emails sent", f"{sent_count:,}")
    k4.metric("Failed sends", f"{failed_count:,}")
    k5.metric("Staged (unreviewed)", f"{staged:,}")

    st.divider()

    col_left, col_right = st.columns(2)

    # --- Lead score distribution ---
    with col_left:
        st.subheader("Lead score distribution")
        if not leads_df.empty and "lead_score" in leads_df.columns:
            scores = pd.to_numeric(leads_df["lead_score"], errors="coerce").dropna()
            if not scores.empty:
                score_counts = scores.astype(int).value_counts().sort_index().rename_axis("score").reset_index(name="count")
                st.bar_chart(score_counts.set_index("score")["count"])
            else:
                st.info("No lead score data available.", icon="ℹ️")
        else:
            st.info("Scrape leads and push them to **Live Leads** to see data here.", icon="🔍")

    # --- Sends over time ---
    with col_right:
        st.subheader("Sends over time")
        if not sent_df.empty and "timestamp" in sent_df.columns and "status" in sent_df.columns:
            sent_only = sent_df[sent_df["status"] == "sent"].copy()
            if not sent_only.empty:
                sent_only["date"] = pd.to_datetime(sent_only["timestamp"], errors="coerce", utc=True).dt.date
                daily = sent_only.groupby("date").size().reset_index(name="emails_sent")
                daily["date"] = daily["date"].astype(str)
                st.line_chart(daily.set_index("date")["emails_sent"])
            else:
                st.info("No successful sends recorded yet.", icon="✉️")
        else:
            st.info("Send some emails via **Compose & Send** to see activity.", icon="✉️")

    # --- Category breakdown ---
    if not leads_df.empty and "category" in leads_df.columns:
        st.divider()
        st.subheader("Leads by category")
        cat_counts = (
            leads_df["category"]
            .replace("", pd.NA)
            .dropna()
            .value_counts()
            .head(15)
            .rename_axis("category")
            .reset_index(name="count")
        )
        if not cat_counts.empty:
            st.bar_chart(cat_counts.set_index("category")["count"])

    # --- Keyword performance ---
    if not leads_df.empty and "keyword" in leads_df.columns:
        st.divider()
        st.subheader("Leads by keyword")
        kw_counts = (
            leads_df["keyword"]
            .replace("", pd.NA)
            .dropna()
            .value_counts()
            .head(20)
            .rename_axis("keyword")
            .reset_index(name="leads")
        )
        if not kw_counts.empty:
            st.bar_chart(kw_counts.set_index("keyword")["leads"])

    # --- Source breakdown ---
    if not leads_df.empty and "source" in leads_df.columns:
        st.divider()
        src_col1, src_col2 = st.columns(2)
        with src_col1:
            st.subheader("Leads by source")
            src_counts = (
                leads_df["source"]
                .replace("", pd.NA)
                .dropna()
                .value_counts()
                .rename_axis("source")
                .reset_index(name="count")
            )
            if not src_counts.empty:
                st.bar_chart(src_counts.set_index("source")["count"])
        with src_col2:
            st.subheader("Emails found by source")
            if "email" in leads_df.columns:
                src_email = (
                    leads_df[leads_df["email"].str.strip() != ""]
                    ["source"]
                    .replace("", pd.NA)
                    .dropna()
                    .value_counts()
                    .rename_axis("source")
                    .reset_index(name="with_email")
                )
                if not src_email.empty:
                    st.bar_chart(src_email.set_index("source")["with_email"])


# ---------------------------------------------------------------------------
# Tab: Unsubscribe Manager
# ---------------------------------------------------------------------------

def tab_unsubscribe() -> None:
    st.header("🚫 Unsubscribe Manager")
    st.caption("Manage opt-out addresses. These are permanently skipped on every send.")
    st.divider()

    unsub_path = Path(UNSUBSCRIBE_TXT)

    def _load() -> List[str]:
        if not unsub_path.exists():
            return []
        lines = unsub_path.read_text(encoding="utf-8").splitlines()
        return [ln.strip().lower() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

    def _save(addresses: List[str]) -> None:
        unsub_path.write_text("\n".join(sorted(set(addresses))) + "\n", encoding="utf-8")

    addresses = _load()
    st.write(f"**{len(addresses)} unsubscribed address(es)** in `{UNSUBSCRIBE_TXT}`")

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
            background: linear-gradient(135deg,#6366f1 0%,#8b5cf6 60%,#0ea5e9 100%);
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
            st.session_state.pop("staged_records", None)
            st.session_state.pop("staged_engine", None)
            if new_count:
                st.success(
                    f"🎉 **{new_count} new lead(s)** added to `{LIVE_LEADS_CSV}`. "
                    f"{dup_count} duplicate(s) skipped."
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
        fcol1, fcol2, fcol3 = st.columns(3)
        with fcol1:
            has_email = st.checkbox("Only rows with email", value=False)
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

    filtered = df.copy()
    if has_email and "email" in filtered.columns:
        filtered = filtered[filtered["email"].str.strip() != ""]
    if "lead_score" in filtered.columns and min_score:
        filtered = filtered[pd.to_numeric(filtered["lead_score"], errors="coerce").fillna(0) >= min_score]
    if category_col and category_filter and category_filter != "(all)":
        filtered = filtered[filtered[category_col] == category_filter]

    st.write(f"**{len(filtered)} rows after filters**")

    # Ensure notes column exists for editing
    if "notes" not in filtered.columns:
        filtered = filtered.copy()
        filtered["notes"] = ""

    # Column config for richer display
    col_cfg: dict = {
        "notes": st.column_config.TextColumn("📝 Notes", width="medium"),
        "lead_score": st.column_config.NumberColumn("⭐ Score"),
        "issues": st.column_config.TextColumn("⚠️ Issues", width="large"),
    }
    if "url" in filtered.columns:
        col_cfg["url"] = st.column_config.LinkColumn("🔗 URL", display_text="Visit")
    if "website" in filtered.columns:
        col_cfg["website"] = st.column_config.LinkColumn("🌐 Website", display_text="Visit")

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

def tab_send() -> None:
    st.header("✉️ Compose & Send")
    st.caption("Personalise and send outreach emails to your leads via SMTP.")
    st.divider()

    with st.expander("🔐 SMTP Settings", expanded=True):
        sc1, sc2 = st.columns(2)
        with sc1:
            smtp_host = st.text_input("SMTP Host", value=os.environ.get("SMTP_HOST", "smtp.gmail.com"))
            from_email = st.text_input("From Email", value=os.environ.get("EMAIL_ADDRESS", ""))
        with sc2:
            smtp_port = st.number_input("SMTP Port", value=587, step=1)
            password = st.text_input("Password / App Password", type="password",
                                     value=os.environ.get("EMAIL_PASSWORD", ""))
        use_ssl = st.checkbox("Use SSL (port 465)", value=False)
        from_name = st.text_input("Sender Display Name", value="")
        st.caption(
            "💡 **Gmail users:** Enable 2-Step Verification and create an "
            "[App Password](https://myaccount.google.com/apppasswords). "
            "Your regular password will not work."
        )

    with st.expander("📝 Email Content", expanded=True):
        subject = st.text_input("Subject line", value="Quick question for {name}",
                                help="Supports placeholders: {name}, {url}, {phone}, or any CSV column name.")
        template_body = st.text_area("Email body template", value=_default_template(), height=220,
                                     help="Use {name}, {url}, {phone} etc. – replaced with values from each lead row.")
        is_html = st.checkbox("HTML email", value=False)

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

        # Build an args namespace matching bulk_emailer.cmd_send expectations
        ns = argparse.Namespace(
            csv=leads_csv,
            template=None,          # we'll monkey-patch template_body below
            subject=subject,
            from_name=from_name,
            smtp_host=smtp_host,
            smtp_port=int(smtp_port),
            ssl=use_ssl,
            email=from_email,
            password=password,
            log=sent_log,
            unsubscribe=unsub_file,
            delay=float(delay),
            html=is_html,
            dry_run=dry_run,
        )

        # Write template to a cross-platform temp file so bulk_emailer can read it
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="outreach_template_", delete=False, encoding="utf-8"
        ) as tmp_f:
            tmp_f.write(template_body)
            tmp_template_path = tmp_f.name
        ns.template = tmp_template_path

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
                if Path(sent_log_path).exists():
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

        st.subheader(f"Replies from leads ({len(lead_replies)})")
        if lead_replies:
            st.dataframe(pd.DataFrame(lead_replies), use_container_width=True)
        else:
            st.info("No replies from known leads found.")

        with st.expander(f"Other inbox messages ({len(other_msgs)})"):
            if other_msgs:
                st.dataframe(pd.DataFrame(other_msgs), use_container_width=True)
            else:
                st.write("None.")


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
                "SMTP Host", value=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                key=f"{key_prefix}_host",
            )
            from_email = st.text_input(
                "From Email", value=os.environ.get("EMAIL_ADDRESS", ""),
                key=f"{key_prefix}_email",
            )
            from_name = st.text_input("Sender Name", value="", key=f"{key_prefix}_fname")
        with fc2:
            smtp_port = st.number_input("SMTP Port", value=587, key=f"{key_prefix}_port")
            password = st.text_input(
                "Password / App Password", type="password",
                value=os.environ.get("EMAIL_PASSWORD", ""),
                key=f"{key_prefix}_pass",
            )
            use_ssl = st.checkbox("Use SSL (port 465)", value=False, key=f"{key_prefix}_ssl")

        subject = st.text_input("Subject", value=default_subject, key=f"{key_prefix}_subj")
        body = st.text_area("Email body", value=default_body, height=180, key=f"{key_prefix}_body")
        dry_run = st.checkbox("Dry run (preview only)", value=True, key=f"{key_prefix}_dry")

        submit = st.form_submit_button(
            f"▶ Send Follow-up #{sequence_num} ({len(due_df)} recipients)",
            type="primary",
        )

    if submit:
        be = importlib.import_module("bulk_emailer")
        import argparse as _ap

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="fu_tmpl_", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(body)
            tmp_path = tmp.name

        ns = _ap.Namespace(
            sequence_num=sequence_num,
            csv=LIVE_LEADS_CSV,
            template=tmp_path,
            subject=subject,
            from_name=from_name,
            smtp_host=smtp_host,
            smtp_port=int(smtp_port),
            ssl=use_ssl,
            email=from_email,
            password=password,
            log=SENT_LOG_CSV,
            unsubscribe=UNSUBSCRIBE_TXT,
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
            background: linear-gradient(135deg,#f97316 0%,#ef4444 60%,#a855f7 100%);
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
_render_sidebar()

tabs = st.tabs(["📊 Dashboard", "🔍 Scrape", "📋 Leads", "✉️ Compose & Send", "📅 Follow-ups", "📑 Sent Log", "💬 Replies", "🚫 Unsubscribes"])

with tabs[0]:
    tab_dashboard()

with tabs[1]:
    tab_scrape()

with tabs[2]:
    tab_leads()

with tabs[3]:
    tab_send()

with tabs[4]:
    tab_follow_ups()

with tabs[5]:
    tab_sent_log()

with tabs[6]:
    tab_replies()

with tabs[7]:
    tab_unsubscribe()
