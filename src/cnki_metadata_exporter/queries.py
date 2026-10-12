from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COLLECTION_RE = re.compile(r"^([A-Za-z][A-Za-z0-9._-]*)\s+(.+)$")
QUERY_RE = re.compile(r"^检索式：\s*([A-Za-z]+)=(.+)$", re.S)


def document_paragraphs(docx_path: Path) -> list[str]:
    with ZipFile(docx_path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:body/w:p", W_NS):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", W_NS)).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def validate_queries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("queries")
    if not isinstance(payload, list) or not payload:
        raise ValueError("检索配置必须是非空 JSON 数组，或包含 queries 数组的对象")

    queries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, source in enumerate(payload, start=1):
        if not isinstance(source, dict):
            raise ValueError(f"第 {index} 个检索集合不是对象")
        item = dict(source)
        cid = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        query = str(item.get("query", "")).strip()
        if not ID_RE.fullmatch(cid):
            raise ValueError(f"第 {index} 个集合 id 无效：{cid!r}")
        if cid in seen:
            raise ValueError(f"检索集合 id 重复：{cid}")
        if not name or not query:
            raise ValueError(f"检索集合 {cid} 缺少 name 或 query")
        publication_time = item.get("publication_time")
        if publication_time is not None:
            if not isinstance(publication_time, dict):
                raise ValueError(f"检索集合 {cid} 的 publication_time 必须是包含 start/end 的对象")
            start_text = str(publication_time.get("start", "")).strip()
            end_text = str(publication_time.get("end", "")).strip()
            try:
                start_date = date.fromisoformat(start_text)
                end_date = date.fromisoformat(end_text)
            except ValueError as exc:
                raise ValueError(
                    f"检索集合 {cid} 的发表时间必须使用 YYYY-MM-DD 格式，并同时提供 start 和 end"
                ) from exc
            if start_date.isoformat() != start_text or end_date.isoformat() != end_text:
                raise ValueError(f"检索集合 {cid} 的发表时间必须使用 YYYY-MM-DD 格式")
            if start_date > end_date:
                raise ValueError(f"检索集合 {cid} 的发表时间 start 不能晚于 end")
            item["publication_time"] = {"start": start_text, "end": end_text}
        seen.add(cid)
        field_match = re.match(r"^([A-Za-z]+)=(.+)$", query, flags=re.S)
        if field_match:
            item.setdefault("field", field_match.group(1))
            item.setdefault("expression", field_match.group(2).strip())
        item["id"] = cid
        item["name"] = name
        item["query"] = query
        item["length"] = len(query)
        queries.append(item)
    return queries


def last_complete_month_range(today: date | None = None) -> dict[str, str]:
    """Return the previous full calendar month as an inclusive date range."""
    current = today or date.today()
    first_of_current_month = current.replace(day=1)
    last_of_previous_month = first_of_current_month - timedelta(days=1)
    first_of_previous_month = last_of_previous_month.replace(day=1)
    return {
        "start": first_of_previous_month.isoformat(),
        "end": last_of_previous_month.isoformat(),
    }


def split_publication_time_range(
    publication_time: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]] | None:
    """Split an inclusive publication-date range into two contiguous ranges."""
    start = date.fromisoformat(publication_time["start"])
    end = date.fromisoformat(publication_time["end"])
    days = (end - start).days
    if days < 1:
        return None
    midpoint = start + timedelta(days=days // 2)
    next_day = midpoint + timedelta(days=1)
    return (
        {"start": start.isoformat(), "end": midpoint.isoformat()},
        {"start": next_day.isoformat(), "end": end.isoformat()},
    )


def load_queries(query_path: Path) -> list[dict[str, Any]]:
    return validate_queries(json.loads(query_path.read_text(encoding="utf-8")))


def extract_docx_queries(
    docx_path: Path,
    *,
    start_heading: str = "四 CNKI",
    end_heading: str = "五 Web of Science",
) -> list[dict[str, Any]]:
    paragraphs = document_paragraphs(docx_path)
    start = next((i for i, text in enumerate(paragraphs) if text.startswith(start_heading)), None)
    end = next((i for i, text in enumerate(paragraphs) if text.startswith(end_heading)), None)
    if start is None or end is None or end <= start:
        raise ValueError("DOCX 中未找到指定的 CNKI 检索策略章节")

    results: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for text in paragraphs[start + 1 : end]:
        heading = COLLECTION_RE.match(text)
        if heading:
            current = {"id": heading.group(1), "name": heading.group(2)}
            results.append(current)
            continue
        query_match = QUERY_RE.match(text)
        if query_match and current is not None:
            field = query_match.group(1)
            expression = query_match.group(2).strip()
            current.update({"field": field, "query": f"{field}={expression}", "expression": expression})
            continue
        if text.startswith("用途：") and current is not None:
            current["purpose"] = text.removeprefix("用途：").strip()
        elif text.startswith("层级：") and current is not None:
            current["tier"] = text.removeprefix("层级：").strip()
    return validate_queries(results)


def resolve_queries(
    *,
    query_path: Path | None = None,
    docx_path: Path | None = None,
    start_heading: str = "四 CNKI",
    end_heading: str = "五 Web of Science",
    snapshot_path: Path | None = None,
) -> list[dict[str, Any]]:
    if (query_path is None) == (docx_path is None):
        raise ValueError("必须且只能指定 --queries 或 --docx")
    queries = (
        load_queries(query_path.resolve())
        if query_path is not None
        else extract_docx_queries(
            docx_path.resolve(),
            start_heading=start_heading,
            end_heading=end_heading,  # type: ignore[union-attr]
        )
    )
    if snapshot_path is not None:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(queries, ensure_ascii=False, indent=2), encoding="utf-8")
    return queries
