# Teemo cùng PDF

Bộ công cụ PDF chạy **ngay trên máy của bạn**, mở bằng trình duyệt. File không bị gửi đi đâu, file gốc không bao giờ bị sửa (kết quả luôn là file mới trong thư mục `PDF-ket-qua` trên Desktop).

## Làm được gì

| Việc | Ghi chú |
| --- | --- |
| Chuyển PDF sang Word | PDF có chữ thật: chuyển thẳng. PDF quét: đọc chữ tiếng Việt bằng Tesseract, dựng lại bảng thật, có màn **soát lỗi hai bên** (trang gốc bên trái, chữ máy đọc bên phải, chữ máy chưa chắc tô vàng) |
| Cắt, nối, xoay | Chọn trang, thứ tự, góc xoay bằng nút bấm |
| Xóa và đổi chỗ trang | Có hoàn tác |
| Nén nhỏ file | 3 mức Nhẹ, Vừa, Mạnh |
| Đánh số trang | 6 vị trí, 4 kiểu, xem trước |
| Rút ảnh ra | Lấy hình nhúng, hoặc chụp cả trang thành ảnh |
| Xóa ghi chú và hình mờ | Gỡ nét tô sáng, ghi chú, chữ mờ, logo in chìm, có xem thử trước/sau |

## Cài đặt (Windows)

1. **Python 3.10 trở lên**, tải ở [python.org](https://www.python.org/downloads/). Khi cài nhớ tick ô *Add Python to PATH*.
2. Mở thư mục này, gõ vào thanh địa chỉ `cmd` rồi Enter, chạy:

   ```
   pip install -r requirements.txt
   ```

3. **Chỉ cần nếu chuyển PDF quét sang Word:** cài Tesseract (bản Windows của UB Mannheim, tải từ [github.com/UB-Mannheim/tesseract/wiki](https://github.com/UB-Mannheim/tesseract/wiki)). Lúc cài, mục *Additional language data* nhớ tick **Vietnamese**. Muốn chữ chính xác hơn, có thể thay file `vie.traineddata` bằng bản `tessdata_best` ở [github.com/tesseract-ocr/tessdata_best](https://github.com/tesseract-ocr/tessdata_best).
4. Bấm đúp **`Mo-Teemo-cung-PDF.bat`**. Trình duyệt tự mở trang làm việc. Muốn có lối tắt, bấm chuột phải file này, chọn *Send to → Desktop*.

Các việc khác (cắt, nối, nén...) không cần Tesseract.

## Giới hạn nói thật

- Đọc chữ bằng máy miễn phí đạt khoảng 97–98% trên bản quét sạch, **không hứa 100%**. Luôn soát lại trước khi dùng. Lỗi dấu lặp đi lặp lại (ví dụ "PHẨM" đọc thành "PHÁM") máy không tự tô vàng được.
- Hình mờ in dính vào ảnh quét không gỡ sạch được.
- Bảng không có đường kẻ chỉ dựng được dạng cột chữ. Logo, dấu mộc bị bỏ.
- Chỉ dùng cho file của bạn hoặc file bạn có quyền xử lý.

## An toàn

- Máy chủ chỉ nghe ở `127.0.0.1` (chính máy bạn), có mã bí mật trong mỗi lần gọi.
- Không gửi dữ liệu ra mạng. Thư mục tạm nằm ở `%LOCALAPPDATA%\PDF-Tool`.

## Giấy phép

[GNU AGPL-3.0](LICENSE). Phần mềm dùng thư viện [PyMuPDF](https://pymupdf.readthedocs.io) (AGPL-3.0), [pdf2docx](https://github.com/ArtifexSoftware/pdf2docx) (MIT), OpenCV, NumPy, python-docx và công cụ [Tesseract](https://github.com/tesseract-ocr/tesseract) (Apache-2.0).
Hình nhân vật hướng dẫn do tác giả tự vẽ bằng SVG, không dùng hình của game hay của người thật.
