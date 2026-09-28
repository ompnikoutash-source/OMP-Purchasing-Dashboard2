"""
Parse Trial Balance PDF — direct char-level parsing.

Key insight from dump analysis:
  - Description/acct chars: x0 < 200
  - Financial chars: x0 >= 200
  - Within a number token: x0_next - x1_prev is near 0 (chars touch)
  - Between columns: gap is always >= 10px

Column right-edge boundaries (from observed character x1 values + midpoints):
  ob  : token_right <= 280
  cd  : 280 < token_right <= 357
  cc  : 357 < token_right <= 433
  cn  : 433 < token_right <= 513
  yd  : 513 < token_right <= 588
  yc  : 588 < token_right <= 664
  bal : 664 < token_right <= 752
"""

import re
import pdfplumber
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

PDF_PATH  = r"C:\Users\niko\Downloads\TB 12.31.2025 updated.pdf"
XLSX_PATH = r"h:\2025\NewForecastingModel\OMPforecasting5\TrialBalance_12312025_v2.xlsx"

DESC_X_CUTOFF = 200   # chars with x0 < this are acct/description
FIN_GAP       = 8     # px — max gap (x0_next - x1_prev) within same token

# Right-edge column boundaries
COL_BOUNDARIES = [
    ("ob",  280),
    ("cd",  357),
    ("cc",  433),
    ("cn",  513),
    ("yd",  588),
    ("yc",  664),
    ("bal", 752),
]

SKIP_LINE = re.compile(
    r'TRIALBALANCE|OLDMASTER|COMPANYTOTAL|GL435|'
    r'OPENINGCURRENTAMOUNT|YTDAMOUNT|NBALANCEDEBIT|'
    r'NUMBERDESCRIP|ACCOUNTACCOUNT|_________|9373742|PERIODENDING',
    re.IGNORECASE
)


def to_float(s):
    s = re.sub(r'[\s,]', '', s)
    if not s:
        return 0.0
    neg = s.endswith('-')
    if neg:
        s = s[:-1]
    try:
        return -float(s) if neg else float(s)
    except ValueError:
        return 0.0


def assign_col(right_x):
    for col_name, boundary in COL_BOUNDARIES:
        if right_x <= boundary:
            return col_name
    return None


def group_fin_tokens(chars_sorted):
    """Group consecutive financial chars into tokens using gap threshold."""
    if not chars_sorted:
        return []
    tokens = []
    cur = [chars_sorted[0]]
    for ch in chars_sorted[1:]:
        prev = cur[-1]
        gap = ch['x0'] - prev.get('x1', prev['x0'] + 5)
        if gap <= FIN_GAP:
            cur.append(ch)
        else:
            tokens.append(cur)
            cur = [ch]
    tokens.append(cur)
    return tokens


def token_right(tok):
    return max(c.get('x1', c['x0'] + 5) for c in tok)


def group_desc_tokens(chars_sorted):
    """Group description chars into words.
    Within-word char gaps are 0 px (chars touch); word-space gaps are ~5.4 px.
    Use threshold of 3 px to split at spaces while keeping word chars together.
    """
    if not chars_sorted:
        return []
    tokens = []
    cur = [chars_sorted[0]]
    for ch in chars_sorted[1:]:
        prev = cur[-1]
        gap = ch['x0'] - prev.get('x1', prev['x0'] + 5)
        if gap <= 3:
            cur.append(ch)
        else:
            tokens.append(cur)
            cur = [ch]
    tokens.append(cur)
    return tokens


def assign_col_by_x1(x1):
    """Assign a single character to a column based on its right-edge x1."""
    for col_name, boundary in COL_BOUNDARIES:
        if x1 <= boundary:
            return col_name
    return None


def extract_rows(pdf_path):
    rows = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            # Group chars by line
            lines = {}
            for ch in page.chars:
                y = round(ch['top'] / 2) * 2
                lines.setdefault(y, []).append(ch)

            for y in sorted(lines):
                all_chars = sorted(lines[y], key=lambda c: c['x0'])
                line_raw = ''.join(c['text'] for c in all_chars).replace(' ', '')

                if SKIP_LINE.search(line_raw):
                    continue

                # Split into description area and financial area
                desc_chars = [c for c in all_chars if c['x0'] < DESC_X_CUTOFF]
                fin_chars   = [c for c in all_chars if c['x0'] >= DESC_X_CUTOFF]

                # --- Description / Account# ---
                desc_tokens = group_desc_tokens(desc_chars)
                desc_words  = [''.join(c['text'] for c in t) for t in desc_tokens]

                acct_num = None
                desc_parts = []
                for w in desc_words:
                    if acct_num is None:
                        if re.match(r'^\d{4,}$', w):
                            acct_num = int(w)
                        else:
                            # Account number and description merged (gap < FIN_GAP):
                            # split leading digit run from the text that follows
                            m = re.match(r'^(\d{4,})(.+)$', w)
                            if m:
                                acct_num = int(m.group(1))
                                rest = m.group(2).strip()
                                if rest:
                                    desc_parts.append(rest)
                            else:
                                desc_parts.append(w)
                    else:
                        desc_parts.append(w)
                desc_str = ' '.join(desc_parts)

                # --- Financial columns ---
                # Assign each individual char to a column by its own x1 (right edge),
                # then tokenize within each column.  This prevents adjacent column
                # values from being merged when the inter-column gap < FIN_GAP.
                col_char_buckets = {c[0]: [] for c in COL_BOUNDARIES}
                for ch in fin_chars:
                    x1 = ch.get('x1', ch['x0'] + 5)
                    col = assign_col_by_x1(x1)
                    if col:
                        col_char_buckets[col].append(ch)

                col_vals = {c[0]: [] for c in COL_BOUNDARIES}
                for col_name, bucket in col_char_buckets.items():
                    if not bucket:
                        continue
                    bucket_sorted = sorted(bucket, key=lambda c: c['x0'])
                    for tok in group_fin_tokens(bucket_sorted):
                        col_vals[col_name].append(''.join(c['text'] for c in tok))

                # Build number values
                def num(col_name):
                    return to_float(''.join(col_vals[col_name]))

                ob  = num('ob')
                cd  = num('cd')
                cc  = num('cc')
                cn  = num('cn')
                yd  = num('yd')
                yc  = num('yc')
                bal = num('bal')

                # Require at least one financial column to have a non-zero number
                # OR the row has an account number
                if ob == cd == cc == cn == yd == yc == bal == 0.0 and acct_num is None:
                    continue

                # Skip rows that are purely zero with no account number
                # (likely blank or sub-total lines with all zeros)
                if not any(col_vals[c[0]] for c in COL_BOUNDARIES) and acct_num is None:
                    continue

                rows.append((acct_num, desc_str, ob, cd, cc, cn, yd, yc, bal))

    return rows


