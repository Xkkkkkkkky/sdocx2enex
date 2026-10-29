"""Samsung Notes 笔迹帧解码与 Pillow 栅格绘制。"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Mapping
from operator import index

from PIL import Image, ImageChops, ImageDraw

from .parser import Reader, open_object_frame, parse_object_base


_MAX_CANVAS_PIXELS = 40_000_000
_PRESSURE_WARNING = "笔压和笔刷仅按点宽度近似绘制，像素效果可能不同于 Samsung Notes 原始笔刷。"
_TILT_WARNING = "笔倾斜数据已读取，但未用于还原笔刷形状。"


def _u16s(reader: Reader, count: int) -> tuple[int, ...]:
    if count < 0:
        raise ValueError("数组长度不能为负数")
    if not count:
        return ()
    return struct.unpack(f"<{count}H", reader.read(count * 2))


def _u32s(reader: Reader, count: int) -> tuple[int, ...]:
    if count < 0:
        raise ValueError("数组长度不能为负数")
    if not count:
        return ()
    return struct.unpack(f"<{count}I", reader.read(count * 4))


def _f32s(reader: Reader, count: int) -> tuple[float, ...]:
    if count < 0:
        raise ValueError("数组长度不能为负数")
    if not count:
        return ()
    return struct.unpack(f"<{count}f", reader.read(count * 4))


def _f64s(reader: Reader, count: int) -> tuple[float, ...]:
    if count < 0:
        raise ValueError("数组长度不能为负数")
    if not count:
        return ()
    return struct.unpack(f"<{count}d", reader.read(count * 8))


def _point_delta(value: int) -> float:
    """解码 sign-magnitude 的坐标增量，低 5 位为 1/32。"""
    magnitude = value & 0x7FFF
    absolute = (magnitude >> 5) + (magnitude & 0x1F) / 32.0
    return -absolute if value & 0x8000 else absolute


def _small_delta(value: int) -> float:
    """解码压力、倾斜和方向的 sign-magnitude 增量，低 12 位为 1/4096。"""
    magnitude = value & 0x7FFF
    absolute = (magnitude >> 12) + (magnitude & 0x0FFF) / 4096.0
    return -absolute if value & 0x8000 else absolute


def _frame_size(blob: bytes, offset: int, label: str) -> int:
    if offset < 0 or offset + 4 > len(blob):
        raise EOFError(f"{label}帧缺少长度前缀（偏移 {offset}）")
    size = struct.unpack_from("<I", blob, offset)[0]
    if size < 12:
        raise ValueError(f"{label}帧长度 {size} 小于最小头部长度（偏移 {offset}）")
    end = offset + size
    if end > len(blob):
        raise EOFError(f"{label}帧（偏移 {offset}，长度 {size}）越过对象数据末尾 {len(blob)}")
    return size


def _read_events(reader: Reader, event_count: int, compressed: bool, has_tilt: bool):
    points: list[tuple[float, float, float]] = []
    if compressed:
        if event_count:
            x, y = reader.f64(), reader.f64()
            point_deltas = _u16s(reader, 2 * (event_count - 1))
            pressure = reader.f32()
            pressure_deltas = _u16s(reader, event_count - 1)
            timestamp = reader.u32()
            timestamp_deltas = _u16s(reader, event_count - 1)

            if has_tilt:
                tilt = reader.f32()
                tilt_deltas = _u16s(reader, event_count - 1)
                orientation = reader.f32()
                orientation_deltas = _u16s(reader, event_count - 1)
            else:
                tilt_deltas = orientation_deltas = ()
                tilt = orientation = 0.0

            points.append((x, y, pressure))
            for event_index in range(event_count - 1):
                x += _point_delta(point_deltas[2 * event_index])
                y += _point_delta(point_deltas[2 * event_index + 1])
                pressure += _small_delta(pressure_deltas[event_index])
                timestamp += timestamp_deltas[event_index]
                if has_tilt:
                    tilt += _small_delta(tilt_deltas[event_index])
                    orientation += _small_delta(orientation_deltas[event_index])
                points.append((x, y, pressure))
    else:
        xy = _f64s(reader, 2 * event_count)
        pressures = _f32s(reader, event_count)
        # 时间戳和倾斜字段虽不返回，仍完整读取，确保后续工具类型及 flex 边界正确。
        _u32s(reader, event_count)
        if has_tilt:
            _f32s(reader, event_count)
            _f32s(reader, event_count)
        points.extend(
            (xy[2 * i], xy[2 * i + 1], pressures[i])
            for i in range(event_count)
        )

    if any(not math.isfinite(v) for point in points for v in point):
        raise ValueError("笔迹包含非有限坐标或压力")
    return points


def _read_flex(reader: Reader, fields: int, prop: int, warnings: list[str]):
    # flex 字段必须按位序读取；未知字段没有可靠长度时不能跳过猜测后续内容。
    pen_size: float | None = None
    fixed_width: float | None = None
    color = (0, 0, 0, 255)
    stop_unknown = False

    if fields & 1:
        warnings.append("笔刷 flex 位 0 尚无已知长度；后续 flex 字段未猜测读取。")
        stop_unknown = True
    else:
        if fields & (1 << 1):
            reader.read(4)  # 高级笔刷配置字符串 ID
        if fields & (1 << 2):
            b, g, r, a = reader.read(4)
            color = (r, g, b, a)
        if fields & (1 << 3):
            pen_size = reader.f32()
            if not math.isfinite(pen_size):
                raise ValueError("pen_size 不是有限数")
        if fields & (1 << 4):
            reader.read(4)
        if fields & (1 << 5):
            warnings.append("笔刷 flex 位 5 的长度未知；保留已读取的颜色和尺寸，不猜测后续字段。")
            stop_unknown = True
        elif fields & (1 << 6):
            warnings.append("笔刷 flex 位 6 尚无已知长度；后续 flex 字段未猜测读取。")
            stop_unknown = True
        else:
            if fields & (1 << 7):
                reader.read(4)  # 笔名字符串 ID
            if fields & (1 << 8):
                fixed_width = reader.f32()
                if not math.isfinite(fixed_width):
                    raise ValueError("fixed_width 不是有限数")

            # 已知但暂不渲染的 flex 效果按 Rust 字段类型读取，以验证帧边界。
            unsupported: list[str] = []
            for bit, name in (
                (9, "尺寸级别"), (10, "粒子密度"), (11, "渲染级别"),
                (12, "原始宽度"), (13, "初始容差"), (14, "线型"),
                (15, "虚线偏移"), (16, "笔迹类型"), (17, "粒子间距"),
                (18, "粒子尺寸"), (19, "图案索引"), (20, "图案缩放"),
                (21, "粒子级别"), (22, "彩虹间距"), (23, "彩虹偏移"),
                (24, "渐变颜色"), (25, "颜色类型"),
            ):
                if not fields & (1 << bit):
                    continue
                unsupported.append(name)
                if bit in (9, 10, 11, 12, 19, 21, 22):
                    reader.read(4)
                elif bit in (13, 15, 17, 18, 20, 23):
                    reader.read(4)
                elif bit in (14, 16, 25):
                    reader.read(2)
                else:  # bit 24：u16 数量，随后每项 4 字节 BGRA。
                    count = reader.u16()
                    reader.read(4 * count)
            if unsupported:
                warnings.append("笔刷高级效果尚未还原：" + "、".join(unsupported) + "。")

            unknown = fields >> 26
            if unknown:
                warnings.append(f"笔刷 flex 包含未知位 {unknown:#x}；未知字段数据未解析。")
                stop_unknown = True

    size = pen_size
    if prop & (1 << 4) and fixed_width is not None:
        size = fixed_width
    if size is None:
        size = 1.0
    if not math.isfinite(size):
        raise ValueError("笔刷尺寸不是有限数")
    if size < 0:
        raise ValueError(f"笔刷尺寸不能为负数：{size}")

    return size, color, stop_unknown


def parse_stroke(blob: bytes) -> dict:
    """解码 type-1 笔迹对象，保留其逐点坐标、压力和渲染元数据。"""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TypeError("stroke blob 必须是 bytes-like 对象")
    data = bytes(blob)

    # 基础帧与笔迹帧分别限界，避免畸形长度借用对象后续字节。
    base_size = _frame_size(data, 0, "对象基础")
    base_reader = Reader(data[:base_size])
    _format_version, _uuid, rect, angle, pivot = parse_object_base(base_reader)
    if base_reader.pos != base_size:
        raise ValueError("对象基础帧未完整解析")

    if len(rect) != 4 or any(not math.isfinite(v) for v in rect):
        raise ValueError("笔迹矩形包含非有限坐标")
    if not math.isfinite(angle):
        raise ValueError("笔迹旋转角不是有限数")
    if pivot is not None and (len(pivot) != 2 or any(not math.isfinite(v) for v in pivot)):
        raise ValueError("笔迹旋转中心包含非有限坐标")

    stroke_size = _frame_size(data, base_size, "笔迹")
    stroke_reader = Reader(data[base_size:base_size + stroke_size])
    frame_start, frame_end, flex_offset, prop, fields = open_object_frame(stroke_reader, 1)
    event_count = stroke_reader.u16()
    compressed = bool(prop & 1)
    has_tilt = bool(prop & (1 << 2))
    points = _read_events(stroke_reader, event_count, compressed, has_tilt)
    tool = stroke_reader.u16()

    warnings = [_PRESSURE_WARNING]
    if has_tilt:
        warnings.append(_TILT_WARNING)

    known_prop_mask = sum(1 << bit for bit in (*range(9), *range(10, 15)))
    unknown_prop = prop & ~known_prop_mask
    if unknown_prop:
        warnings.append(f"笔迹属性包含未知位 {unknown_prop:#x}。")

    unsupported_properties = [
        name for bit, name in (
            (1, "仅回放标记"), (6, "置顶笔效果"), (7, "透明度锁定"),
            (11, "固定不透明度"), (12, "彩虹笔效果"),
            (13, "直线模式"), (14, "显现效果"),
        ) if prop & (1 << bit)
    ]
    if unsupported_properties:
        warnings.append("笔迹属性效果尚未还原：" + "、".join(unsupported_properties) + "。")

    if flex_offset:
        # offset 相对笔迹帧起点；固定事件区不得与 flex 区重叠。
        if flex_offset < stroke_reader.pos:
            raise ValueError(
                f"笔迹 flex 偏移 {flex_offset} 位于固定数据末尾 {stroke_reader.pos} 之前"
            )
        stroke_reader.pos = frame_start + flex_offset
        size, color, stop_unknown = _read_flex(stroke_reader, fields, prop, warnings)
        if not stop_unknown and stroke_reader.pos != frame_end:
            raise ValueError(
                f"笔迹 flex 数据结束于 {stroke_reader.pos}，帧结束于 {frame_end}"
            )
    else:
        if fields:
            warnings.append("笔迹 flex 偏移为零，按格式忽略 flex 位域。")
        size, color = 1.0, (0, 0, 0, 255)
        if stroke_reader.pos != frame_end:
            raise ValueError(
                f"笔迹帧 flex 偏移为零但仍有 {frame_end - stroke_reader.pos} 字节未解析"
            )

    # 已读的 flex 尺寸字段也必须有限；没有尺寸时用可见的 1 个数据单位绘制。
    return {
        "rect": tuple(rect),
        "angle": float(angle),
        "pivot": tuple(pivot) if pivot is not None else None,
        "points": points,
        "color": color,
        "size": float(size),
        "property": prop,
        "tool": tool,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _path_width(size: float, scale: float, pressure: float) -> float:
    # 保留压力曲线的相对宽度，同时保证零压力采样点仍能连成可见细线。
    width = size * scale * max(0.05, min(1.0, pressure))
    if not math.isfinite(width):
        raise ValueError("缩放后的笔刷宽度不是有限数")
    return max(1.0, width)


def _draw_path(
    draw: ImageDraw.ImageDraw,
    points,
    color,
    size: float,
    scale: float,
    y_offset: int = 0,
) -> None:
    if not points:
        return
    widths = [_path_width(size, scale, point[2]) for point in points]
    for i in range(len(points) - 1):
        width = max(1, round((widths[i] + widths[i + 1]) / 2))
        # 先按全页像素坐标向零截断，再减去块偏移，保持分块与整页栅格化一致。
        draw.line((
            int(points[i][0]), int(points[i][1]) - y_offset,
            int(points[i + 1][0]), int(points[i + 1][1]) - y_offset,
        ), fill=color, width=width)
    # 用圆形补齐端帽和拐点；每条 stroke 会先画在独立 layer，避免半透明笔刷自相叠黑。
    for (x, y, _pressure), width in zip(points, widths):
        radius = width / 2
        left, top, right, bottom = (
            int(x - radius), int(y - radius), int(x + radius), int(y + radius)
        )
        draw.ellipse((left, top - y_offset, right, bottom - y_offset), fill=color)


def draw_strokes(
    strokes: Iterable[Mapping],
    size: tuple[int, int],
    scale: float,
    origin: tuple[float, float] = (0, 0),
    *,
    y_offset: int = 0,
) -> Image.Image:
    """以透明 RGBA 图层绘制笔迹；画布边界与总像素数会严格检查。"""
    if len(size) != 2:
        raise ValueError("画布尺寸必须是 (宽, 高)")
    width, height = (index(size[0]), index(size[1]))
    if width <= 0 or height <= 0:
        raise ValueError(f"画布尺寸必须为正数：{width}x{height}")
    if width * height > _MAX_CANVAS_PIXELS:
        raise ValueError(f"笔迹画布超过 {_MAX_CANVAS_PIXELS} 像素限制：{width}x{height}")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError(f"绘制比例必须是有限正数：{scale}")
    if len(origin) != 2 or any(not math.isfinite(value) for value in origin):
        raise ValueError("绘制原点必须是两个有限坐标")
    y_offset = index(y_offset)

    ink = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for stroke in strokes:
        points = stroke["points"]
        if not points:
            continue
        base_size = float(stroke["size"])
        if not math.isfinite(base_size) or base_size < 0:
            raise ValueError(f"笔刷尺寸无效：{base_size}")
        color = tuple(stroke["color"])
        if len(color) != 4 or any(not 0 <= channel <= 255 for channel in color):
            raise ValueError(f"笔刷颜色必须是 RGBA 四元组：{color}")
        angle = float(stroke["angle"])
        if not math.isfinite(angle):
            raise ValueError(f"笔迹角度不是有限数：{angle}")
        rect = stroke["rect"]
        if len(rect) != 4 or any(not math.isfinite(value) for value in rect):
            raise ValueError("笔迹矩形包含非有限坐标")
        pivot = stroke["pivot"]
        if pivot is None:
            pivot_x, pivot_y = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        else:
            if len(pivot) != 2 or any(not math.isfinite(value) for value in pivot):
                raise ValueError("笔迹旋转中心包含非有限坐标")
            pivot_x, pivot_y = pivot

        radians = math.radians(angle)
        cos_a, sin_a = math.cos(radians), math.sin(radians)
        transformed = []
        for point in points:
            if len(point) != 3 or any(not math.isfinite(value) for value in point):
                raise ValueError("笔迹点必须包含有限的 x、y、pressure")
            x, y, pressure = point
            dx, dy = x - pivot_x, y - pivot_y
            # 文档坐标的 y 轴向下，因此正角按页面视觉方向顺时针旋转。
            rx = pivot_x + dx * cos_a - dy * sin_a
            ry = pivot_y + dx * sin_a + dy * cos_a
            px, py = (rx - origin[0]) * scale, (ry - origin[1]) * scale
            if not math.isfinite(px) or not math.isfinite(py):
                raise ValueError("变换后的笔迹坐标不是有限数")
            transformed.append((px, py, pressure))

        is_eraser = bool(stroke["property"] & (1 << 3)) or stroke["tool"] == 4
        if is_eraser:
            mask = Image.new("L", (width, height), 0)
            _draw_path(ImageDraw.Draw(mask), transformed, 255, base_size, scale, y_offset)
            ink.putalpha(ImageChops.subtract(ink.getchannel("A"), mask))
            continue

        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_path(ImageDraw.Draw(layer), transformed, color, base_size, scale, y_offset)
        ink.alpha_composite(layer)

    return ink
