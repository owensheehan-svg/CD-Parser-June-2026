#!/usr/bin/env python3
"""Closing Disclosure (CD) parser.

Extracts from each CD PDF (text-layer or scanned, via Tesseract OCR):
  - Loan amount, loan ID, closing date, disbursement date
  - Settlement agent contact info (name, address, contact, email, phone)
  - All fees paid to Multiply (direct or FBO), with the column each amount
    sits in: borrower_at / borrower_before / seller_at / seller_before /
    paid_by_others, and a lender-paid flag when "(L)" is present.

Output: CSV, one row per PDF (itemized fees as JSON in multiply_fees_json).

Requirements:
  pip install pdfplumber pytesseract pillow
  system: tesseract-ocr, poppler-utils (pdftoppm)

Usage:
  python cd_parser.py file1.pdf file2.pdf -o results.csv
  python cd_parser.py /path/to/folder -o results.csv
"""
import argparse, csv, json, re, subprocess, sys, tempfile
from pathlib import Path
import pdfplumber, pytesseract
from PIL import Image

DPI = 300
MONEY_RE = re.compile(r"^-?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})$|^-?\$\d+(?:\.\d{2})?$")
MULTIPLY_RE = re.compile(r"MULTIPLY|MULTIPL[VY]|MUITIPLY", re.I)
COLUMN_NAMES = ["borrower_at", "borrower_before", "seller_at", "seller_before", "paid_by_others"]

def render_pages(pdf_path, tmpdir):
    prefix = Path(tmpdir) / "pg"
    subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", "-png", str(pdf_path), str(prefix)],
                   check=True, capture_output=True)
    return sorted(Path(tmpdir).glob("pg-*.png"), key=lambda p: int(p.stem.split("-")[-1]))

