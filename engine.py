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
    if clean is None:
        return 0.0
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

    def size_l(l):
        if "size" in l:
            return max(7.0, min(28.0, round(l["size"] * 2) / 2))
        return size_of(l["h"])

    def size_of(h):
        s = 12.0 * h / med_h
        s = max(9.0, min(24.0, s))
        # dấu chồng nhau làm khung chữ cao lên -> gom cỡ gần 12 về đúng 12 cho đều
        if abs(s - 12) < 3.4:
            s = 12.0
        return round(s * 2) / 2

    def is_bold(l):
        if "bold" in l:
            return bool(l["bold"])
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
            run.font.bold = (w["bold"] if "bold" in w else bold) or None
            if w.get("italic") or (line.get("italic") and "italic" not in w):
                run.font.italic = True
            if line.get("underline"):
                run.font.underline = True
            if line.get("font"):
                fam = clean_font_name(line["font"], _FONT_CACHE)
                run.font.name = fam
                run._element.rPr.rFonts.set(qn("w:eastAsia"), fam)
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
                if it["l"] - cols[-1][-1]["r"] > (0.02 if it.get("size") is not None else 0.07) * W:
                    cols.append([it])
                else:
                    cols[-1].append(it)
            bnd["cols"] = cols
            blocks.append(("band", bnd, bnd["t"]))
        for tb in pg["tables"]:
            blocks.append(("table", tb, tb["bbox"][1]))
        for im in pg.get("images", []):
            blocks.append(("image", im, im["y"]))
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

            if kind == "image":
                import io as _io
                ip = doc.add_paragraph()
                ip.paragraph_format.space_before = Pt(min(18.0, space))
                wcm = min(text_w_cm, max(0.8, obj["w"] * PXCM))
                mid = obj["x"] + obj["w"] / 2
                if abs(mid - W / 2) < 0.06 * W:
                    ip.alignment = WD_ALIGN_PARAGRAPH.CENTER
                elif obj["x"] + obj["w"] > 0.85 * W and obj["x"] > 0.5 * W:
                    ip.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                else:
                    ind = (obj["x"] - base_left_px) * PXCM
                    if ind > 1.2:
                        ip.paragraph_format.left_indent = Cm(ind)
                ip.add_run().add_picture(_io.BytesIO(obj["png"]), width=Cm(wcm))
                prev_bottom = obj["y"] + obj["h"]
                prev_par, prev_full = None, False
                i += 1
                continue

            if kind == "band" and len(obj["cols"]) >= 2 and obj["cols"][0][0].get("size") is not None:
                # ---- PDF chữ thật: vùng nhiều cột (đầu thư: cơ quan bên trái, quốc hiệu bên phải) -> bảng 1 hàng, mỗi cột xếp dòng theo thứ tự
                k = len(obj["cols"])
                rng = [[min(l["l"] for l in obj["cols"][c]), max(l["r"] for l in obj["cols"][c])] for c in range(k)]
                tol = 0.04 * W
                region = [(obj, None)]
                j = i + 1
                while j < len(blocks) and blocks[j][0] == "band":
                    b2 = blocks[j][1]
                    if (b2["t"] - region[-1][0]["b"]) * 72 / DPI > 40:
                        break
                    if len(b2["cols"]) == k:
                        region.append((b2, None))
                    elif len(b2["cols"]) == 1:
                        ln0 = b2["cols"][0]
                        lo_, hi_ = min(l["l"] for l in ln0), max(l["r"] for l in ln0)
                        hit = [c for c in range(k) if lo_ >= rng[c][0] - tol and hi_ <= rng[c][1] + tol]
                        if not hit:
                            break
                        region.append((b2, hit[0]))
                    else:
                        break
                    j += 1
                    for bb, only in region[-1:]:
                        for c in range(k):
                            ls_ = bb["cols"][c] if only is None else (bb["cols"][0] if c == only else [])
                            if ls_:
                                rng[c][0] = min(rng[c][0], min(l["l"] for l in ls_))
                                rng[c][1] = max(rng[c][1], max(l["r"] for l in ls_))
                colines = [[] for _ in range(k)]
                for bb, only in region:
                    for c in range(k):
                        ls_ = bb["cols"][c] if only is None else (bb["cols"][0] if c == only else [])
                        colines[c].extend(ls_)
                edges = [base_left_px] + [(rng[c - 1][1] + rng[c][0]) / 2 for c in range(1, k)] + [W - right_m / PXCM]
                top0 = min(l["t"] for cl in colines for l in cl)
                if space:
                    sp = doc.add_paragraph()
                    sp.paragraph_format.space_before = Pt(0)
                    sp.add_run("").font.size = Pt(max(1, space))
                t = doc.add_table(rows=1, cols=k)
                t.autofit = False
                from docx.oxml import OxmlElement
                mar = OxmlElement("w:tblCellMar")
                for side in ("left", "right"):
                    e_ = OxmlElement("w:" + side)
                    e_.set(qn("w:w"), "0")
                    e_.set(qn("w:type"), "dxa")
                    mar.append(e_)
                t._tbl.tblPr.append(mar)
                for ci in range(k):
                    t.cell(0, ci).width = Cm(max(1.5, (edges[ci + 1] - edges[ci]) * PXCM))
                for ci in range(k):
                    cell = t.cell(0, ci)
                    lo, hi = edges[ci], edges[ci + 1]
                    prev_b = None
                    first = True
                    for l in sorted(colines[ci], key=lambda a: a["t"]):
                        cp = cell.paragraphs[0] if first else cell.add_paragraph()
                        gap = (l["t"] - (prev_b if prev_b is not None else top0)) * 72 / DPI
                        cp.paragraph_format.space_before = Pt(max(0.0, min(40.0, (gap - (0 if prev_b is None else 2)) * 0.9)))
                        if abs((l["l"] + l["r"]) / 2 - (lo + hi) / 2) < 0.06 * (hi - lo):
                            cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        else:
                            ind = (l["l"] - lo) * PXCM
                            if ind > 0.8:
                                cp.paragraph_format.left_indent = Cm(ind)
                        add_words(cp, l, size_l(l), is_bold(l))
                        stats["words"] += len(l["words"])
                        prev_b = l["b"]
                        first = False
                prev_bottom = max(l["b"] for cl in colines for l in cl)
                prev_par, prev_full = None, False
                i = j
                continue

            if kind == "band" and len(obj["cols"]) == 1:
                # ---- đoạn văn thường (có thể gộp dòng tiếp nối)
                items = obj["cols"][0]
                for l in items:
                    size = size_l(l)
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
                            add_words(cp, l, size_l(l), is_bold(l))
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
                    add_words(cp, l, size_l(l), is_bold(l))
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
        try:
            st = convert_native(pdf_path, docx_path)
            if st is not None:
                return docx_path, f"PDF có chữ thật: dựng lại bố cục chuẩn ({st['words']} chữ, {st['tables']} bảng)."
        except Exception as e:
            log(f"Cách dựng mới không được ({e}), dùng cách dự phòng")
        from pdf2docx import Converter
        log("PDF có chữ thật -> chuyển trực tiếp, giữ nguyên bảng và định dạng")
        cv = Converter(pdf_path)
        try:
            cv.convert(docx_path)
        finally:
            cv.close()
        fix_docx_fonts(docx_path)
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
        try:
            st = convert_native(pdf_path, docx_path, lock)
            if st is not None:
                return dict(kind="text", note=f"File có chữ thật: Teemo dựng lại bố cục chuẩn ({st['words']} chữ, {st['tables']} bảng), giữ font, chữ đậm, nghiêng. Không cần soát lỗi đọc chữ.")
        except Exception as e:
            log(f"Cách dựng mới không được ({e}), dùng cách dự phòng")
        from pdf2docx import Converter
        log("PDF có chữ thật -> chuyển trực tiếp")
        with lock:
            cv = Converter(pdf_path)
            try:
                cv.convert(docx_path)
            finally:
                cv.close()
        fix_docx_fonts(docx_path)
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


