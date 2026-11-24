#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""Crop bank e-receipts and print them on Kingdee 240 x 140 mm paper.

Version 2: each receipt is rasterised at 600 DPI and placed as a single
image on its own page.  The output pages contain no text layer and no
hidden content from the rest of the source A4 sheet.  The receipt is never
enlarged: it is printed at 100% size when it fits, otherwise it is scaled
down just enough to keep a 4 mm white margin.

Requires pdfplumber, pypdf, pypdfium2 and Pillow.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import zlib
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from pdfminer.pdfparser import PDFSyntaxError
from PIL import Image
from pypdf import PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

MM = 72.0 / 25.4

PAPER_MM = (240.0, 140.0)
DEFAULT_DPI = 600
DEFAULT_MARGIN_MM = 4.0
PALETTE_COLORS = 64

# A frame element must span at least this fraction of the page (width or
# height) to be treated as receipt structure rather than a table cell.
STRUCT_WIDTH_RATIO = 0.45
STRUCT_HEIGHT_RATIO = 0.12

# Two content bands closer than this are considered the same receipt.
BAND_GAP_PT = 24.0

# Extra space around the detected content so a border stroke is never cut.
CLIP_PAD_PT = 2.0

# A rectangle in PDF top-left coordinates: (x0, top, x1, bottom).
Rect = tuple[float, float, float, float]


def _union(a: Rect, b: Rect) -> Rect:
    return (
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3]),
    )


def _horizontal_overlap(rect: Rect, band: Rect) -> bool:
    return rect[2] >= band[0] - 6.0 and rect[0] <= band[2] + 6.0


def _merged_coverage(
    intervals: list[tuple[float, float]],
) -> tuple[float, list[tuple[float, float]]]:
    """Merge 1-D intervals and return (covered length, merged intervals)."""
    intervals.sort()
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 2.0:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    covered = sum(end - start for start, end in merged)
    return covered, merged


def _table_rows(drawings: list[Rect], page_width: float) -> list[Rect]:
    """Recover table rows that are split into several cell-width segments."""
    segments = [r for r in drawings if r[3] - r[1] <= 3.0 and r[2] - r[0] >= 20.0]
    groups: list[list[Rect]] = []
    for rect in sorted(segments, key=lambda r: (r[1], r[0])):
        y_center = (rect[1] + rect[3]) / 2
        for group in groups:
            group_center = sum((r[1] + r[3]) / 2 for r in group) / len(group)
            if abs(group_center - y_center) <= 2.0:
                group.append(rect)
                break
        else:
            groups.append([rect])

    rows: list[Rect] = []
    for group in groups:
        covered, merged = _merged_coverage([(r[0], r[2]) for r in group])
        if covered >= page_width * STRUCT_WIDTH_RATIO:
            rows.append(
                (
                    min(x0 for x0, _ in merged),
                    min(r[1] for r in group),
                    max(x1 for _, x1 in merged),
                    max(r[3] for r in group),
                )
            )
    return rows


def _table_bands(frames: list[Rect]) -> list[Rect]:
    """Group overlapping frame lines into table blocks.

    PSBC puts three receipts on one A4 page.  Each receipt has a table
    border, but its footer is only ~17 pt above the next receipt's header,
    so content clustering alone would merge all three.  The table frames
    give reliable anchors for splitting them apart.
    """
    ordered = sorted(frames, key=lambda r: (r[1], r[0]))
    groups: list[list[float]] = []
    for rect in ordered:
        if groups and rect[1] <= groups[-1][3] + 6.0:
            group = groups[-1]
            group[0] = min(group[0], rect[0])
            group[1] = min(group[1], rect[1])
            group[2] = max(group[2], rect[2])
            group[3] = max(group[3], rect[3])
        else:
            groups.append([rect[0], rect[1], rect[2], rect[3]])

    result: list[Rect] = []
    for group in groups:
        band = (group[0], group[1], group[2], group[3])
        if band[3] - band[1] >= 30:
            result.append(band)
    return result


