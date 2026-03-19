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

## Running the Tests

```bash
python -m pytest test_google_maps_scraper.py
```

---

## Troubleshooting

- **Bing / Google blocks requests** – The scrapers use realistic browser headers and add polite delays between requests. If you are still blocked, try reducing `--results` or adding a longer delay by editing `REQUEST_DELAY` at the top of the script.
- **Playwright browser not found** – Run `playwright install chromium` to install the required browser.
- **SSL certificate errors** – Ensure your Python environment has up-to-date CA certificates (`pip install --upgrade certifi`).
