#!/usr/bin/env python3
"""Download Closing Disclosure PDF attachments from an Airtable table.

Pulls every attachment from the configured attachment field and saves it
to a local folder, then (optionally) runs cd_parser on the folder.

Setup:
  1. Create a personal access token at https://airtable.com/create/tokens
     with scopes: data.records:read, schema.bases:read (for your base).
  2. Set environment variables (or pass flags):
       AIRTABLE_TOKEN   - the PAT
       AIRTABLE_BASE    - base ID (starts with "app", visible in the base URL)
       AIRTABLE_TABLE   - table name or ID (starts with "tbl")
       AIRTABLE_FIELD   - name of the attachment field holding the CD PDFs

Usage:
  python airtable_fetch.py                          # download to ./downloads
  python airtable_fetch.py --out downloads --parse  # download then parse to CSV
  python airtable_fetch.py --view "Unparsed"        # only records in a view
"""

import argparse
import os
import re
import sys
import urllib.parse
import urllib.request
import json
from pathlib import Path

API = "https://api.airtable.com/v0"


def req(url, token):
    r = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(r) as resp:
        return json.load(resp)


def list_records(token, base, table, view=None):
    records, offset = [], None
    while True:
        params = {"pageSize": "100"}
        if view:
            params["view"] = view
        if offset:
            params["offset"] = offset
        url = f"{API}/{base}/{urllib.parse.quote(table)}?{urllib.parse.urlencode(params)}"
        data = req(url, token)
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return records


def safe_name(s):
    return re.sub(r"[^\w.\-]+", "_", s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("AIRTABLE_TOKEN"))
    ap.add_argument("--base", default=os.environ.get("AIRTABLE_BASE"))
    ap.add_argument("--table", default=os.environ.get("AIRTABLE_TABLE"))
    ap.add_argument("--field", default=os.environ.get("AIRTABLE_FIELD"),
                    help="attachment field name containing the CD PDFs")
    ap.add_argument("--view", default=None, help="optional view name to filter records")
    ap.add_argument("--out", default="downloads", help="download folder")
    ap.add_argument("--parse", action="store_true",
                    help="run cd_parser on the downloaded folder afterward")
    ap.add_argument("--csv", default="cd_results.csv", help="output CSV when --parse")
    args = ap.parse_args()

    missing = [n for n, v in [("token", args.token), ("base", args.base),
                              ("table", args.table), ("field", args.field)] if not v]
    if missing:
        sys.exit(f"Missing required settings: {', '.join(missing)} "
                 "(set AIRTABLE_TOKEN / AIRTABLE_BASE / AIRTABLE_TABLE / AIRTABLE_FIELD "
                 "or pass --token/--base/--table/--field)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    records = list_records(args.token, args.base, args.table, args.view)
    n = 0
    for rec in records:
        for att in rec.get("fields", {}).get(args.field, []) or []:
            if not att.get("filename", "").lower().endswith(".pdf"):
                continue
            # prefix with record id so results can be matched back to Airtable
            dest = out / f"{rec['id']}__{safe_name(att['filename'])}"
            if dest.exists():
                continue
            print(f"Downloading {dest.name}", file=sys.stderr)
            urllib.request.urlretrieve(att["url"], dest)
            n += 1
    print(f"Downloaded {n} new PDF(s) to {out}/ "
          f"({len(records)} records scanned)", file=sys.stderr)

    if args.parse:
        import subprocess
        subprocess.run([sys.executable,
                        str(Path(__file__).parent / "cd_parser.py"),
                        str(out), "-o", args.csv], check=True)


if __name__ == "__main__":
    main()