def _split_clip_by_tables(
    clip: Rect,
    tables: list[Rect],
    words: list[Rect],
    images: list[Rect],
    drawings: list[Rect],
    page_width: float,
    page_height: float,
) -> list[Rect]:
    """Split a merged multi-receipt clip at the boundaries between tables."""
    boundaries = [
        (tables[i][3] + tables[i + 1][1]) / 2.0
        for i in range(len(tables) - 1)
    ]

    def group_index(rect: Rect) -> int | None:
        center = (rect[1] + rect[3]) / 2.0
        if center < clip[1] or center > clip[3]:
            return None
        index = 0
        while index < len(boundaries) and center > boundaries[index]:
            index += 1
        return index

    groups: list[list[Rect]] = [[] for _ in tables]
    for rect in words + images + drawings:
        if rect[3] - rect[1] >= page_height * 0.5:
            continue
        index = group_index(rect)
        if index is not None:
            groups[index].append(rect)

    result: list[Rect] = []
    for table, items in zip(tables, groups):
        band = table
        for rect in items:
            band = _union(band, rect)
        band = (
            max(clip[0], band[0]),
            max(clip[1], band[1]),
            min(clip[2], band[2]),
            min(clip[3], band[3]),
        )
        if band[2] - band[0] < 10 or band[3] - band[1] < 10:
            continue
        band = (
            max(0.0, band[0] - CLIP_PAD_PT),
            max(0.0, band[1] - CLIP_PAD_PT),
            min(page_width, band[2] + CLIP_PAD_PT),
            min(page_height, band[3] + CLIP_PAD_PT),
        )
        result.append(band)
    return result


def page_content_rects(
    page: pdfplumber.page.Page,
) -> tuple[list[Rect], list[Rect], list[Rect]]:
    """Return drawing, image and word rectangles on a page."""
    page_width, page_height = float(page.width), float(page.height)

    drawings: list[Rect] = []
    for obj in page.curves + page.lines + page.rects:
        rect = (
            float(obj["x0"]),
            float(obj["top"]),
            float(obj["x1"]),
            float(obj["bottom"]),
        )
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            continue
        if (
            rect[2] - rect[0] >= page_width * 0.98
            and rect[3] - rect[1] >= page_height * 0.98
        ):
            continue
        drawings.append(rect)

    images: list[Rect] = []
    for obj in page.images:
        rect = (
            float(obj["x0"]),
            float(obj["top"]),
            float(obj["x1"]),
            float(obj["bottom"]),
        )
        if rect[2] > rect[0] and rect[3] > rect[1]:
            images.append(rect)

    word_items: list[tuple[Rect, str]] = []
    for obj in page.extract_words():
        text = str(obj.get("text", "")).strip()
        rect = (
            float(obj["x0"]),
            float(obj["top"]),
            float(obj["x1"]),
            float(obj["bottom"]),
        )
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            continue
        if rect[2] <= 0 or rect[0] >= page_width or rect[3] <= 0 or rect[1] >= page_height:
            continue
        word_items.append((rect, text))

    # PSBC draws the dashed separator between stacked receipts as many
    # standalone "-" words.  Drop only runs of these dashes, not a single
    # dash that may legitimately sit in a table cell.
    dash_groups: dict[float, list[Rect]] = {}
    for rect, text in word_items:
        if text and set(text) <= {"-", "–", "—", "_"}:
            key = round((rect[1] + rect[3]) / 2.0, 1)
            dash_groups.setdefault(key, []).append(rect)

    separator_rects: set[Rect] = set()
    for group in dash_groups.values():
        if len(group) < 5:
            continue
        left = min(r[0] for r in group)
        right = max(r[2] for r in group)
        if right - left >= page_width * 0.3:
            separator_rects.update(group)

    words = [
        rect
        for rect, _text in word_items
        if rect not in separator_rects
    ]
    return drawings, images, words