def build_xlsx(rows, path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trial Balance 12-31-2025"

    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", fgColor="1F4E79")
    num_fmt     = '#,##0.00;[Red]-#,##0.00'
    thin        = Side(border_style="thin", color="BFBFBF")
    bdr         = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells("A1:I1")
    ws["A1"] = "OLD MASTER PRODUCTS, INC."
    ws["A1"].font = Font(name="Calibri", bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")

    ws.merge_cells("A2:I2")
    ws["A2"] = "TRIAL BALANCE - PERIOD ENDING 12/31/2025"
    ws["A2"].font = Font(name="Calibri", bold=True, size=12)
    ws["A2"].alignment = Alignment(horizontal="center")

    ws.merge_cells("A3:I3")
    ws["A3"] = "Printed: 4/15/2026"
    ws["A3"].font = Font(name="Calibri", italic=True, size=10)
    ws["A3"].alignment = Alignment(horizontal="center")

    headers = ["Account #", "Description",
               "Opening Balance",
               "Curr Debit", "Curr Credit", "Curr Net",
               "YTD Debit", "YTD Credit", "Balance"]
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=5, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border = bdr

    R0 = 6
    for i, (acct, desc, ob, cd, cc, cn, yd, yc, bal) in enumerate(rows):
        r = R0 + i
        vals = [acct, desc, ob, cd, cc, cn, yd, yc, bal]
        for col, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=col, value=v)
            c.border = bdr
            if col == 1:
                c.alignment = Alignment(horizontal="center")
            elif col == 2:
                c.alignment = Alignment(horizontal="left")
            else:
                c.number_format = num_fmt
                c.alignment = Alignment(horizontal="right")
        if i % 2 == 0:
            for col in range(1, 10):
                ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor="EBF3FB")

    tr = R0 + len(rows)
    ws.cell(row=tr, column=2, value="COMPANY TOTAL")
    for col in range(3, 10):
        c = ws.cell(row=tr, column=col,
                    value=f"=SUM({get_column_letter(col)}{R0}:{get_column_letter(col)}{tr-1})")
        c.number_format = num_fmt
        c.border = bdr
        c.alignment = Alignment(horizontal="right")
    for col in range(1, 10):
        ws.cell(row=tr, column=col).fill = PatternFill("solid", fgColor="1F4E79")
        ws.cell(row=tr, column=col).font = Font(bold=True, color="FFFFFF")

    for col, w in enumerate([12, 32, 18, 16, 16, 16, 16, 16, 16], 1):
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.freeze_panes = "A6"
    ws.auto_filter.ref = f"A5:I{tr-1}"

    wb.save(path)
    print(f"Saved {len(rows)} rows to {path}")


if __name__ == "__main__":
    import sys

    print("Extracting rows from PDF...")
    rows = extract_rows(PDF_PATH)
    print(f"  Found {len(rows)} rows")

    print("\nFirst 20 rows:")
    for r in rows[:20]:
        print(r)
    print("\nLast 10 rows:")
    for r in rows[-10:]:
        print(r)

    tot_cd = sum(r[3] for r in rows)
    tot_cc = sum(r[4] for r in rows)
    tot_yd = sum(r[6] for r in rows)
    tot_yc = sum(r[7] for r in rows)
    print(f"\nCurr Debit total:  {tot_cd:,.2f}  (PDF expects: 14,177,746.33)")
    print(f"Curr Credit total: {tot_cc:,.2f}  (PDF expects: 14,177,746.33)")
    print(f"YTD Debit total:   {tot_yd:,.2f}  (PDF expects: 181,056,079.19)")
    print(f"YTD Credit total:  {tot_yc:,.2f}  (PDF expects: 181,056,079.19)")

    build_xlsx(rows, XLSX_PATH)
