# sdocx2enex

将三星笔记导出的 `.sdocx` 转成 Mac 备忘录可导入的 ENEX。

## 使用

Python 3.10 或更新版本：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m sdocx2enex 'samsung note' -o output/samsung-notes.enex
```

也可以指定单个文件：

```sh
.venv/bin/sdocx2enex 'samsung note/梦#1_190621_103303.sdocx' -o output/dream.enex
```

修改时间有两种模式，默认保留原始修改时间：

```sh
# 保留原始创建时间和原始修改时间（默认）
.venv/bin/python -m sdocx2enex 'samsung note' -o output/samsung-notes.enex --modified-time original
# 创建时间仍取原始值，修改时间设为与创建时间相同
.venv/bin/python -m sdocx2enex 'samsung note' -o output/samsung-notes-created.enex --modified-time created
```

目录递归扫描，按路径排序；`--report PATH` 可指定报告路径，默认输出同名 `.report.json`。程序拒绝用源文件作为输出，输出路径也不能以 `.sdocx` 结尾。重新运行会替换指定的 ENEX 和报告，不修改输入。

退出码：`0` 表示全部转换完成（仍须阅读警告），`1` 表示转换或写入失败，`2` 表示输入参数不正确。部分文件失败时仍输出成功的笔记，并逐项报告失败原因；全部失败时不会写出空 ENEX，已有 ENEX 不会被替换，以报告的 `output_written` 为准。ENEX 和报告分别暂存后原子替换，并非跨两个文件的事务。

## 导入 Mac 备忘录

在备忘录中选择“我的 Mac”或 iCloud 下的目标文件夹，使用“文件 → 导入到备忘录”，选择生成的 ENEX。iCloud 下导入的笔记会同步到该账户。

[苹果说明](https://support.apple.com/en-ca/102223)确认 ENEX 可以包含多条笔记，也明确指出不支持的附件或功能可能不会导入。**通过 ENEX 格式验证不等于通过 Apple Notes 的实际导入验收。** 当前自动化测试验证生成文件；尚未确认本机备忘录导入后的创建/修改时间、附件类型和所有样式。未直接读写 Notes 数据库。

## 保存内容与已知差异

| 内容 | 处理方式与限制 |
| --- | --- |
| 标题 | 使用内部标题，保留中文和 emoji。内部标题为空时保持空标题，不用文件名或“未命名笔记”补齐。ENEX 不允许的控制字符、首尾空白和超长标题会规范化并报告。 |
| 创建、修改时间 | 读取 ZIP 结束记录之后的 S-Pen 尾部中原始创建、修改时间（毫秒）。`original` 保留两者，`created` 将修改时间设为创建时间。写入 ENEX 时转为 UTC 整秒，不使用文件系统时间。 |
| 文本 | 保留可编辑文本、空行、粗体、斜体、下划线、删除线、颜色、字号、对齐和缩进。格式区间按 UTF-16 计算，避免 emoji 导致偏移。 |
| 列表 | 复选框使用 `en-todo`；编号和圆点显示为文本标记，不是原生可编辑列表。 |
| 手写 | 解码压缩/未压缩笔迹并用 Pillow 栅格化为 PNG，保留采样点、颜色、相对位置和近似笔压宽度。笔刷纹理、倾斜、复杂效果不保证一致，也不能再作为笔迹编辑。 |
| 图片、绘画 | 按页面对象的媒体 ID 找到嵌入图像。绘画使用文件自带的显示图，统一合成页面 PNG，不重复附加原图。部分裁剪、边框、旋转中心和网页卡片样式未完全还原。 |
| PDF | 按页面中的 PDF 引用和页码用 MuPDF 渲染 PNG，不另附原始 PDF。只生成有内容的视觉页面；空白页差异记入报告。 |
| 混合排版 | 正文、页面文本框、网页链接、页面图像分区保存。不能完整还原文本与手写、图片的原始叠放关系。页面图像宽度最多 1800 像素；长页先按每块不超过 3200 像素高分块再绘制，因此不会仅因整页面积超过 4000 万像素而失败。每块画布、单个页面图片目标和源图片仍分别受 4000 万像素限制。 |
| 超链接与其他格式 | 网页卡片保留 URL；文本中的超链接标注、字体名称、公式等尚未完整解码，会报告。原文仍保留。 |
| 私有格式、未知对象 | `.spi` 私有缓存、`.spd/.spp` 编辑数据和未知类型未写入 ENEX；请自行保留输入 SDOCX。当前样例的手写通过实际笔迹生成，绘画有 JPEG 显示版本，因此不依赖 SPI 缓存。嵌入文本内部的对象布局目前未还原。 |

ENEX 只包含原有文本、网页链接和转换后的页面 PNG。不附加原 PDF/JPEG、源 SDOCX、`metadata.json`、“原始附件”或“源文件与精确时间元数据”区块，也不插入解释性占位文字。页面 PNG 都有正文 `en-media` 引用和正确的二进制 MD5。转换说明和警告只写在独立报告中，不嵌入笔记。

### 时间字段的核验

本批 15 个样例中，文件名日期全部与外层尾部的 `display_modified_time` 一致到秒，**文件名记录的是修改时间，不能当成原始创建时间**。`display_created_time` 单独保存创建时间：例如“梦#1”创建于北京时间 2019-06-02 16:17:06，修改于文件名中的 2019-06-21 10:33:03。

这两项时间位于 ZIP 的 EOCD 记录及其注释之后，不在解压目录中。ZIP 成员 `end_tag.bin` 和 `note.note` 中的时间可能是格式转换或保存时产生的值；只读这些成员会漏掉原始日期。程序按字段结构解析外层尾部，显示时间使用毫秒，`note.note` 的固定头时间使用微秒。样例的外层尾部显示时间均有效，且创建不晚于修改；损坏、非法、加密尾部明确报错。

若文件没有外层显示时间，创建时间依次使用有效文件名后缀 `_YYMMDD_HHMMSS`（Asia/Shanghai，年份为 2000 + YY）和 `note.note` 创建时间；`original` 模式的修改时间使用 `note.note` 修改时间，并在报告中警告。原始尾部存在时，改文件名不会改变导出日期。

JSON 报告包含源文件 SHA-256、固定头的 `source_created_us` / `source_modified_us`、尾部的 `display_created_ms` / `display_modified_ms`、实际导出时间 `created_utc` / `modified_utc`、所选模式 `modified_time_mode` 及两项时间各自的来源 `created_time_source` / `modified_time_source`。ENEX 仅能保存整秒，精确的原始毫秒保留在报告中。

本批合并文件的体积主要来自 97 页 PDF 图像及 base64 编码。需要分批导入时，分别对单个文件运行即可。

对于加密文件、损坏帧、缺失媒体、含子对象的容器、超出限制的内容，程序明确报告失败。默认单 ZIP 成员解压上限 128 MiB、总解压上限 512 MiB、单块画布 4000 万像素；长页先分块后绘制，每块高度最多 3200 像素。单个页面图片目标和源图片也各受 4000 万像素限制；不支持的对象明确记入警告，未转换的原始数据仅在输入 SDOCX 中保留。

## 验证与样例

```sh
.venv/bin/python -m pytest -q
```

测试包括全部 15 个本地样例、离线官方 ENEX/ENML DTD 验证、UTF-16 格式范围、外层原始时间解析、两种修改时间模式、缺失尾部时的日期规则、空标题、无多余附件和说明文字、PNG 解码及双向媒体引用、笔迹解码、畸形输入和 CLI 错误处理。样例是私人文件，已在 `.gitignore` 中；未带样例的环境会跳过样例集成测试，单元测试仍可运行。

本批样例差异见 [SAMPLE_REPORT.md](SAMPLE_REPORT.md)，机器可读的完整报告与 ENEX 一同生成。没有三星原生 PDF/截图参照，自动化验证不能证明视觉像素级一致；已检查生成的手写、绘画、PDF 和网页卡片预览。

## 代码结构

- `parser.py`：ZIP、元数据、页面和文本二进制读取。
- `text.py`：文本和段落样式到 ENML。
- `ink.py`：笔迹解码和透明图层绘制。
- `media.py`：媒体 ID 绑定、PDF/图片页面合成。
- `enex.py`：ENEX、附件和差异报告。
- `__main__.py`：命令行与批处理入口。

图像、PDF、XML 分别复用 Pillow、PyMuPDF、lxml。格式研究基于用户提供的 [sdocx_parser](https://github.com/0xA01F3/sdocx_parser) 及 [sdocx2pdf](https://github.com/squ1dd13/sdocx2pdf)。