def find_receipt_clips(page: pdfplumber.page.Page) -> list[Rect]:
    """Find one clip rectangle per receipt on the page.

    A receipt is a vertical cluster of its frame, table rows, text and
    images.  Grouping all of those together also handles layouts where the
    left/right border runs continuously through several stacked receipts.
    """
    page_width, page_height = float(page.width), float(page.height)
    drawings, images, words = page_content_rects(page)

    frames = [
        r
        for r in drawings
        if r[2] - r[0] >= page_width * STRUCT_WIDTH_RATIO
        or r[3] - r[1] >= page_height * STRUCT_HEIGHT_RATIO
    ]
    rows = _table_rows(drawings, page_width)
    seeds = frames + rows + words + images

    if not seeds:
        content = drawings + images + words
        if not content:
            return []
        clip = content[0]
        for rect in content[1:]:
            clip = _union(clip, rect)
        clip = (
            max(0.0, clip[0] - CLIP_PAD_PT),
            max(0.0, clip[1] - CLIP_PAD_PT),
            min(page_width, clip[2] + CLIP_PAD_PT),
            min(page_height, clip[3] + CLIP_PAD_PT),
        )
        return [clip]

    seeds.sort(key=lambda r: (r[1], r[0]))
    bands: list[list[float]] = []
    for rect in seeds:
        if bands and rect[1] <= bands[-1][1] + BAND_GAP_PT:
            bands[-1][1] = max(bands[-1][1], rect[3])
            bands[-1][2] = min(bands[-1][2], rect[0])
            bands[-1][3] = max(bands[-1][3], rect[2])
        else:
            bands.append([rect[1], rect[3], rect[0], rect[2]])

    clips: list[Rect] = []
    for top, bottom, left, right in bands:
        band: Rect = (left, top, right, bottom)

        # Add seals or lines drawn as vectors.  A page-long border is left
        # out so it cannot expand this receipt into the next one.
        for rect in drawings:
            if rect[3] - rect[1] >= page_height * 0.5:
                continue
            center_y = (rect[1] + rect[3]) / 2
            if top <= center_y <= bottom and _horizontal_overlap(rect, band):
                band = _union(band, rect)

        band = (
            max(0.0, band[0]),
            max(0.0, band[1]),
            min(page_width, band[2]),
            min(page_height, band[3]),
        )
        if band[2] - band[0] < 10 or band[3] - band[1] < 10:
            continue
        band = (
            max(0.0, band[0] - CLIP_PAD_PT),
            max(0.0, band[1] - CLIP_PAD_PT),
            min(page_width, band[2] + CLIP_PAD_PT),
            min(page_height, band[3] + CLIP_PAD_PT),
        )
        clips.append(band)

    tables = _table_bands(frames)
    split_clips: list[Rect] = []
    for clip in clips:
        inside = [
            table
            for table in tables
            if table[1] >= clip[1] - 2 and table[3] <= clip[3] + 2
        ]
        if len(inside) >= 2:
            split_clips.extend(
                _split_clip_by_tables(
                    clip,
                    inside,
                    words,
                    images,
                    drawings,
                    page_width,
                    page_height,
                )
            )
        else:
            split_clips.append(clip)
    clips = split_clips

    clips.sort(key=lambda r: (r[1], r[0]))
    return clips


def _add_image_page(
    writer: PdfWriter,
    image: Image.Image,
    page_w: float,
    page_h: float,
    draw_w: float,
    draw_h: float,
    left: float,
    top: float,
) -> None:
    """Add a page containing one indexed-color image (no text layer)."""
    indexed = image.quantize(
        colors=PALETTE_COLORS,
        method=Image.MEDIANCUT,
        dither=Image.NONE,
    )
    palette = bytes(indexed.getpalette()[: PALETTE_COLORS * 3])
    data = zlib.compress(indexed.tobytes(), 9)

    image_obj = DecodedStreamObject()
    image_obj.set_data(data)
    image_obj[NameObject("/Type")] = NameObject("/XObject")
    image_obj[NameObject("/Subtype")] = NameObject("/Image")
    image_obj[NameObject("/Width")] = NumberObject(indexed.width)
    image_obj[NameObject("/Height")] = NumberObject(indexed.height)
    image_obj[NameObject("/ColorSpace")] = ArrayObject(
        [
            NameObject("/Indexed"),
            NameObject("/DeviceRGB"),
            NumberObject(PALETTE_COLORS - 1),
            ByteStringObject(palette),
        ]
    )
    image_obj[NameObject("/BitsPerComponent")] = NumberObject(8)
    image_obj[NameObject("/Filter")] = NameObject("/FlateDecode")
    image_ref = writer._add_object(image_obj)

    page = writer.add_blank_page(width=page_w, height=page_h)
    content = (
        f"q {draw_w:.4f} 0 0 {draw_h:.4f} "
        f"{left:.4f} {page_h - top - draw_h:.4f} cm /Im0 Do Q\n"
    ).encode("ascii")
    content_obj = DecodedStreamObject()
    content_obj.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(content_obj)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image_ref})}
    )


