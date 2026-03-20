"""
Bulk Email Sender & Reply Checker
Reads a leads CSV produced by the scrapers, sends personalised outreach
emails via SMTP, logs every send attempt, and can check your inbox for
replies from leads.

Sub-commands
~~~~~~~~~~~~
list     – Print all unique email addresses in a leads CSV.
send     – Send bulk emails to the leads (skips already-sent and
           unsubscribed addresses).
replies  – Connect via IMAP and list messages from your leads.

Usage – list:
    python bulk_emailer.py list --csv leads.csv

Usage – send:
    python bulk_emailer.py send \\
        --csv leads.csv \\
        --template email_template.txt \\
        --subject "Quick question for {name}" \\
        --from-name "Your Name" \\
        --smtp-host smtp.gmail.com \\
        --smtp-port 587 \\
        --email you@gmail.com \\
        --password "your-app-password"

Usage – replies:
    python bulk_emailer.py replies \\
        --imap-host imap.gmail.com \\
        --email you@gmail.com \\
        --password "your-app-password"

Environment variables (alternative to CLI flags):
    SMTP_HOST, SMTP_PORT, IMAP_HOST, IMAP_PORT,
    EMAIL_ADDRESS, EMAIL_PASSWORD
"""

from __future__ import annotations

import argparse
import csv
import email as email_lib
import imaplib
import logging
import os
import smtplib
import ssl
import string
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENT_LOG_FIELDNAMES = ["timestamp", "to_email", "to_name", "subject", "status", "error", "sequence_num"]

# Follow-up schedule: sequence number → minimum days since the PREVIOUS send.
# seq 1 = Day-3 follow-up (3 days after first email)
# seq 2 = Day-7 final     (4 days after Day-3 follow-up = 7 days total)
FOLLOW_UP_SCHEDULE: dict[int, int] = {1: 3, 2: 4}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_unsubscribe_list(path: str) -> set[str]:
    """Return the set of unsubscribed email addresses from *path*."""
    p = Path(path)
    if not p.exists():
        return set()
    emails: set[str] = set()
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lower()
            if line and not line.startswith("#"):
                emails.add(line)
    return emails


def _load_sent_emails(log_path: str) -> set[str]:
    """Return the set of emails that were successfully sent according to the log."""
    p = Path(log_path)
    if not p.exists():
        return set()
    sent: set[str] = set()
    with p.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("status") == "sent":
                sent.add(row["to_email"].lower().strip())
    return sent