# ---------------------------------------------------------------- Sửa tên font trong Word
_FONT_MAP = [  # (tiền tố tên đã bỏ khoảng trắng, viết thường) -> tên font Windows/Office thật
    ("timesnewroman", "Times New Roman"), ("times", "Times New Roman"), ("arialnarrow", "Arial Narrow"),
    ("arialblack", "Arial Black"), ("arial", "Arial"), ("calibri", "Calibri"), ("cambria", "Cambria"),
    ("tahoma", "Tahoma"), ("verdana", "Verdana"), ("couriernew", "Courier New"), ("courier", "Courier New"),
    ("segoeui", "Segoe UI"), ("georgia", "Georgia"), ("palatino", "Palatino Linotype"),
    ("bookantiqua", "Book Antiqua"), ("trebuchet", "Trebuchet MS"), ("comicsans", "Comic Sans MS"),
    ("helvetica", "Arial"), ("symbol", "Symbol"), ("wingdings", "Wingdings"), ("constantia", "Constantia"),
    ("candara", "Candara"), ("consolas", "Consolas"), ("garamond", "Garamond"), ("century", "Century Schoolbook"),
]


def _installed_fonts():
    out = set()
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                k = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
            except OSError:
                continue
            i = 0
            while True:
                try:
                    name = winreg.EnumValue(k, i)[0]
                except OSError:
                    break
                i += 1
                out.add(name.split("(")[0].strip().lower())
    except Exception:
        pass
    return out


