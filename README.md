# Outreach Lead Generator 🎯

Automated tool to extract contact information from websites for cold outreach campaigns.

## Features ✅

- **Email extraction** — Find contact emails on websites
- **Phone detection** — Scrape phone numbers  
- **Social media links** — Extract LinkedIn, Twitter, Facebook, Instagram
- **Lead scoring** — Automatic quality scoring based on data richness
- **Website analysis** — Detect issues (missing titles, poor design indicators)
- **Batch processing** — Process multiple URLs from JSON config
- **CSV export** — Ready for email campaigns

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

### Option 1: Scrape URLs (Recommended - Fastest)

Edit `urls_to_scrape.json` and add your target websites:

```json
{
  "urls": [
    {
      "name": "Company Name",
      "url": "https://www.example.com"
    },
    {
      "name": "Another Company",
      "url": "https://www.anothercompany.com"
    }
  ],
  "settings": {
    "delay_between_sites": 1.5
  }
}
```

Then run:

```bash
python scrape_urls.py --config urls_to_scrape.json --output leads.csv
```

## Workflow 🚀

### Step 1: Get URLs

**Find businesses using these strategies:**

1. **Direct Search** — Google: "plumbers in Sydney", "dentists near me"
2. **Low-quality sites** — `site:.com "powered by wix"`
3. **Free builders** — `site:.com "free website builder"`  
4. **Business directories** — Search "business directory [city]"

Copy the URLs you want to analyze.

### Step 2: Add to Config

Edit [`urls_to_scrape.json`](urls_to_scrape.json):

```json
{
  "urls": [
    {"name": "Plumber ABC", "url": "https://plumberabc.com"},
    {"name": "Salon XYZ", "url": "https://salonxyz.com"}
  ]
}
```

### Step 3: Run Scraper

```bash
python scrape_urls.py --output leads.csv
```

### Step 4: Review Results

Open `leads.csv` — check the `lead_score` column:

- **Score ≥ 4** — Excellent (has email or phone)
- **Score 2-3** — Good (has social links)
- **Score < 0** — Poor (missing info)

## Output Fields

| Field | Description |
|-------|-------------|
| `keyword` | Company name from config |
| `url` | Website URL |
| `title` | Page title |
| `email` | ✅ Contact email |
| `phone` | ✅ Phone number |
| `linkedin` | LinkedIn profile URL |
| `twitter` | Twitter/X profile |
| `facebook` | Facebook page |
| `instagram` | Instagram profile |
| `contact_page` | Link to contact page |
| `issues` | Website problems detected |
| `lead_score` | Quality score (higher = better) |

## Scoring System

- `+3` — Email found ⭐⭐⭐ (Best!)
- `+2` — Phone found ⭐⭐
- `+1` — LinkedIn link
- `+1` — Any other social link
- `-1` per issue detected

## Tips 💡

1. **Target quality** — 10 great leads > 100 random leads
2. **Batch processing** — Add 30 URLs, run scraper, send 10 emails/day
3. **Low-quality sites** — Sites with issues are easier to convert (they NEED help!)
4. **Follow up** — Most replies come after 3-5 follow-ups
5. **Personalize** — Add 1-2 custom lines to each email

## Examples

### Find Plumbers Needing Help

```
1. Google: "plumbers in [city] site:.com"
2. Look for old designs, Wix sites, slow loading
3. Copy URLs → urls_to_scrape.json
4. python scrape_urls.py
5. Email the lowest scores (they need your help most!)
```

### Find Business Directories (30+ leads at once)

```
1. Google: "business directory Sydney" or "restaurants in NYC"
2. Open directory → copy all business links
3. Add to urls_to_scrape.json
4. Run scraper
5. Export top 20 by score
```

## Commands

```bash
# Basic usage (reads from urls_to_scrape.json)
python scrape_urls.py

# Specify config and output
python scrape_urls.py --config urls_to_scrape.json --output my_leads.csv

# Single keyword (may be blocked)
python bing_email_scraper.py --keyword "plumbers in Sydney" --results 5
```

## Files

- **`scrape_urls.py`** — Main scraper (recommended)
- **`bing_email_scraper.py`** — Scraper with search capability
- **`urls_to_scrape.json`** — Config with URLs to process
- **`search_patterns.json`** — Config for search queries (less reliable)
- **`leads.csv`** — Output file with extracted leads

## Troubleshooting

**No emails found?**
- Some sites hide emails behind contact forms
- Check `contact_page` field
- Visit manually if needed

**Getting rate-limited?**
- Increase `delay_between_sites` in config
- Default (1.5s) is respectful

**Slow performance?**
- Reduce number of URLs
- Run multiple batches

## Requirements

- Python 3.8+
- See `requirements.txt` for dependencies

---

**Happy outreaching! 🚀**