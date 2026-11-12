"""Máy chủ nội bộ cho "Bàn làm việc PDF": cắt, nối, xoay, sắp xếp trang.
Chỉ nghe trên 127.0.0.1 (chính máy này), có mã bí mật trong mỗi lần gọi. File không đi đâu.
File gốc không bao giờ bị sửa: kết quả luôn là file mới trong Desktop\\PDF-ket-qua.
"""
import argparse
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)

APP_DIR = Path(__file__).resolve().parent
HOME = Path.home()
DATA = Path(os.environ.get("LOCALAPPDATA", str(HOME / "AppData" / "Local"))) / "PDF-Tool"
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
_desk = HOME / "Desktop"
OUT = (_desk if _desk.exists() else HOME / "Documents") / "PDF-ket-qua"
OUT.mkdir(parents=True, exist_ok=True)
LOG = DATA / "log.txt"
MAX_UPLOAD = 1024 * 1024 * 1024  # 1 GB mỗi file

def _lay_ma():
    p = DATA / "token.txt"
    try:
        m = p.read_text(encoding="utf-8").strip()
        if len(m) >= 20:
            return m
    except Exception:
        pass
    m = secrets.token_urlsafe(18)
    try:
        p.write_text(m, encoding="utf-8")
    except Exception:
        pass
    return m


TOKEN = _lay_ma()
PORT = 0
LOCK = threading.RLock()          # PyMuPDF không an toàn khi dùng song song
FILES = {}                        # id -> dict(name, path, pages, sizes)
DOCS = {}                         # id -> Document đang mở (để vẽ ảnh thu nhỏ nhanh)
THUMBS = OrderedDict()
LAST_PING = time.time()
SEEN_PING = False
DEV_DIR = None


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def safe_name(s, default="file"):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s)).strip(" .")
    return (s or default)[:120]


def unique_path(folder, stem, ext):
    p = folder / (stem + ext)
    n = 2
    while p.exists():
        p = folder / f"{stem} ({n}){ext}"
        n += 1
    return p


def get_doc(fid):
    d = DOCS.get(fid)
    if d is None:
        d = pymupdf.open(FILES[fid]["path"])
        DOCS[fid] = d
    return d


