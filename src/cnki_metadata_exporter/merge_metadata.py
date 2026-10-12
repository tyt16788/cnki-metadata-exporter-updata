from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .queries import load_queries

FIELD_NAMES = {
    "SrcDatabase-来源库": "来源库",
    "Title-题名": "题名",
    "Title-书名": "题名",
    "Author-作者": "作者",
    "Organ-单位": "单位",
    "Source-文献来源": "文献来源",
    "Keyword-关键词": "关键词",
    "Summary-摘要": "摘要",
    "PubTime-发表时间": "发表时间",
    "PubTime-出版日期": "发表时间",
    "FirstDuty-第一责任人": "第一责任人",
    "Fund-基金": "基金",
    "Year-年": "年",
    "Volume-卷": "卷",
    "Period-期": "期",
    "PageCount-页码": "页码",
    "CLC-中图分类号": "中图分类号",
    "ISSN-国际标准刊号": "ISSN",
    "URL-网址": "URL",
    "DOI-DOI": "DOI",
    "标准号": "标准号",
}
METADATA_FIELDS = list(dict.fromkeys(FIELD_NAMES.values()))
DEFAULT_PAPER_DATABASES = {"期刊", "博士", "硕士", "中国会议", "国际会议"}
RAW_FIELDS = METADATA_FIELDS + ["检索集合", "集合名称", "数据层级", "集合内序号", "批次文件"]
UNIQUE_FIELDS = METADATA_FIELDS + ["检索集合", "集合名称", "数据层级", "命中集合数", "原始记录数"]


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self.row = []
        elif tag.lower() in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag.lower() == "br" and self.cell is not None:
            self.cell.append("；")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(clean_text("".join(self.cell)))
            self.cell = None
        elif tag.lower() == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None


def clean_text(value: Any) -> str:
    text = re.sub(r"</?(?:sup|sub|i|b)>", "", str(value or ""), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def parse_native_xls(path: Path) -> list[dict[str, str]]:
    parser = TableParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    header: list[str] | None = None
    records: list[dict[str, str]] = []
    for row in parser.rows:
        if row and row[0].startswith("SrcDatabase-") and any(cell.startswith("Title-") for cell in row):
            header = [FIELD_NAMES.get(cell, cell) for cell in row]
            continue
        if header is None:
            continue
        values = row[: len(header)] + [""] * max(0, len(header) - len(row))
        record = {name: clean_text(value) for name, value in zip(header, values)}
        if record.get("题名"):
            records.append(record)
    return records


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", clean_text(value)).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def normalize_doi(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi\s*[:：]?\s*", "", value)
    return value.strip().rstrip(".,;；")


def normalize_url(value: str) -> str:
    return clean_text(value).lower().strip().rstrip("/")


def first_author(record: dict[str, Any]) -> str:
    author = clean_text(record.get("第一责任人") or record.get("作者") or "")
    return normalize(re.split(r"[;；,，]", author)[0])


