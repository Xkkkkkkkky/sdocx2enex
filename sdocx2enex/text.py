"""将 SDocx 文本对象渲染为 ENML 允许的片段。

格式解码参考公开项目 sdocx_parser.py：
https://github.com/0xA01F3/sdocx_parser/blob/main/sdocx_parser.py。
本项目许可证见仓库根目录 LICENSE。
"""

from __future__ import annotations

import bisect
import html
import math
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .parser import TextObject


# Samsung span/paragraph 类型编号，依据 .research/sdocx2pdf 的格式定义。
_BOOLEAN_SPANS = {5: "b", 6: "i", 7: "u", 20: "s"}
_EDITING_SPANS = {15, 16, 18, 21, 22}
_ALIGNMENTS = {0: None, 1: "right", 2: "center"}


def _xml_safe_text(value: str, warnings: list[str]) -> str:
    """将 XML 1.0 不允许的码点替换为 U+FFFD，保留字符串长度。"""
    safe: list[str] = []
    replaced = 0
    for char in value:
        codepoint = ord(char)
        valid = (
            codepoint in (0x9, 0xA, 0xD)
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )
        if valid:
            safe.append(char)
        else:
            safe.append("\uFFFD")
            replaced += 1
    if replaced:
        warnings.append(f"文本中有 {replaced} 个 XML 非法字符，已替换为 U+FFFD。")
    return "".join(safe)


def _utf16_boundaries(text: str) -> list[int]:
    """返回每个 Python 字符边界对应的 UTF-16 code-unit 偏移。"""
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    return offsets


def _span_python_range(
    start: int,
    end: int,
    boundaries: list[int],
    warnings: list[str],
) -> tuple[int, int]:
    """把半开 UTF-16 范围转成 Python 字符范围；错误边界向外覆盖整字符。"""
    total = boundaries[-1]
    clipped_start = max(0, min(start, total))
    clipped_end = max(0, min(end, total))
    if clipped_start != start or clipped_end != end:
        warnings.append(
            f"文本格式范围 [{start}, {end}) 超出 UTF-16 文本长度 {total}，已裁剪。"
        )
    if clipped_end <= clipped_start:
        return 0, 0

    py_start = bisect.bisect_right(boundaries, clipped_start) - 1
    py_end = bisect.bisect_left(boundaries, clipped_end)
    if boundaries[py_start] != clipped_start or boundaries[py_end] != clipped_end:
        warnings.append(
            f"文本格式范围 [{start}, {end}) 切分了 UTF-16 代理对，已扩展到完整字符。"
        )
    return py_start, py_end


def _split_lines(text: str) -> list[tuple[int, int]]:
    """按 LF、CRLF 或 CR 切行，范围不包含换行符且保留尾部空行。"""
    if not text:
        return []

    rows: list[tuple[int, int]] = []
    row_start = 0
    index = 0
    while index < len(text):
        if text[index] == "\n" or text[index] == "\r":
            rows.append((row_start, index))
            if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 2
            else:
                index += 1
            row_start = index
        else:
            index += 1
    rows.append((row_start, len(text)))
    return rows