class Loi(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------ nghiệp vụ
def nhan_file(name, stream, length):
    if length <= 0:
        raise Loi("File rỗng.")
    if length > MAX_UPLOAD:
        raise Loi("File quá lớn (tối đa 1 GB).", 413)
    fid = uuid.uuid4().hex[:12]
    part = UPLOADS / f"{fid}.part"
    left = length
    with open(part, "wb") as f:
        while left > 0:
            chunk = stream.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left > 0:
        raise Loi("Tải file bị đứt giữa chừng, thử lại.")
    path = UPLOADS / f"{fid}.pdf"
    os.replace(part, path)
    with LOCK:
        try:
            doc = pymupdf.open(path)
        except Exception:
            raise Loi("Không mở được file này. Có thể không phải PDF hoặc file bị hỏng.")
        if doc.needs_pass:
            doc.close()
            raise Loi("File này có mật khẩu. Anh bỏ mật khẩu trước rồi thêm lại nhé.")
        if doc.page_count == 0:
            doc.close()
            raise Loi("File PDF này không có trang nào.")
        sizes = []
        for i in range(doc.page_count):
            r = doc.load_page(i).rect
            sizes.append([round(r.width, 1), round(r.height, 1)])
        DOCS[fid] = doc
        FILES[fid] = dict(name=name, path=str(path), pages=doc.page_count, sizes=sizes, size=path.stat().st_size)
    return dict(id=fid, name=name, pages=len(sizes), sizes=sizes, size=path.stat().st_size)


def ve_trang(fid, page, width, rot):
    key = (fid, page, width, rot)
    with LOCK:
        if key in THUMBS:
            THUMBS.move_to_end(key)
            return THUMBS[key]
        if fid not in FILES:
            raise Loi("Không tìm thấy file.", 404)
        doc = get_doc(fid)
        if page < 0 or page >= doc.page_count:
            raise Loi("Trang không tồn tại.", 404)
        pg = doc.load_page(page)
        orig = pg.rotation
        if rot:
            pg.set_rotation((orig + rot) % 360)
        try:
            w0 = pg.rect.width or 1
            s = max(0.05, width / w0)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
            data = pix.tobytes("jpeg", jpg_quality=78)
        finally:
            if rot:
                pg.set_rotation(orig)
        THUMBS[key] = data
        while len(THUMBS) > 500:
            THUMBS.popitem(last=False)
        return data


def kiem_tra_muc(it):
    fid = it.get("file")
    if fid not in FILES:
        raise Loi("Có trang thuộc file không còn trong phiên làm việc. Thêm lại file đó.")
    p = int(it.get("page", -1))
    if p < 0 or p >= FILES[fid]["pages"]:
        raise Loi("Số trang không hợp lệ.")
    r = int(it.get("rot", 0)) % 360
    if r % 90:
        raise Loi("Góc xoay không hợp lệ.")
    return fid, p, r


def tao_mot_file(name, items, cache):
    """Tạo 1 file PDF mới từ danh sách trang. cache: các file nguồn đã mở (dùng chung cho cả lượt lưu)."""
    if not items:
        raise Loi("Chưa có trang nào để lưu.")
    chk = [kiem_tra_muc(i) for i in items]
    fids = {c[0] for c in chk}
    out = None
    try:
        if len(fids) == 1 and len(chk) >= 0.3 * FILES[chk[0][0]]["pages"]:
            # lấy gần hết file: mở file gốc rồi chọn trang (giữ nguyên chất lượng, mục lục...)
            fid = chk[0][0]
            out = pymupdf.open(FILES[fid]["path"])
            out.select([c[1] for c in chk])
            for idx, c in enumerate(chk):
                if c[2]:
                    pg = out.load_page(idx)
                    pg.set_rotation((pg.rotation + c[2]) % 360)
        else:
            # lấy ít trang / nối nhiều file: chỉ chép đúng các trang cần
            out = pymupdf.open()
            i = 0
            while i < len(chk):
                fid, p, r = chk[i]
                j = i
                while j + 1 < len(chk) and chk[j + 1][0] == fid and chk[j + 1][1] == chk[j][1] + 1:
                    j += 1
                if fid not in cache:
                    cache[fid] = pymupdf.open(FILES[fid]["path"])
                start = out.page_count
                out.insert_pdf(cache[fid], from_page=p, to_page=chk[j][1])
                for k in range(i, j + 1):
                    if chk[k][2]:
                        pg = out.load_page(start + (k - i))
                        pg.set_rotation((pg.rotation + chk[k][2]) % 360)
                i = j + 1
        path = unique_path(OUT, safe_name(name, "ket-qua"), ".pdf")
        out.save(path, garbage=4, deflate=True)
        return dict(name=path.name, path=str(path), pages=len(chk), size=path.stat().st_size)
    finally:
        if out is not None:
            out.close()


def xuat(body):
    outs = body.get("outputs") or []
    if not outs or len(outs) > 1000:
        raise Loi("Yêu cầu lưu không hợp lệ.")
    res = []
    cache = {}
    with LOCK:
        try:
            for o in outs:
                res.append(tao_mot_file(o.get("name", "ket-qua"), o.get("items") or [], cache))
        finally:
            for d in cache.values():
                d.close()
    log(f"xuat {len(res)} file")
    return dict(files=res, folder=str(OUT))



# ------------------------------------------------------------------ nén nhỏ, đánh số trang
MUC_NEN = {
    # tên: (ngưỡng dpi để nén lại ảnh, dpi đích, chất lượng JPEG) — None = không đụng vào ảnh
    "nhe": None,
    "vua": (170, 130, 75),
    "manh": (110, 96, 55),
}


def nen_file(body):
    fid = body.get("file")
    level = body.get("level", "vua")
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    if level not in MUC_NEN:
        raise Loi("Mức nén không hợp lệ.")
    src = Path(FILES[fid]["path"])
    before = src.stat().st_size
    tmp_dir = DATA / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex[:10]}.pdf"
    with LOCK:
        doc = pymupdf.open(src)
        try:
            cfg = MUC_NEN[level]
            if cfg:
                doc.rewrite_images(dpi_threshold=cfg[0], dpi_target=cfg[1], quality=cfg[2])
            doc.save(tmp, garbage=4, clean=True, deflate=True, deflate_images=True, deflate_fonts=True, use_objstms=1)
            pages = doc.page_count
        finally:
            doc.close()
    after = tmp.stat().st_size
    if after >= before * 0.98:
        log(f"nen {level}: khong nho hon ({before} -> {after})")
        return dict(kept=False, before=before, after=after, level=level)
    path = unique_path(OUT, safe_name(body.get("name") or (FILES[fid]["name"][:-4] + "_nen"), "nen"), ".pdf")
    os.replace(tmp, path)
    log(f"nen {level}: {before} -> {after}")
    return dict(kept=True, before=before, after=after, level=level, folder=str(OUT),
                files=[dict(name=path.name, path=str(path), pages=pages, size=after)])


VITRI = {"bl", "bc", "br", "tl", "tc", "tr"}
KIEU = {"n", "trang", "n_tong", "gach"}


def nhan_so(n, tong, kieu):
    return {"n": f"{n}", "trang": f"Trang {n}", "n_tong": f"{n}/{tong}", "gach": f"- {n} -"}[kieu]