def candidate_keys(record: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    doi = normalize_doi(record.get("DOI", ""))
    url = normalize_url(record.get("URL", ""))
    title = normalize(record.get("题名", ""))
    author = first_author(record)
    year = normalize(record.get("年") or record.get("发表时间", ""))[:4]
    source = normalize(record.get("文献来源", ""))
    if doi:
        keys.append("doi:" + doi)
    if url:
        keys.append("url:" + url)
    if title and author:
        keys.append("ta:" + title + "|" + author)
    if len(title) >= 12 and year and source:
        keys.append("tys:" + title + "|" + year + "|" + source)
    if not keys and title:
        keys.append("t:" + title)
    return keys


def merge_value(old: str, new: str) -> str:
    old, new = clean_text(old), clean_text(new)
    if not old:
        return new
    if not new or old == new:
        return old
    return new if len(new) > len(old) else old


def deduplicate(raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    lookup: dict[str, int] = {}
    memberships: list[list[str]] = []
    names: list[list[str]] = []
    tiers: list[list[str]] = []
    source_counts: list[int] = []
    for record in raw_records:
        keys = candidate_keys(record)
        index = next((lookup[key] for key in keys if key in lookup), None)
        if index is None:
            unique.append({field: clean_text(record.get(field, "")) for field in METADATA_FIELDS})
            memberships.append([])
            names.append([])
            tiers.append([])
            source_counts.append(0)
            index = len(unique) - 1
        else:
            for field in METADATA_FIELDS:
                unique[index][field] = merge_value(unique[index].get(field, ""), record.get(field, ""))
        for target, value in (
            (memberships[index], clean_text(record.get("检索集合", ""))),
            (names[index], clean_text(record.get("集合名称", ""))),
            (tiers[index], clean_text(record.get("数据层级", ""))),
        ):
            if value and value not in target:
                target.append(value)
        source_counts[index] += 1
        for key in set(keys + candidate_keys(unique[index])):
            lookup[key] = index
    for index, item in enumerate(unique):
        item["检索集合"] = "；".join(memberships[index])
        item["集合名称"] = "；".join(names[index])
        item["数据层级"] = "；".join(tiers[index])
        item["命中集合数"] = len(memberships[index])
        item["原始记录数"] = source_counts[index]
    return unique


def resolve_batch_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def collect(
    root: Path,
    query_path: Path,
    included_databases: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    queries = load_queries(query_path)
    native_root = root / "output" / "native_exports"
    raw_records: list[dict[str, Any]] = []
    stats: list[dict[str, Any]] = []
    missing: list[str] = []
    for query in queries:
        cid = str(query["id"])
        manifest_path = native_root / cid / "manifest.json"
        if not manifest_path.exists():
            missing.append(cid)
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        collection_records: list[dict[str, Any]] = []
        for batch in manifest.get("exported_batches", []):
            path = resolve_batch_path(root, str(batch["file"]))
            records = parse_native_xls(path)
            expected = int(batch["expected_records"])
            if len(records) != expected:
                raise RuntimeError(f"{path.name} 解析 {len(records)} 条，应为 {expected} 条")
            start = int(batch["first_record"])
            for offset, record in enumerate(records):
                normalized = {field: clean_text(record.get(field, "")) for field in METADATA_FIELDS}
                normalized.update(
                    {
                        "检索集合": cid,
                        "集合名称": query["name"],
                        "数据层级": clean_text(query.get("tier", "")),
                        "集合内序号": start + offset,
                        "批次文件": path.name,
                    }
                )
                collection_records.append(normalized)
        expected_total = int(manifest.get("result_count", 0))
        if len(collection_records) != expected_total:
            raise RuntimeError(f"{cid} 合计 {len(collection_records)} 条，应为 {expected_total} 条")
        accepted = [row for row in collection_records if row.get("来源库") in included_databases]
        raw_records.extend(accepted)
        stats.append(
            {
                "检索集合": cid,
                "集合名称": query["name"],
                "命中量": expected_total,
                "导出批次数": len(manifest.get("exported_batches", [])),
                "导出记录数": len(collection_records),
                "保留记录数": len(accepted),
                "排除记录数": len(collection_records) - len(accepted),
                "检索字段": query.get("field", ""),
            }
        )
    if missing:
        raise RuntimeError("缺少以下集合的完整原生导出：" + "、".join(missing))
    unique = deduplicate(raw_records)
    linked_counts: defaultdict[str, int] = defaultdict(int)
    for record in unique:
        for cid in clean_text(record.get("检索集合", "")).split("；"):
            if cid:
                linked_counts[cid] += 1
    for item in stats:
        item["去重后关联记录数"] = linked_counts[item["检索集合"]]
    return raw_records, unique, stats


def main() -> None:
    parser = argparse.ArgumentParser(description="合并知网原生 XLS 批次并去重")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="运行数据目录")
    parser.add_argument("--queries", type=Path, required=True, help="JSON 检索集合配置")
    parser.add_argument("--out", type=Path, required=True, help="合并结果目录")
    parser.add_argument(
        "--include-database",
        action="append",
        dest="included_databases",
        help="保留的来源库，可重复；默认保留期刊、博士、硕士及会议论文",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    included = set(args.included_databases or DEFAULT_PAPER_DATABASES)
    raw, unique, stats = collect(root, args.queries.resolve(), included)
    queries = load_queries(args.queries.resolve())
    exported_records = sum(int(item["导出记录数"]) for item in stats)
    excluded_records = sum(int(item["排除记录数"]) for item in stats)
    payloads = {
        "raw.json": {"columns": RAW_FIELDS, "rows": raw},
        "unique.json": {"columns": UNIQUE_FIELDS, "rows": unique},
        "stats.json": {"columns": list(stats[0].keys()) if stats else [], "rows": stats},
        "queries.json": {"columns": ["id", "name", "field", "query", "purpose", "tier"], "rows": queries},
        "summary.json": {
            "exported_records": exported_records,
            "retained_records": len(raw),
            "unique_records": len(unique),
            "excluded_records": excluded_records,
            "collections": len(stats),
            "included_databases": sorted(included),
        },
    }
    for name, payload in payloads.items():
        (out / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payloads["summary.json"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
