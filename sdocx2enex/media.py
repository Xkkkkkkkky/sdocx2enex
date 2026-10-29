"""页面视觉内容：复用 Pillow / MuPDF，二进制字段参考 sdocx2pdf（MIT）。"""
from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass
from html import escape

import pymupdf
from PIL import Image

from .parser import Reader, open_object_frame, parse_object_base, skip_frame, parse_common
from .ink import parse_stroke, draw_strokes


@dataclass
class Resource:
    name: str
    mime: str
    data: bytes
    width: int | None = None
    height: int | None = None


def bindings(data: bytes) -> dict[int, str]:
    """mediaInfo 明确绑定 ID；不靠文件名或 ZIP 排列猜测页面关联。"""
    r = Reader(data)
    newer = data.endswith(b'EOFX')
    if newer:
        r.u32()
    elif not data.endswith(b'EOF'):
        raise ValueError('mediaInfo 缺少 EOF 标记')
    result = {}
    for _ in range(r.u16()):
        end = r.pos + 4 + r.u32() if newer else None
        bind_id = r.u32()
        name = r.short_u16_string()
        result[bind_id] = 'media/' + name
        r.read(64 + 2 + 8)
        if newer:
            r.u8()
            if r.pos != end:
                raise ValueError('mediaInfo 条目长度不一致')
    return result


def page_backgrounds(page: dict) -> tuple[int | None, list[tuple]]:
    """读取页面背景图和 PDF 页码，坐标是原始页面坐标。"""
    r = Reader(page['header'])
    r.u32()
    flex = r.u32()
    r.bitfield()
    fields = r.bitfield()
    r.pos = flex
    background = None
    pdfs = []
    for bit in range(9):
        if not fields & (1 << bit):
            continue
        if bit == 0:
            r.read(32)
        elif bit == 1:
            for _ in range(r.u16()):
                r.short_u16_string()
        elif bit == 2:
            r.short_u16_string()
        elif bit == 3:
            background = struct.unpack('<i', r.read(4))[0]
        elif bit in (4, 5, 6, 7):
            r.read(4)
        elif bit == 8:
            for _ in range(r.u16()):
                bind_id, index = r.u32(), r.u32()
                fmt = '<4i' if page['format_version'] >= 2034 else '<4d'
                rect = struct.unpack(fmt, r.read(struct.calcsize(fmt)))
                pdfs.append((bind_id, index, rect))
    return background, pdfs


def object_media(kind: int, blob: bytes) -> tuple[tuple, float, tuple | None, int | None, str, list[str]]:
    r = Reader(blob)
    _, _, rect, angle, pivot = parse_object_base(r)
    notices = []
    if kind == 14:
        start, _, flex, _, fields = open_object_frame(r, 14)
        r.pos = start + flex
        if fields & 1:
            r.u32()  # 可编辑绘画源文件；显示版本由 thumbnail ID 指定。
        image_id = r.u32() if fields & 2 else None
        if fields & ~3:
            notices.append('绘画的裁剪或比例参数未完全还原；使用内嵌显示图像。')
        return rect, angle, pivot, image_id, '', notices
    if kind == 13:
        start, _, flex, _, fields = open_object_frame(r, 13)
        r.pos = start + flex
        if fields & 1:
            r.u32()
        image_id = r.u32() if fields & 2 else None
        body = r.short_u16_string() if fields & 4 else ''
        title = r.short_u16_string() if fields & 8 else ''
        uri = r.short_u16_string() if fields & 16 else ''
        label = escape(title or uri)
        if uri.startswith(('https://', 'http://', 'mailto:')):
            label = f'<a href="{escape(uri, quote=True)}">{label}</a>'
        elif uri:
            label += ' ' + escape(uri)
        return rect, angle, pivot, image_id, f'<div>{label}</div><div>{escape(body)}</div>', notices
    if kind != 3:
        raise ValueError(f'不支持图像对象类型 {kind}')
    skip_frame(r)  # ShapeBase
    start, end, flex, _, fields = open_object_frame(r, 7)
    r.pos = start + flex
    if fields & 1:
        parse_common(r)
    for bit, size in ((1, 1), (2, 4), (3, 4), (4, 4)):
        if fields & (1 << bit):
            r.read(size)
    image_id = None
    if fields & 32:
        r.u32()  # effect length
        effect = r.u8()
        if effect == 2:
            r.u8()  # image type
            image_id = struct.unpack('<i', r.read(4))[0]
        else:
            notices.append(f'图片填充类型 {effect} 无法显示。')
    r.pos = end
    _, _, _, _, image_fields = open_object_frame(r, 3)
    if image_fields:
        notices.append('图片裁剪、边框等效果未完全还原；页面视觉效果可能与原图有差异。')
    return rect, angle, pivot, image_id, '', notices


