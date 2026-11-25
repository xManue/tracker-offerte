import io
import re
from contextlib import closing

from .ingest import parse_file


def read_stock(raw, filename):
    suffix = filename.rsplit(".", 1)[-1].lower()
    if suffix == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            rows = []
            for sheet in workbook:
                values = sheet.values
                columns = next(values, ())
                names = [str(value).strip() if value is not None else "" for value in columns]
                for line, row in enumerate(values, start=2):
                    if any(value is not None for value in row):
                        rows.append((len(rows) + 1, {"external_id": f"{sheet.title}:{line}", **dict(zip(names, row)),
                                                   "origin_ref": f"{sheet.title}, riga {line}"}))
            return rows
        finally:
            workbook.close()
    if suffix == "pdf":
        from pypdf import PdfReader

        document = PdfReader(io.BytesIO(raw))
        if len(document.pages) > 20:
            raise ValueError("Dividi il PDF in file da al massimo 20 pagine")
        result = []
        columns = ("mpn", "title", "quantity", "condition", "price")
        for page, pdf_page in enumerate(document.pages):
            body = pdf_page.extract_text(extraction_mode="layout") or ""
            from_scan = not body.strip()
            if from_scan:
                body = pdf_ocr(raw, page)
            for line, content in enumerate(body.splitlines(), start=1):
                content = content.strip()
                if not content:
                    continue
                parts = re.split(r"\s{2,}|\s*;\s*|\t+", content)
                item = dict(zip(columns, parts)) if len(parts) >= 3 else {"title": content}
                result.append((len(result) + 1, {"external_id": f"p{page + 1}:{line}", **item,
                    "origin_ref": f"Pagina {page + 1}, riga {line}" + (" (OCR)" if from_scan else "")}))
        return result
    return parse_file(raw, filename)


def pdf_ocr(raw, page):
    import pypdfium2

    doc = pypdfium2.PdfDocument(raw)
    try:
        with closing(doc[page]) as pdf_page, closing(pdf_page.render(scale=2)) as bitmap:
            return image_evidence(bitmap.to_pil(), lines=True)["text"]
    finally:
        doc.close()


def image_evidence(image, *, lines=False):
    import imagehash
    import pytesseract

    rgb = image.convert("RGB")
    rgb.thumbnail((3000, 3000))
    try:
        data = pytesseract.image_to_data(rgb, lang="ita+eng", output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError as error:
        raise ValueError("Tesseract richiesto per l’OCR") from error
    except pytesseract.TesseractError as error:
        raise ValueError("Controlla i dati lingua ita e eng di Tesseract") from error
    boxes, grouped = [], {}
    for i, token in enumerate(data["text"]):
        if token.strip() and float(data["conf"][i]) >= 0:
            box = {key: data[key][i] for key in ("left", "top", "width", "height")}
            boxes.append({"word": token, "bounds": list(box.values())})
            key = tuple(data[field][i] for field in ("block_num", "par_num", "line_num"))
            group = grouped.setdefault(key, [])
            gap = box["left"] - group[-1][1] if group else 0
            group.append((("  " if gap > box["height"] * 1.2 else " ") + token, box["left"] + box["width"]))
    text = "\n".join("".join(value for value, _ in group).strip() for group in grouped.values())
    return {"text": text if lines else " ".join(box["word"] for box in boxes), "boxes": boxes,
            "size": list(rgb.size), "photo_hash": str(imagehash.phash(rgb))}
