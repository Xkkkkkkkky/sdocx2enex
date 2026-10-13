"""把解析后的 SDocx 笔记组装为 ENEX 文档。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from lxml import etree

from .parser import Note, parse_sdocx
from .text import render_text

ENEX_DTD = "http://xml.evernote.com/pub/evernote-export3.dtd"
ENML_DTD = "http://xml.evernote.com/pub/enml2.dtd"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FILENAME_DATE = re.compile(r"_([0-9]{6})_([0-9]{6})$")


def _xml_10_character(char: str) -> bool:
    """返回字符是否允许出现在 XML 1.0 文本节点中。"""
    codepoint = ord(char)
    return (
        codepoint in (0x9, 0xA, 0xD)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _normalize_title(value: str, warnings: list[str]) -> str:
    """规范化 ENEX 标题，并保留原值供报告和 metadata 使用。"""
    chars: list[str] = []
    line_breaks = 0
    controls = 0
    invalid_xml = 0
    previous_line_break = False
    for char in value:
        if char in "\r\n\u0085\u2028\u2029":
            if not previous_line_break:
                chars.append(" ")
            line_breaks += 1
            previous_line_break = True
        elif unicodedata.category(char) == "Cc":
            chars.append(" ")
            controls += 1
            previous_line_break = False
        elif not _xml_10_character(char):
            chars.append("\uFFFD")
            invalid_xml += 1
            previous_line_break = False
        else:
            chars.append(char)
            previous_line_break = False

    title = "".join(chars)
    if line_breaks:
        warnings.append("标题含换行，已替换为空格。")
    if controls:
        warnings.append(f"标题含 {controls} 个控制字符，已替换为空格。")
    if invalid_xml:
        warnings.append(f"标题含 {invalid_xml} 个 XML 非法字符，已替换为 U+FFFD。")

    trimmed = title.strip()
    if trimmed != title:
        warnings.append("标题两端空白已移除。")
    title = trimmed
    if len(title) > 255:
        title = title[:255].rstrip()
        warnings.append("标题超过 255 个码点，已截短。")

    if not title:
        title = "未命名笔记"
        warnings.append("原始标题为空，ENEX 标题已设为“未命名笔记”。")
    return title


def _utc_datetime(timestamp_us: int) -> datetime:
    """用整数微秒换算 UTC 时间，避免浮点时间戳精度损失。"""
    return _EPOCH + timedelta(microseconds=timestamp_us)


def _enex_datetime(value: datetime) -> str:
    return (
        f"{value.year:04d}{value.month:02d}{value.day:02d}T"
        f"{value.hour:02d}{value.minute:02d}{value.second:02d}Z"
    )


def _report_datetime(value: datetime) -> str:
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}Z"
    )


def _warn_filename_date(source: Path, created: datetime, warnings: list[str]) -> None:
    """只将文件名末尾日期用于差异提示，不参与笔记时间推断。"""
    match = _FILENAME_DATE.search(source.stem)
    if not match:
        return
    yymmdd, _hhmmss = match.groups()
    try:
        file_date = datetime(
            2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
        ).date()
    except ValueError:
        return
    note_date = created.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if file_date != note_date:
        warnings.append(
            f"文件名日期 {file_date.isoformat()} 与笔记创建日期 "
            f"{note_date.isoformat()} 不同；笔记时间仍以内部时间戳为准。"
        )


def _append_fragment(parent: etree._Element, fragment: str) -> None:
    """把可信的渲染器 ENML 片段解析后接到节点下。"""
    if not fragment:
        return
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    wrapper = etree.fromstring(f"<fragment>{fragment}</fragment>".encode("utf-8"), parser)
    for child in list(wrapper):
        parent.append(child)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _resource_key(resource) -> tuple[str, str]:
    return _md5(resource.data), resource.mime


def _is_image(resource) -> bool:
    return resource.mime.startswith("image/")


def _sanitize_resource_names(resources: list, warnings: list[str]) -> None:
    """保证 resource-attributes 中的文件名可序列化为 XML 1.0。"""
    for resource in resources:
        safe_name = "".join(
            char if _xml_10_character(char) else "\uFFFD" for char in resource.name
        )
        if safe_name != resource.name:
            warnings.append(
                f"附件文件名 {resource.name!r} 含 XML 非法字符，已替换为 U+FFFD。"
            )
            resource.name = safe_name


def _add_en_media(parent: etree._Element, resource) -> None:
    attributes = {"type": resource.mime, "hash": _md5(resource.data)}
    if _is_image(resource) and resource.width is not None and resource.height is not None:
        attributes["width"] = str(resource.width)
        attributes["height"] = str(resource.height)
    etree.SubElement(parent, "en-media", **attributes)


def _add_resource(note_element: etree._Element, resource) -> None:
    resource_element = etree.SubElement(note_element, "resource")
    data_element = etree.SubElement(resource_element, "data", encoding="base64")
    data_element.text = base64.b64encode(resource.data).decode("ascii")
    etree.SubElement(resource_element, "mime").text = resource.mime
    if _is_image(resource) and resource.width is not None and resource.height is not None:
        etree.SubElement(resource_element, "width").text = str(resource.width)
        etree.SubElement(resource_element, "height").text = str(resource.height)
    attributes = etree.SubElement(resource_element, "resource-attributes")
    etree.SubElement(attributes, "file-name").text = resource.name
    if not _is_image(resource):
        etree.SubElement(attributes, "attachment").text = "true"


def _note_element(note: Note, report: dict) -> etree._Element:
    from .media import Resource, render_pages

    warnings = list(note.warnings)
    original_title = note.title.text
    title = _normalize_title(original_title, warnings)

    created = _utc_datetime(note.created_us)
    modified = _utc_datetime(note.modified_us)
    if note.created_us % 1_000_000:
        warnings.append("创建时间包含微秒；ENEX 仅保存到整秒，已截去微秒。")
    if note.modified_us % 1_000_000:
        warnings.append("更新时间包含微秒；ENEX 仅保存到整秒，已截去微秒。")
    _warn_filename_date(note.source, created, warnings)

    body_html = render_text(note.body, warnings)
    page_text_html: list[str] = []
    page_text_count = 0
    has_page_text = False
    for page in note.pages:
        for text_object in page.get("texts", []):
            has_page_text = True
            page_text_count += len(text_object.text)
            page_text_html.append(render_text(text_object, warnings))
    if has_page_text:
        warnings.append("页面文本框已转为连续文本；文本框坐标和页面布局未还原。")

    page_resources, originals, web_links_html = render_pages(note, warnings)
    # 页面图像和附件均按约定顺序放置；源文件和 metadata 始终排在最后。
    source_bytes = note.source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    metadata_resource = Resource(
        "metadata.json",
        "application/json",
        b"",
    )
    resources = [*page_resources, *originals]
    source_resource = Resource(note.source.name, "application/octet-stream", source_bytes)
    resources.extend((source_resource, metadata_resource))
    _sanitize_resource_names(resources, warnings)
    # ink/media 可能对每个对象重复报告同一问题；保留首次出现顺序并去重。
    warnings[:] = list(dict.fromkeys(warnings))

    metadata = {
        "original_title": original_title,
        "created_us": note.created_us,
        "modified_us": note.modified_us,
        "source_sha256": source_hash,
        "warnings": warnings,
    }
    metadata_resource.data = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # metadata 在最后生成，按实际二进制 MD5 和 MIME 去重附件。
    unique_resources = []
    seen: set[tuple[str, str]] = set()
    for resource in resources:
        key = _resource_key(resource)
        if key not in seen:
            seen.add(key)
            unique_resources.append(resource)

    enml_root = etree.Element("en-note")
    if note.pages:
        background = note.pages[0].get("background")
        if isinstance(background, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", background):
            enml_root.set("style", f"background-color:{background}")
    _append_fragment(enml_root, body_html)
    for fragment in page_text_html:
        _append_fragment(enml_root, fragment)
    _append_fragment(enml_root, web_links_html)
    embedded_keys = set()
    for resource in page_resources:
        container = etree.SubElement(enml_root, "div")
        _add_en_media(container, resource)
        embedded_keys.add(_resource_key(resource))

    original_keys = {_resource_key(resource) for resource in originals}
    visible_originals = [
        resource
        for resource in unique_resources
        if _resource_key(resource) in original_keys
        and _resource_key(resource) not in embedded_keys
    ]
    if visible_originals:
        heading = etree.SubElement(enml_root, "div")
        etree.SubElement(heading, "b").text = "原始附件"
        for resource in visible_originals:
            container = etree.SubElement(enml_root, "div")
            _add_en_media(container, resource)
            embedded_keys.add(_resource_key(resource))

    remaining_resources = [
        resource
        for resource in unique_resources
        if _resource_key(resource) not in embedded_keys
    ]
    if remaining_resources:
        heading = etree.SubElement(enml_root, "div")
        etree.SubElement(heading, "b").text = "源文件与精确时间元数据"
        for resource in remaining_resources:
            container = etree.SubElement(enml_root, "div")
            _add_en_media(container, resource)
            embedded_keys.add(_resource_key(resource))

    enml = etree.tostring(
        enml_root,
        encoding="UTF-8",
        xml_declaration=True,
        doctype=f'<!DOCTYPE en-note SYSTEM "{ENML_DTD}">',
    ).decode("utf-8")

    note_element = etree.Element("note")
    etree.SubElement(note_element, "title").text = title
    etree.SubElement(note_element, "content").text = etree.CDATA(enml)
    etree.SubElement(note_element, "created").text = _enex_datetime(created)
    etree.SubElement(note_element, "updated").text = _enex_datetime(modified)
    for resource in unique_resources:
        _add_resource(note_element, resource)

    report.update(
        {
            "title": title,
            "original_title": original_title,
            "created_us": note.created_us,
            "modified_us": note.modified_us,
            "created_utc": _report_datetime(created),
            "modified_utc": _report_datetime(modified),
            "text_characters": len(note.body.text) + page_text_count,
            "page_count": len(note.pages),
            "stroke_count": sum(
                1
                for page in note.pages
                for kind, _blob in page.get("objects", [])
                if kind == 1
            ),
            "resource_count": len(unique_resources),
            "source_sha256": source_hash,
            "warnings": warnings,
            "status": "success",
        }
    )
    return note_element


def convert_note(path: str | Path) -> tuple[etree._Element, dict]:
    """转换单个 SDocx，返回 ENEX note 元素和逐笔记报告。"""
    source = Path(path)
    note = parse_sdocx(source)
    report = {"source": str(source)}
    element = _note_element(note, report)
    return element, report


def _same_path(left: Path, right: Path) -> bool:
    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _validate_destinations(paths: list[Path], output: Path, report_path: Path) -> None:
    if output.suffix.lower() == ".sdocx":
        raise ValueError(f"ENEX 输出路径不能使用 .sdocx 后缀：{output}")
    if report_path.suffix.lower() == ".sdocx":
        raise ValueError(f"报告路径不能使用 .sdocx 后缀：{report_path}")
    if _same_path(output, report_path):
        raise ValueError("ENEX 输出路径与报告路径不能相同")
    for source in paths:
        if _same_path(source, output):
            raise ValueError(f"ENEX 输出路径会覆盖输入文件：{source}")
        if _same_path(source, report_path):
            raise ValueError(f"报告路径会覆盖输入文件：{source}")


def _failed_report(source: Path, error: Exception) -> dict:
    try:
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        source_hash = None
    return {
        "source": str(source),
        "title": None,
        "original_title": None,
        "created_us": None,
        "modified_us": None,
        "created_utc": None,
        "modified_utc": None,
        "text_characters": 0,
        "page_count": 0,
        "stroke_count": 0,
        "resource_count": 0,
        "source_sha256": source_hash,
        "warnings": [f"转换失败：{error}"],
        "status": "failed",
        "error": str(error),
    }


def _stage_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output_file:
            output_file.write(data)
            output_file.flush()
            os.fsync(output_file.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def build_enex(
    paths: Iterable[str | Path], output: str | Path, report_path: str | Path
) -> dict:
    """转换输入集合并原子写入成功笔记和逐文件 JSON 报告。"""
    sources = [Path(path) for path in paths]
    output_path = Path(output)
    report_file = Path(report_path)
    if not sources:
        raise ValueError("没有可转换的 SDocx 输入")
    _validate_destinations(sources, output_path, report_file)

    note_elements: list[etree._Element] = []
    file_reports: list[dict] = []
    for source in sources:
        try:
            note_element, one_report = convert_note(source)
            note_elements.append(note_element)
            file_reports.append(one_report)
        except Exception as error:
            file_reports.append(_failed_report(source, error))

    success_count = len(note_elements)
    failure_count = len(file_reports) - success_count
    if failure_count == 0:
        status = "success"
    elif success_count:
        status = "partial"
    else:
        status = "failed"

    result = {
        "output": str(output_path),
        "report": str(report_file),
        "status": status,
        "success_count": success_count,
        "failure_count": failure_count,
        "output_written": bool(success_count),
        "files": file_reports,
    }

    enex_bytes = None
    if success_count:
        root = etree.Element(
            "en-export",
            **{
                "export-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                "application": "sdocx2enex",
                "version": "0.1",
            },
        )
        root.extend(note_elements)
        enex_bytes = etree.tostring(
            root,
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
            doctype=f'<!DOCTYPE en-export SYSTEM "{ENEX_DTD}">',
        )

    report_bytes = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    staged_output: Path | None = None
    staged_report: Path | None = None
    try:
        if enex_bytes is not None:
            staged_output = _stage_file(output_path, enex_bytes)
        staged_report = _stage_file(report_file, report_bytes)
        if staged_output is not None:
            os.replace(staged_output, output_path)
            staged_output = None
        os.replace(staged_report, report_file)
        staged_report = None
    finally:
        if staged_output is not None:
            staged_output.unlink(missing_ok=True)
        if staged_report is not None:
            staged_report.unlink(missing_ok=True)
    return result