def danh_so_trang(body):
    fid = body.get("file")
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    pos = body.get("pos", "bc")
    kieu = body.get("style", "n")
    if pos not in VITRI or kieu not in KIEU:
        raise Loi("Lựa chọn không hợp lệ.")
    try:
        start = int(body.get("start", 1))
        first = int(body.get("from", 1))
        size = float(body.get("size", 11))
    except (TypeError, ValueError):
        raise Loi("Số nhập vào không hợp lệ.")
    size = max(6.0, min(36.0, size))
    n_pages = FILES[fid]["pages"]
    if first < 1 or first > n_pages:
        raise Loi(f"Trang bắt đầu phải từ 1 đến {n_pages}.")
    with LOCK:
        doc = pymupdf.open(FILES[fid]["path"])
        try:
            tong = n_pages - first + start  # số cuối cùng sẽ hiện ra
            for i in range(first - 1, n_pages):
                page = doc.load_page(i)
                n = start + (i - (first - 1))
                text = nhan_so(n, tong, kieu)
                W, H = page.rect.width, page.rect.height  # khung đã tính cả góc xoay của trang
                tw = pymupdf.get_text_length(text, fontname="helv", fontsize=size)
                m = 30.0
                x = {"l": m, "c": (W - tw) / 2, "r": W - m - tw}[pos[1]]
                y = (H - m + size * 0.25) if pos[0] == "b" else (m + size * 0.8)
                pt = pymupdf.Point(x, y) * page.derotation_matrix
                page.insert_text(pt, text, fontsize=size, fontname="helv", color=(0.12, 0.12, 0.12),
                                 rotate=page.rotation)
            path = unique_path(OUT, safe_name(body.get("name") or (FILES[fid]["name"][:-4] + "_danhso"), "danhso"), ".pdf")
            doc.save(path, garbage=3, deflate=True)
        finally:
            doc.close()
    log("danh so trang")
    return dict(files=[dict(name=path.name, path=str(path), pages=n_pages, size=path.stat().st_size)], folder=str(OUT))



# ------------------------------------------------------------------ rút ảnh, chụp trang
def _pix_rgb(doc, xref, smask=0):
    """Ảnh nhúng -> Pixmap RGB (có alpha nếu có mặt nạ trong suốt)."""
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace is not None and pix.colorspace.n > 3:     # CMYK -> RGB
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if smask:
        try:
            mask = pymupdf.Pixmap(doc, smask)
            if (mask.width, mask.height) != (pix.width, pix.height):
                mask = pymupdf.Pixmap(mask, pix.width, pix.height)
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)
            pix = pymupdf.Pixmap(pix, mask)
        except Exception:
            pass
    return pix


def quet_anh(fid):
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    with LOCK:
        doc = get_doc(fid)
        seen = {}
        for pno in range(doc.page_count):
            for im in doc.get_page_images(pno, full=True):
                xref, smask, w, h = im[0], im[1], im[2], im[3]
                e = seen.setdefault(xref, dict(xref=xref, smask=smask, w=w, h=h, pages=[]))
                if pno + 1 not in e["pages"]:
                    e["pages"].append(pno + 1)
        out = sorted(seen.values(), key=lambda e: (e["pages"][0], e["xref"]))
        for e in out:
            e["small"] = e["w"] < 64 or e["h"] < 64
    return dict(images=out, total_pages=FILES[fid]["pages"])


def ve_anh(fid, xref, width):
    key = ("img", fid, xref, width)
    with LOCK:
        if key in THUMBS:
            THUMBS.move_to_end(key)
            return THUMBS[key]
        if fid not in FILES:
            raise Loi("Không tìm thấy file.", 404)
        doc = get_doc(fid)
        smask = 0
        for pno in range(doc.page_count):
            hit = [im for im in doc.get_page_images(pno, full=True) if im[0] == xref]
            if hit:
                smask = hit[0][1]
                break
        else:
            raise Loi("Không tìm thấy hình.", 404)
        try:
            pix = _pix_rgb(doc, xref, smask)
            if pix.width > width:
                h = max(1, round(pix.height * width / pix.width))
                pix = pymupdf.Pixmap(pix, width, h)
            data = pix.tobytes("png")
        except Exception:
            raise Loi("Không đọc được hình này.", 404)
        THUMBS[key] = data
        while len(THUMBS) > 500:
            THUMBS.popitem(last=False)
        return data