def _paragraph_rows(t: TextObject, line_count: int, warnings: list[str]) -> list[dict]:
    rows = [
        {"indent": 0, "align": 0, "bullet": None}
        for _ in range(line_count)
    ]

    for paragraph in t.paragraphs:
        paragraph_type = int(paragraph.paragraph_type)
        start = int(paragraph.start)
        end = int(paragraph.end)
        if end <= start:
            continue

        clipped_start = max(0, min(start, line_count))
        clipped_end = max(0, min(end, line_count))
        if clipped_start != start or clipped_end != end:
            warnings.append(
                f"段落格式范围 [{start}, {end}) 超出文本行数 {line_count}，已裁剪。"
            )
        if clipped_end <= clipped_start:
            continue

        extra = bytes(paragraph.extra)
        if paragraph_type == 2:  # IndentLevel
            if len(extra) < 4:
                warnings.append("缩进段落格式的数据不足 4 字节，已忽略。")
                continue
            level = struct.unpack_from("<I", extra)[0]
            for line_index in range(clipped_start, clipped_end):
                rows[line_index]["indent"] = level
        elif paragraph_type == 3:  # Alignment
            if len(extra) < 4:
                warnings.append("对齐段落格式的数据不足 4 字节，已忽略。")
                continue
            alignment = struct.unpack_from("<I", extra)[0]
            if alignment not in _ALIGNMENTS:
                warnings.append(f"未知对齐方式 {alignment} 未还原。")
            for line_index in range(clipped_start, clipped_end):
                rows[line_index]["align"] = alignment
        elif paragraph_type == 5:  # Bullet: 四个 little-endian u32
            # 样例中的 8 个零字节表示“无列表样式”，不是截断的列表数据。
            if extra == b"\x00" * 8:
                continue
            if len(extra) < 16:
                warnings.append("列表段落格式的数据不足 16 字节，已忽略。")
                continue
            style, value, _reserved, _group = struct.unpack_from("<4I", extra)
            if style not in (0, 2, 4, 8):
                warnings.append(f"未知列表样式 {style} 未还原。")
                continue
            for line_index in range(clipped_start, clipped_end):
                rows[line_index]["bullet"] = {
                    "style": style,
                    "value": value,
                    "range_start": start,
                }
        elif paragraph_type == 6:  # ParsingState 仅表示编辑状态
            continue
        elif paragraph_type in (4, 8, 9, 10):
            warnings.append(f"段落格式类型 {paragraph_type} 尚未还原。")
        else:
            warnings.append(f"未知段落格式类型 {paragraph_type} 尚未还原。")

    return rows


def _span_attributes(
    t: TextObject, text: str, boundaries: list[int], warnings: list[str]
) -> list[dict]:
    char_attrs = [
        {"tags": set(), "fg": None, "bg": None, "size": None}
        for _ in text
    ]
    for span in t.spans:
        span_type = int(span.span_type)
        start, end = _span_python_range(
            int(span.start), int(span.end), boundaries, warnings
        )
        if end <= start:
            continue
        extra = bytes(span.extra)

        if span_type in _BOOLEAN_SPANS:
            if len(extra) < 2:
                warnings.append(f"布尔文本格式 {span_type} 的数据不足 2 字节，已忽略。")
                continue
            if struct.unpack_from("<H", extra)[0] == 1:
                tag = _BOOLEAN_SPANS[span_type]
                for index in range(start, end):
                    char_attrs[index]["tags"].add(tag)
        elif span_type in (1, 17):  # ForegroundColour / BackgroundColour, BGRA
            if len(extra) < 4:
                warnings.append(f"颜色文本格式 {span_type} 的数据不足 4 字节，已忽略。")
                continue
            blue, green, red, alpha = extra[:4]
            if alpha == 0:
                continue
            color = f"#{red:02x}{green:02x}{blue:02x}"
            key = "fg" if span_type == 1 else "bg"
            for index in range(start, end):
                char_attrs[index][key] = color
        elif span_type == 3:  # FontSize
            if len(extra) < 4:
                warnings.append("字号文本格式的数据不足 4 字节，已忽略。")
                continue
            point_size = struct.unpack_from("<f", extra)[0]
            if not math.isfinite(point_size) or not 0 < point_size <= 1000:
                warnings.append(f"无效字号 {point_size!r} 未应用。")
                continue
            for index in range(start, end):
                char_attrs[index]["size"] = point_size
        elif span_type == 4:  # FontName 标注尚未解码
            warnings.append("字体名称标注尚未解码。")
        elif span_type == 9:  # Hypertext 标注尚未解码
            warnings.append("超链接标注尚未解码，显示文字已保留；网页卡片的链接另行保留。")
        elif span_type in _EDITING_SPANS or span_type == 0:
            continue
        elif span_type == 19:
            warnings.append("时间戳文本格式尚未还原。")
        elif span_type == 23:
            warnings.append("公式文本格式尚未还原。")
        else:
            warnings.append(f"未知文本格式类型 {span_type} 尚未还原。")

    return char_attrs