def ocr_words(img_path):
    data = pytesseract.image_to_data(Image.open(img_path), config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    words = []
    for i, txt in enumerate(data["text"]):
        txt = txt.strip()
        if not txt or int(data["conf"][i]) < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append({"text": txt, "x0": x, "x1": x + w, "y": y + h / 2, "h": h})
    return words

def words_to_lines(words, y_tol=None):
    if not words:
        return []
    if y_tol is None:
        med_h = sorted(w["h"] for w in words)[len(words) // 2]
        y_tol = max(6, med_h * 0.6)
    words = sorted(words, key=lambda w: w["y"])
    lines, cur, cur_y = [], [words[0]], words[0]["y"]
    for w in words[1:]:
        if abs(w["y"] - cur_y) <= y_tol:
            cur.append(w); cur_y = sum(x["y"] for x in cur) / len(cur)
        else:
            lines.append(sorted(cur, key=lambda x: x["x0"])); cur, cur_y = [w], w["y"]
    lines.append(sorted(cur, key=lambda x: x["x0"]))
    return lines

def line_text(line):
    return " ".join(w["text"] for w in line)

def get_page_words(pdf_path):
    pages, needs_ocr = [], []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            txt = page.extract_text() or ""
            if len(txt.strip()) > 100:
                pages.append([{"text": w["text"], "x0": w["x0"], "x1": w["x1"],
                               "y": (w["top"] + w["bottom"]) / 2, "h": w["bottom"] - w["top"]}
                              for w in page.extract_words()])
            else:
                pages.append(None); needs_ocr.append(i)
    if needs_ocr:
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as td:
            imgs = render_pages(pdf_path, td)
            todo = [i for i in needs_ocr if i < len(imgs)]
            with ThreadPoolExecutor(max_workers=5) as ex:
                for i, ws in zip(todo, ex.map(lambda i: ocr_words(imgs[i]), todo)):
                    pages[i] = ws
            for i in needs_ocr:
                if pages[i] is None:
                    pages[i] = []
    return pages

def clean_money(s):
    return float(s.replace("$", "").replace(",", ""))

def extract_page1_fields(lines, result):
    full = "\n".join(line_text(l) for l in lines)
    m = re.search(r"Disbursement\s*Date\s*(\d{1,2}/\d{1,2}/\d{2,4})", full, re.I)
    if m: result["disbursement_date"] = m.group(1)
    m = re.search(
        r"Loan\s*Amount\s*\$?\s*((?:\d{1,3}(?:[ ,.;]{1,2}\d{3})+|\d+)(?:\.\d{2})?)",
        full, re.I)
    if m:
        raw = re.sub(r"[ ,;]", "", m.group(1))
        # treat a dot followed by 3 digits as an OCR'd thousands separator
        raw = re.sub(r"\.(?=\d{3})", "", raw)
        result.setdefault("_loan_amount_candidates", []).append(clean_money(raw))
    m = re.search(r"Closing\s*Date\s*(\d{1,2}/\d{1,2}/\d{2,4})", full, re.I)
    if m: result["closing_date"] = m.group(1)
    m = re.search(r"Loan\s*ID\s*#?\s*(\d{6,})", full, re.I)
    if m: result["loan_id"] = m.group(1)

def find_fee_columns(lines):
    for idx, line in enumerate(lines):
        t = line_text(line)
        if re.search(r"Borrower-?Paid", t, re.I) and re.search(r"Seller-?Paid", t, re.I):
            def center(w): return (w["x0"] + w["x1"]) / 2
            bor = sel = oth = None
            for w in line:
                if re.match(r"Borrower-?Paid", w["text"], re.I): bor = center(w)
                elif re.match(r"Seller-?Paid", w["text"], re.I): sel = center(w)
                elif re.match(r"(Paid|Others?)$", w["text"], re.I): oth = center(w)
            closings = []
            for sub in lines[idx + 1: idx + 3]:
                for w in sub:
                    if re.match(r"Closing[.,]?$", w["text"], re.I):
                        closings.append(center(w))
                if len(closings) >= 4: break
            closings = sorted(closings)[:4]
            if len(closings) == 4 and oth:
                return list(zip(COLUMN_NAMES, closings + [oth]))
            if bor and sel and oth:
                off = (sel - bor) / 4
                return list(zip(COLUMN_NAMES, [bor - off, bor + off, sel - off, sel + off, oth]))
    # Fallback for badly-OCR'd headers: find the At/Before Closing sub-header
    # line (>=4 words containing "Closing", possibly fused like "AtClosing").
    for idx, line in enumerate(lines[:15]):
        def center(w): return (w["x0"] + w["x1"]) / 2
        closings = [center(w) for w in line if re.search(r"Closing", w["text"], re.I)]
        if len(closings) < 4:
            continue
        closings = sorted(closings)[:4]
        oth = None
        for sub in lines[max(0, idx - 3): idx + 2]:
            for w in sub:
                if re.match(r"(Pa[il]?[dl]?by|Others?)$", w["text"], re.I):
                    oth = center(w)
        if oth is None:
            oth = closings[3] + (closings[3] - closings[2])
        return list(zip(COLUMN_NAMES, closings + [oth]))
    return None

def assign_column(x, columns):
    return min(columns, key=lambda c: abs(c[1] - x))[0]

def extract_multiply_fees(lines, columns):
    fees = []
    for line in lines:
        t = line_text(line)
        if not MULTIPLY_RE.search(t): continue
        if re.search(r"Contact|Email|@", t): continue
        amounts = [w for w in line if MONEY_RE.match(w["text"])]
        if not amounts: continue
        first_amt_x = min(w["x0"] for w in amounts)
        desc = " ".join(w["text"] for w in line if w["x1"] <= first_amt_x)
        desc = re.sub(r"\s+", " ", desc.replace("|", " ")).strip(" -.,")
        lender_paid = bool(re.search(r"\(L\)", t))
        for w in amounts:
            col = assign_column((w["x0"] + w["x1"]) / 2, columns) if columns else "unknown"
            fees.append({"description": desc, "amount": clean_money(w["text"]),
                         "column": col, "lender_paid": lender_paid})
    return fees

def extract_settlement_agent(pages_lines):
    out = {}
    for lines in pages_lines:
        header_idx, col_x0 = None, None
        for idx, line in enumerate(lines):
            t = line_text(line)
            if re.search(r"Settlement\s*Agent", t, re.I) and re.search(r"Lender|Broker", t, re.I):
                for w in line:
                    if re.search(r"ettlement", w["text"], re.I):
                        col_x0 = w["x0"] - 10; break
                if col_x0 is not None:
                    header_idx = idx; break
        if header_idx is None: continue
        out_labels = {"Name": "name", "Address": "address", "Contact": "contact",
                      "Email": "email", "Phone": "phone"}
        delim_labels = ("Name", "Address", "NMLS", "CO", "Contact", "Email", "Phone", "License")
        all_h = sorted(w["h"] for l in lines for w in l)
        med_h = all_h[len(all_h) // 2]
        rows = []
        for line in lines[header_idx + 1:]:
            first = line[0]["text"]; t = line_text(line)
            if not any(first.startswith(lab) for lab in delim_labels): continue
            key = None
            if not re.match(r"Contact\s+(NMLS|CO)", t, re.I):
                for lab, k in out_labels.items():
                    if first.startswith(lab): key = k; break
            rows.append((key, line[0]["y"]))
        if not rows: continue
        all_words = [w for line in lines[header_idx + 1:] for w in line if w["x0"] >= col_x0]
        rows.sort(key=lambda r: r[1])
        for i, (key, y0) in enumerate(rows):
            if key is None or key in out: continue
            y1 = rows[i + 1][1] if i + 1 < len(rows) else y0 + med_h * 3.5
            band = [w for w in all_words if y0 - med_h * 0.6 <= w["y"] < y1 - med_h * 0.6]
            val = " ".join(line_text(l) for l in words_to_lines(band)).strip()
            if val: out[key] = val
        if out: break
    for k, v in out.items():
        out[k] = re.sub(r"\s+", " ", v.replace("|", " ").replace("_", " ")).strip(" -.,")
    if "email" in out:
        out["email"] = out["email"].replace(" ", "")
    return out

def parse_cd(pdf_path):
    result = {"file": pdf_path.name, "loan_amount": None, "disbursement_date": None,
              "closing_date": None, "loan_id": None}
    pages = get_page_words(pdf_path)
    pages_lines = [words_to_lines(ws) for ws in pages]
    for lines in pages_lines:
        extract_page1_fields(lines, result)
    cands = result.pop("_loan_amount_candidates", [])
    if cands:
        # majority vote across pages (loan amount appears on pages 1 and 3);
        # ties resolve to the later occurrence
        from collections import Counter
        counts = Counter(cands)
        best = max(counts.values())
        result["loan_amount"] = [c for c in cands if counts[c] == best][-1]
    fees = []
    for lines in pages_lines:
        page_text = " ".join(line_text(l) for l in lines[:6])
        if not re.search(r"Cost\s*Details|Origination\s*Charges", page_text, re.I):
            continue
        cols = find_fee_columns(lines)
        if cols: fees.extend(extract_multiply_fees(lines, cols))
    result["multiply_fees"] = fees
    result["multiply_fee_total"] = round(sum(f["amount"] for f in fees), 2)
    result["multiply_fee_total_borrower"] = round(
        sum(f["amount"] for f in fees if f["column"].startswith("borrower")), 2)
    result["multiply_fee_total_lender"] = round(
        sum(f["amount"] for f in fees if f["lender_paid"] or f["column"] == "paid_by_others"), 2)
    sa = extract_settlement_agent(pages_lines)
    for k in ("name", "address", "contact", "email", "phone"):
        result[f"settlement_agent_{k}"] = sa.get(k, "")
    return result

CSV_FIELDS = ["file", "loan_id", "loan_amount", "closing_date", "disbursement_date",
              "multiply_fee_total", "multiply_fee_total_borrower", "multiply_fee_total_lender",
              "multiply_fees_json", "settlement_agent_name", "settlement_agent_address",
              "settlement_agent_contact", "settlement_agent_email", "settlement_agent_phone"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--output", default="cd_results.csv")
    args = ap.parse_args()
    pdfs = []
    for inp in args.inputs:
        p = Path(inp)
        pdfs.extend(sorted(p.glob("*.pdf")) if p.is_dir() else [p])
    rows = []
    for pdf in pdfs:
        print(f"Parsing {pdf.name} ...", file=sys.stderr)
        try:
            r = parse_cd(pdf)
        except Exception as e:
            r = {"file": pdf.name}
            print(f"  ERROR: {e}", file=sys.stderr)
        r["multiply_fees_json"] = json.dumps(r.get("multiply_fees", []))
        rows.append(r)
    with open(args.output, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"Wrote {args.output} ({len(rows)} rows)", file=sys.stderr)

if __name__ == "__main__":
    main()