def rut_anh(body):
    fid = body.get("file")
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    xrefs = [int(x) for x in (body.get("xrefs") or [])]
    if not xrefs or len(xrefs) > 3000:
        raise Loi("Chưa chọn hình nào.")
    fmt = body.get("format", "goc")
    if fmt not in ("goc", "png", "jpg"):
        raise Loi("Định dạng không hợp lệ.")
    base = safe_name(body.get("name") or (FILES[fid]["name"][:-4] + "_anh"), "anh")
    folder = unique_path(OUT, base, "")
    folder.mkdir(parents=True)
    saved, skipped = [], 0
    with LOCK:
        doc = get_doc(fid)
        info = {}
        for pno in range(doc.page_count):
            for im in doc.get_page_images(pno, full=True):
                info.setdefault(im[0], (im[1], pno + 1))
        for k, xref in enumerate(xrefs, 1):
            if xref not in info:
                skipped += 1
                continue
            smask, pno = info[xref]
            stem = f"anh-{k:03d}-trang{pno}"
            try:
                done = False
                if fmt == "goc":
                    ex = doc.extract_image(xref)
                    if ex and not ex.get("smask") and ex.get("ext") in ("jpeg", "jpg", "png"):
                        p = folder / (stem + (".jpg" if ex["ext"] in ("jpeg", "jpg") else ".png"))
                        p.write_bytes(ex["image"])
                        saved.append(p.name)
                        done = True
                if not done:
                    pix = _pix_rgb(doc, xref, smask)
                    use_jpg = fmt == "jpg"
                    p = folder / (stem + (".jpg" if use_jpg else ".png"))
                    if use_jpg and pix.alpha:
                        pix = pymupdf.Pixmap(pix, 0)
                    if use_jpg:
                        pix.save(p, jpg_quality=92)
                    else:
                        pix.save(p)
                    saved.append(p.name)
            except Exception as e:
                log(f"rut anh {xref}: {e}")
                skipped += 1
    if not saved:
        raise Loi("Không lưu được hình nào.")
    log(f"rut anh: {len(saved)}")
    return dict(images=dict(folder=str(folder), count=len(saved), skipped=skipped, names=saved[:12]), folder=str(OUT))


def chup_trang(body):
    fid = body.get("file")
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    pages = sorted({int(p) for p in (body.get("pages") or [])})
    n = FILES[fid]["pages"]
    if not pages or pages[0] < 1 or pages[-1] > n or len(pages) > 500:
        raise Loi("Chưa chọn trang hợp lệ.")
    dpi = max(72, min(400, int(body.get("dpi", 150))))
    fmt = "jpg" if body.get("format") == "jpg" else "png"
    base = safe_name(body.get("name") or (FILES[fid]["name"][:-4] + "_trang"), "trang")
    folder = unique_path(OUT, base, "")
    folder.mkdir(parents=True)
    names = []
    w = len(str(n))
    with LOCK:
        doc = get_doc(fid)
        for p in pages:
            pix = doc.load_page(p - 1).get_pixmap(dpi=dpi, alpha=False)
            path = folder / f"trang-{str(p).zfill(w)}.{fmt}"
            if fmt == "jpg":
                pix.save(path, jpg_quality=92)
            else:
                pix.save(path)
            names.append(path.name)
    log(f"chup {len(names)} trang")
    return dict(images=dict(folder=str(folder), count=len(names), skipped=0, names=names[:12]), folder=str(OUT))


# ------------------------------------------------------------------ xóa ghi chú, hình mờ
KIEU_GHI_CHU = {
    "Highlight": "Tô sáng", "Underline": "Gạch chân", "StrikeOut": "Gạch ngang", "Squiggly": "Gạch lượn",
    "Text": "Ghi chú (biểu tượng)", "FreeText": "Ô chữ ghi chú", "Ink": "Nét bút vẽ",
    "Square": "Khung chữ nhật", "Circle": "Hình tròn", "Line": "Đường kẻ / mũi tên",
    "Polygon": "Đa giác", "PolyLine": "Đường gấp khúc", "Caret": "Dấu chèn", "Stamp": "Con dấu / nhãn",
    "FileAttachment": "File đính kèm",
}
GAN_WATERMARK = re.compile(r"water|mark|draft|confiden|nh[aáà]p|m[aậ]t", re.I)


def _chuan(s):
    return re.sub(r"\s+", " ", s.strip()).lower()


