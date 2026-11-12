"""Bộ xử lý PDF -> Word.
- PDF có chữ thật: dùng pdf2docx (giữ nguyên bảng, định dạng).
- PDF quét (ảnh): xoay thẳng, dò khung bảng, đọc chữ bằng Tesseract (tiếng Việt),
  dựng lại thành đoạn văn + bảng thật trong Word. Chữ máy không chắc được tô vàng.
Mọi thứ chạy trên máy, không gửi gì lên mạng.
"""
import csv
import io
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import pymupdf

DPI = 300
LOW_CONF = 65          # chữ thường dưới mức này -> tô vàng
LOW_CONF_DIGIT = 85    # chữ có số: kiểm khắt khe hơn
DROP_LINE_CONF = 35    # dòng dưới mức này coi là nhiễu (logo, dấu mộc...)
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- Tesseract
def find_tesseract():
    import shutil
    p = shutil.which("tesseract")
    cands = [p] if p else []
    cands += [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    raise RuntimeError("Chưa có Tesseract (công cụ đọc chữ trong ảnh). Cần cài trước.")


TESS = None


def tess_words(img, psm, lang="vie", SC=1.5, sharp=True):
    """Chạy Tesseract, trả về danh sách từ: dict(b,p,l,txt,conf,x,y,w,h).
    Mặc định phóng to 1,5 lần + làm nét: dấu tiếng Việt rất nhỏ, đọc đúng hơn (đã đo thử)."""
    global TESS
    if TESS is None:
        TESS = find_tesseract()
    if SC != 1.0:
        img = cv2.resize(img, None, fx=SC, fy=SC, interpolation=cv2.INTER_CUBIC)
    if sharp:
        img = cv2.addWeighted(img, 1.8, cv2.GaussianBlur(img, (0, 0), 3), -0.8, 0)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "i.png")
        cv2.imencode(".png", img)[1].tofile(p)
        r = subprocess.run([TESS, p, "stdout", "--psm", str(psm), "-l", lang, "tsv"],
                           capture_output=True, creationflags=NOWIN,
                           env=dict(os.environ, OMP_THREAD_LIMIT="1"))
    out = []
    text = r.stdout.decode("utf-8", errors="replace")
    for row in csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(row) < 12 or row[0] != "5":
            continue
        txt = row[11].strip()
        if not txt:
            continue
        try:
            out.append(dict(b=int(row[2]), p=int(row[3]), l=int(row[4]), txt=txt,
                            conf=float(row[10]), x=int(int(row[6]) / SC), y=int(int(row[7]) / SC),
                            w=int(int(row[8]) / SC), h=int(int(row[9]) / SC)))
        except ValueError:
            continue
    return out


def word_unsure(w):
    """Chữ cần anh liếc kiểm: máy không chắc, hoặc các lần đọc không đồng ý."""
    thr = LOW_CONF_DIGIT if any(ch.isdigit() for ch in w["txt"]) else LOW_CONF
    return w["conf"] < thr or bool(w.get("unsure"))


def _norm_tok(t):
    return t.strip(".,;:!?()[]\"'“”-–")


def tess_vote(img, psm):
    """Đọc 3 lần với 3 cách xử lý ảnh; chữ nào các lần đọc không đồng ý thì đánh dấu 'unsure'.
    Kết quả chính lấy từ cách đã đo là chính xác nhất (phóng 1,5 lần + làm nét)."""
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_main = ex.submit(tess_words, img, psm)
        f_a = ex.submit(tess_words, img, psm, "vie", 1.0, False)
        f_b = ex.submit(tess_words, img, psm, "vie", 2.0, False)
        main, a, b = f_main.result(), f_a.result(), f_b.result()
    sa = {_norm_tok(w["txt"]) for w in a}
    sb = {_norm_tok(w["txt"]) for w in b}
    for w in main:
        tok = _norm_tok(w["txt"])
        w["unsure"] = bool(tok) and (tok not in sa or tok not in sb)
    return main


def group_lines(words):
    """Gom từ thành dòng theo (khối, đoạn, dòng) của Tesseract."""
    d = {}
    for w in words:
        d.setdefault((w["b"], w["p"], w["l"]), []).append(w)
    lines = []
    for key, ws in d.items():
        ws.sort(key=lambda a: a["x"])
        hs = [a["h"] for a in ws]
        lines.append(dict(
            key=key, words=ws, txt=" ".join(a["txt"] for a in ws),
            l=min(a["x"] for a in ws), t=min(a["y"] for a in ws),
            r=max(a["x"] + a["w"] for a in ws), b=max(a["y"] + a["h"] for a in ws),
            h=float(np.percentile(hs, 75)), conf=float(np.mean([a["conf"] for a in ws])),
        ))
    return lines