_FONT_CACHE = None


def clean_font_name(name, installed=None):
    """TimesNewRomanPS-BoldMT / ABCDEF+Arial,Bold ... -> tên font có thật trong Word."""
    import re
    n = re.sub(r"^[A-Z]{6}\+", "", name.strip())
    key = re.sub(r"[\s_]", "", n).lower()
    for pre, real in _FONT_MAP:
        if key.startswith(pre):
            return real
    base = re.sub(r"([-,](bold|italic|oblique|regular|roman|light|medium|semibold|black))+$", "", n, flags=re.I)
    base = re.sub(r"(ps)?mt$", "", base, flags=re.I).strip()
    global _FONT_CACHE
    if installed is None:
        if _FONT_CACHE is None:
            _FONT_CACHE = _installed_fonts()
        installed = _FONT_CACHE
    if base and (base.lower() in installed or any(f.startswith(base.lower()) for f in installed)):
        return base
    return "Times New Roman"  # font lạ không có trên máy: Word sẽ tự thay bằng font xấu, nên chọn font văn bản chuẩn


def fix_docx_fonts(docx_path):
    """Sửa tên font trong file Word do pdf2docx tạo ra (PDF hay ghi tên kiểu PostScript mà Word không có)."""
    import re
    import shutil
    import zipfile
    installed = _installed_fonts()
    tag = re.compile(r"<w:rFonts[^>]*>")
    pat = re.compile(r'(w:(?:ascii|hAnsi|eastAsia|cs)=")([^"]*)(")')
    tmp = docx_path + ".tmp"
    changed = {}
    with zipfile.ZipFile(docx_path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.startswith("word/") and item.filename.endswith(".xml") and b"w:rFonts" in data:
                txt = data.decode("utf-8")

                def sub(m):
                    new = clean_font_name(m.group(2), installed)
                    if new != m.group(2):
                        changed[m.group(2)] = new
                    return m.group(1) + new + m.group(3)

                txt = tag.sub(lambda t: pat.sub(sub, t.group(0)), txt)
                data = txt.encode("utf-8")
            zout.writestr(item, data)
    shutil.move(tmp, docx_path)
    return changed


# ---------------------------------------------------------------- PDF chữ thật: tự dựng bố cục
def _span_style(spans, cx, cy):
    """Kiểu chữ (font, cỡ, đậm, nghiêng) của mảnh chữ chứa điểm (cx, cy)."""
    best = None
    for sp in spans:
        x0, y0, x1, y1 = sp["bbox"]
        if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
            return sp
        if best is None:
            best = sp
    return best


def _words_to_lines(words, key_fn):
    d = {}
    for w in words:
        d.setdefault(key_fn(w), []).append(w)
    lines = []
    for key, ws in d.items():
        ws.sort(key=lambda a: a["x"])
        sizes = [a["size"] for a in ws]
        bold = all(a["bold"] for a in ws)
        lines.append(dict(
            key=(key[0], key[1], 0), words=ws, txt=" ".join(a["txt"] for a in ws),
            l=min(a["x"] for a in ws), t=min(a["y"] for a in ws),
            r=max(a["x"] + a["w"] for a in ws), b=max(a["y"] + a["h"] for a in ws),
            h=float(np.percentile([a["h"] for a in ws], 75)), conf=100.0,
            size=float(np.median(sizes)), bold=bold,
            italic=all(a["italic"] for a in ws), font=ws[0]["font"]))
    return lines


def native_pages(pdf_path, lock=None):
    """Đọc PDF chữ thật thành cùng cấu trúc với bản quét (dòng chữ + bảng), để dùng chung bộ dựng Word.
    Trả về (pages, dims) hoặc None nếu file có hình lớn / bố cục phức tạp (khi đó dùng pdf2docx)."""
    import contextlib
    lock = lock or contextlib.nullcontext()
    K = DPI / 72.0
    pages, dims = [], []
    with lock:
        doc = pymupdf.open(pdf_path)
        try:
            for page in doc:
                pr = page.rect
                area = pr.width * pr.height
                big = 0.0
                imgrects = []
                for im in page.get_image_info():
                    r = pymupdf.Rect(im["bbox"]) & pr
                    if r.is_empty or r.width < 6 or r.height < 6:
                        continue
                    big += r.width * r.height
                    imgrects.append(r)
                if big > 0.12 * area or len(imgrects) > 5:
                    return None  # tài liệu thiết kế nhiều hình/biểu tượng: để pdf2docx giữ hình dáng
                try:
                    for dr in page.get_drawings():
                        fill = dr.get("fill")
                        r = dr["rect"]
                        if fill and r.width * r.height > 0.04 * area and min(fill) < 0.93:
                            return None  # có mảng màu nền lớn (banner, khung màu)
                except Exception:
                    pass
                words = []
                for bn, b in enumerate(page.get_text("rawdict")["blocks"]):
                    if b.get("type") != 0:
                        continue
                    for ln, line in enumerate(b["lines"]):
                        chars = []
                        for sp in line["spans"]:
                            fl, fname, size = sp["flags"], sp["font"], sp["size"]
                            low = fname.lower()
                            st = dict(size=size, font=fname,
                                      bold=bool(fl & 16) or "bold" in low or "black" in low,
                                      italic=bool(fl & 2) or "italic" in low or "oblique" in low)
                            for ch in sp["chars"]:
                                chars.append((ch["c"], ch["bbox"], st))
                        chars.sort(key=lambda a: a[1][0])
                        cur, wn = [], 0

                        def flush():
                            nonlocal cur, wn
                            if not cur:
                                return
                            x0 = min(c[1][0] for c in cur)
                            y0 = min(c[1][1] for c in cur)
                            x1 = max(c[1][2] for c in cur)
                            y1 = max(c[1][3] for c in cur)
                            st0 = cur[0][2]
                            words.append(dict(b=bn, p=ln, l=wn, txt="".join(c[0] for c in cur), conf=100.0, unsure=False,
                                              x=x0 * K, y=y0 * K, w=(x1 - x0) * K, h=(y1 - y0) * K,
                                              size=st0["size"], bold=st0["bold"], italic=st0["italic"], font=st0["font"]))
                            wn += 1
                            cur = []

                        for c in chars:
                            if c[0].isspace():
                                flush()
                                continue
                            if cur and c[1][0] - cur[-1][1][2] > 0.16 * c[2]["size"]:
                                flush()
                            cur.append(c)
                        flush()
                images = []
                for r in imgrects:
                    over = sum(1 for w in words if r.contains(pymupdf.Point((w["x"] + w["w"] / 2) / K, (w["y"] + w["h"] / 2) / K)))
                    if over:
                        continue  # ảnh nền có chữ nằm đè lên: bỏ, tránh chữ bị lặp
                    try:
                        png = page.get_pixmap(clip=r, dpi=150, alpha=False).tobytes("png")
                    except Exception:
                        continue
                    images.append(dict(x=r.x0 * K, y=r.y0 * K, w=r.width * K, h=r.height * K, png=png))
                # bảng có khung
                tables = []
                tb_rects = []
                try:
                    found = page.find_tables(strategy="lines_strict").tables
                except Exception:
                    found = []
                for t in found:
                    cells = [c for c in t.cells if c]
                    if len(cells) < 2 or t.row_count < 1 or t.col_count < 1:
                        continue
                    bb = t.bbox
                    tb_rects.append(pymupdf.Rect(bb))
                    cl = []
                    for (cx0, cy0, cx1, cy1) in cells:
                        cl.append([int(cx0 * K), int(cy0 * K), max(2, int((cx1 - cx0) * K)), max(2, int((cy1 - cy0) * K))])
                    tb = dict(bbox=(int(bb[0] * K), int(bb[1] * K), int((bb[2] - bb[0]) * K), int((bb[3] - bb[1]) * K)), cells=cl)
                    build_grid(tb)
                    for c in tb["cells"]:
                        cr = pymupdf.Rect(c[0] / K, c[1] / K, (c[0] + c[2]) / K, (c[1] + c[3]) / K)
                        inside = [w for w in words if cr.contains(pymupdf.Point((w["x"] + w["w"] / 2) / K, (w["y"] + w["h"] / 2) / K))]
                        for w in inside:
                            w["_used"] = True
                        # gom thành dòng theo độ chồng dọc
                        inside.sort(key=lambda a: (a["y"], a["x"]))
                        grp = []
                        for w in inside:
                            for g in grp:
                                ov = min(g["b"], w["y"] + w["h"]) - max(g["t"], w["y"])
                                if ov > 0.5 * min(g["b"] - g["t"], w["h"]):
                                    g["ws"].append(w)
                                    g["t"], g["b"] = min(g["t"], w["y"]), max(g["b"], w["y"] + w["h"])
                                    break
                            else:
                                grp.append(dict(t=w["y"], b=w["y"] + w["h"], ws=[w]))
                        ox, oy = c[0] - 16, c[1] - 16
                        ls = []
                        for gi, g in enumerate(grp):
                            ws = [dict(w, x=w["x"] - ox, y=w["y"] - oy) for w in g["ws"]]
                            for w in ws:
                                w["b"], w["p"] = 0, gi
                            ls.extend(_words_to_lines(ws, lambda a, gi=gi: (0, gi)))
                        c.append((ls, c[2] + 4))
                    tables.append(tb)
                # chữ ngoài bảng
                rest = [w for w in words if not w.get("_used")]
                lines = _words_to_lines(rest, lambda a: (a["b"], a["p"]))
                # gạch chân: nét ngang mảnh nằm sát dưới dòng chữ
                try:
                    hl = []
                    for dr in page.get_drawings():
                        r = dr["rect"]
                        if r.height <= 1.6 and r.width > 8:
                            hl.append(r)
                    for l in lines:
                        bot = l["b"] / K
                        x0, x1 = l["l"] / K, l["r"] / K
                        for r in hl:
                            top_ = l["t"] / K
                            if top_ + 0.7 * (bot - top_) <= r.y0 <= bot + 0.3 * l["size"] and min(r.x1, x1) - max(r.x0, x0) >= 0.6 * (x1 - x0):
                                l["underline"] = True
                                break
                except Exception:
                    pass
                pages.append(dict(gray=None, clean=None, tables=tables, lines=lines, dropped=0, images=images,
                                  size=(int(pr.height * K), int(pr.width * K))))
                dims.append((pr.width, pr.height))
        finally:
            doc.close()
    return pages, dims


def convert_native(pdf_path, docx_path, lock=None):
    """Thử dựng Word từ PDF chữ thật. Trả về stats, hoặc None nếu nên dùng pdf2docx."""
    res = native_pages(pdf_path, lock)
    if res is None:
        return None
    pages, dims = res
    if not any(pg["lines"] or pg["tables"] for pg in pages):
        return None
    return build_docx(pages, dims, docx_path)
