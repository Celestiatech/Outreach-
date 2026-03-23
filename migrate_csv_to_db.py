"""
migrate_csv_to_db.py — One-time migration of legacy CSV/txt files into SQLite.

Run this ONCE to import your existing data:
    python migrate_csv_to_db.py

After migration the CSV files are no longer used by the app (kept as backup).
"""

from pathlib import Path
import db

FILES = {
    "leads.csv":       "leads",
    "live_leads.csv":  "live_leads",
    "sent_log.csv":    "sent_log",
    "replies_log.csv": "replies_log",
    "pipeline.csv":    "pipeline",
}

def main():
    print(f"Initialising SQLite database → {db.DB_PATH}")
    db.init_db()

    total = 0
    for csv_file, table in FILES.items():
        n = db.migrate_csv_if_exists(csv_file)
        if n:
            print(f"  ✓ {csv_file:25s} → {table:15s}  ({n} rows)")
            total += n
        elif Path(csv_file).exists():
            print(f"  ⚠ {csv_file:25s} → {table:15s}  (table already has data, skipped)")
        else:
            print(f"  – {csv_file:25s}  (file not found, skipped)")

    n = db.migrate_unsubscribe_txt_if_exists("unsubscribe.txt")
    if n:
        print(f"  ✓ {'unsubscribe.txt':25s} → {'unsubscribe':15s}  ({n} emails)")
    else:
        print(f"  – {'unsubscribe.txt':25s}  (not found or already migrated)")

    print(f"\nDone. {total} total rows imported into {db.DB_PATH}")
    print("Your CSV files are untouched and kept as backup.")

if __name__ == "__main__":
    main()
