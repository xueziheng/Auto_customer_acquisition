"""固定离线子进程：受限文档转文本，不访问数据库、凭证或网络。"""

from __future__ import annotations

import csv
import io
import json
import re
import resource
import sys
import zipfile
from typing import Any
from xml.etree import ElementTree

MAX_FILE = 10 * 1024 * 1024
MAX_CHARS = 60000
MAX_PAGES = 100
MAX_ARCHIVE_ENTRIES = 2000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 16 * 1024 * 1024


class Rejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _bounded(text: str, maximum: int) -> str:
    if len(text) > maximum:
        raise Rejected("text_limit")
    if "\x00" in text:
        raise Rejected("invalid_document")
    return text


def _xml(raw: bytes) -> ElementTree.Element:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise Rejected("invalid_document") from None
    if "\x00" in text or re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise Rejected("unsafe_xml")
    return ElementTree.fromstring(text)


def _archive(data: bytes) -> zipfile.ZipFile:
    if not data.startswith(b"PK\x03\x04"):
        raise Rejected("type_mismatch")
    archive = zipfile.ZipFile(io.BytesIO(data))
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise Rejected("archive_limit")
    total = 0
    names: set[str] = set()
    for entry in entries:
        if (
            entry.filename.startswith(("/", "\\"))
            or "\\" in entry.filename
            or ".." in entry.filename.split("/")
            or entry.filename in names
            or entry.flag_bits & 1
            or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        ):
            raise Rejected("invalid_document")
        names.add(entry.filename)
        if "vbaproject" in entry.filename.casefold():
            raise Rejected("unsupported_type")
        total += entry.file_size
        if (
            total > MAX_ARCHIVE_BYTES
            or entry.file_size > MAX_ENTRY_BYTES
            or (
                entry.file_size > 1024 * 1024
                and entry.file_size > max(entry.compress_size, 1) * 100
            )
        ):
            raise Rejected("archive_limit")
    if "[Content_Types].xml" not in names:
        raise Rejected("type_mismatch")
    return archive


def _entry(archive: zipfile.ZipFile, name: str) -> bytes:
    with archive.open(name) as stream:
        raw = stream.read(MAX_ENTRY_BYTES + 1)
    if len(raw) > MAX_ENTRY_BYTES:
        raise Rejected("archive_limit")
    return raw


def _relationship_warnings(archive: zipfile.ZipFile) -> list[str]:
    external = False
    for name in archive.namelist():
        if name.endswith(".rels"):
            root = _xml(_entry(archive, name))
            external = external or any(
                node.attrib.get("TargetMode") == "External" for node in root
            )
    return ["external_links_ignored"] if external else []


def _docx(data: bytes, max_chars: int) -> tuple[str, list[str]]:
    with _archive(data) as archive:
        if "word/document.xml" not in archive.namelist():
            raise Rejected("type_mismatch")
        _xml(_entry(archive, "[Content_Types].xml"))
        warnings = _relationship_warnings(archive)
        root = _xml(_entry(archive, "word/document.xml"))
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in root.iter(ns + "p"):
            pieces = []
            for node in paragraph.iter():
                if node.tag == ns + "t":
                    pieces.append(node.text or "")
                elif node.tag in (ns + "br", ns + "cr"):
                    pieces.append("\n")
                elif node.tag == ns + "tab":
                    pieces.append("\t")
            paragraphs.append("".join(pieces))
            _bounded("\n".join(paragraphs), max_chars)
        return _bounded("\n".join(paragraphs), max_chars), warnings


def _xlsx(data: bytes, max_chars: int, max_pages: int) -> tuple[str, list[str]]:
    with _archive(data) as archive:
        names = archive.namelist()
        if "xl/workbook.xml" not in names:
            raise Rejected("type_mismatch")
        _xml(_entry(archive, "[Content_Types].xml"))
        _xml(_entry(archive, "xl/workbook.xml"))
        warnings = _relationship_warnings(archive)
        warnings.append("spreadsheet_raw_values")
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = _xml(_entry(archive, "xl/sharedStrings.xml"))
            for node in root.iter(ns + "si"):
                shared.append("".join(part.text or "" for part in node.iter(ns + "t")))
                if len(shared) > 100000:
                    raise Rejected("archive_limit")
        sheets = sorted(
            name
            for name in names
            if re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml", name)
        )
        if not sheets or len(sheets) > max_pages:
            raise Rejected("page_limit")
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        formula = False
        for name in sheets:
            writer.writerow(["[" + name + "]"])
            root = _xml(_entry(archive, name))
            for row in root.iter(ns + "row"):
                cells = []
                for cell in row.findall(ns + "c"):
                    if cell.find(ns + "f") is not None:
                        text = "[公式未计算]"
                        formula = True
                    elif cell.attrib.get("t") == "inlineStr":
                        text = "".join(n.text or "" for n in cell.iter(ns + "t"))
                    else:
                        value = cell.find(ns + "v")
                        text = value.text or "" if value is not None else ""
                        if cell.attrib.get("t") == "s":
                            index = int(text)
                            if not 0 <= index < len(shared):
                                raise Rejected("invalid_document")
                            text = shared[index]
                    # 保留单元格地址，不能把缺失列错误对齐为相邻规格。
                    cells.append(cell.attrib.get("r", "?") + "=" + text)
                writer.writerow(cells)
                _bounded(output.getvalue(), max_chars)
        if formula:
            warnings.append("formulas_not_evaluated")
        return _bounded(output.getvalue(), max_chars), warnings