def quet_don(fid):
    import math
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    n = FILES[fid]["pages"]
    with LOCK:
        doc = get_doc(fid)
        ann, wm_annot = {}, 0
        for page in doc:
            for a in page.annots() or []:
                nm = a.type[1]
                if nm == "Watermark":
                    wm_annot += 1
                elif nm in KIEU_GHI_CHU:
                    ann[nm] = ann.get(nm, 0) + 1
        # chữ mờ lặp lại nhiều trang
        scan = list(range(n)) if n <= 40 else sorted({round(i * (n - 1) / 39) for i in range(40)})
        cand = {}
        for pno in scan:
            d = doc.load_page(pno).get_text("dict")
            for b in d["blocks"]:
                if b.get("type") != 0:
                    continue
                for ln in b["lines"]:
                    dx, dy = ln["dir"]
                    ang = abs(math.degrees(math.atan2(dy, dx))) % 180
                    rotated = 8 < ang < 172
                    for s in ln["spans"]:
                        txt = s["text"].strip()
                        if len(txt) < 3:
                            continue
                        c = s["color"]
                        lum = (0.3 * ((c >> 16) & 255) + 0.59 * ((c >> 8) & 255) + 0.11 * (c & 255)) / 255
                        big = s["size"] >= 28
                        light = lum > 0.6 and s["size"] >= 14
                        if not (rotated or big or light):
                            continue
                        e = cand.setdefault(_chuan(txt), dict(text=txt, pages=set(), rotated=False, light=False, big=False))
                        e["pages"].add(pno)
                        e["rotated"] |= rotated
                        e["light"] |= light
                        e["big"] |= big
        need = max(2, math.ceil(0.5 * len(scan))) if len(scan) > 1 else 1
        texts = []
        for k, e in cand.items():
            ok = len(e["pages"]) >= need and (e["rotated"] or e["light"] or e["big"])
            if len(scan) == 1:
                ok = e["rotated"] or e["light"]
            if ok:
                texts.append(dict(key=k, text=e["text"], pages=len(e["pages"]), of=len(scan), rotated=e["rotated"], light=e["light"]))
        texts.sort(key=lambda e: -e["pages"])
        # hình lặp lại mọi trang
        imgs = []
        if n > 1:
            for e in quet_anh(fid)["images"]:
                if len(e["pages"]) >= max(2, math.ceil(0.5 * n)) and not e["small"]:
                    area = 0.0
                    try:
                        pg = doc.load_page(e["pages"][0] - 1)
                        rs = pg.get_image_rects(e["xref"])
                        if rs:
                            area = (rs[0].width * rs[0].height) / max(1.0, pg.rect.width * pg.rect.height)
                    except Exception:
                        pass
                    imgs.append(dict(xref=e["xref"], pages=len(e["pages"]), w=e["w"], h=e["h"], alpha=bool(e["smask"]), area=round(area, 3),
                                     likely=bool(e["smask"]) and area >= 0.15))
        # lớp (layer) có tên giống watermark
        layers = []
        try:
            for xr, info in (doc.get_ocgs() or {}).items():
                if GAN_WATERMARK.search(info.get("name", "")):
                    layers.append(dict(xref=xr, name=info.get("name", ""), on=bool(info.get("on", True))))
        except Exception:
            pass
    return dict(annots=[dict(type=k, label=KIEU_GHI_CHU[k], count=v) for k, v in sorted(ann.items(), key=lambda kv: -kv[1])],
                watermark_annots=wm_annot, texts=texts, images=imgs, layers=layers, pages=n)


def ap_dung_don(doc, body):
    """Dọn trực tiếp trên doc (đã mở bản riêng). Trả về số lượng đã gỡ."""
    kinds = set(body.get("annot_types") or [])
    do_wm_annot = bool(body.get("watermark_annots"))
    texts = {_chuan(x) for x in (body.get("texts") or [])}
    xrefs = {int(x) for x in (body.get("images") or [])}
    layers = [int(x) for x in (body.get("layers") or [])]
    res = dict(annots=0, text=0, images=0, layers=0)
    for page in doc:
        if kinds or do_wm_annot:
            for a in list(page.annots() or []):
                nm = a.type[1]
                if (nm in kinds) or (do_wm_annot and nm == "Watermark"):
                    page.delete_annot(a)
                    res["annots"] += 1
        if texts:
            raw = page.get_text("rawdict")
            hit = 0
            for b in raw["blocks"]:
                if b.get("type") != 0:
                    continue
                for ln in b["lines"]:
                    for s in ln["spans"]:
                        if _chuan("".join(c["c"] for c in s["chars"])) in texts:
                            for c in s["chars"]:
                                r = pymupdf.Rect(c["bbox"])
                                if r.is_empty:
                                    continue
                                cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                                rr = pymupdf.Rect(cx - r.width * 0.3, cy - r.height * 0.3, cx + r.width * 0.3, cy + r.height * 0.3)
                                page.add_redact_annot(rr, fill=False)
                            hit += 1
            if hit:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
                res["text"] += hit
        if xrefs:
            here = {im[0] for im in page.get_images(full=True)}
            for xr in xrefs & here:
                page.delete_image(xr)
                res["images"] += 1
    if layers:
        try:
            doc.set_layer(-1, off=layers)
            res["layers"] = len(layers)
        except Exception as e:
            log(f"layer: {e}")
    return res


