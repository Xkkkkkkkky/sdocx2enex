# sdocx2enex

将三星笔记导出的 `.sdocx` 文件转换为 ENEX，方便导入 Mac 备忘录。

Convert Samsung Notes `.sdocx` files to ENEX for import into Apple Notes on Mac.

## 安装 / Install

需要 Python 3.10 或更新版本。 / Requires Python 3.10 or later.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## 转换 / Convert

转换文件夹中的所有笔记，包括子文件夹：

Convert all notes in a folder, including subfolders:

```sh
.venv/bin/python -m sdocx2enex 'samsung note' -o output/notes.enex
```

也可以转换单个文件：

You can also convert a single file:

```sh
.venv/bin/python -m sdocx2enex 'samsung note/example.sdocx' -o output/note.enex
```

默认保留笔记的创建和修改时间。加上 `--modified-time created`，可将修改时间设为创建时间：

By default, note creation and modification times are kept. Add `--modified-time created` to set the modification time to the creation time:

```sh
.venv/bin/python -m sdocx2enex 'samsung note' -o output/notes.enex --modified-time created
```

源文件不会被修改。再次运行会覆盖指定的输出文件。

Source files are left unchanged. Running again overwrites the output file.

## 导入 Mac 备忘录 / Import into Apple Notes

打开备忘录，选择目标文件夹，再选择“文件 → 导入到备忘录”，打开生成的 `.enex` 文件。

Open Apple Notes, select a destination folder, then choose **File → Import to Notes** and open the generated `.enex` file.

## 保留内容与限制 / What is kept and what may change

- 标题、创建和修改时间。 / Titles, creation times and modification times.
- 可编辑文本及常见格式，如粗体、颜色和复选框。 / Editable text and common formatting, such as bold, colors and checkboxes.
- 手写、绘画、图片和 PDF 页面以图片保存。 / Handwriting, drawings, images and PDF pages are saved as images.

复杂排版和部分样式可能与原笔记不同，手写内容不能再作为笔迹编辑。请保留原始 `.sdocx` 文件。

Complex layouts and some styles may look different from the original notes. Handwriting can no longer be edited as pen strokes. Keep your original `.sdocx` files.

## 致谢 / Credits

格式解析参考以下项目： / Format parsing is based on:

- [sdocx_parser](https://github.com/0xA01F3/sdocx_parser)
- [sdocx2pdf](https://github.com/squ1dd13/sdocx2pdf)
