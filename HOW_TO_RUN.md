# How to Run the Outreach Scripts

This repository contains two lead-generation scrapers:

| Script | Source |
|---|---|
| `bing_email_scraper.py` | Searches Bing for a keyword and extracts contact info from result pages |
| `google_maps_scraper.py` | Searches Google Maps for a keyword and extracts business contact info |

Both scripts save results to an enriched CSV file ready for outreach campaigns.

---

## Prerequisites

- Python 3.10 or higher
- pip

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/Celestiatech/Outreach-.git
cd Outreach-
```

### 2. (Optional) Create and activate a virtual environment

```bash
python -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Playwright browsers (required for `google_maps_scraper.py` only)

```bash
playwright install chromium
```

---

## Running the Scripts

### Bing Email Scraper

Searches Bing for a keyword, visits each result page, and extracts emails, phone numbers, and social links.

```bash
python bing_email_scraper.py --keyword "digital agency UK" --results 20 --output leads.csv
```

**Arguments:**

| Argument | Required | Default | Description |
|---|---|---|---|
| `--keyword` | Yes | — | Search keyword or phrase |
| `--results` | No | `10` | Number of Bing result pages to inspect |
| `--output` | No | `leads.csv` | Path to the output CSV file |

**Example:**

```bash
python bing_email_scraper.py --keyword "web design agency London" --results 30 --output london_leads.csv
```

---

### Google Maps Scraper

Searches Google Maps for a keyword, extracts business listings, and visits each business website to collect contact details.

```bash
python google_maps_scraper.py --keyword "digital agency London" --results 50 --output leads.csv
```

**Arguments:**

| Argument | Required | Default | Description |
|---|---|---|---|
| `--keyword` | Yes | — | Search keyword or phrase |
| `--results` | No | `20` | Number of Google Maps results to inspect |
| `--output` | No | `leads.csv` | Path to the output CSV file |

**Example:**

```bash
python google_maps_scraper.py --keyword "plumber New York" --results 40 --output ny_plumbers.csv
```

---

## Output

Both scripts produce a CSV file with the following columns:

| Column | Description |
|---|---|
| `keyword` | The search keyword used |
| `url` / `website` | The business website URL |
| `title` / `name` | Page title or business name |
| `email` | Email address found |
| `phone` | Phone number found |
| `linkedin` | LinkedIn profile URL |
| `twitter` | Twitter / X profile URL |
| `facebook` | Facebook page URL |
| `instagram` | Instagram profile URL |
| `contact_page` | URL of the contact page |
| `issues` | Website issues detected (e.g. no SSL, missing meta description) |
| `lead_score` | Numeric quality score (higher is better) |

> **Note:** One row is written per (URL, email) pair. If no email is found, a row is still included with an empty email field.

---

---

## Bulk Email Sender (`bulk_emailer.py`)

Reads the leads CSV produced by either scraper, sends personalised outreach
emails via SMTP, logs every send attempt, and can check your inbox for replies.

All three SMTP / IMAP credentials can be supplied as CLI flags **or** as
environment variables (`SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST`, `IMAP_PORT`,
`EMAIL_ADDRESS`, `EMAIL_PASSWORD`).