def xem_thu_don(body):
    import base64
    fid = body.get("file")
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    pno = max(0, min(FILES[fid]["pages"] - 1, int(body.get("page", 1)) - 1))
    with LOCK:
        doc = pymupdf.open(FILES[fid]["path"])
        try:
            res = ap_dung_don(doc, body)
            pix = doc.load_page(pno).get_pixmap(matrix=pymupdf.Matrix(1.3, 1.3), alpha=False)
            data = pix.tobytes("jpeg", jpg_quality=80)
        finally:
            doc.close()
    return dict(after="data:image/jpeg;base64," + base64.b64encode(data).decode(), removed=res, page=pno + 1)


def don_file(body):
    fid = body.get("file")
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    tmp_dir = DATA / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex[:10]}.pdf"
    with LOCK:
        doc = pymupdf.open(FILES[fid]["path"])
        try:
            res = ap_dung_don(doc, body)
            if not any(res.values()):
                return dict(kept=False, removed=res)
            doc.save(tmp, garbage=3, deflate=True)
            pages = doc.page_count
        finally:
            doc.close()
    path = unique_path(OUT, safe_name(body.get("name") or (FILES[fid]["name"][:-4] + "_sach"), "sach"), ".pdf")
    os.replace(tmp, path)
    log(f"don file: {res}")
    return dict(kept=True, removed=res, folder=str(OUT),
                files=[dict(name=path.name, path=str(path), pages=pages, size=path.stat().st_size)])


# ------------------------------------------------------------------ chuyển sang Word + soát lỗi
WORDS = OrderedDict()   # mã việc -> trạng thái (giữ tối đa 3 việc gần nhất, mỗi việc nặng vì còn ảnh trang)
WORD_DIR = DATA / "word"


def _engine():
    sys.path.insert(0, str(APP_DIR.parent))
    import engine
    return engine


def word_bat_dau(body):
    fid = body.get("file")
    if fid not in FILES:
        raise Loi("Không tìm thấy file.", 404)
    jid = uuid.uuid4().hex[:10]
    workdir = WORD_DIR / jid
    workdir.mkdir(parents=True, exist_ok=True)
    job = dict(status="running", i=0, n=1, msg="Teemo đang mở file…", file=fid, workdir=str(workdir),
               docx=str(workdir / "ban-chuyen.docx"), name=FILES[fid]["name"], lock=threading.Lock())
    with LOCK:
        WORDS[jid] = job
        while len(WORDS) > 3:
            WORDS.popitem(last=False)

    def run():
        try:
            eng = _engine()

            def lg(m):
                job["msg"] = str(m).strip()

            def pr(i, n):
                job["i"], job["n"] = i, max(1, n)

            res = eng.convert_for_review(FILES[fid]["path"], job["docx"], str(workdir), log=lg, progress=pr, lock=LOCK)
            job["kind"] = res["kind"]
            job["note"] = res["note"]
            if res["kind"] == "scan":
                job["pages"], job["dims"] = res["pages"], res["dims"]
                job["review"] = eng.review_data(res["pages"])
                job["ids"] = {l["id"] for pg in job["review"] for l in pg["lines"]}
            job["status"] = "done"
            log(f"word xong {jid} ({res['kind']})")
        except Exception as e:
            log("LOI word: " + "".join(traceback.format_exception(e)))
            job["status"] = "error"
            job["error"] = str(e) or e.__class__.__name__

    threading.Thread(target=run, daemon=True).start()
    return dict(job=jid)


def lay_viec_word(jid):
    job = WORDS.get(jid)
    if not job:
        raise Loi("Việc chuyển này không còn trong bộ nhớ (app đã bật lại?). Anh chuyển lại nhé.", 404)
    return job


def word_trang_thai(jid):
    job = lay_viec_word(jid)
    r = dict(status=job["status"], i=job["i"], n=job["n"], msg=job["msg"])
    if job["status"] == "done":
        r.update(kind=job["kind"], note=job["note"])
    if job["status"] == "error":
        r["error"] = job.get("error", "Lỗi không rõ")
    return r


def word_soat(jid):
    job = lay_viec_word(jid)
    if job["status"] != "done" or job.get("kind") != "scan":
        raise Loi("Việc này không có phần soát lỗi.")
    return dict(pages=job["review"], note=job["note"])


def word_anh_trang(jid, n):
    job = lay_viec_word(jid)
    f = Path(job["workdir"]) / f"p{n}.jpg"
    if not f.is_file():
        raise Loi("Không có ảnh trang.", 404)
    return f.read_bytes()