def crop_pdf(
    inputs: list[Path],
    output: Path,
    dpi: int,
    margin_mm: float,
    fill: bool,
    quiet: bool,
) -> tuple[int, int]:
    """Build the output PDF.  Returns (input page count, receipt count)."""
    writer = PdfWriter()
    page_w, page_h = (PAPER_MM[0] * MM, PAPER_MM[1] * MM)
    margin = margin_mm * MM
    available_w = page_w - 2 * margin
    available_h = page_h - 2 * margin
    if available_w <= 0 or available_h <= 0:
        raise ValueError("留白大于纸张尺寸")

    source_pages = 0
    receipt_count = 0

    for input_path in inputs:
        if not input_path.exists():
            raise FileNotFoundError(input_path)
        data = input_path.read_bytes()
        try:
            pdfium_doc = pdfium.PdfDocument(data)
        except Exception as exc:
            raise ValueError(f"{input_path.name} 无法打开: {exc}") from exc

        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if len(pdfium_doc) != len(pdf.pages):
                    raise ValueError(f"{input_path.name} 页数解析不一致")
                if not quiet:
                    print(f"处理: {input_path.name}（{len(pdf.pages)} 页）")

                for page_index, plumber_page in enumerate(pdf.pages):
                    source_pages += 1
                    clips = find_receipt_clips(plumber_page)
                    if not clips:
                        continue
                    pdfium_page = pdfium_doc[page_index]
                    source_w, source_h = pdfium_page.get_size()

                    for clip_index, clip in enumerate(clips, start=1):
                        clip_w = clip[2] - clip[0]
                        clip_h = clip[3] - clip[1]
                        scale = min(available_w / clip_w, available_h / clip_h)
                        if not fill:
                            scale = min(scale, 1.0)
                        draw_w = clip_w * scale
                        draw_h = clip_h * scale
                        target_w = max(1, round(draw_w / 72.0 * dpi))
                        target_h = max(1, round(draw_h / 72.0 * dpi))

                        crop = (
                            clip[0],
                            source_h - clip[3],
                            source_w - clip[2],
                            clip[1],
                        )
                        bitmap = pdfium_page.render(
                            scale=dpi / 72.0,
                            crop=crop,
                            may_draw_forms=True,
                        )
                        image = bitmap.to_pil()
                        if image.mode != "RGB":
                            image = image.convert("RGB")
                        if image.size != (target_w, target_h):
                            image = image.resize(
                                (target_w, target_h), Image.LANCZOS
                            )

                        left = (page_w - draw_w) / 2
                        top = (page_h - draw_h) / 2
                        _add_image_page(
                            writer,
                            image,
                            page_w,
                            page_h,
                            draw_w,
                            draw_h,
                            left,
                            top,
                        )

                        receipt_count += 1
                        if not quiet:
                            print(
                                f"  第 {page_index + 1} 页 回单 {clip_index}: "
                                f"{clip_w / MM:.1f} x {clip_h / MM:.1f} mm "
                                f"-> {scale * 100:.0f}% ({dpi} DPI)"
                            )
        finally:
            pdfium_doc.close()

    if receipt_count == 0:
        raise ValueError("没有识别到任何回单，请确认 PDF 是带框的电子回单。")

    writer.add_metadata(
        {
            "/Title": f"银行回单 - 金蝶纸（{dpi} DPI 截图）",
            "/Creator": "bank-receipt-crop-v2",
            "/Producer": "pypdf",
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        writer.write(handle)
    return source_pages, receipt_count


def _pause_before_exit() -> None:
    """Keep the console open when the frozen exe is started by drag-and-drop."""
    if not getattr(sys, "frozen", False):
        return
    if os.environ.get("RECEIPT_CROP_NO_PAUSE") == "1":
        return
    try:
        input("\n按回车键退出...")
    except EOFError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把银行电子回单按 600 DPI 截图，排到金蝶 240x140mm 纸上。",
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="银行回单 PDF")
    parser.add_argument("-o", "--output", type=Path, help="输出 PDF 路径")
    parser.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help=f"截图分辨率，默认 {DEFAULT_DPI}",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=DEFAULT_MARGIN_MM,
        metavar="MM",
        help=f"四周留白（毫米），默认 {DEFAULT_MARGIN_MM:g}",
    )
    parser.add_argument(
        "--fill",
        action="store_true",
        help="允许放大填满纸张（默认只缩不放，保持原尺寸）",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="不打印处理明细")
    args = parser.parse_args(argv)

    if args.dpi < 150 or args.dpi > 1200:
        parser.error("--dpi 应在 150 到 1200 之间")

    inputs = [p.resolve() for p in args.inputs]
    output = (
        args.output.resolve()
        if args.output
        else inputs[0].with_name("回单_金蝶纸_240x140.pdf")
    )
    if output in inputs:
        parser.error("输出文件不能是输入文件之一")

    try:
        source_pages, receipt_count = crop_pdf(
            inputs,
            output,
            args.dpi,
            args.margin,
            args.fill,
            args.quiet,
        )
    except (OSError, ValueError, PdfReadError, PDFSyntaxError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(
        f"完成: {source_pages} 页 -> {receipt_count} 张回单 -> "
        f"{output} ({args.dpi} DPI, 240 x 140 mm)"
    )
    return 0


if __name__ == "__main__":
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        print(
            "把银行回单 PDF 拖到本程序图标上即可。\n"
            "输出 PDF 会放在第一个 PDF 所在的文件夹。"
        )
        _pause_before_exit()
        raise SystemExit(0)
    exit_code = main()
    _pause_before_exit()
    raise SystemExit(exit_code)
