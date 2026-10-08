"""离线真实解析；合成Office/PDF材料，不读取生产上传，不访问模型。"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import pytest

from connectors.files._knowledge_parser import Rejected, extract
from connectors.files.knowledge import (
    KnowledgeFileParser,
    KnowledgeParseFailure,
    KnowledgeParseLimits,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name, value in files.items():
            target.writestr(name, value)
    return output.getvalue()


def docx(text="Synthetic steel hinge", **extra):
    return archive(
        {
            "[Content_Types].xml": "<Types/>",
            "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + text
            + "</w:t></w:r></w:p></w:body></w:document>",
            **extra,
        }
    )


def xlsx():
    return archive(
        {
            "[Content_Types].xml": "<Types/>",
            "xl/workbook.xml": "<workbook/>",
            "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>Width</t></is></c><c r="C1"><v>10.25</v></c><c r="D1"><f>HYPERLINK("https://example.invalid")</f><v>999</v></c></row></sheetData></worksheet>',
        }
    )


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown", "text/csv"])
def test_utf8_bom_and_unicode_are_preserved(mime):
    text, warnings = extract(
        "\ufeff规格,尺寸\n合成钢材,10mm".encode(), mime, 60000, 100
    )
    assert text == "规格,尺寸\n合成钢材,10mm"
    assert warnings == []


def test_docx_text_and_external_link_warning_without_following_link():
    data = docx(
        **{
            "word/_rels/document.xml.rels": '<Relationships><Relationship TargetMode="External" Target="https://example.invalid"/></Relationships>'
        }
    )
    text, warnings = extract(data, DOCX, 60000, 100)
    assert text == "Synthetic steel hinge"
    assert warnings == ["external_links_ignored"]


def test_xlsx_keeps_cell_coordinates_and_never_evaluates_formula():
    text, warnings = extract(xlsx(), XLSX, 60000, 100)
    assert "A1=Width" in text and "C1=10.25" in text
    assert "D1=[公式未计算]" in text
    assert "999" not in text and "https://" not in text
    assert "formulas_not_evaluated" in warnings


@pytest.mark.parametrize(
    "data,mime,reason",
    [
        (b"%PDF-malformed", DOCX, "type_mismatch"),
        (docx(), XLSX, "type_mismatch"),
        (
            docx(
                "&x;",
                **{
                    "word/_rels/unsafe.rels": '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x>&x;</x>'
                },
            ),
            DOCX,
            "unsafe_xml",
        ),
        (docx(**{"word/vbaProject.bin": b"macro"}), DOCX, "unsupported_type"),
        (docx(**{"../escape": b"x"}), DOCX, "invalid_document"),
        (docx(**{"bomb.bin": b"x" * (2 * 1024 * 1024)}), DOCX, "archive_limit"),
        (b"123456", "text/plain", "text_limit"),
    ],
)
def test_rejected_document_shapes_are_fixed(data, mime, reason):
    maximum = 5 if reason == "text_limit" else 60000
    with pytest.raises(Rejected) as error:
        extract(data, mime, maximum, 100)
    assert error.value.reason == reason


def test_type_and_limits_fail_without_spawning():
    parser = KnowledgeFileParser(
        python_binary=Path("/missing"), bwrap_binary=Path("/missing")
    )
    assert parser is not None
    with pytest.raises(KnowledgeParseFailure, match="parser_unavailable"):
        KnowledgeParseLimits(max_chars=60001).validate()


@pytest.fixture
def parser():
    if not shutil.which("bwrap") or not Path("/proc").exists():
        pytest.skip("需显式Linux bwrap离线解析验收")
    return KnowledgeFileParser(
        python_binary=Path("/usr/bin/python3"), bwrap_binary=Path("/usr/bin/bwrap")
    )


@pytest.mark.parametrize(
    "data,mime,expected",
    [
        (b"synthetic plain text", "text/plain", "synthetic plain text"),
        (docx(), DOCX, "Synthetic steel hinge"),
        (xlsx(), XLSX, "C1=10.25"),
    ],
)
async def test_real_sandbox_parses_bounded_files(parser, data, mime, expected):
    result = await parser.extract(data, mime)
    assert expected in result.text


async def test_scanned_pdf_provides_visual_input_in_real_sandbox(parser):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    result = await parser.extract(stream.getvalue(), "application/pdf")
    assert result.text == ""
    assert len(result.images) == 1
    assert result.image_source_pages == (1,)
    assert "vision_transcription_required" in result.warnings


async def test_mixed_pdf_text_and_blank_page_reports_incomplete_extraction(parser):
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen.canvas import Canvas

    text_pdf = io.BytesIO()
    canvas = Canvas(text_pdf)
    canvas.drawString(10, 100, "Synthetic steel hinge")
    canvas.save()
    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(text_pdf.getvalue())).pages[0])
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    result = await parser.extract(output.getvalue(), "application/pdf")
    assert "Synthetic steel hinge" in result.text
    assert result.image_source_pages == (2,)
    assert len(result.images) == 1
    assert "vision_transcription_required" in result.warnings


async def test_pdf_encryption_is_not_silently_decrypted(parser):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("synthetic-document-passphrase")
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(KnowledgeParseFailure) as error:
        await parser.extract(output.getvalue(), "application/pdf")
    assert error.value.reason == "encrypted_document"


def image_bytes(*, size=(80, 40), fmt="PNG", animated=False):
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    image = Image.new("RGB", size, "white")
    output = io.BytesIO()
    options = {}
    if fmt == "PNG":
        metadata = PngInfo()
        metadata.add_text("Comment", "synthetic-private-metadata")
        options["pnginfo"] = metadata
    if animated:
        other = Image.new("RGB", size, "black")
        options.update(save_all=True, append_images=[other], duration=100, loop=0)
    image.save(output, format=fmt, **options)
    return output.getvalue()


@pytest.mark.parametrize(
    "fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
async def test_real_sandbox_normalizes_images_and_strips_metadata(parser, fmt, mime):
    from PIL import Image

    result = await parser.extract(image_bytes(fmt=fmt), mime)
    assert result.text == ""
    assert result.image_source_pages == (1,)
    assert result.warnings == ("vision_transcription_required",)
    assert len(result.images) == 1
    normalized = result.images[0]
    assert normalized.mime_type == "image/png"
    assert normalized.byte_length == len(normalized.data)
    assert b"synthetic-private-metadata" not in normalized.data
    with Image.open(io.BytesIO(normalized.data)) as decoded:
        assert decoded.format == "PNG"
        assert decoded.size == (80, 40)


async def test_image_resizing_is_explicit_and_visual_limit_is_not_silent(parser):
    from PIL import Image

    result = await parser.extract(image_bytes(size=(2000, 1000)), "image/png")
    assert "image_resized_1600" in result.warnings
    with Image.open(io.BytesIO(result.images[0].data)) as decoded:
        assert decoded.size == (1600, 800)


async def test_image_magic_mismatch_and_animation_are_rejected(parser):
    with pytest.raises(KnowledgeParseFailure) as mismatch:
        await parser.extract(image_bytes(fmt="JPEG"), "image/png")
    assert mismatch.value.reason == "type_mismatch"
    with pytest.raises(KnowledgeParseFailure) as animated:
        await parser.extract(image_bytes(animated=True), "image/png")
    assert animated.value.reason == "unsupported_type"


async def test_more_than_ten_scanned_pages_is_rejected_without_truncation(parser):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(11):
        writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(KnowledgeParseFailure) as error:
        await parser.extract(output.getvalue(), "application/pdf")
    assert error.value.reason == "source_limit_exceeded"


async def test_eleven_text_pages_remain_supported_without_vision(parser):
    from reportlab.pdfgen.canvas import Canvas

    output = io.BytesIO()
    canvas = Canvas(output)
    for index in range(1, 12):
        canvas.drawString(20, 200, f"Synthetic specification page {index}")
        canvas.showPage()
    canvas.save()
    result = await parser.extract(output.getvalue(), "application/pdf")
    assert "[Page 11]" in result.text
    assert "Synthetic specification page 11" in result.text
    assert result.images == ()
    assert result.image_source_pages == ()


async def test_text_and_image_pdf_keeps_original_page_mapping(parser):
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    output = io.BytesIO()
    canvas = Canvas(output)
    canvas.drawString(20, 200, "Text-only first page")
    canvas.showPage()
    canvas.drawString(20, 200, "Second page with product image")
    canvas.drawImage(
        ImageReader(io.BytesIO(image_bytes())), 20, 50, width=80, height=40
    )
    canvas.save()
    result = await parser.extract(output.getvalue(), "application/pdf")
    assert "Text-only first page" in result.text
    assert "Second page with product image" in result.text
    assert len(result.images) == 1
    assert result.image_source_pages == (2,)


async def test_parser_cancellation_waits_for_spawn_and_reaps_process(monkeypatch):
    import asyncio
    import os
    import sys

    from connectors.files import knowledge

    parser = KnowledgeFileParser(
        python_binary=Path(sys.executable), bwrap_binary=Path("/usr/bin/true")
    )
    original = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release = asyncio.Event()
    children = []

    async def delayed_spawn(*args, **kwargs):
        process = await original(
            sys.executable,
            "-c",
            "import sys,time;sys.stdin.buffer.read();time.sleep(60)",
            **kwargs,
        )
        children.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(knowledge.asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(parser.extract(b"synthetic text", "text/plain"))
    await asyncio.wait_for(spawned.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert len(children) == 1
    assert children[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)


async def test_oversized_png_pixel_header_is_rejected_before_decompression(parser):
    import struct
    import zlib

    payload = bytearray(image_bytes())
    payload[16:24] = struct.pack("!II", 50000, 50000)
    payload[29:33] = struct.pack("!I", zlib.crc32(payload[12:29]) & 0xFFFFFFFF)
    with pytest.raises(KnowledgeParseFailure) as error:
        await parser.extract(bytes(payload), "image/png")
    assert error.value.reason == "source_limit_exceeded"