def extract(
    data: bytes, mime_type: str, max_chars: int, max_pages: int
) -> tuple[str, list[str]]:
    if mime_type in ("text/plain", "text/markdown", "text/csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            raise Rejected("invalid_document") from None
        text = _bounded(text, max_chars)
        if mime_type == "text/csv":
            csv.field_size_limit(max_chars)
            for index, row in enumerate(csv.reader(io.StringIO(text), strict=True)):
                if index > 10000 or len(row) > 1000:
                    raise Rejected("text_limit")
        return text, []
    if (
        mime_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        return _docx(data, max_chars)
    if mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return _xlsx(data, max_chars, max_pages)
    if mime_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise Rejected("type_mismatch")
        sys.path.insert(0, "/parser-libs")
        import pypdf

        if pypdf.__version__ != "6.16.2" or sys.version_info[:2] != (3, 12):
            raise Rejected("parser_unavailable")
        reader = pypdf.PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise Rejected("encrypted_document")
        if len(reader.pages) > max_pages:
            raise Rejected("page_limit")
        texts = []
        empty = []
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if not text.strip():
                empty.append(str(index))
            else:
                texts.append("[Page " + str(index) + "]\n" + text)
            _bounded("\n\n".join(texts), max_chars)
        if not texts:
            raise Rejected("needs_ocr")
        return "\n\n".join(texts), (
            ["pages_without_text:" + ",".join(empty)] if empty else []
        )
    raise Rejected("unsupported_type")


# 知识图像输入的受控规范；所有输入先真实解码再去除元数据重新编码。
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
IMAGE_EDGE = 1600


def _image(data: bytes, expected_mime: str) -> tuple[dict[str, object], bool]:
    import base64
    import hashlib
    import warnings

    sys.path.insert(0, "/parser-libs")
    from PIL import Image, ImageOps

    actual_formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as check:
                if check.format != actual_formats[expected_mime]:
                    raise Rejected("type_mismatch")
                if getattr(check, "n_frames", 1) != 1:
                    raise Rejected("unsupported_type")
                if (
                    check.width <= 0
                    or check.height <= 0
                    or check.width * check.height > MAX_IMAGE_PIXELS
                ):
                    raise Rejected("source_limit_exceeded")
                check.verify()
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                resized = max(oriented.size) > IMAGE_EDGE
                if resized:
                    oriented.thumbnail(
                        (IMAGE_EDGE, IMAGE_EDGE), Image.Resampling.LANCZOS
                    )
                rgba = oriented.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, "white")
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                flattened.save(output, format="PNG", compress_level=6)
        normalized = output.getvalue()
        if len(normalized) > MAX_IMAGE_BYTES:
            raise Rejected("source_limit_exceeded")
        return {
            "mime_type": "image/png",
            "data": base64.b64encode(normalized).decode("ascii"),
            "sha256": hashlib.sha256(normalized).hexdigest(),
            "byte_length": len(normalized),
        }, resized

    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError):
        raise Rejected("source_limit_exceeded") from None


def _has_image(resources: Any, seen: set[int] | None = None, depth: int = 0) -> bool:
    if depth > 16:
        raise Rejected("source_limit_exceeded")
    if resources is None:
        return False
    if seen is None:
        seen = set()
    resolved = resources.get_object()
    if id(resolved) in seen:
        return False
    seen.add(id(resolved))
    if len(seen) > 10000:
        raise Rejected("source_limit_exceeded")
    xobjects = resolved.get("/XObject")
    if xobjects is None:
        return False
    for reference in xobjects.get_object().values():
        obj = reference.get_object()
        if obj.get("/Subtype") == "/Image":
            return True
        if obj.get("/Subtype") == "/Form" and _has_image(
            obj.get("/Resources"), seen, depth + 1
        ):
            return True
    return False


