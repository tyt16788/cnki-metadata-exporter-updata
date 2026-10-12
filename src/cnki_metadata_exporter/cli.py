from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .exporter import CaptchaPending, CNKIMetadataExporter
from .queries import resolve_queries


def add_query_source(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--queries", type=Path, help="JSON 检索集合配置")
    source.add_argument("--docx", type=Path, help="包含正式检索策略的 DOCX")
    parser.add_argument("--docx-start", default="四 CNKI", help="DOCX 中 CNKI 章节起始标题")
    parser.add_argument("--docx-end", default="五 Web of Science", help="DOCX 中下一章节标题")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CNKI 检索结果列表与详情元数据导出器")
    add_query_source(p)
    p.add_argument("--root", type=Path, default=Path.cwd(), help="运行数据目录")
    p.add_argument("--collection", action="append", help="集合编号，可重复；默认全部")
    p.add_argument("--list-only", action="store_true", help="仅采集结果列表，不进入详情页")
    p.add_argument("--headless", action="store_true", help="无头运行；首次使用不建议")
    p.add_argument("--print-queries", action="store_true", help="只解析并打印检索式")
    return p


async def run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    queries = resolve_queries(
        query_path=args.queries,
        docx_path=args.docx,
        start_heading=args.docx_start,
        end_heading=args.docx_end,
        snapshot_path=root / "data" / "queries.resolved.json",
    )
    if args.print_queries:
        print(json.dumps(queries, ensure_ascii=False, indent=2))
        return 0

    selected = set(args.collection or [str(item["id"]) for item in queries])
    collections = [item for item in queries if str(item["id"]) in selected]
    missing = selected - {str(item["id"]) for item in collections}
    if missing:
        raise ValueError("未知集合：" + "、".join(sorted(missing)))

    exporter = CNKIMetadataExporter(root, root / ".cnki-profile", headless=args.headless)
    await exporter.launch()
    try:
        stats = []
        for collection in collections:
            stats.append(await exporter.export_collection(collection, list_only=args.list_only))
        (root / "output" / "run-summary.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0
    except CaptchaPending as exc:
        exporter.log(str(exc))
        return 2
    finally:
        await exporter.close()


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
