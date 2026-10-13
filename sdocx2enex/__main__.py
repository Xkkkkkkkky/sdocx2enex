"""命令行入口：python -m sdocx2enex INPUT -o OUTPUT。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .enex import build_enex


def _collect_inputs(input_path: Path) -> list[Path]:
    if not input_path.exists():
        raise ValueError(f"输入路径不存在：{input_path}")
    if input_path.is_file():
        if input_path.suffix.lower() != ".sdocx":
            raise ValueError(f"输入文件不是 .sdocx：{input_path}")
        return [input_path]
    if not input_path.is_dir():
        raise ValueError(f"输入路径既不是文件也不是目录：{input_path}")

    files = [
        path
        for path in input_path.rglob("*")
        if path.is_file() and path.suffix.lower() == ".sdocx"
    ]
    files.sort(key=lambda path: (str(path).casefold(), str(path)))
    if not files:
        raise ValueError(f"目录中没有 .sdocx 文件：{input_path}")
    return files


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sdocx2enex",
        description="将 Samsung Notes .sdocx 文件转换为 ENEX。",
    )
    parser.add_argument("input", type=Path, help="一个 .sdocx 文件或递归扫描的目录")
    parser.add_argument("-o", "--output", required=True, type=Path, help="ENEX 输出路径")
    parser.add_argument("--report", type=Path, help="JSON 报告路径，默认与输出同名 .report.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        inputs = _collect_inputs(args.input)
    except ValueError as error:
        parser.error(str(error))

    report_path = args.report or args.output.with_suffix(".report.json")
    try:
        report = build_enex(inputs, args.output, report_path)
    except Exception as error:
        print(f"转换失败：{error}", file=sys.stderr)
        return 1

    warning_count = sum(
        1
        for item in report["files"]
        if item["status"] == "success" and item["warnings"]
    )
    print(
        f"成功笔记：{report['success_count']}，失败笔记：{report['failure_count']}，"
        f"含警告笔记：{warning_count}"
    )
    print(f"ENEX 输出：{args.output}")
    print(f"JSON 报告：{report_path}")

    if report["failure_count"]:
        for item in report["files"]:
            if item["status"] == "failed":
                print(f"转换失败：{item['source']}：{item['error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