def _visual_pdf(data: bytes, max_chars: int, max_pages: int) -> dict[str, object]:
    import subprocess
    from pathlib import Path

    if not data.startswith(b"%PDF-"):
        raise Rejected("type_mismatch")
    sys.path.insert(0, "/parser-libs")
    import pypdf

    if pypdf.__version__ != "6.16.2" or sys.version_info[:2] != (3, 12):
        raise Rejected("parser_unavailable")
    reader = pypdf.PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise Rejected("encrypted_document")
    if len(reader.pages) > max_pages:
        raise Rejected("page_limit")
    texts: list[str] = []
    image_pages: list[int] = []
    empty_pages: list[int] = []
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            texts.append("[Page " + str(index) + "]\n" + text)
        else:
            empty_pages.append(index)
        if not text.strip() or _has_image(page.get("/Resources")):
            image_pages.append(index)
        _bounded("\n\n".join(texts), max_chars)
    if len(image_pages) > MAX_IMAGES:
        raise Rejected("source_limit_exceeded")
    images: list[dict[str, object]] = []
    warnings: list[str] = []
    if empty_pages:
        warnings.append("vision_transcription_required")
    if image_pages:
        if not Path("/pdftoppm").is_file():
            raise Rejected("parser_unavailable")
        source = Path("/tmp/source.pdf")
        source.write_bytes(data)
        total = 0
        for page_number in image_pages:
            output = Path("/tmp/page.png")
            process = subprocess.run(
                [
                    "/pdftoppm",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-png",
                    "-scale-to",
                    str(IMAGE_EDGE),
                    str(source),
                    "/tmp/page",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=25,
                check=False,
            )
            if (
                process.returncode != 0
                or not output.is_file()
                or output.stat().st_size > MAX_IMAGE_BYTES
            ):
                raise Rejected("invalid_document")
            image, _ = _image(output.read_bytes(), "image/png")
            output.unlink()
            images.append(image)
            total += int(str(image["byte_length"]))
            if total > MAX_TOTAL_IMAGE_BYTES:
                raise Rejected("source_limit_exceeded")
        warnings.append("visual_pages_rendered_1600")
    return {
        "text": "\n\n".join(texts),
        "warnings": warnings,
        "images": images,
        "image_source_pages": image_pages,
    }


def extract_with_images(
    data: bytes, mime_type: str, max_chars: int, max_pages: int
) -> dict[str, object]:
    if mime_type in ("image/png", "image/jpeg", "image/webp"):
        image, resized = _image(data, mime_type)
        warnings = ["vision_transcription_required"]
        if resized:
            warnings.append("image_resized_1600")
        return {
            "text": "",
            "warnings": warnings,
            "images": [image],
            "image_source_pages": [1],
        }
    if mime_type == "application/pdf":
        return _visual_pdf(data, max_chars, max_pages)
    text, warnings = extract(data, mime_type, max_chars, max_pages)
    return {"text": text, "warnings": warnings, "images": [], "image_source_pages": []}


def main() -> int:
    # 在读取不受信 bytes 和导入 PDF 库之前施加资源上限。
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024,) * 2)
    try:
        header = sys.stdin.buffer.readline(4097)
        if len(header) > 4096 or not header.endswith(b"\n"):
            raise Rejected("invalid_document")
        settings = json.loads(header)
        max_bytes = settings["max_bytes"]
        max_chars = settings["max_chars"]
        max_pages = settings["max_pages"]
        if any(
            type(value) is not int or value <= 0
            for value in (max_bytes, max_chars, max_pages)
        ):
            raise Rejected("invalid_document")
        if max_bytes > MAX_FILE or max_chars > MAX_CHARS or max_pages > MAX_PAGES:
            raise Rejected("invalid_document")
        data = sys.stdin.buffer.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            raise Rejected("size_limit")
        result = extract_with_images(data, settings["mime_type"], max_chars, max_pages)
        if not str(result["text"]).strip() and not result["images"]:
            raise Rejected("empty_document")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Rejected as error:
        print(json.dumps({"reason": error.reason}))
        return 2
    except Exception:  # noqa: BLE001 - 文档解析失败禁止输出文件材料和第三方异常。
        print('{"reason":"invalid_document"}')
        return 2


if __name__ == "__main__":
    sys.exit(main())