def _migrate_sent_log(path: str) -> None:
    """
    Add the ``sequence_num`` column to an existing sent log that pre-dates it.

    This is a one-time, idempotent migration: if the column is already present
    the function returns immediately without touching the file.
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return
    with p.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if "sequence_num" in (reader.fieldnames or []):
            return  # already migrated
        rows = list(reader)
    for row in rows:
        row.setdefault("sequence_num", "0")
    with p.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SENT_LOG_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Migrated sent log '%s': added sequence_num column.", path)


def _append_sent_log(log_path: str, record: dict) -> None:
    """Append one *record* to the sent log CSV, writing a header if needed."""
    _migrate_sent_log(log_path)
    p = Path(log_path)
    file_exists = p.exists() and p.stat().st_size > 0
    with p.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SENT_LOG_FIELDNAMES, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow({**{f: "" for f in SENT_LOG_FIELDNAMES}, **record})


def _get_display_name(row: dict) -> str:
    """Return the best human-readable name from a CSV row."""
    # google_maps_scraper uses 'name'; bing_email_scraper uses 'title'
    name = row.get("name") or row.get("title") or ""
    if name:
        return name
    # Fall back to the website domain
    url = row.get("website") or row.get("url") or ""
    if url:
        try:
            netloc = urlparse(url).netloc
            if netloc:
                return netloc
        except ValueError:
            pass
    email = row.get("email", "")
    return email.split("@")[-1] if "@" in email else "there"


def _render_template(template: str, row: dict) -> str:
    """
    Substitute ``{column}`` placeholders in *template* using values from *row*.

    Unknown placeholders are left unchanged instead of raising a KeyError.
    """
    safe = {k: (v or "") for k, v in row.items()}
    if "name" not in safe:
        safe["name"] = _get_display_name(row)

    formatter = string.Formatter()
    parts: list[str] = []
    for literal_text, field_name, _format_spec, _conversion in formatter.parse(template):
        parts.append(literal_text)
        if field_name is not None:
            parts.append(str(safe.get(field_name, "{" + field_name + "}")))
    return "".join(parts)


def _build_message(
    from_email: str,
    from_name: str,
    to_email: str,
    subject: str,
    body: str,
    is_html: bool = False,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html" if is_html else "plain", "utf-8"))
    return msg


def _read_leads_csv(csv_path: str) -> list[dict]:
    """Return all rows from *csv_path* as a list of dicts."""
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> None:
    """Print all unique email addresses found in the leads CSV."""
    rows = _read_leads_csv(args.csv)
    seen: set[str] = set()
    count = 0
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        name = _get_display_name(row)
        print(f"{email:<45}  {name}")
        count += 1
    print(f"\nTotal unique emails: {count}")


def cmd_send(args: argparse.Namespace) -> None:
    """Send bulk outreach emails to the leads in the CSV."""
    # --- Load template ---
    template_path = Path(args.template)
    if not template_path.exists():
        logger.error("Template file not found: %s", args.template)
        raise SystemExit(1)
    template_body = template_path.read_text(encoding="utf-8")

    # --- Load control lists ---
    unsubscribed = _load_unsubscribe_list(args.unsubscribe)
    already_sent = _load_sent_emails(args.log)
    logger.info("%d unsubscribed address(es) loaded.", len(unsubscribed))
    logger.info("%d address(es) already sent – will skip.", len(already_sent))

    # --- Resolve credentials ---
    smtp_host = args.smtp_host or os.environ.get("SMTP_HOST", "")
    smtp_port = args.smtp_port if args.smtp_port is not None else int(os.environ.get("SMTP_PORT", "587"))
    from_email = args.email or os.environ.get("EMAIL_ADDRESS", "")
    password = args.password or os.environ.get("EMAIL_PASSWORD", "")

    if not (smtp_host and from_email and password):
        logger.error(
            "SMTP host, sender email, and password are all required. "
            "Pass them via CLI flags or set SMTP_HOST / EMAIL_ADDRESS / EMAIL_PASSWORD."
        )
        raise SystemExit(1)

    # --- Build recipient list ---
    rows = _read_leads_csv(args.csv)
    seen_emails: set[str] = set()
    recipients: list[dict] = []
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        if not email:
            continue
        if email in unsubscribed:
            logger.info("Skipping unsubscribed: %s", email)
            continue
        if email in already_sent:
            logger.info("Skipping already sent: %s", email)
            continue
        if email in seen_emails:
            continue
        seen_emails.add(email)
        recipients.append(row)

    if not recipients:
        logger.info("No new recipients to send to.")
        return


    logger.info("Preparing to send to %d address(es).", len(recipients))

    # --- Dry-run preview ---
    if args.dry_run:
        for row in recipients:
            name = _get_display_name(row)
            email = row["email"].strip()
            safe = {**row, "name": name}
            subject = _render_template(args.subject, safe)
            logger.info("[DRY RUN] → %-45s <%s> | %s", name, email, subject)
        logger.info("[DRY RUN] Would send to %d address(es). No emails sent.", len(recipients))
        return

    # --- Send ---
    context = ssl.create_default_context()
    sent_count = 0
    failed_count = 0

    try:
        if args.ssl:
            smtp_connection = smtplib.SMTP_SSL(smtp_host, smtp_port, context=context)
        else:
            smtp_connection = smtplib.SMTP(smtp_host, smtp_port)

        with smtp_connection as server:
            if not args.ssl:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            server.login(from_email, password)

            for row in recipients:
                email = row["email"].strip()
                name = _get_display_name(row)
                safe = {**row, "name": name, "from_name": args.from_name}

                subject = _render_template(args.subject, safe)
                body = _render_template(template_body, safe)
                msg = _build_message(from_email, args.from_name, email, subject, body, args.html)

                log_record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "to_email": email,
                    "to_name": name,
                    "subject": subject,
                    "status": "",
                    "error": "",
                    "sequence_num": "0",
                }

                try:
                    server.sendmail(from_email, email, msg.as_string())
                    logger.info("Sent  → %s <%s>", name, email)
                    log_record["status"] = "sent"
                    sent_count += 1
                except smtplib.SMTPException as exc:
                    logger.error("Failed → %s <%s>: %s", name, email, exc)
                    log_record["status"] = "failed"
                    log_record["error"] = str(exc)
                    failed_count += 1

                _append_sent_log(args.log, log_record)
                time.sleep(args.delay)

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed. "
            "For Gmail, create an App Password at https://myaccount.google.com/apppasswords"
        )
        raise SystemExit(1)
    except (smtplib.SMTPConnectError, OSError) as exc:
        logger.error("Cannot connect to SMTP server %s:%s – %s", smtp_host, smtp_port, exc)
        raise SystemExit(1)

    logger.info("Done. Sent: %d  Failed: %d", sent_count, failed_count)


def cmd_follow_up(args: argparse.Namespace) -> None:
    """
    Send follow-up emails to contacts that are due for the next sequence step.

    The follow-up schedule is:
    * sequence_num = 1 → Day-3 follow-up (≥ 3 days since last send)
    * sequence_num = 2 → Day-7 final     (≥ 4 days since sequence 1 send)
    """
    from collections import defaultdict

    target_seq: int = args.sequence_num
    days_needed: int = FOLLOW_UP_SCHEDULE.get(target_seq, 3)

    # ── Read sent log ────────────────────────────────────────────────────────
    p = Path(args.log)
    if not p.exists():
        logger.error("Sent log not found: %s", args.log)
        raise SystemExit(1)

    # Build per-email history: email → list of {sequence_num, timestamp, to_name}
    history: dict[str, list[dict]] = defaultdict(list)
    with p.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("status") == "sent":
                email = row["to_email"].lower().strip()
                history[email].append({
                    "sequence_num": int(row.get("sequence_num") or 0),
                    "timestamp": row.get("timestamp", ""),
                    "to_name": row.get("to_name", ""),
                })

    # ── Identify contacts due for this follow-up ─────────────────────────────
    now = datetime.now(timezone.utc)
    due: list[dict] = []

    for email, entries in history.items():
        max_seq = max(e["sequence_num"] for e in entries)
        if max_seq >= target_seq:
            continue  # already received this follow-up (or a later one)

        # Find the most recent *sent* entry (any sequence)
        entries_sorted = sorted(entries, key=lambda e: e["timestamp"], reverse=True)
        recent = entries_sorted[0]
        ts_str = recent["timestamp"]
        try:
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            sent_at = datetime.fromisoformat(ts_str)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue

        days_since = (now - sent_at).days
        if days_since >= days_needed:
            due.append({
                "email": email,
                "to_name": recent["to_name"],
                "days_since": days_since,
            })

    if not due:
        logger.info(
            "No contacts due for follow-up #%d (threshold: %d days). Nothing to send.",
            target_seq, days_needed,
        )
        return

    logger.info("%d contact(s) due for follow-up #%d.", len(due), target_seq)

    # ── Load template ────────────────────────────────────────────────────────
    template_path = Path(args.template)
    if not template_path.exists():
        logger.error("Template file not found: %s", args.template)
        raise SystemExit(1)
    template_body = template_path.read_text(encoding="utf-8")

    # ── Load leads CSV for personalisation enrichment ────────────────────────
    lead_lookup: dict[str, dict] = {}
    if args.csv and Path(args.csv).exists():
        for row in _read_leads_csv(args.csv):
            e = (row.get("email") or "").lower().strip()
            if e:
                lead_lookup[e] = row

    # ── Load unsubscribe list ────────────────────────────────────────────────
    unsubscribed = _load_unsubscribe_list(args.unsubscribe)

    # ── Resolve SMTP credentials ─────────────────────────────────────────────
    smtp_host = args.smtp_host or os.environ.get("SMTP_HOST", "")
    smtp_port = args.smtp_port if args.smtp_port is not None else int(os.environ.get("SMTP_PORT", "587"))
    from_email = args.email or os.environ.get("EMAIL_ADDRESS", "")
    password = args.password or os.environ.get("EMAIL_PASSWORD", "")

    if not (smtp_host and from_email and password):
        logger.error(
            "SMTP host, sender email, and password are all required for follow-up sends."
        )
        raise SystemExit(1)

    # ── Dry-run preview ──────────────────────────────────────────────────────
    if args.dry_run:
        for item in due:
            logger.info(
                "[DRY RUN] Follow-up #%d → %-45s <%s> (day %d)",
                target_seq, item["to_name"], item["email"], item["days_since"],
            )
        logger.info("[DRY RUN] Would send %d follow-up(s). No emails sent.", len(due))
        return

    # ── Send ─────────────────────────────────────────────────────────────────
    context = ssl.create_default_context()
    sent_count = 0
    failed_count = 0

    try:
        if args.ssl:
            smtp_conn = smtplib.SMTP_SSL(smtp_host, smtp_port, context=context)
        else:
            smtp_conn = smtplib.SMTP(smtp_host, smtp_port)

        with smtp_conn as server:
            if not args.ssl:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            server.login(from_email, password)

            for item in due:
                email = item["email"]
                if email in unsubscribed:
                    logger.info("Skipping unsubscribed: %s", email)
                    continue

                # Enrich template data from the leads CSV when available
                row = lead_lookup.get(email, {})
                row.setdefault("email", email)
                row.setdefault("name", item["to_name"] or email.split("@")[0])
                safe = {**row, "name": row["name"], "from_name": args.from_name}

                subject = _render_template(args.subject, safe)
                body = _render_template(template_body, safe)
                msg = _build_message(from_email, args.from_name, email, subject, body, args.html)

                log_record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "to_email": email,
                    "to_name": item["to_name"],
                    "subject": subject,
                    "status": "",
                    "error": "",
                    "sequence_num": str(target_seq),
                }

                try:
                    server.sendmail(from_email, email, msg.as_string())
                    logger.info("Follow-up #%d sent → %s <%s>", target_seq, item["to_name"], email)
                    log_record["status"] = "sent"
                    sent_count += 1
                except smtplib.SMTPException as exc:
                    logger.error("Failed → %s: %s", email, exc)
                    log_record["status"] = "failed"
                    log_record["error"] = str(exc)
                    failed_count += 1

                _append_sent_log(args.log, log_record)
                time.sleep(args.delay)

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed. "
            "For Gmail, create an App Password at https://myaccount.google.com/apppasswords"
        )
        raise SystemExit(1)
    except (smtplib.SMTPConnectError, OSError) as exc:
        logger.error("Cannot connect to SMTP server %s:%s – %s", smtp_host, smtp_port, exc)
        raise SystemExit(1)

    logger.info("Done. Follow-up #%d sent: %d  Failed: %d", target_seq, sent_count, failed_count)



    """Connect via IMAP and show messages from lead email addresses."""
    imap_host = args.imap_host or os.environ.get("IMAP_HOST", "")
    imap_port = args.imap_port if args.imap_port is not None else int(os.environ.get("IMAP_PORT", "993"))
    from_email = args.email or os.environ.get("EMAIL_ADDRESS", "")
    password = args.password or os.environ.get("EMAIL_PASSWORD", "")

    if not (imap_host and from_email and password):
        logger.error(
            "IMAP host, email address, and password are all required. "
            "Pass them via CLI flags or set IMAP_HOST / EMAIL_ADDRESS / EMAIL_PASSWORD."
        )
        raise SystemExit(1)

    # Load lead email addresses from sent log for matching
    sent_emails: set[str] = set()
    if args.log and Path(args.log).exists():
        with open(args.log, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") == "sent":
                    sent_emails.add(row["to_email"].lower().strip())

    logger.info("Loaded %d sent address(es) to match against replies.", len(sent_emails))

    since_date = (
        datetime.now(timezone.utc) - timedelta(days=args.since)
    ).strftime("%d-%b-%Y")

    try:
        with imaplib.IMAP4_SSL(imap_host, imap_port) as imap:
            imap.login(from_email, password)
            imap.select(args.folder, readonly=True)

            _, data = imap.search(None, f'SINCE "{since_date}"')
            msg_ids = data[0].split()
            logger.info(
                "Found %d message(s) in '%s' since %s.",
                len(msg_ids),
                args.folder,
                since_date,
            )

            lead_replies: list[dict] = []
            other_messages: list[dict] = []

            for msg_id in msg_ids:
                _, msg_data = imap.fetch(msg_id, "(RFC822)")
                raw_bytes = msg_data[0][1]
                if not isinstance(raw_bytes, bytes):
                    continue

                parsed = email_lib.message_from_bytes(raw_bytes)
                from_name, from_addr = parseaddr(parsed.get("From", ""))
                from_addr = from_addr.lower().strip()
                subject = parsed.get("Subject", "(no subject)")
                date = parsed.get("Date", "")

                entry = {
                    "from": from_addr,
                    "name": from_name or from_addr,
                    "subject": subject,
                    "date": date,
                }

                if from_addr in sent_emails:
                    lead_replies.append(entry)
                else:
                    other_messages.append(entry)

    except imaplib.IMAP4.error as exc:
        logger.error("IMAP error: %s", exc)
        raise SystemExit(1)

    print(f"\n{'='*60}")
    print(f"  Replies from leads ({len(lead_replies)})")
    print(f"{'='*60}")
    for r in lead_replies:
        print(f"  [{r['date']}]")
        print(f"  From:    {r['name']} <{r['from']}>")
        print(f"  Subject: {r['subject']}")
        print()

    if not lead_replies:
        print("  (none)")

    print(f"\n{'='*60}")
    print(f"  Other inbox messages ({len(other_messages)})")
    print(f"{'='*60}")
    for r in other_messages:
        print(f"  [{r['date']}] {r['name']} <{r['from']}> — {r['subject']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bulk Email Sender & Reply Checker – "
            "send outreach emails to leads and track replies."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- list ----
    list_p = sub.add_parser("list", help="List all unique emails in a leads CSV.")
    list_p.add_argument(
        "--csv", default="leads.csv",
        help="Path to the leads CSV file (default: leads.csv).",
    )

    # ---- send ----
    send_p = sub.add_parser("send", help="Send bulk outreach emails to leads.")
    send_p.add_argument(
        "--csv", default="leads.csv",
        help="Path to the leads CSV file (default: leads.csv).",
    )
    send_p.add_argument(
        "--template", required=True,
        help="Path to the email body template file (plain text or HTML).",
    )
    send_p.add_argument(
        "--subject", required=True,
        help="Email subject line. Supports placeholders like {name}, {company}.",
    )
    send_p.add_argument(
        "--from-name", default="",
        help="Sender display name (e.g. 'Alice at Acme').",
    )
    send_p.add_argument(
        "--smtp-host", default="",
        help="SMTP server hostname (or set SMTP_HOST env var).",
    )
    send_p.add_argument(
        "--smtp-port", type=int, default=587,
        help="SMTP port (default: 587 for STARTTLS; use 465 with --ssl).",
    )
    send_p.add_argument(
        "--ssl", action="store_true",
        help="Use direct SSL (port 465) instead of STARTTLS (port 587).",
    )
    send_p.add_argument(
        "--email", default="",
        help="Sender email address (or set EMAIL_ADDRESS env var).",
    )
    send_p.add_argument(
        "--password", default="",
        help="Email password or app-password (or set EMAIL_PASSWORD env var).",
    )
    send_p.add_argument(
        "--log", default="sent_log.csv",
        help="Sent-log CSV file (default: sent_log.csv). Already-sent addresses are skipped.",
    )
    send_p.add_argument(
        "--unsubscribe", default="unsubscribe.txt",
        help="File of unsubscribed addresses, one per line (default: unsubscribe.txt).",
    )
    send_p.add_argument(
        "--delay", type=float, default=2.0,
        help="Seconds to wait between sends (default: 2.0).",
    )
    send_p.add_argument(
        "--html", action="store_true",
        help="Treat the template as HTML rather than plain text.",
    )
    send_p.add_argument(
        "--dry-run", action="store_true",
        help="Preview which emails would be sent without actually sending anything.",
    )

    # ---- replies ----
    rep_p = sub.add_parser("replies", help="Check inbox for replies from leads.")
    rep_p.add_argument(
        "--imap-host", default="",
        help="IMAP server hostname (or set IMAP_HOST env var).",
    )
    rep_p.add_argument(
        "--imap-port", type=int, default=993,
        help="IMAP port (default: 993).",
    )
    rep_p.add_argument(
        "--email", default="",
        help="Your email address (or set EMAIL_ADDRESS env var).",
    )
    rep_p.add_argument(
        "--password", default="",
        help="Email password or app-password (or set EMAIL_PASSWORD env var).",
    )
    rep_p.add_argument(
        "--log", default="sent_log.csv",
        help="Sent-log CSV used to identify lead replies (default: sent_log.csv).",
    )
    rep_p.add_argument(
        "--folder", default="INBOX",
        help="IMAP folder to check (default: INBOX).",
    )
    rep_p.add_argument(
        "--since", type=int, default=30,
        help="Look back this many days (default: 30).",
    )

    # ---- follow_up ----
    fu_p = sub.add_parser(
        "follow_up",
        help="Send follow-up emails to contacts due for the next sequence step.",
    )
    fu_p.add_argument(
        "--sequence-num", type=int, default=1, choices=[1, 2],
        dest="sequence_num",
        help="Which follow-up to send: 1=Day-3, 2=Day-7 final (default: 1).",
    )
    fu_p.add_argument(
        "--csv", default="live_leads.csv",
        help="Leads CSV used for personalisation enrichment (default: live_leads.csv).",
    )
    fu_p.add_argument(
        "--template", required=True,
        help="Path to the follow-up email body template file.",
    )
    fu_p.add_argument(
        "--subject", required=True,
        help="Email subject line. Supports {name} and other CSV placeholders.",
    )
    fu_p.add_argument(
        "--from-name", default="",
        help="Sender display name.",
    )
    fu_p.add_argument("--smtp-host", default="", help="SMTP server hostname.")
    fu_p.add_argument("--smtp-port", type=int, default=587, help="SMTP port.")
    fu_p.add_argument("--ssl", action="store_true", help="Use direct SSL (port 465).")
    fu_p.add_argument("--email", default="", help="Sender email address.")
    fu_p.add_argument("--password", default="", help="Email password or app-password.")
    fu_p.add_argument(
        "--log", default="sent_log.csv",
        help="Sent-log CSV (default: sent_log.csv). Used to determine who is due.",
    )
    fu_p.add_argument(
        "--unsubscribe", default="unsubscribe.txt",
        help="Unsubscribe list (default: unsubscribe.txt).",
    )
    fu_p.add_argument(
        "--delay", type=float, default=2.0,
        help="Seconds to wait between sends (default: 2.0).",
    )
    fu_p.add_argument("--html", action="store_true", help="Send as HTML email.")
    fu_p.add_argument(
        "--dry-run", action="store_true",
        help="Preview who would receive a follow-up without actually sending.",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "send":
        cmd_send(args)
    elif args.command == "replies":
        cmd_replies(args)
    elif args.command == "follow_up":
        cmd_follow_up(args)


if __name__ == "__main__":
    main()