def _escape_text(text: str) -> str:
    # 保留原始 U+FFFC 对象占位符，不向正文添加解释文字。
    return html.escape(text, quote=True)


def _render_range(text: str, attrs: list[dict], start: int, end: int) -> str:
    if start >= end:
        return ""

    pieces: list[str] = []
    run_start = start
    current = attrs[start]
    for index in range(start + 1, end + 1):
        next_attrs = attrs[index] if index < end else None
        same = (
            next_attrs is not None
            and current["tags"] == next_attrs["tags"]
            and current["fg"] == next_attrs["fg"]
            and current["bg"] == next_attrs["bg"]
            and current["size"] == next_attrs["size"]
        )
        if same:
            continue

        content = _escape_text(text[run_start:index])
        # 标签次序固定，保证同一组样式总是生成相同的 ENML。
        for tag in reversed(("s", "b", "i", "u")):
            if tag in current["tags"]:
                content = f"<{tag}>{content}</{tag}>"
        styles: list[str] = []
        if current["fg"] is not None:
            styles.append(f"color:{current['fg']}")
        if current["bg"] is not None:
            styles.append(f"background-color:{current['bg']}")
        if current["size"] is not None:
            styles.append(f"font-size:{current['size']:g}pt")
        if styles:
            content = f'<span style="{";".join(styles)}">{content}</span>'
        pieces.append(content)

        if index < end:
            run_start = index
            current = next_attrs

    return "".join(pieces)


def render_text(t: TextObject, warnings: list[str]) -> str:
    """把一个 TextObject 渲染为 ENML DTD 允许的 div/span 片段。"""
    text = _xml_safe_text(t.text, warnings)
    if not text:
        return ""

    boundaries = _utf16_boundaries(text)
    for index, char in enumerate(text):
        if char == "\uFFFC":
            utf16_offset = boundaries[index]
            warnings.append(
                f"文本 UTF-16 偏移 {utf16_offset} 含 U+FFFC：嵌入对象位置未恢复，保留原始占位字符。"
            )

    line_ranges = _split_lines(text)
    paragraphs = _paragraph_rows(t, len(line_ranges), warnings)
    char_attrs = _span_attributes(t, text, boundaries, warnings)
    rendered: list[str] = []

    if any(
        row["bullet"] is not None and row["bullet"]["style"] in (4, 8)
        for row in paragraphs
    ):
        warnings.append("编号和项目符号列表以字符模拟，无法作为可编辑的原生列表。")

    for line_index, (start, end) in enumerate(line_ranges):
        row = paragraphs[line_index]
        body = _render_range(text, char_attrs, start, end)
        bullet = row["bullet"]
        prefix = ""
        if bullet is not None:
            if bullet["style"] == 2:
                checked = "true" if bullet["value"] else "false"
                prefix = f'<en-todo checked="{checked}"/>'
                if body:
                    prefix += " "
            elif bullet["style"] == 4:
                number = bullet["value"] or 1
                number += max(0, line_index - bullet["range_start"])
                prefix = f"{number}. "
            elif bullet["style"] == 8:
                prefix = "• "

        styles = ["white-space:pre-wrap"]
        indent = row["indent"]
        if indent:
            # 与参考实现一致，每级缩进按 2em 显示。
            styles.append(f"margin-left:{indent * 2}em")
        alignment = _ALIGNMENTS.get(row["align"])
        if alignment:
            styles.append(f"text-align:{alignment}")

        content = prefix + body
        if not content:
            content = "<br/>"
        rendered.append(f'<div style="{";".join(styles)}">{content}</div>')

    return "".join(rendered)
