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
MULTIPLY_RE = re.compile(r"MULTIPLY|MULTIPL[VYE]|MUITIPLY|MULTPLY", re.I)
XACTUS_RE = re.compile(r"XACTUS|XACTU5|XACTLIS", re.I)  # OCR-tolerant
COLUMN_NAMES = ["borrower_at", "borrower_before", "seller_at", "seller_before", "paid_by_others"]

def render_pages(pdf_path, tmpdir):
    prefix = Path(tmpdir) / "pg"
    subprocess.run(["pdftoppm", "-r", str(DPI), "-gray", "-png", str(pdf_path), str(prefix)],
                   check=True, capture_output=True)
    return sorted(Path(tmpdir).glob("pg-*.png"), key=lambda p: int(p.stem.split("-")[-1]))

def autorotate(img):
    """Rotate a scanned page upright using tesseract's orientation detection."""
    try:
        osd = pytesseract.image_to_osd(img)
        m = re.search(r"Rotate:\s*(\d+)", osd)
        deg = int(m.group(1)) if m else 0
        if deg:
            return img.rotate(-deg, expand=True)
    except Exception:
        pass
    return img


def ocr_words(img_path, threshold=None):
    img = autorotate(Image.open(img_path))
    if threshold:
        img = img.point(lambda p: 255 if p > threshold else 0)
    data = pytesseract.image_to_data(img, config="--psm 6",
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

def get_page_words(pdf_path, threshold=None, tmpdir=None):
    """Returns (pages, images). images[i] is the rendered PNG path for pages
    that needed OCR (None for text-layer pages). Caller owns tmpdir lifetime."""
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
    images = [None] * len(pages)
    if needs_ocr:
        from concurrent.futures import ThreadPoolExecutor
        td = tmpdir or tempfile.mkdtemp()
        imgs = render_pages(pdf_path, td)
        todo = [i for i in needs_ocr if i < len(imgs)]
        for i in todo:
            images[i] = str(imgs[i])
        with ThreadPoolExecutor(max_workers=5) as ex:
            for i, ws in zip(todo, ex.map(lambda i: ocr_words(imgs[i], threshold), todo)):
                pages[i] = ws
        for i in needs_ocr:
            if pages[i] is None:
                pages[i] = []
    return pages, images

def clean_money(s):
    return float(s.replace("$", "").replace(",", ""))

def extract_page1_fields(lines, result):
    full = "\n".join(line_text(l) for l in lines)
    m = re.search(r"Disbursement\s*Date\s*(\d{1,2}/\d{1,2}/\d{2,4})", full, re.I)
    if m: result["disbursement_date"] = m.group(1)
    # Match only lines that BEGIN with "Loan Amount" (allowing OCR junk or a
    # line number) and are immediately followed by the figure. Look at the
    # first few tokens only: page 3's Cash to Close row shows Loan Estimate
    # then Final - take the LAST amount in that window (the Final column).
    for lm in re.finditer(r"^[\W\d]{0,4}Loan\s*Amount\s*[:;]?\s+([^\n]*)", full, re.I | re.M):
        window = " ".join(lm.group(1).split()[:4])
        amts = re.findall(
            r"\$?\s?((?:\d{1,3}(?:[ ,.;]{1,2}\d{3})+|\d{4,})(?:\.\d{2})?)", window)
        if not amts:
            continue
        raw = re.sub(r"[ ,;]", "", amts[-1])
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

FEE_CAP = 100000  # sanity ceiling for a single Multiply fee


def parse_fee_amount(token):
    """OCR-tolerant fee amount parser. Returns (value, clean) or None.
    clean=True when the token had a proper .NN decimal ending; clean=False
    when the decimal point was lost and the last 2 digits were assumed cents."""
    # Strip leading lender-paid marker before amount parsing (e.g. scanned "(L) $7,768.25")
    _tok = re.sub(r"^\(L\)\s*", "", token.strip(), flags=re.I)
    t = _tok.replace("\u00a7", "$").strip("|()[]{}:;,. \u2014-")  # OCR: '\u00a7' = '$'
    if not re.match(r"^\$?[\d,.;]+$", t):
        return None
    digits = re.sub(r"[^\d.]", "", t.replace("$", ""))
    if not digits or len(re.sub(r"\D", "", digits)) < 3:
        return None
    try:
        if re.search(r"\.\d{2}$", digits):
            if digits.count(".") > 1:  # dots as thousands separators
                val = float(digits[:-3].replace(".", "") + digits[-3:])
            else:
                val = float(digits)
            clean = True
        else:
            d = re.sub(r"\D", "", digits)
            if "$" not in t and len(d) < 5:
                return None  # short bare number: likely suite/line number, not a fee
            val = float(d) / 100  # assume lost decimal point
            clean = False
    except ValueError:
        return None
    if not (0 < val <= FEE_CAP):
        return None
    return round(val, 2), clean


def band_amounts(img_path, row_y, row_h, x_from):
    """Last-resort OCR of one fee row's amount region (right of the
    description). Full-page OCR often loses amounts to table grid lines;
    a tight crop reads them cleanly. Returns [(x_center, value, clean)]."""
    img = autorotate(Image.open(img_path))
    y0, y1 = int(row_y - row_h * 1.4), int(row_y + row_h * 1.4)
    x0 = int(min(x_from, img.width - 50))
    scale = 2
    best = []
    # OCR fails on strips that include the page's blank right margin, so try
    # a bounded window first, then a window shifted toward the right columns.
    for xa, xb in ((x0, x0 + 950), (x0 + 600, img.width - 40), (x0, img.width - 40)):
        xa, xb = int(max(0, xa)), int(min(img.width, xb))
        if xb - xa < 100:
            continue
        crop = img.crop((xa, max(0, y0), xb, min(img.height, y1)))
        if crop.height < 8:
            continue
        c2 = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
        for th in (None, 150, 190):
            c = c2 if th is None else c2.point(lambda p: 255 if p > th else 0)
            data = pytesseract.image_to_data(c, config="--psm 6",
                                             output_type=pytesseract.Output.DICT)
            center = c.height / 2
            for i, txt in enumerate(data["text"]):
                v = parse_fee_amount(txt.strip())
                if v is None or not v[1]:
                    continue
                wy = data["top"][i] + data["height"][i] / 2
                if abs(wy - center) > row_h * scale:  # keep this row only
                    continue
                wx = xa + (data["left"][i] + data["width"][i] / 2) / scale
                best.append((wx, v[0], v[1]))
            if best:
                return best
    return best


def assign_column(x, columns):
    return min(columns, key=lambda c: abs(c[1] - x))[0]

ORIG_FEE_RE = re.compile(
    r"(?:Loan\s+)?(?:Broker|Origination)\s+(?:Origination\s+)?(?:Fee|Compensation|Charge)", re.I)


def extract_multiply_fees(lines, columns, img_path=None):
    """Returns (fees, flags, skipped). Fees: lines mentioning Multiply.
    Flags: (a) Xactus fee lines not disclosed FBO Multiply, (b) Section A
    origination/broker fee lines with no visible Multiply payee.

    Amounts are matched to the NEAREST description row by vertical position
    rather than strict line grouping: on skewed scans the amount column often
    sits half a row below its label, which otherwise assigns a neighboring
    row's amount to the wrong fee."""
    import statistics
    # description rows: lines with at least two real words
    rows = []
    for line in lines:
        alpha = [w for w in line if re.search(r"[A-Za-z]{3,}", w["text"])]
        if len(alpha) < 2:
            continue
        rows.append({"y": statistics.median(w["y"] for w in line),
                     "line": line, "text": line_text(line), "amounts": []})
    if not rows:
        return [], [], 0
    all_h = sorted(w["h"] for l in lines for w in l)
    med_h = all_h[len(all_h) // 2] if all_h else 20
    # collect amount tokens anywhere on the page, assign to nearest row
    for line in lines:
        for w in line:
            if MONEY_RE.match(w["text"]):
                val, clean = clean_money(w["text"]), True
            else:
                v = parse_fee_amount(w["text"])
                if v is None:
                    continue
                val, clean = v
            row = min(rows, key=lambda r: abs(r["y"] - w["y"]))
            if abs(row["y"] - w["y"]) <= med_h * 1.3:
                row["amounts"].append((w, val, clean))

    fees, flags = [], []
    skipped = 0
    in_section_a = False
    for row in rows:
        t = row["text"]
        if re.search(r"Origination\s*Charges", t, re.I):
            in_section_a = True
            continue  # the section header/subtotal line itself is not a fee
        if re.search(r"^\W{0,3}B[.,]?\s*Services|Did\s*Not\s*Shop", t, re.I):
            in_section_a = False
        is_multiply = bool(MULTIPLY_RE.search(t))
        is_bare_xactus = bool(XACTUS_RE.search(t)) and not is_multiply
        is_anon_orig = (in_section_a and not is_multiply and not is_bare_xactus
                        and bool(ORIG_FEE_RE.search(t))
                        and not re.search(r"Lender\s+Fee|of\s+Loan\s+Amount|\(Points\)", t, re.I))
        if not (is_multiply or is_bare_xactus or is_anon_orig): continue
        if re.search(r"Contact|Email|@", t): continue
        amounts = row["amounts"]
        if not amounts and img_path:
            # re-OCR just this row's amount region (beats grid-line interference).
            # Crop from the amount-column region: trailing OCR junk on the row
            # makes "end of description" unreliable, so use column anchors or
            # the right 45% of the page.
            page_w = max(w["x1"] for l in lines for w in l)
            if columns:
                x_from = min(x for _, x in columns) - 120
            else:
                x_from = page_w * 0.55
            for wx, val, clean in band_amounts(img_path, row["y"], med_h, x_from):
                amounts.append(({"x0": wx - 30, "x1": wx + 30, "y": row["y"], "h": med_h},
                                val, clean))
        if not amounts:
            skipped += 1  # Multiply/Xactus line whose amount OCR'd unreadably
            continue
        if columns and len(amounts) > 1:
            # one amount per column per row: keep the vertically nearest
            by_col = {}
            for w, val, clean in amounts:
                col = assign_column((w["x0"] + w["x1"]) / 2, columns)
                d = abs(w["y"] - row["y"])
                if col not in by_col or d < by_col[col][0]:
                    by_col[col] = (d, (w, val, clean))
            amounts = [v for _, v in by_col.values()]
        first_amt_x = min(w["x0"] for w, _, _ in amounts)
        desc = " ".join(w["text"] for w in row["line"] if w["x1"] <= first_amt_x)
        desc = re.sub(r"\s+", " ", desc.replace("|", " ")).strip(" -.,")
        lender_paid = bool(re.search(r"\(L\)", t))
        for w, val, clean in amounts:
            col = assign_column((w["x0"] + w["x1"]) / 2, columns) if columns else "unknown"
            item = {"description": desc, "amount": val,
                    "column": col, "lender_paid": lender_paid,
                    "ocr_confidence": "high" if clean else "low"}
            if is_bare_xactus:
                item["flag"] = "Xactus fee not disclosed FBO Multiply"
                flags.append(item)
            elif is_anon_orig:
                item["flag"] = "Origination/broker fee with no Multiply payee"
                fees.append(item)
            else:
                fees.append(item)
    return fees, flags, skipped


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

def rescue_fees_from_image(img_path):
    """String-mode OCR rescue for fee lines whose amounts the word-level pass
    couldn't read. Tries several binarization thresholds; keeps amounts with a
    clean decimal format, majority-voted across thresholds."""
    from collections import Counter
    img = autorotate(Image.open(img_path))
    found = {}  # normalized desc -> Counter of (amount, clean)
    descs = {}
    for th in (None, 140, 160, 190):
        im = img if th is None else img.point(lambda p: 255 if p > th else 0)
        text = pytesseract.image_to_string(im, config="--psm 6")
        for raw_line in text.splitlines():
            is_multiply = bool(MULTIPLY_RE.search(raw_line))
            is_bare_xactus = bool(XACTUS_RE.search(raw_line)) and not is_multiply
            if not (is_multiply or is_bare_xactus): continue
            if re.search(r"Contact|Email|@", raw_line): continue
            vals = []
            for tok in raw_line.split():
                v = parse_fee_amount(tok)
                if v is not None:
                    vals.append(v)
            if not vals: continue
            m = MULTIPLY_RE.search(raw_line) or XACTUS_RE.search(raw_line)
            key = re.sub(r"[^a-z]", "", raw_line[:m.start()].lower())[:40]
            descs.setdefault(key, (raw_line.strip()[:80], is_bare_xactus))
            found.setdefault(key, Counter()).update(vals)
    fees, flags = [], []
    seen = set()  # (amount, is_xactus) - same fee OCR'd differently per threshold
    for key, counter in found.items():
        clean_vals = Counter({k: v for k, v in counter.items() if k[1]})
        best = (clean_vals or counter).most_common(1)[0][0]
        desc, bare_x = descs[key]
        if (best[0], bare_x) in seen:
            continue
        seen.add((best[0], bare_x))
        item = {"description": desc, "amount": best[0], "column": "unknown",
                "lender_paid": bool(re.search(r"\(L\)", desc)),
                "ocr_confidence": "high" if best[1] else "low"}
        if bare_x:
            item["flag"] = "Xactus fee not disclosed FBO Multiply"
            flags.append(item)
        else:
            fees.append(item)
    return fees, flags


def _fee_score(fees, skipped):
    clean = sum(1 for f in fees if f.get("ocr_confidence") == "high")
    return (clean, len(fees), -skipped)


def _extract_all(pages, images=None):
    """One extraction pass over per-page word lists."""
    pages_lines = [words_to_lines(ws) for ws in pages]
    fields = {}
    for lines in pages_lines:
        extract_page1_fields(lines, fields)
    cands = fields.pop("_loan_amount_candidates", [])
    if cands:
        # first occurrence = page 1 Loan Terms box (authoritative); later
        # pages are a cross-check only - disagreement gets flagged for review
        fields["loan_amount"] = cands[0]
        if any(abs(c - cands[0]) > 0.01 for c in cands[1:]):
            fields["loan_amount_mismatch"] = sorted(set(cands))
    fees, flags, skipped = [], [], 0
    fee_pages = []
    for pi, lines in enumerate(pages_lines):
        page_text = " ".join(line_text(l) for l in lines)
        if not (re.search(r"Cost\s*Detail|Originat|Borrower-?Paid|Loan\s*Costs", page_text, re.I)
                or MULTIPLY_RE.search(page_text)):
            continue
        if re.search(r"Loan\s*Calculations|Contact\s*Information", page_text, re.I) \
                and not re.search(r"Cost\s*Detail|Origination\s*Char", page_text, re.I):
            continue  # page 5 (contact table) mentions Multiply but holds no fees
        fee_pages.append(pi)
        cols = find_fee_columns(lines)  # may be None -> column "unknown"
        img = images[pi] if images else None
        f, fl, sk = extract_multiply_fees(lines, cols, img)
        fees.extend(f); flags.extend(fl); skipped += sk
    sa = extract_settlement_agent(pages_lines)
    return fields, fees, flags, skipped, sa, fee_pages


def parse_cd(pdf_path):
    import shutil
    result = {"file": pdf_path.name, "loan_amount": None, "disbursement_date": None,
              "closing_date": None, "loan_id": None}
    tmpdir = tempfile.mkdtemp()
    try:
        pages, images = get_page_words(pdf_path, tmpdir=tmpdir)
        fields, fees, flags, skipped, sa, fee_pages = _extract_all(pages, images)

        # Retry with binarized OCR when the first pass clearly missed something.
        for threshold in (140, 190):
            if skipped == 0 and fees and fields.get("loan_amount") \
                    and all(f.get("ocr_confidence") == "high" for f in fees):
                break
            pages2, _ = get_page_words(pdf_path, threshold=threshold, tmpdir=tmpdir)
            f2, fees2, flags2, skipped2, sa2, fp2 = _extract_all(pages2, images)
            if _fee_score(fees2, skipped2) > _fee_score(fees, skipped):
                fees, flags, skipped = fees2, flags2, skipped2
            fee_pages = sorted(set(fee_pages) | set(fp2))
            for k, v in f2.items():
                fields.setdefault(k, v)
            for k, v in sa2.items():
                sa.setdefault(k, v)

        # String-mode rescue on scanned fee pages if confidence is still low.
        if skipped > 0 or not fees or any(f.get("ocr_confidence") == "low" for f in fees):
            rfees, rflags = [], []
            for pi in fee_pages:
                if images[pi]:
                    rf, rfl = rescue_fees_from_image(images[pi])
                    rfees.extend(rf); rflags.extend(rfl)
            if _fee_score(rfees, 0) > _fee_score(fees, skipped):
                by_amt = {f["amount"]: f for f in fees}
                for rf in rfees:  # keep column when the word pass had read it
                    wf = by_amt.get(rf["amount"])
                    if wf and wf["column"] != "unknown":
                        rf["column"] = wf["column"]
                        rf["lender_paid"] = wf["lender_paid"]
                fees, flags, skipped = rfees, rflags, 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    mismatch = fields.pop("loan_amount_mismatch", None)
    if mismatch:
        flags.append({"flag": "Loan amount differs across pages - verify page 1",
                      "description": f"candidates: {mismatch}", "amount": fields.get("loan_amount"),
                      "column": "", "lender_paid": False, "ocr_confidence": "low"})
    def dedupe(items):
        seen, out = set(), []
        for f in items:
            key = (f["amount"], f.get("flag", ""),
                   re.sub(r"[^a-z]", "", f["description"].lower())[:25])
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out
    fees, flags = dedupe(fees), dedupe(flags)

    result.update(fields)
    result["multiply_fees"] = fees
    result["flags"] = flags
    result["unreadable_fee_lines"] = skipped
    result["low_confidence"] = sum(1 for f in fees if f.get("ocr_confidence") == "low")
    result["multiply_fee_total"] = round(sum(f["amount"] for f in fees), 2)
    result["multiply_fee_total_borrower"] = round(
        sum(f["amount"] for f in fees if f["column"].startswith("borrower")), 2)
    result["multiply_fee_total_lender"] = round(
        sum(f["amount"] for f in fees if f["lender_paid"] or f["column"] == "paid_by_others"), 2)
    for k in ("name", "address", "contact", "email", "phone"):
        result[f"settlement_agent_{k}"] = sa.get(k, "")
    return result


CSV_FIELDS = ["file", "loan_id", "loan_amount", "closing_date", "disbursement_date",
              "multiply_fee_total", "multiply_fee_total_borrower", "multiply_fee_total_lender",
              "multiply_fees_json", "flags_json", "unreadable_fee_lines", "low_confidence", "settlement_agent_name", "settlement_agent_address",
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
        r["flags_json"] = json.dumps(r.get("flags", []))
        rows.append(r)
    with open(args.output, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"Wrote {args.output} ({len(rows)} rows)", file=sys.stderr)

if __name__ == "__main__":
    main()