def load_image(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError('图片超过 4000 万像素限制')
        return image.convert('RGBA')


def render_pages(note, warnings: list[str]) -> tuple[list[Resource], str]:
    """保留正文；将页面对象合成为 PNG，并返回文档链接 HTML。"""
    refs = bindings(note.media_info)
    pages, links = [], []

    for number, page in enumerate(note.pages, 1):
        background, pdfs = page_backgrounds(page)
        elements = []
        if background is not None and background >= 0:
            elements.append(('image', background, (0, 0, page['width'], page['height']), 0.0, None))
        for bind_id, index, rect in pdfs:
            elements.append(('pdf', (bind_id, index), rect, 0.0, None))
        for kind, blob in page['objects']:
            if kind == 1:
                stroke = parse_stroke(blob)
                warnings.extend(stroke['warnings'])
                elements.append(('ink', stroke, stroke['rect'], stroke['angle'], stroke['pivot']))
            elif kind in (3, 13, 14):
                rect, angle, pivot, bind_id, link, notices = object_media(kind, blob)
                warnings.extend(notices)
                if link:
                    links.append(link)
                if bind_id is not None:
                    elements.append(('image', bind_id, rect, angle, pivot))
            elif kind != 2:
                warnings.append(f'第 {number} 页的对象类型 {kind} 尚不能渲染；数据仅保留在输入 SDOCX 中，未写入 ENEX。')
        if not elements:
            if pdfs == [] and any(data.startswith(b'%PDF-') for data in note.media.values()):
                warnings.append(f'第 {number} 页没有 PDF 引用或可视对象；保留正文，但不生成空白页面图像。')
            continue
        # 非 PDF 页面仅裁掉视觉对象之外的空白，不缩小手写字符。
        bounds = [e[2] for e in elements]
        x0 = min(0, min(r[0] for r in bounds))
        y0 = 0 if pdfs else max(0, min(r[1] for r in bounds) - 12)
        x1 = max(page['width'], max(r[2] for r in bounds))
        y1 = max(r[3] for r in bounds) + (0 if pdfs else 12)
        if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x1 <= x0 or y1 <= y0:
            raise ValueError(f'第 {number} 页坐标非法')
        scale = min(1.5, 1800 / (x1 - x0))
        width, height = math.ceil((x1-x0)*scale), math.ceil((y1-y0)*scale)
        # 先创建不超过 3200 像素高的画布，再绘制页面内容，避免整页 RGBA 占用过大。
        warned_rotations = set()
        warned_images = set()
        for chunk, tile_top in enumerate(range(0, height, 3200), 1):
            tile_height = min(3200, height - tile_top)
            if width * tile_height > 40_000_000:
                raise ValueError(f'第 {number} 页超过 4000 万像素限制')
            canvas = Image.new('RGBA', (width, tile_height), page['background'] or '#fcfcfc')
            ink_group = []

            def flush_ink():
                if ink_group:
                    # 笔迹先按整页原点映射并取整，再减去块偏移，保持与整页绘制一致。
                    canvas.alpha_composite(draw_strokes(
                        ink_group, canvas.size, scale, (x0, y0), y_offset=tile_top
                    ))
                    ink_group.clear()

            for element_index, (kind, value, rect, angle, pivot) in enumerate(elements):
                if kind == 'ink':
                    ink_group.append(value)
                    continue
                flush_ink()
                bind_id = value[0] if kind == 'pdf' else value
                name = refs.get(bind_id)
                if name not in note.media:
                    raise ValueError(f'第 {number} 页引用不存在的 media ID {bind_id}')
                data = note.media[name]
                target = (max(1, round((rect[2]-rect[0])*scale)), max(1, round((rect[3]-rect[1])*scale)))
                if target[0]*target[1] > 40_000_000:
                    raise ValueError('页面图像目标尺寸过大')
                left, top = round((rect[0]-x0)*scale), round((rect[1]-y0)*scale)
                if angle == 0 and (top + target[1] <= tile_top or top >= tile_top + tile_height):
                    continue
                if kind == 'pdf':
                    with pymupdf.open(stream=data, filetype='pdf') as pdf:
                        pdf_page = pdf[value[1]]
                        pix = pdf_page.get_pixmap(matrix=pymupdf.Matrix(target[0]/pdf_page.rect.width, target[1]/pdf_page.rect.height), alpha=True)
                        picture = Image.frombytes('RGBA', (pix.width, pix.height), pix.samples)
                else:
                    try:
                        picture = load_image(data)
                    except (OSError, ValueError) as error:
                        if element_index not in warned_images:
                            warnings.append(f'第 {number} 页图像 {name} 无法解码（{error}）；原始数据仅在输入 SDOCX 中，未写入 ENEX。')
                            warned_images.add(element_index)
                        continue
                picture = picture.resize(target, Image.Resampling.LANCZOS)
                if angle:
                    if element_index not in warned_rotations:
                        warnings.append('旋转图片按中心旋转；自定义旋转中心或边界可能有差异。')
                        warned_rotations.add(element_index)
                    rotated = picture.rotate(-angle, Image.Resampling.BICUBIC, expand=True)
                    left -= (rotated.width-picture.width)//2
                    top -= (rotated.height-picture.height)//2
                    picture = rotated
                canvas.alpha_composite(picture, (left, top - tile_top))
            flush_ink()
            tile = canvas.convert('RGB')
            out = io.BytesIO()
            tile.save(out, 'PNG')
            pages.append(Resource(f'page-{number:03d}-{chunk:02d}.png', 'image/png', out.getvalue(), *tile.size))
    if pages and (note.body.text or any(p['texts'] for p in note.pages)):
        warnings.append('可编辑文本与页面图像分区保存；文本、手写和图片之间的原始叠放位置未完全还原。')
    return pages, ''.join(links)
