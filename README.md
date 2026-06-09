# cd-parser

Parses mortgage Closing Disclosures (including scanned PDFs, via OCR) and outputs a CSV. Pulls CD attachments straight from Airtable.

## What it extracts

- Loan amount, loan ID, closing date, disbursement date
- Settlement agent contact info (name, address, contact, email, phone)
- Every fee paid to Multiply (direct or FBO), each tagged with its column on the CD: `borrower_at`, `borrower_before`, `seller_at`, `seller_before`, or `paid_by_others`, plus a `lender_paid` flag when the fee is marked "(L)"

## Install

```bash
pip install -r requirements.txt
# plus system packages:
#   macOS:  brew install tesseract poppler
#   Ubuntu: sudo apt install tesseract-ocr poppler-utils
#   Windows: install Tesseract (UB Mannheim build) and Poppler, add both to PATH
```

## Usage

Parse local PDFs:

```bash
python cd_parser.py /path/to/cds -o results.csv
python cd_parser.py one.pdf two.pdf -o results.csv
```

Pull from Airtable and parse in one step:

```bash
export AIRTABLE_TOKEN=pat...   # token with data.records:read scope
export AIRTABLE_BASE=appXXXXXXXXXXXXXX
export AIRTABLE_TABLE="Loans"
export AIRTABLE_FIELD="Closing Disclosure"

python airtable_fetch.py --parse --csv results.csv
```

Downloaded files are named `<recordId>__<filename>.pdf`, so each CSV row can be traced back to its Airtable record. Already-downloaded files are skipped, so re-running only fetches new CDs. Use `--view "ViewName"` to restrict to a view.

## Output columns

`file, loan_id, loan_amount, closing_date, disbursement_date, multiply_fee_total, multiply_fee_total_borrower, multiply_fee_total_lender, multiply_fees_json, settlement_agent_name, settlement_agent_address, settlement_agent_contact, settlement_agent_email, settlement_agent_phone`

`multiply_fees_json` holds the itemized fees: description, amount, column, lender_paid.

## Notes / limitations

- Scanned CDs are OCR'd at 300 DPI with Tesseract. Column assignment uses word x-coordinates against the detected Borrower-Paid / Seller-Paid / Paid By Others headers, with a fallback detector for low-quality scans.
- OCR digit misreads are possible on poor scans. The loan amount is cross-checked across pages 1 and 3 (majority vote), but spot-check fee amounts on very low-quality documents.
- Writing results back to Airtable fields is a straightforward extension of `airtable_fetch.py` (PATCH to the records endpoint) — not yet implemented.