# ---------------------------------------------------------------- Xử lý ảnh
def render_page(page):
    pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy()


def deskew(gray):
    """Xoay thẳng trang quét bị nghiêng (tìm góc làm các dòng chữ thẳng nhất)."""
    small_w = 900
    sc = small_w / gray.shape[1]
    sm = cv2.resize(gray, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
    bw = cv2.adaptiveThreshold(sm, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    h, w = bw.shape

    def score(a):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0)
        r = cv2.warpAffine(bw, M, (w, h), flags=cv2.INTER_NEAREST)
        return float(np.var(r.sum(axis=1).astype(np.float64)))

    best = max(np.arange(-4, 4.01, 0.4), key=score)
    best = max(np.arange(best - 0.4, best + 0.41, 0.05), key=score)
    if abs(best) < 0.08:
        return gray, 0.0
    H, W = gray.shape
    M = cv2.getRotationMatrix2D((W / 2, H / 2), best, 1.0)
    return cv2.warpAffine(gray, M, (W, H), flags=cv2.INTER_CUBIC, borderValue=255), float(best)


def normalize(gray):
    """San phẳng nền (bóng đổ, ố vàng) để chữ rõ hơn."""
    bg = cv2.GaussianBlur(gray, (0, 0), 25)
    return cv2.divide(gray, bg, scale=255)


def detect_tables(norm):
    """Tìm bảng có kẻ khung. Trả về (danh sách bảng, mặt nạ đường kẻ cần xóa)."""
    h, w = norm.shape
    bw = cv2.adaptiveThreshold(norm, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    k = max(40, w // 60)
    hm = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    vm = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))
    # nối chỗ đứt nhỏ của đường kẻ
    hm = cv2.morphologyEx(hm, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 1)))
    vm = cv2.morphologyEx(vm, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 15)))
    grid = cv2.dilate(cv2.bitwise_or(hm, vm), np.ones((3, 3), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(grid, connectivity=8)
    line_mask = np.zeros_like(grid)
    tables = []
    for i in range(1, n):
        x, y, cw, ch, area = st[i]
        if cw < 100 or ch < 50:
            continue
        comp = (lab == i).astype(np.uint8) * 255
        nh = cv2.connectedComponents(cv2.bitwise_and(hm, comp))[0] - 1
        nv = cv2.connectedComponents(cv2.bitwise_and(vm, comp))[0] - 1
        if nh < 2 or nv < 2:
            continue
        line_mask = cv2.bitwise_or(line_mask, comp)
        sub = comp[y:y + ch, x:x + cw]
        free = (sub == 0).astype(np.uint8)
        m, flab, fst, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
        cells = []
        for j in range(1, m):
            cx, cy, cwid, chei, ar = fst[j]
            if cx == 0 or cy == 0 or cx + cwid >= cw or cy + chei >= ch:
                continue  # chạm mép = nền ngoài bảng
            if cwid < 12 or chei < 10 or ar < 150:
                continue
            cells.append([x + cx, y + cy, cwid, chei])
        if len(cells) >= 2:
            tables.append(dict(bbox=(x, y, cw, ch), cells=cells))
    return tables, line_mask


def snap(values, tol):
    values = sorted(values)
    groups = [[values[0]]]
    for v in values[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [int(np.mean(g)) for g in groups]


def nearest(reps, v):
    return int(np.argmin([abs(v - r) for r in reps]))


def build_grid(tb):
    xs = snap([c[0] for c in tb["cells"]] + [c[0] + c[2] for c in tb["cells"]], 14)
    ys = snap([c[1] for c in tb["cells"]] + [c[1] + c[3] for c in tb["cells"]], 14)
    tb["xs"], tb["ys"] = xs, ys
    for c in tb["cells"]:
        x, y, w, h = c[:4]
        c0, c1 = nearest(xs, x), nearest(xs, x + w)
        r0, r1 = nearest(ys, y), nearest(ys, y + h)
        if c1 <= c0:
            c1 = c0 + 1
        if r1 <= r0:
            r1 = r0 + 1
        c.extend([r0, r1, c0, c1])  # c[4..7]
    return tb


def ocr_cell(clean, rect):
    x, y, w, h = rect
    crop = clean[max(0, y - 2):y + h + 2, max(0, x - 2):x + w + 2]
    crop = cv2.copyMakeBorder(crop, 14, 14, 14, 14, cv2.BORDER_CONSTANT, value=255)
    ws = tess_vote(crop, 6)
    if not ws:
        ws = tess_vote(crop, 7)
    if not ws:
        ws = tess_vote(crop, 10)
    return group_lines(ws), crop.shape[1] - 28


def stroke_width(clean, l, t, r, b):
    """Độ dày nét chữ (điểm ảnh): chữ đậm có nét dày hơn rõ rệt."""
    crop = clean[max(0, t):b, max(0, l):r]
    if crop.size == 0:
        return 0.0
    bw = (crop < 150).astype(np.uint8)
    if bw.sum() == 0:
        return 0.0
    dt = cv2.distanceTransform(bw, cv2.DIST_L2, 3)
    mx = cv2.dilate(dt, np.ones((3, 3), np.uint8))
    ridge = (dt >= mx - 1e-6) & (bw > 0)
    return float(2 * dt[ridge].mean()) if ridge.any() else 0.0


# ---------------------------------------------------------------- Trang -> khối
def analyze_page(gray, log):
    gray, angle = deskew(gray)
    if abs(angle) >= 0.08:
        log(f"  xoay thẳng trang {angle:+.2f} độ")
    norm = normalize(gray)
    tables, line_mask = detect_tables(norm)
    clean = norm.copy()
    clean[line_mask > 0] = 255
    for tb in tables:
        build_grid(tb)
    text_img = clean.copy()
    for tb in tables:
        x, y, w, h = tb["bbox"]
        text_img[y:y + h, x:x + w] = 255

    def run_text(img, vote=False):
        return group_lines((tess_vote if vote else tess_words)(img, 3))

    lines = run_text(text_img)
    # trang chụp nằm ngang/ngược: thử xoay nếu độ tin cậy thấp
    if lines and np.mean([l["conf"] for l in lines]) < 45:
        best = (np.mean([l["conf"] for l in lines]), 0, lines)
        for code, name in ((cv2.ROTATE_90_CLOCKWISE, 90), (cv2.ROTATE_180, 180), (cv2.ROTATE_90_COUNTERCLOCKWISE, 270)):
            ls = run_text(cv2.rotate(text_img, code))
            if ls:
                c = np.mean([l["conf"] for l in ls])
                if c > best[0] + 15:
                    best = (c, name, ls)
        if best[1]:
            log(f"  trang bị xoay {best[1]} độ, đã tự xoay lại")
            code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[best[1]]
            return analyze_page(cv2.rotate(gray, code), log)
    lines = run_text(text_img, vote=True)  # đọc lại có so sánh nhiều lần để tô vàng chữ nghi ngờ
    dropped = [l for l in lines if l["conf"] < DROP_LINE_CONF]
    lines = [l for l in lines if l["conf"] >= DROP_LINE_CONF]

    with ThreadPoolExecutor(max_workers=4) as ex:
        jobs = []
        for ti, tb in enumerate(tables):
            for c in tb["cells"]:
                jobs.append((tb, c, ex.submit(ocr_cell, clean, c[:4])))
        for tb, c, fut in jobs:
            c.append(fut.result())  # c[8] = (lines, cell_inner_width)
    return dict(gray=gray, clean=clean, tables=tables, lines=lines, dropped=len(dropped), size=gray.shape)


# ---------------------------------------------------------------- Dựng Word
def build_docx(pages, page_dims_pt, out_path, edits=None):
    if edits is not None:
        apply_edits(pages, edits)
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    PXCM = 2.54 / DPI
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    st.paragraph_format.space_after = Pt(0)
    st.paragraph_format.space_before = Pt(0)
    st.paragraph_format.line_spacing = 1.0

    # --- thống kê toàn tài liệu: cỡ chữ chuẩn, độ đậm chuẩn
    all_h, all_d = [], []
    for pg in pages:
        for l in pg["lines"]:
            all_h.append(l["h"])
            l["stroke"] = stroke_width(pg["clean"], l["l"], l["t"], l["r"], l["b"])
            if len(l["txt"].replace(" ", "")) > 2:
                all_d.append(l["stroke"])
        for tb in pg["tables"]:
            for c in tb["cells"]:
                for l in c[8][0]:
                    all_h.append(l["h"])
                    ox, oy = c[0] - 2 - 14, c[1] - 2 - 14  # gốc ảnh cắt ô (đã đệm)
                    l["stroke"] = stroke_width(pg["clean"], max(0, ox + l["l"]), max(0, oy + l["t"]),
                                               ox + l["r"], oy + l["b"])
                    if len(l["txt"].replace(" ", "")) > 2:
                        all_d.append(l["stroke"])
    med_h = float(np.median(all_h)) if all_h else 40.0
    med_d = float(np.median([d for d in all_d if d > 0] or [4.0]))

    def size_of(h):
        s = 12.0 * h / med_h
        s = max(9.0, min(24.0, s))
        # dấu chồng nhau làm khung chữ cao lên -> gom cỡ gần 12 về đúng 12 cho đều
        if abs(s - 12) < 3.4:
            s = 12.0
        return round(s * 2) / 2

    def is_bold(l):
        # nét dày hơn 30% so với chữ thường cùng cỡ; dòng quá ngắn (1-2 ký tự) không đủ cơ sở
        if len(l["txt"].replace(" ", "")) <= 2:
            return False
        return l.get("stroke", 0) > 1.2 * med_d * max(1.0, l["h"] / med_h)

    def add_words(par, line, size, bold):
        first = True
        for w in line["words"]:
            run = par.add_run(("" if first else " ") + w["txt"])
            first = False
            run.font.size = Pt(size)
            run.font.bold = bold or None
            if word_unsure(w):
                run.font.highlight_color = WD_COLOR_INDEX.YELLOW

    # --- khổ giấy theo trang đầu
    sec = doc.sections[0]
    pw, ph = page_dims_pt[0]
    sec.page_width, sec.page_height = Pt(pw), Pt(ph)
    all_l = [l["l"] for pg in pages for l in pg["lines"]] + [tb["bbox"][0] for pg in pages for tb in pg["tables"]]
    all_r = [l["r"] for pg in pages for l in pg["lines"]] + [tb["bbox"][0] + tb["bbox"][2] for pg in pages for tb in pg["tables"]]
    page_w_px = pages[0]["size"][1]
    left_m = min(max((min(all_l) if all_l else 150) * PXCM, 1.2), 3.5)
    right_m = min(max(((page_w_px - max(all_r)) if all_r else 150) * PXCM, 1.2), 3.5)
    sec.left_margin, sec.right_margin = Cm(left_m), Cm(right_m)
    sec.top_margin = sec.bottom_margin = Cm(1.5)
    text_w_cm = pw * 2.54 / 72 - left_m - right_m
    base_left_px = left_m / PXCM

    stats = dict(low=0, words=0, dropped=0, tables=0, cells=0)

    for pi, pg in enumerate(pages):
        if pi > 0:
            sp = doc.add_paragraph()
            sp.paragraph_format.page_break_before = True
            sp.add_run("").font.size = Pt(1)
        stats["dropped"] += pg["dropped"]
        W = pg["size"][1]
        right_max = max([l["r"] for l in pg["lines"]] + [0])

        # ----- khối: dòng chữ (gộp theo dải ngang) và bảng
        bands = []
        for l in sorted(pg["lines"], key=lambda a: a["t"]):
            for bnd in bands:
                ov = min(bnd["b"], l["b"]) - max(bnd["t"], l["t"])
                if ov > 0.5 * min(bnd["b"] - bnd["t"], l["b"] - l["t"]):
                    bnd["items"].append(l)
                    bnd["t"], bnd["b"] = min(bnd["t"], l["t"]), max(bnd["b"], l["b"])
                    break
            else:
                bands.append(dict(t=l["t"], b=l["b"], items=[l]))
        blocks = []  # ('band', band) | ('table', tb)
        for bnd in bands:
            bnd["items"].sort(key=lambda a: a["l"])
            # hai dòng cách xa trong cùng dải -> cột cạnh nhau
            cols = [[bnd["items"][0]]]
            for it in bnd["items"][1:]:
                if it["l"] - cols[-1][-1]["r"] > 0.07 * W:
                    cols.append([it])
                else:
                    cols[-1].append(it)
            bnd["cols"] = cols
            blocks.append(("band", bnd, bnd["t"]))
        for tb in pg["tables"]:
            blocks.append(("table", tb, tb["bbox"][1]))
        blocks.sort(key=lambda a: a[2])

        prev_bottom = 0
        prev_par = None
        prev_full = False
        pending_cols = None  # bảng không khung cho tiêu đề 2 cột

        i = 0
        while i < len(blocks):
            kind, obj, top = blocks[i]
            gap_pt = max(0.0, (top - prev_bottom) * 72 / DPI) if prev_bottom else 0.0
            space = min(18.0, gap_pt * 0.8) if prev_bottom else 0.0

            if kind == "band" and len(obj["cols"]) == 1:
                # ---- đoạn văn thường (có thể gộp dòng tiếp nối)
                items = obj["cols"][0]
                for l in items:
                    size = size_of(l["h"])
                    bold = is_bold(l)
                    center = abs((l["l"] + l["r"]) / 2 - W / 2) < 0.05 * W and l["l"] > 0.12 * W
                    cont = (prev_par is not None and prev_full and not center
                            and abs(l["l"] - prev_par["l"]) < 0.03 * W
                            and (l["t"] - prev_bottom) * 72 / DPI < 0.9 * size
                            and l["key"][:2] == prev_par["key"][:2])
                    if cont:
                        run = prev_par["par"].add_run(" ")
                        run.font.size = Pt(size)
                        add_words(prev_par["par"], l, size, bold)
                    else:
                        p = doc.add_paragraph()
                        pf = p.paragraph_format
                        pf.space_before = Pt(space if l is items[0] else 0)
                        if center:
                            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        else:
                            ind = (l["l"] - base_left_px) * PXCM
                            if ind > 1.2:
                                pf.left_indent = Cm(ind)
                        add_words(p, l, size, bold)
                        prev_par = dict(par=p, l=l["l"], key=l["key"])
                    prev_full = l["r"] >= 0.88 * right_max
                    prev_bottom = l["b"]
                    stats["words"] += len(l["words"])
                    stats["low"] += sum(1 for w in l["words"] if word_unsure(w))
                i += 1
                continue

            prev_par, prev_full = None, False
            if kind == "band":
                # ---- gom các dải liên tiếp có cùng số cột thành 1 bảng không khung
                k = len(obj["cols"])
                group = [obj]
                j = i + 1
                while j < len(blocks) and blocks[j][0] == "band" and len(blocks[j][1]["cols"]) == k:
                    group.append(blocks[j][1])
                    j += 1
                starts = [min(l["l"] for l in group[0]["cols"][c]) for c in range(k)]
                edges = [base_left_px] + [(group[0]["cols"][c - 1][-1]["r"] + group[0]["cols"][c][0]["l"]) / 2 for c in range(1, k)] \
                    + [W - right_m / PXCM]
                if space:
                    sp = doc.add_paragraph()
                    sp.paragraph_format.space_before = Pt(0)
                    sp.add_run("").font.size = Pt(max(1, space))
                t = doc.add_table(rows=len(group), cols=k)
                t.autofit = False
                for ci in range(k):
                    wcm = max(1.5, (edges[ci + 1] - edges[ci]) * PXCM)
                    for r in range(len(group)):
                        t.cell(r, ci).width = Cm(wcm)
                for r, bnd in enumerate(group):
                    for ci in range(k):
                        cell = t.cell(r, ci)
                        cp = cell.paragraphs[0]
                        ls = bnd["cols"][ci]
                        for n_l, l in enumerate(ls):
                            if n_l:
                                cp = cell.add_paragraph()
                            lo, hi = edges[ci], edges[ci + 1]
                            if abs((l["l"] + l["r"]) / 2 - (lo + hi) / 2) < 0.06 * (hi - lo):
                                cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            add_words(cp, l, size_of(l["h"]), is_bold(l))
                            stats["words"] += len(l["words"])
                prev_bottom = group[-1]["b"]
                i = j
                continue

            # ---- bảng có khung
            tb = obj
            nr, nc = len(tb["ys"]) - 1, len(tb["xs"]) - 1
            if space:
                sp = doc.add_paragraph()
                sp.add_run("").font.size = Pt(max(1, space))
            t = doc.add_table(rows=nr, cols=nc)
            t.style = "Table Grid"
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            t.autofit = False
            widths = [max(0.6, (tb["xs"][c + 1] - tb["xs"][c]) * PXCM) for c in range(nc)]
            tot = sum(widths)
            if tot > text_w_cm:
                widths = [w * text_w_cm / tot for w in widths]
            for c in range(nc):
                t.columns[c].width = Cm(widths[c])
                for r in range(nr):
                    t.cell(r, c).width = Cm(widths[c])
            stats["tables"] += 1
            for c in sorted(tb["cells"], key=lambda a: (a[4], a[6])):
                x, y, w, h, r0, r1, c0, c1, (ls, inner_w) = c[:9]
                try:
                    cell = t.cell(r0, c0)
                    if r1 - r0 > 1 or c1 - c0 > 1:
                        cell = cell.merge(t.cell(min(r1, nr) - 1, min(c1, nc) - 1))
                except Exception:
                    cell = t.cell(min(r0, nr - 1), min(c0, nc - 1))
                stats["cells"] += 1
                if not ls:
                    continue
                cp = cell.paragraphs[0]
                # nối các dòng của ô thành 1 đoạn (chữ tự xuống dòng theo độ rộng ô)
                ls = sorted(ls, key=lambda a: a["t"])
                lmin, rmax = min(l["l"] for l in ls), max(l["r"] for l in ls)
                cmid = inner_w / 2 + 14
                if abs((lmin + rmax) / 2 - cmid) < 0.08 * inner_w and (rmax - lmin) < 0.85 * inner_w:
                    cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                elif (inner_w + 14 - rmax) < 0.08 * inner_w and (lmin - 14) > 0.25 * inner_w:
                    cp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                for n_l, l in enumerate(ls):
                    if n_l:
                        run = cp.add_run(" ")
                    add_words(cp, l, size_of(l["h"]), is_bold(l))
                    stats["words"] += len(l["words"])
                    stats["low"] += sum(1 for w in l["words"] if word_unsure(w))
            prev_bottom = tb["bbox"][1] + tb["bbox"][3]
            i += 1

    doc.save(out_path)
    return stats


# ---------------------------------------------------------------- Chính
def pdf_is_scanned(pdf_path):
    d = pymupdf.open(pdf_path)
    try:
        return [len(p.get_text().strip()) < 25 for p in d]
    finally:
        d.close()


def convert_pdf(pdf_path, docx_path, log=print, progress=None):
    """Chuyển 1 PDF. Trả về (đường dẫn, ghi chú)."""
    flags = pdf_is_scanned(pdf_path)
    if not any(flags):
        from pdf2docx import Converter
        log("PDF có chữ thật -> chuyển trực tiếp, giữ nguyên bảng và định dạng")
        cv = Converter(pdf_path)
        try:
            cv.convert(docx_path)
        finally:
            cv.close()
        return docx_path, "PDF có chữ thật, chuyển trực tiếp."
    n_scan = sum(flags)
    log(f"PDF quét: {n_scan}/{len(flags)} trang là ảnh -> đọc chữ bằng OCR")
    d = pymupdf.open(pdf_path)
    pages, dims = [], []
    try:
        for i, page in enumerate(d):
            log(f"Trang {i + 1}/{len(d)}")
            if progress:
                progress(i, len(d))
            dims.append((page.rect.width, page.rect.height))
            pages.append(analyze_page(render_page(page), log))
    finally:
        d.close()
    stats = build_docx(pages, dims, docx_path)
    note = (f"Đã đọc {stats['words']} chữ, {stats['tables']} bảng ({stats['cells']} ô). "
            f"{stats['low']} chữ máy chưa chắc được TÔ VÀNG trong Word, anh liếc kiểm. ")
    if stats["dropped"]:
        note += f"Bỏ {stats['dropped']} dòng nhiễu (logo/dấu mộc/vết bẩn), nếu thiếu thì gõ bổ sung."
    return docx_path, note


# ---------------------------------------------------------------- Soát lỗi hai bên
def _cell_origin(c):
    return max(0, c[0] - 2) - 14, max(0, c[1] - 2) - 14


def iter_lines(pi, pg):
    """Mọi dòng chữ của một trang kèm mã định danh và gốc tọa độ (để vẽ khung lên ảnh trang)."""
    for n, l in enumerate(pg["lines"]):
        yield f"t{pi}.{n}", l, 0, 0
    for ti, tb in enumerate(pg["tables"]):
        for ci, c in enumerate(tb["cells"]):
            ox, oy = _cell_origin(c)
            for n, l in enumerate(c[8][0]):
                yield f"c{pi}.{ti}.{ci}.{n}", l, ox, oy


def assign_ids(pages):
    for pi, pg in enumerate(pages):
        for lid, l, ox, oy in iter_lines(pi, pg):
            l["id"] = lid
            l["words0"], l["txt0"] = l["words"], l["txt"]


def apply_edits(pages, edits):
    """Áp chữ anh đã sửa vào các dòng (làm lại từ bản máy đọc gốc, nên sửa nhiều lần không chồng nhau)."""
    for pi, pg in enumerate(pages):
        for lid, l, ox, oy in iter_lines(pi, pg):
            if "words0" not in l:
                continue
            new = edits.get(lid)
            if new is None or new.strip() == l["txt0"].strip():
                l["words"], l["txt"] = l["words0"], l["txt0"]
                continue
            new = " ".join(new.split())
            w0 = l["words0"][0] if l["words0"] else dict(b=0, p=0, l=0, x=l["l"], y=l["t"], w=l["r"] - l["l"], h=l["b"] - l["t"])
            if not new:
                l["words"], l["txt"] = [], ""
            else:
                l["words"] = [dict(w0, txt=new, conf=100.0, unsure=False,
                                   x=l["l"], y=l["t"], w=l["r"] - l["l"], h=l["b"] - l["t"])]
                l["txt"] = new


def review_data(pages):
    out = []
    for pi, pg in enumerate(pages):
        H, W = pg["size"][0], pg["size"][1]
        lines = []
        for lid, l, ox, oy in iter_lines(pi, pg):
            if not l.get("words0"):
                continue
            bad = [w["txt"] for w in l["words0"] if word_unsure(w)]
            lines.append(dict(id=lid, txt=l["txt0"], bad=bad,
                              x=int(ox + l["l"]), y=int(oy + l["t"]), w=int(l["r"] - l["l"]), h=int(l["b"] - l["t"])))
        lines.sort(key=lambda a: (round(a["y"] / 25), a["x"]))
        out.append(dict(n=pi, w=int(W), h=int(H), lines=lines))
    return out


def convert_for_review(pdf_path, docx_path, workdir, log=print, progress=None, lock=None):
    """Chuyển PDF -> Word. PDF quét: giữ lại kết quả đọc chữ + ảnh từng trang để soát lỗi hai bên.
    lock: khóa dùng chung với phần còn lại của app (PyMuPDF không chạy song song được)."""
    import contextlib
    lock = lock or contextlib.nullcontext()
    with lock:
        flags = pdf_is_scanned(pdf_path)
    if not any(flags):
        from pdf2docx import Converter
        log("PDF có chữ thật -> chuyển trực tiếp")
        with lock:
            cv = Converter(pdf_path)
            try:
                cv.convert(docx_path)
            finally:
                cv.close()
        return dict(kind="text", note="File này có chữ thật nên chuyển thẳng, giữ nguyên bảng và định dạng. Không cần soát lỗi đọc chữ.")
    os.makedirs(workdir, exist_ok=True)
    with lock:
        d = pymupdf.open(pdf_path)
        total = len(d)
        d.close()
    pages, dims = [], []
    for i in range(total):
        log(f"Trang {i + 1}/{total}")
        if progress:
            progress(i, total)
        with lock:
            d = pymupdf.open(pdf_path)
            try:
                page = d[i]
                dims.append((page.rect.width, page.rect.height))
                gray = render_page(page)
            finally:
                d.close()
        pg = analyze_page(gray, log)
        g = pg["gray"]
        sc = min(1.0, 1300.0 / g.shape[1])
        small = cv2.resize(g, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA) if sc < 1 else g
        cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 82])[1].tofile(os.path.join(workdir, f"p{i}.jpg"))
        pages.append(pg)
    if progress:
        progress(total, total)
    assign_ids(pages)
    stats = build_docx(pages, dims, docx_path)
    note = (f"Đã đọc {stats['words']} chữ, {stats['tables']} bảng ({stats['cells']} ô). "
            f"{stats['low']} chữ máy chưa chắc.")
    if stats["dropped"]:
        note += f" Bỏ {stats['dropped']} dòng nhiễu (logo/dấu mộc/vết bẩn)."
    return dict(kind="scan", pages=pages, dims=dims, stats=stats, note=note)
