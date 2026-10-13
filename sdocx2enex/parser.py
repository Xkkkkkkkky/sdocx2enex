"""Samsung Notes .sdocx 二进制读取器。

本模块裁剪并改写自 [0xA01F3/sdocx_parser](https://github.com/0xA01F3/sdocx_parser)
中的二进制解析逻辑，遵循 GPL-3.0；授权文本见仓库根目录 LICENSE。
"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias


MAX_MEMBER_SIZE = 128 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_SIZE = 512 * 1024 * 1024


class Reader:
    """小端字节读取器；任何负长度、越界或非法起始位置都会报错。"""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        if not 0 <= pos <= len(data):
            raise ValueError(f"读取位置 {pos} 超出数据长度 {len(data)}")
        self.data = data
        self.pos = pos

    def read(self, n: int) -> bytes:
        if not 0 <= self.pos <= len(self.data):
            raise ValueError(f"读取位置 {self.pos} 超出数据长度 {len(self.data)}")
        if n < 0:
            raise ValueError(f"读取长度不能为负数：{n}")
        end = self.pos + n
        if end > len(self.data):
            raise EOFError(
                f"需要读取 {n} 字节（偏移 {self.pos}），"
                f"但只剩 {len(self.data) - self.pos} 字节"
            )
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.read(8))[0]

    def bitfield(self) -> int:
        """读取 u8 长度和对应的小端位域，长度最大为 4 字节。"""
        size = self.u8()
        if size > 4:
            raise ValueError(f"位域长度 {size} 大于 4（偏移 {self.pos - 1}）")
        return int.from_bytes(self.read(size), "little") if size else 0

    def short_u16_string(self) -> str:
        """读取 u16 个 UTF-16LE 代码单元长度的字符串。"""
        return self.read(2 * self.u16()).decode("utf-16-le")

    def long_u16_string(self) -> str:
        """读取 u32 个 UTF-16LE 代码单元长度的字符串。"""
        return self.read(2 * self.u32()).decode("utf-16-le")

    def short_u8_string(self) -> str:
        """读取 u16 字节长度的 UTF-8 字符串。"""
        return self.read(self.u16()).decode("utf-8")


def open_object_frame(r: Reader, expected_data_type: int):
    """打开有长度前缀的对象帧，返回起止位置、flex 偏移和两个位域。"""
    frame_start = r.pos
    size = r.u32()  # 长度包含这 4 字节
    if size < 12:
        raise ValueError(f"对象帧长度 {size} 小于最小头部长度（偏移 {frame_start}）")
    frame_end = frame_start + size
    if frame_end > len(r.data):
        raise EOFError(
            f"对象帧（偏移 {frame_start}，长度 {size}）越过数据末尾 {len(r.data)}"
        )

    data_type = r.u16()
    if data_type != expected_data_type:
        raise ValueError(
            f"期望对象帧类型 {expected_data_type}，实际为 {data_type} "
            f"（偏移 {frame_start}）"
        )
    flex_offset = r.u32()
    prop = r.bitfield()
    fields = r.bitfield()
    header_size = r.pos - frame_start
    if header_size > size:
        raise ValueError(
            f"对象帧头长度 {header_size} 大于帧长度 {size}（偏移 {frame_start}）"
        )
    if flex_offset and not header_size <= flex_offset <= size:
        raise ValueError(
            f"对象帧 flex 偏移 {flex_offset} 超出帧头或帧边界 "
            f"（偏移 {frame_start}，长度 {size}）"
        )
    return frame_start, frame_end, flex_offset, prop, fields


def skip_frame(r: Reader) -> None:
    """按长度跳过一个帧，并拒绝小于头部或越过当前数据的长度。"""
    start = r.pos
    size = r.u32()
    if size < 4:
        raise ValueError(f"帧长度 {size} 小于长度前缀本身（偏移 {start}）")
    end = start + size
    if end > len(r.data):
        raise EOFError(f"帧（偏移 {start}，长度 {size}）越过数据末尾 {len(r.data)}")
    r.pos = end


SPAN_TYPES = {
    0: "None", 1: "ForegroundColour", 3: "FontSize", 4: "FontName",
    5: "Bold", 6: "Italic", 7: "Underline", 9: "Hypertext",
    15: "ComposingBackgroundColour", 16: "Composing", 17: "BackgroundColour",
    18: "ComposingTag", 19: "TimeStamp", 20: "Strikethrough",
    21: "Suggestion", 22: "SpellCorrection", 23: "Formula",
}


@dataclass
class Span:
    span_type: int
    start: int
    end: int
    interval_type: int
    extra: bytes

    @property
    def type_name(self) -> str:
        return SPAN_TYPES.get(self.span_type, f"Unknown({self.span_type})")


@dataclass
class Paragraph:
    paragraph_type: int
    start: int  # 行索引，不是字符偏移
    end: int
    extra: bytes


@dataclass
class TextObject:
    uuid: str
    rect: tuple[float, float, float, float]
    format_version: int
    text: str = ""
    spans: list[Span] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    margins: tuple[float, float, float, float] | None = None
    gravity: int | None = None
    angle: float = 0.0
    pivot: tuple[float, float] | None = None


PageObject: TypeAlias = tuple[int, bytes]
Page: TypeAlias = dict[str, object]


@dataclass
class Note:
    source: Path
    title: TextObject
    body: TextObject
    created_us: int
    modified_us: int
    pages: list[Page]
    media: dict[str, bytes]
    warnings: list[str]
    media_info: bytes


def parse_page_id_info(data: bytes) -> list[str]:
    """读取 ``pageIdInfo.dat`` 中按阅读顺序排列的页面 UUID。"""
    r = Reader(data)
    r.read(32)  # note 哈希
    count = r.u16()
    pages = []
    for _ in range(count):
        pages.append(r.short_u16_string())
        r.read(32)  # 页面哈希
    return pages


def bgra_colour(payload: bytes) -> str | None:
    """把 BGRA 颜色转成十六进制；透明色表示未指定颜色。"""
    if len(payload) < 4 or payload[3] == 0:
        return None
    return f"#{payload[2]:02x}{payload[1]:02x}{payload[0]:02x}"


# 页面头中背景色以下已知字段的长度；位 1–3 的长度尚未确认。
_PAGE_FLEX_SIZES_BELOW_BG = {0: 32, 4: 4}
_PAGE_BG_FIELD_BIT = 5


def page_background(data: bytes, flex_offset: int, page_end: int, fields: int):
    if not flex_offset or not (fields >> _PAGE_BG_FIELD_BIT & 1):
        return None
    offset = flex_offset
    for bit in range(_PAGE_BG_FIELD_BIT):
        if fields >> bit & 1:
            if bit not in _PAGE_FLEX_SIZES_BELOW_BG:
                return None
            offset += _PAGE_FLEX_SIZES_BELOW_BG[bit]
    if offset < 0 or offset + 4 > min(page_end, len(data)):
        return None
    return bgra_colour(data[offset:offset + 4])


def parse_page_header(r: Reader):
    """读取页面头，返回 UUID、尺寸、格式版本、背景色，并定位到图层区。"""
    page_start = r.pos
    page_end = r.u32()
    flex_offset = r.u32()
    r.bitfield()
    fields = r.bitfield()
    r.u32()  # orientation
    width = r.u32()
    height = r.u32()
    r.u32()  # offset x
    r.u32()  # offset y
    uuid = r.short_u16_string()
    r.i64()  # 页面修改时间，读取器不对外提供
    format_version = r.u32()
    r.u32()  # 最低格式版本

    if page_end < r.pos or page_end > len(r.data):
        raise ValueError(
            f"页面头结束位置 {page_end} 超出有效范围 "
            f"[{r.pos}, {len(r.data)}]（偏移 {page_start}）"
        )
    background = page_background(r.data, flex_offset, page_end, fields)
    r.pos = page_end  # 跳过页面头的可选字段
    return uuid, width, height, format_version, background


def iter_layer_objects(r: Reader):
    """依次读取页面对象；当前格式的子对象尚未实现，因此显式报错。"""
    layer_count = r.u16()
    r.u16()  # 当前图层索引
    for _ in range(layer_count):
        skip_frame(r)  # 图层头
        object_count = r.u32()
        for _ in range(object_count):
            object_type = r.u8()
            child_count = r.u16()
            if child_count:
                raise ValueError(f"暂不支持含有 {child_count} 个子对象的页面对象")
            yield object_type, r.read(r.u32())  # 对象帧及其哈希
        r.read(32)  # 图层哈希


def parse_object_base(r: Reader):
    """解析对象基础帧以及旋转角和旋转中心两个 flex 字段。"""
    frame_start, frame_end, flex_offset, _prop, fields = open_object_frame(r, 0)
    frame_data = r.data[frame_start:frame_end]
    frame = Reader(frame_data, r.pos - frame_start)

    format_version = frame.u32()
    uuid = frame.short_u8_string()
    frame.i64()  # 修改时间
    rect = (frame.f64(), frame.f64(), frame.f64(), frame.f64())
    frame.u32()  # 时间戳
    frame.u8()  # resize mode

    angle = 0.0
    if flex_offset and fields & 1:
        angle = Reader(frame_data, flex_offset).f32()

    # 位 18 是最后一个 flex 字段时，从帧尾读取旋转中心，避免猜测未知字段长度。
    pivot = None
    if flex_offset and (fields >> 18 & 1) and not (fields >> 19):
        tail = Reader(frame_data, len(frame_data) - 16)
        pivot = (tail.f64(), tail.f64())

    r.pos = frame_end
    return format_version, uuid, rect, angle, pivot


def parse_common(r: Reader) -> dict[str, object]:
    """读取文本、UTF-16 索引的 spans、段落信息和文本框布局。"""
    payload_size = r.u32()  # 长度只计后续 payload，不含此 u32
    payload = Reader(r.read(payload_size))

    text = payload.long_u16_string()
    spans = []
    for _ in range(payload.u32()):
        record_size = payload.u16()
        if record_size < 16:
            raise ValueError(f"span 记录长度 {record_size} 小于 16")
        spans.append(
            Span(
                payload.u32(), payload.u32(), payload.u32(), payload.u32(),
                payload.read(record_size - 16),
            )
        )

    paragraphs = []
    for _ in range(payload.u32()):
        record_size = payload.u16()
        if record_size < 12:
            raise ValueError(f"段落记录长度 {record_size} 小于 12")
        paragraphs.append(
            Paragraph(
                payload.u32(), payload.u32(), payload.u32(),
                payload.read(record_size - 12),
            )
        )

    margins = (payload.f32(), payload.f32(), payload.f32(), payload.f32())
    gravity = payload.u8()
    # span 的 start/end 保留文件中的 UTF-16 代码单元索引，由渲染层转换。
    return {
        "text": text,
        "spans": spans,
        "paragraphs": paragraphs,
        "margins": margins,
        "gravity": gravity,
    }


def parse_text_object(obj_bytes: bytes) -> TextObject:
    """解析 type-2 文本对象：ObjectBase、ShapeBase、Shape 和 Text。"""
    r = Reader(obj_bytes)
    format_version, uuid, rect, angle, pivot = parse_object_base(r)
    skip_frame(r)  # ShapeBase

    frame_start, frame_end, flex_offset, _prop, fields = open_object_frame(r, 7)
    obj = TextObject(
        uuid=uuid,
        rect=rect,
        format_version=format_version,
        angle=angle,
        pivot=pivot,
    )
    if flex_offset and fields & 1:
        payload_start = frame_start + flex_offset
        common_reader = Reader(r.data[payload_start:frame_end])
        common = parse_common(common_reader)
        obj.text = common["text"]
        obj.spans = common["spans"]
        obj.paragraphs = common["paragraphs"]
        obj.margins = common["margins"]
        obj.gravity = common["gravity"]
    r.pos = frame_end
    return obj


def _parse_note(note_bytes: bytes) -> tuple[TextObject, TextObject, int, int]:
    """解析 note.note 固定字段中的标题、正文和微秒时间戳。"""
    r = Reader(note_bytes)
    r.u32()  # flex offset；本模块不解析 note flex 字段
    r.bitfield()  # property flags
    r.bitfield()  # field flags
    r.u32()  # format version
    r.short_u16_string()  # id
    r.u32()  # revision
    created_us = r.i64()
    modified_us = r.i64()
    for _ in range(5):  # 宽、高、横纵向 padding、最低格式版本
        r.u32()
    title_blob = r.read(r.u32())
    body_blob = r.read(r.u32())
    return parse_text_object(title_blob), parse_text_object(body_blob), created_us, modified_us


def parse_note_title_body(note_bytes: bytes) -> tuple[TextObject, TextObject]:
    """读取 note.note 中的标题和正文两个文本对象。"""
    title, body, _created_us, _modified_us = _parse_note(note_bytes)
    return title, body


def _index_zip(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """检查 ZIP 重复成员和声明的解压大小，再建立唯一成员索引。"""
    members: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for info in zf.infolist():
        if info.filename in members:
            raise ValueError(f"ZIP 中存在重复成员：{info.filename!r}")
        if info.file_size < 0:
            raise ValueError(f"ZIP 成员声明了无效大小：{info.filename!r}")
        if info.file_size > MAX_MEMBER_SIZE:
            raise ValueError(
                f"ZIP 成员 {info.filename!r} 超过 128 MiB 解压限制"
            )
        total_size += info.file_size
        if total_size > MAX_ARCHIVE_UNCOMPRESSED_SIZE:
            raise ValueError("ZIP 解压总大小超过 512 MiB 限制")
        members[info.filename] = info
    return members


def _read_member(
    zf: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    name: str,
) -> bytes:
    info = members.get(name)
    if info is None or info.is_dir():
        raise ValueError(f"sdocx 缺少必需成员：{name!r}")
    data = zf.read(info)
    if len(data) != info.file_size:
        raise ValueError(
            f"ZIP 成员 {name!r} 实际解压长度 {len(data)} "
            f"与声明长度 {info.file_size} 不符"
        )
    return data


def _validate_page_uuid(uuid: str) -> None:
    # UUID 会直接用于 ZIP 成员名，拒绝路径分隔符和路径穿越片段。
    if not uuid or "/" in uuid or "\\" in uuid or uuid in {".", ".."}:
        raise ValueError(f"pageIdInfo.dat 中含有非法页面 UUID：{uuid!r}")


def parse_sdocx(path: str | Path) -> Note:
    """读取一个 .sdocx ZIP，保留所有页面对象原始字节和媒体文件。"""
    source = Path(path)
    with zipfile.ZipFile(source) as zf:
        members = _index_zip(zf)
        note_bytes = _read_member(zf, members, "note.note")
        page_ids = parse_page_id_info(_read_member(zf, members, "pageIdInfo.dat"))
        media_info = _read_member(zf, members, "media/mediaInfo.dat")
        title, body, created_us, modified_us = _parse_note(note_bytes)

        pages: list[Page] = []
        for page_id in page_ids:
            _validate_page_uuid(page_id)
            page_data = _read_member(zf, members, f"{page_id}.page")
            page_reader = Reader(page_data)
            uuid, width, height, format_version, background = parse_page_header(page_reader)
            header = page_data[:page_reader.pos]
            objects: list[PageObject] = []
            texts: list[TextObject] = []
            for object_type, blob in iter_layer_objects(page_reader):
                objects.append((object_type, blob))
                if object_type == 2:
                    texts.append(parse_text_object(blob))
            pages.append(
                {
                    "uuid": uuid,
                    "width": width,
                    "height": height,
                    "format_version": format_version,
                    "background": background,
                    "objects": objects,
                    "texts": texts,
                    "header": header,
                }
            )

        media = {
            name: _read_member(zf, members, name)
            for name in sorted(members)
            if name.startswith("media/")
            and name != "media/mediaInfo.dat"
            and not members[name].is_dir()
        }

    return Note(
        source=source,
        title=title,
        body=body,
        created_us=created_us,
        modified_us=modified_us,
        pages=pages,
        media=media,
        warnings=[],
        media_info=media_info,
    )
