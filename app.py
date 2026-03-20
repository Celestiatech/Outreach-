"""
Outreach UI – Streamlit dashboard
----------------------------------
Wraps the existing scraper and bulk-emailer scripts in a browser-based UI.

Run:
    streamlit run app.py
"""

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

LEADS_CSV = "leads.csv"
SENT_LOG_CSV = "sent_log.csv"
UNSUBSCRIBE_TXT = "unsubscribe.txt"
EMAIL_TEMPLATE_TXT = "email_template.txt"

BING_FIELDNAMES = [
    "keyword", "url", "title", "email", "phone",
    "linkedin", "twitter", "facebook", "instagram",
    "contact_page", "issues", "lead_score",
]
MAPS_FIELDNAMES = [
    "keyword", "name", "address", "phone", "website", "rating", "reviews",
    "category", "email", "linkedin", "twitter", "facebook", "instagram",
    "contact_page", "issues", "lead_score",
]


def _read_csv(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(p, dtype=str).fillna("")


def _write_csv(path: str, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False)


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
        leads_df = _read_csv(LEADS_CSV)
        sent_df = _read_csv(SENT_LOG_CSV)

        total_leads = len(leads_df)
        unique_emails = (
            leads_df["email"].str.strip().replace("", pd.NA).dropna().nunique()
            if "email" in leads_df.columns else 0
        )
        sent_n = int((sent_df["status"] == "sent").sum()) if "status" in sent_df.columns else 0

        st.caption("📊 QUICK STATS")
        s1, s2 = st.columns(2)
        s1.metric("Leads", f"{total_leads:,}")
        s2.metric("Sent", f"{sent_n:,}")
        s1.metric("Emails", f"{unique_emails:,}")

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
            | 📑 Sent Log | Track sends |
            | 💬 Replies | Inbox check |
            | 🚫 Unsub | Opt-out list |
            """,
            unsafe_allow_html=False,
        )

        st.divider()
        st.caption("⚡ TIPS")
        st.info(
            "Run **Scrape** first to collect leads, then go to **Compose & Send** "
            "to send outreach emails.",
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

    leads_df = _read_csv(LEADS_CSV)
    sent_df = _read_csv(SENT_LOG_CSV)

    # --- KPI row ---
    k1, k2, k3, k4, k5 = st.columns(5)

    total_leads = len(leads_df)
    unique_emails = leads_df["email"].str.strip().replace("", pd.NA).dropna().nunique() if "email" in leads_df.columns else 0

    sent_count = int((sent_df["status"] == "sent").sum()) if "status" in sent_df.columns else 0
    failed_count = int((sent_df["status"] == "failed").sum()) if "status" in sent_df.columns else 0
    success_rate = f"{sent_count / (sent_count + failed_count) * 100:.0f}%" if (sent_count + failed_count) > 0 else "—"

    k1.metric("Total leads", f"{total_leads:,}")
    k2.metric("Unique emails", f"{unique_emails:,}")
    k3.metric("Emails sent", f"{sent_count:,}")
    k4.metric("Failed sends", f"{failed_count:,}")
    k5.metric("Success rate", success_rate)

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
            st.info("Run the **Scrape** tab first to populate leads.", icon="🔍")

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


# ---------------------------------------------------------------------------
# Tab: Unsubscribe Manager
# ---------------------------------------------------------------------------

def tab_unsubscribe() -> None:
    st.header("🚫 Unsubscribe Manager")
    st.caption("Manage opt-out addresses. These are permanently skipped on every send.")
    st.divider()

    unsub_path = Path(UNSUBSCRIBE_TXT)

    def _load() -> list[str]:
        if not unsub_path.exists():
            return []
        lines = unsub_path.read_text(encoding="utf-8").splitlines()
        return [ln.strip().lower() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

    def _save(addresses: list[str]) -> None:
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
        remove_set: set[str] = set()
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
    st.header("🔍 Scrape Leads")
    st.caption("Search Google Maps or Bing and extract business contact information automatically.")
    st.divider()

    col1, col2 = st.columns([2, 1])
    with col1:
        keyword = st.text_input("Search keyword", placeholder="e.g. digital agency London")
    with col2:
        num_results = st.number_input("Number of results", min_value=1, max_value=200, value=10, step=5)

    engine = st.radio("Search engine", ["Google Maps", "Bing"], horizontal=True)
    output_path = st.text_input("Save results to", value=LEADS_CSV)
    append = st.checkbox("Append to existing CSV (instead of overwrite)", value=True)

    run_btn = st.button("▶ Run Scraper", type="primary", disabled=not keyword.strip())

    log_box = st.empty()
    result_box = st.empty()

    if run_btn and keyword.strip():
        log_lines: list[str] = []
        log_q: queue.Queue = queue.Queue()

        # Attach queue handler to the root logger so scraper output is captured
        q_handler = _QueueHandler(log_q)
        q_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(q_handler)

        records: list[dict] = []
        error_holder: list[str] = []

        def _run() -> None:
            try:
                if engine == "Bing":
                    from bing_email_scraper import scrape, save_to_csv
                    records.extend(scrape(keyword=keyword.strip(), num_results=int(num_results)))
                    if records:
                        if append and Path(output_path).exists():
                            existing = _read_csv(output_path)
                            combined = pd.concat(
                                [existing, pd.DataFrame(records)], ignore_index=True
                            ).drop_duplicates()
                            _write_csv(output_path, combined)
                        else:
                            save_to_csv(records, output_path)
                else:
                    from google_maps_scraper import scrape as maps_scrape, save_to_csv as maps_save
                    records.extend(maps_scrape(keyword=keyword.strip(), num_results=int(num_results)))
                    if records:
                        if append and Path(output_path).exists():
                            existing = _read_csv(output_path)
                            combined = pd.concat(
                                [existing, pd.DataFrame(records)], ignore_index=True
                            ).drop_duplicates()
                            _write_csv(output_path, combined)
                        else:
                            maps_save(records, output_path)
            except ImportError as exc:
                error_holder.append(f"Missing dependency: {exc}. Run `pip install -r requirements.txt`.")
            except OSError as exc:
                error_holder.append(f"File error while saving results: {exc}")
            except Exception as exc:  # noqa: BLE001
                error_holder.append(f"Scraper error ({type(exc).__name__}): {exc}")

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        progress = st.progress(0, text="Scraping…")
        tick = 0
        while thread.is_alive():
            while not log_q.empty():
                log_lines.append(log_q.get_nowait())
            log_box.text_area("Live log", "\n".join(log_lines[-60:]), height=220, key=f"log_{tick}")
            tick += 1
            progress.progress(min(tick % 100, 99), text="Scraping…")
            time.sleep(0.5)

        # Drain remaining log messages
        while not log_q.empty():
            log_lines.append(log_q.get_nowait())
        root_logger.removeHandler(q_handler)

        progress.progress(100, text="Done")
        log_box.text_area("Live log", "\n".join(log_lines[-60:]), height=220, key="log_final")

        if error_holder:
            st.error(f"Scraper error: {error_holder[0]}")
        elif records:
            st.success(f"✅ Scraped {len(records)} row(s) → saved to `{output_path}`")
            df = pd.DataFrame(records)
            result_box.dataframe(df, use_container_width=True)

            csv_bytes = df.to_csv(index=False).encode()
            st.download_button(
                "⬇ Download results CSV",
                data=csv_bytes,
                file_name=Path(output_path).name,
                mime="text/csv",
            )
        else:
            st.warning("No records returned. Try a different keyword or engine.")


# ---------------------------------------------------------------------------
# Tab: Leads
# ---------------------------------------------------------------------------

def tab_leads() -> None:
    st.header("📋 Leads")
    st.caption("Browse, filter, and download your collected leads.")
    st.divider()

    uploaded = st.file_uploader("Upload a leads CSV", type="csv")
    if uploaded:
        df = pd.read_csv(uploaded, dtype=str).fillna("")
        st.session_state["leads_df"] = df
        st.success(f"Loaded {len(df)} rows from uploaded file.", icon="✅")
    elif Path(LEADS_CSV).exists():
        if st.button("📂 Load leads.csv from disk"):
            st.session_state["leads_df"] = _read_csv(LEADS_CSV)

    df: pd.DataFrame = st.session_state.get("leads_df", pd.DataFrame())
    if df.empty:
        st.info("No leads loaded yet. Upload a CSV or run the **Scrape** tab first.", icon="📂")
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
    st.dataframe(filtered, use_container_width=True)

    csv_bytes = filtered.to_csv(index=False).encode()
    st.download_button(
        "⬇ Download filtered CSV",
        data=csv_bytes,
        file_name="filtered_leads.csv",
        mime="text/csv",
    )

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
        leads_csv = st.text_input("Leads CSV path", value=LEADS_CSV)
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

        log_lines: list[str] = []
        log_q: queue.Queue = queue.Queue()
        q_handler = _QueueHandler(log_q)
        q_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(q_handler)

        error_holder: list[str] = []

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

        lead_replies: list[dict] = []
        other_msgs: list[dict] = []
        error_holder: list[str] = []

        import email as email_lib
        from email.utils import parseaddr

        def _fetch_replies() -> None:
            try:
                since_date = (
                    datetime.now(timezone.utc) - timedelta(days=int(since_days))
                ).strftime("%d-%b-%Y")

                sent_emails: set[str] = set()
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
# Main layout
# ---------------------------------------------------------------------------

_inject_css()
_render_sidebar()

tabs = st.tabs(["📊 Dashboard", "🔍 Scrape", "📋 Leads", "✉️ Compose & Send", "📑 Sent Log", "💬 Replies", "🚫 Unsubscribes"])

with tabs[0]:
    tab_dashboard()

with tabs[1]:
    tab_scrape()

with tabs[2]:
    tab_leads()

with tabs[3]:
    tab_send()

with tabs[4]:
    tab_sent_log()

with tabs[5]:
    tab_replies()

with tabs[6]:
    tab_unsubscribe()