> **Gmail users:** Enable 2-Step Verification, then create an
> [App Password](https://myaccount.google.com/apppasswords) and use that as
> the `--password` value. Your regular password will not work.

---

### 1. List all unique emails in a leads file

```bash
python bulk_emailer.py list --csv leads.csv
```

---

### 2. Preview emails without sending (dry run)

```bash
python bulk_emailer.py send \
    --csv leads.csv \
    --template email_template.txt \
    --subject "Quick question for {name}" \
    --from-name "Alice at Acme" \
    --smtp-host smtp.gmail.com \
    --email you@gmail.com \
    --password "your-app-password" \
    --dry-run
```

---

### 3. Send bulk emails

```bash
python bulk_emailer.py send \
    --csv leads.csv \
    --template email_template.txt \
    --subject "Quick question for {name}" \
    --from-name "Alice at Acme" \
    --smtp-host smtp.gmail.com \
    --email you@gmail.com \
    --password "your-app-password"
```

**Send arguments:**

| Argument | Required | Default | Description |
|---|---|---|---|
| `--csv` | No | `leads.csv` | Leads CSV file |
| `--template` | Yes | — | Path to email body template (plain text or HTML) |
| `--subject` | Yes | — | Subject line; supports `{name}`, `{url}`, any CSV column |
| `--from-name` | No | _(empty)_ | Sender display name |
| `--smtp-host` | Yes* | `SMTP_HOST` env | SMTP server hostname |
| `--smtp-port` | No | `587` | SMTP port (587 = STARTTLS, 465 = SSL) |
| `--ssl` | No | off | Use direct SSL on port 465 instead of STARTTLS |
| `--email` | Yes* | `EMAIL_ADDRESS` env | Sender email address |
| `--password` | Yes* | `EMAIL_PASSWORD` env | Email password / app-password |
| `--log` | No | `sent_log.csv` | Log file; already-sent addresses are skipped on re-runs |
| `--unsubscribe` | No | `unsubscribe.txt` | One email per line; those addresses are always skipped |
| `--delay` | No | `2.0` | Seconds between sends |
| `--html` | No | off | Treat template as HTML |
| `--dry-run` | No | off | Preview recipients without sending |

*Required unless the matching environment variable is set.

**Template placeholders** – use any column name from the CSV wrapped in
`{curly braces}`, e.g. `{name}`, `{email}`, `{url}`, `{phone}`. Unknown
placeholders are left unchanged.

**Tracking & deduplication** – each successful send is written to
`sent_log.csv`. If you run the script again (e.g. after adding more leads),
already-sent addresses are automatically skipped.

**Unsubscribes** – add an email address (one per line) to `unsubscribe.txt`
to permanently exclude it from all future sends.

---

### 4. Check for replies from leads

```bash
python bulk_emailer.py replies \
    --imap-host imap.gmail.com \
    --email you@gmail.com \
    --password "your-app-password"
```

Replies are matched against the addresses in `sent_log.csv` so you can see
at a glance which leads have written back.

**Replies arguments:**

| Argument | Required | Default | Description |
|---|---|---|---|
| `--imap-host` | Yes* | `IMAP_HOST` env | IMAP server hostname |
| `--imap-port` | No | `993` | IMAP port |
| `--email` | Yes* | `EMAIL_ADDRESS` env | Your email address |
| `--password` | Yes* | `EMAIL_PASSWORD` env | Email password / app-password |
| `--log` | No | `sent_log.csv` | Sent log used to identify lead replies |
| `--folder` | No | `INBOX` | IMAP folder to check |
| `--since` | No | `30` | How many days back to look |

---

### Email template

Edit `email_template.txt` to customise the message body. Placeholders like
`{name}`, `{url}`, `{phone}` are replaced with values from each row of the
leads CSV.

**Common SMTP / IMAP settings:**

| Provider | SMTP host | SMTP port | IMAP host | IMAP port |
|---|---|---|---|---|
| Gmail | `smtp.gmail.com` | `587` | `imap.gmail.com` | `993` |
| Outlook / Hotmail | `smtp.office365.com` | `587` | `outlook.office365.com` | `993` |
| Yahoo Mail | `smtp.mail.yahoo.com` | `587` | `imap.mail.yahoo.com` | `993` |

---

## Streamlit Dashboard (app.py)

A browser-based UI that wraps all of the scripts above. Run it with:

```bash
streamlit run app.py
```

The dashboard opens automatically in your browser at `http://localhost:8501` and provides seven tabs:

| Tab | What it does |
|---|---|
| 📊 **Dashboard** | Key metrics (total leads, emails sent/failed, success rate), lead-score chart, sends-over-time chart, category breakdown |
| 🔍 **Scrape** | Run the Bing or Google Maps scraper with a live log stream; results preview + CSV download |
| 📋 **Leads** | Upload or load `leads.csv`; filter by score / category / has-email; score distribution and category charts; download filtered CSV |
| ✉️ **Compose & Send** | SMTP config, inline template editor, dry-run or real send; live progress log |
| 📑 **Sent Log** | View `sent_log.csv` with summary metrics (sent, failed, success rate) and a sends-per-day chart |
| 💬 **Replies** | IMAP inbox check surfacing replies from known leads vs. other messages |
| 🚫 **Unsubscribes** | View, add, bulk-add, and remove addresses from `unsubscribe.txt` |



```bash
python -m pytest test_google_maps_scraper.py
```

---

## Troubleshooting

- **Bing / Google blocks requests** – The scrapers use realistic browser headers and add polite delays between requests. If you are still blocked, try reducing `--results` or adding a longer delay by editing `REQUEST_DELAY` at the top of the script.
- **Playwright browser not found** – Run `playwright install chromium` to install the required browser.
- **SSL certificate errors** – Ensure your Python environment has up-to-date CA certificates (`pip install --upgrade certifi`).