def word_luu(body):
    job = lay_viec_word(body.get("job"))
    if job["status"] != "done":
        raise Loi("Việc chuyển chưa xong.")
    edits = body.get("edits") or {}
    if not isinstance(edits, dict) or len(edits) > 20000:
        raise Loi("Dữ liệu sửa không hợp lệ.")
    name = safe_name(body.get("name") or (job["name"][:-4] + "_word"), "word")
    path = unique_path(OUT, name, ".docx")
    n_sua = 0
    with job["lock"]:
        if job["kind"] == "scan":
            ids = job["ids"]
            ed = {k: v for k, v in edits.items() if k in ids and isinstance(v, str) and len(v) < 2000}
            n_sua = len(ed)
            _engine().build_docx(job["pages"], job["dims"], str(path), ed)
        else:
            import shutil
            shutil.copyfile(job["docx"], path)
    log(f"word luu {path.name}, {n_sua} dòng sửa")
    npg = FILES.get(job["file"], {}).get("pages", 0)
    return dict(files=[dict(name=path.name, path=str(path), pages=npg, size=path.stat().st_size)],
                folder=str(OUT), edited=n_sua)


def trong_thu_muc_ket_qua(p):
    try:
        p = Path(p).resolve()
        return OUT.resolve() in p.parents and p.exists()
    except Exception:
        return False


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PDFTool"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # pythonw không có stderr
        pass

    # --- tiện ích
    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _raw(self, code, data, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _kiem_tra_nguon(self):
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            raise Loi("Địa chỉ không hợp lệ.", 403)
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            raise Loi("Nguồn gọi không hợp lệ.", 403)

    def _kiem_tra_ma(self, q):
        t = self.headers.get("X-Token") or (q.get("t") or [""])[0]
        if not secrets.compare_digest(t, TOKEN):
            raise Loi("Thiếu mã bí mật. Anh bấm lối tắt Teemo cùng PDF trên Desktop để mở lại.", 403)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 16 * 1024 * 1024:
            raise Loi("Dữ liệu gửi lên không hợp lệ.")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # --- điều phối
    def do_GET(self):
        self._xu_ly("GET")

    def do_POST(self):
        self._xu_ly("POST")

    def _xu_ly(self, method):
        global LAST_PING, SEEN_PING
        try:
            self._kiem_tra_nguon()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path

            if method == "GET" and path == "/":
                if not secrets.compare_digest((q.get("t") or [""])[0], TOKEN):
                    cu = ("<!doctype html><meta charset='utf-8'><title>Teemo cùng PDF</title><body style=\"font-family:Candara,Segoe UI,sans-serif;"
                          "background:#1F4D3A;color:#F6EFD9;text-align:center;padding:12vh 20px\"><div style='font-size:64px'>🍄</div>"
                          "<h1 style='font-family:Constantia,Georgia,serif'>Trang này đã cũ rồi</h1><p style='font-size:20px'>Anh đóng thẻ này, rồi bấm lối tắt "
                          "<b>Teemo cùng PDF</b> trên Desktop để mở lại nhé.</p>")
                    return self._raw(403, cu.encode("utf-8"), "text/html; charset=utf-8")
                html = (APP_DIR / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
                return self._raw(200, html.encode("utf-8"), "text/html; charset=utf-8")

            if not path.startswith("/api/") and not path.startswith("/dev/"):
                raise Loi("Không tìm thấy.", 404)
            self._kiem_tra_ma(q)

            if path == "/api/ping":
                LAST_PING, SEEN_PING = time.time(), True
                return self._json(200, dict(ok=True, out=str(OUT)))

            if method == "POST" and path == "/api/upload":
                name = safe_name(unquote((q.get("name") or ["file.pdf"])[0]), "file.pdf")
                info = nhan_file(name, self.rfile, int(self.headers.get("Content-Length") or 0))
                return self._json(200, info)

            m = re.fullmatch(r"/api/thumb/([0-9a-f]+)/(\d+)", path)
            if method == "GET" and m:
                w = max(60, min(2000, int((q.get("w") or ["220"])[0])))
                r = int((q.get("r") or ["0"])[0]) % 360
                data = ve_trang(m.group(1), int(m.group(2)), w, r)
                return self._raw(200, data, "image/jpeg", "private, max-age=600")

            if method == "POST" and path == "/api/export":
                return self._json(200, xuat(self._body_json()))

            if method == "GET" and path == "/api/images/scan":
                return self._json(200, quet_anh((q.get("file") or [""])[0]))

            m2 = re.fullmatch(r"/api/img/([0-9a-f]+)/(\d+)", path)
            if method == "GET" and m2:
                w = max(40, min(1600, int((q.get("w") or ["240"])[0])))
                return self._raw(200, ve_anh(m2.group(1), int(m2.group(2)), w), "image/png", "private, max-age=600")

            if method == "POST" and path == "/api/images/extract":
                return self._json(200, rut_anh(self._body_json()))

            if method == "POST" and path == "/api/pages/render":
                return self._json(200, chup_trang(self._body_json()))

            if method == "GET" and path == "/api/clean/scan":
                return self._json(200, quet_don((q.get("file") or [""])[0]))

            if method == "POST" and path == "/api/clean/preview":
                return self._json(200, xem_thu_don(self._body_json()))

            if method == "POST" and path == "/api/clean/apply":
                return self._json(200, don_file(self._body_json()))

            if method == "POST" and path == "/api/compress":
                return self._json(200, nen_file(self._body_json()))

            if method == "POST" and path == "/api/number":
                return self._json(200, danh_so_trang(self._body_json()))

            if method == "POST" and path == "/api/word/start":
                return self._json(200, word_bat_dau(self._body_json()))

            if method == "GET" and path == "/api/word/status":
                return self._json(200, word_trang_thai((q.get("job") or [""])[0]))

            if method == "GET" and path == "/api/word/review":
                return self._json(200, word_soat((q.get("job") or [""])[0]))

            m3 = re.fullmatch(r"/api/word/page/([0-9a-f]+)/(\d+)", path)
            if method == "GET" and m3:
                return self._raw(200, word_anh_trang(m3.group(1), int(m3.group(2))), "image/jpeg", "private, max-age=600")

            if method == "POST" and path == "/api/word/save":
                return self._json(200, word_luu(self._body_json()))

            if method == "POST" and path == "/api/open":
                b = self._body_json()
                target = b.get("path")
                if target and trong_thu_muc_ket_qua(target):
                    os.startfile(target)
                else:
                    os.startfile(str(OUT))
                return self._json(200, dict(ok=True))

            if method == "GET" and path.startswith("/dev/") and DEV_DIR:
                f = (Path(DEV_DIR) / unquote(path[5:])).resolve()
                if Path(DEV_DIR).resolve() in f.parents and f.is_file():
                    return self._raw(200, f.read_bytes(), "application/pdf")
            raise Loi("Không tìm thấy.", 404)
        except Loi as e:
            try:
                self._json(e.code, dict(error=str(e)))
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("LOI: " + "".join(traceback.format_exception(e)))
            try:
                self._json(500, dict(error="Lỗi bên trong app: " + str(e)))
            except Exception:
                pass


# ------------------------------------------------------------------ khởi động
class MayChu(ThreadingHTTPServer):
    """Giữ cổng ĐỘC QUYỀN: nếu đã có bản khác đang chạy thì bản mới không được chiếm chung cổng."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


RUNNING = DATA / "running.json"


def dang_chay_san():
    try:
        info = json.loads(RUNNING.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{info['port']}/api/ping?t={info['token']}"
        with urlopen(url, timeout=1.5) as r:
            if r.status == 200:
                return info
    except Exception:
        return None
    return None


def main():
    global PORT, DEV_DIR, LAST_PING
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dev", help="thư mục file mẫu để thử (chỉ dùng khi kiểm thử)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--out", help="thư mục kết quả khác (chỉ dùng khi kiểm thử)")
    a = ap.parse_args()
    DEV_DIR = a.dev
    if a.dev:
        global TOKEN
        TOKEN = secrets.token_urlsafe(18)  # kiểm thử: mã riêng, không đụng mã thật
    if a.out:
        global OUT
        OUT = Path(a.out)
        OUT.mkdir(parents=True, exist_ok=True)

    old = None if a.dev else dang_chay_san()
    if old:
        if not a.no_browser:
            webbrowser.open(f"http://127.0.0.1:{old['port']}/?t={old['token']}")
        return

    srv = None
    ports = [a.port] if a.port else list(range(8765, 8790))
    for idx, p in enumerate(ports):
        try:
            srv = MayChu(("127.0.0.1", p), Handler)
            PORT = srv.server_address[1]
            break
        except OSError:
            if idx == 0 and not a.dev:
                # cổng chính đang bận: có thể một bản khác vừa bật xong -> dùng lại bản đó
                for _ in range(15):
                    time.sleep(0.3)
                    old = dang_chay_san()
                    if old:
                        if not a.no_browser:
                            webbrowser.open(f"http://127.0.0.1:{old['port']}/?t={old['token']}")
                        return
            continue
    if srv is None:
        log("Không mở được cổng nào")
        sys.exit(1)
    srv.daemon_threads = True
    if not a.dev:
        RUNNING.write_text(json.dumps(dict(port=PORT, token=TOKEN, pid=os.getpid())), encoding="utf-8")
    url = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
    log("chạy tại " + url.split("?")[0])
    print(url, flush=True)

    def canh():
        start = time.time()
        while True:
            time.sleep(5)
            if SEEN_PING and time.time() - LAST_PING > 1800:
                log("không còn trang nào mở, tự tắt")
                srv.shutdown()
                return
            if not SEEN_PING and time.time() - start > 600:
                log("không ai mở trang, tự tắt")
                srv.shutdown()
                return

    threading.Thread(target=canh, daemon=True).start()
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    finally:
        if not a.dev:
            try:
                os.replace(RUNNING, DATA / "running.old")
            except Exception:
                pass


if __name__ == "__main__":
    main()
