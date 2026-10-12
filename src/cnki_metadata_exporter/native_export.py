from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .exporter import CaptchaPending, CNKIMetadataExporter, locator_text, number_text
from .queries import (
    last_complete_month_range,
    resolve_queries,
    split_publication_time_range,
)


class CNKINativeExporter(CNKIMetadataExporter):
    """Export CNKI's own self-defined XLS records without opening detail pages."""

    async def selected_count(self, page: Page) -> int:
        return int(number_text(await locator_text(page.locator("#selectCount"))) or 0)

    async def clear_selection(self, page: Page) -> None:
        if await self.selected_count(page) == 0:
            return
        clear = page.locator("a[href*='filenameClear']").first
        if not await clear.count():
            clear = page.get_by_text("清除", exact=True).first
        if await clear.count():
            try:
                await clear.click(force=True)
            except Exception:
                await clear.evaluate("e => e.click()")
        for _ in range(30):
            await page.wait_for_timeout(200)
            if await self.selected_count(page) == 0:
                return
        raise RuntimeError("知网已选文献未能清空")

    async def current_row_count(self, page: Page) -> int:
        rows = page.locator("table.result-table-list tbody tr")
        if not await rows.count():
            rows = page.locator(".result-table-list tbody tr")
        return await rows.count()

    async def select_current_page(self, page: Page) -> int:
        rows = await self.current_row_count(page)
        if rows == 0:
            return 0
        before = await self.selected_count(page)
        target = before + rows
        for attempt in range(1, 4):
            checkbox = page.locator("#selectCheckAll1").first
            if not await checkbox.count():
                checkbox = page.locator("input[type='checkbox'][id*='selectCheckAll']").first
            if not await checkbox.count():
                raise RuntimeError("未找到知网当前页全选框")
            try:
                if await checkbox.is_checked():
                    await checkbox.click(force=True)
                    await page.wait_for_timeout(300)
            except Exception:
                await checkbox.evaluate("e => { e.checked = false; }")
            try:
                await checkbox.click(force=True)
            except Exception:
                await checkbox.evaluate("e => e.click()")
            for _ in range(35):
                await page.wait_for_timeout(200)
                await self.wait_for_human_verification(page)
                count = await self.selected_count(page)
                if count >= target:
                    return count - before
            after = await self.selected_count(page)
            self.log(f"当前页第 {attempt} 次全选未生效（应新增 {rows}，实际新增 {after - before}），稍候重试")
            if attempt == 1:
                await page.wait_for_timeout(8000)
                continue
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            ready = False
            for _ in range(120):
                await page.wait_for_timeout(500)
                if "verify/" in page.url:
                    await self.wait_for_human_verification(page)
                if await self.current_row_count(page) > 0:
                    ready = True
                    break
            if not ready:
                raise RuntimeError("刷新后未恢复检索结果页")
        after = await self.selected_count(page)
        raise RuntimeError(f"当前页应新增 {rows} 条，实际新增 {after - before} 条")

    async def open_custom_export(self, page: Page) -> Page:
        link = page.locator("a[exporttype='selfDefine']").first
        if not await link.count():
            menu = page.get_by_text("导出与分析", exact=False).first
            if await menu.count():
                await menu.click(force=True)
            link = page.locator("a[exporttype='selfDefine']").first
        if not await link.count():
            raise RuntimeError("未找到知网自定义导出入口")
        try:
            async with self.context.expect_page(timeout=20000) as popup_info:  # type: ignore[union-attr]
                try:
                    await link.click(force=True)
                except Exception:
                    await link.evaluate("e => e.click()")
            export_page = await popup_info.value
        except PlaywrightTimeoutError:
            pages = [p for p in self.context.pages if "dm8/manage/export.html" in p.url]  # type: ignore[union-attr]
            if not pages:
                raise RuntimeError("自定义导出页面未打开")
            export_page = pages[-1]
        await export_page.wait_for_load_state("domcontentloaded", timeout=60000)
        await self.wait_for_human_verification(export_page)
        await export_page.locator("#litoexcel, .export-sidebar-a").first.wait_for(
            state="visible", timeout=30000
        )
        fields = export_page.locator(
            "input[name='SELFDEFINE_selfFiledList'], input[name='newdefine_selfFiledList']"
        )
        if await fields.count() < 4:
            side_link = export_page.locator("a[displaymode='selfDefine']").first
            if not await side_link.count():
                side_link = export_page.get_by_text("自定义", exact=True).last
            if await side_link.count():
                try:
                    await side_link.click(force=True)
                except Exception:
                    await side_link.evaluate("e => e.click()")
                await fields.first.wait_for(state="visible", timeout=30000)
        return export_page

    async def select_all_export_fields(self, export_page: Page) -> None:
        selector = "input[name='SELFDEFINE_selfFiledList'], input[name='newdefine_selfFiledList']"
        inputs = export_page.locator(selector)
        for attempt in range(1, 5):
            if await inputs.count() >= 4:
                break
            side_link = export_page.locator("a[displaymode='selfDefine']").first
            if not await side_link.count():
                side_link = export_page.get_by_text("自定义", exact=True).last
            if await side_link.count():
                try:
                    await side_link.click(force=True)
                except Exception:
                    await side_link.evaluate("e => e.click()")
            await export_page.wait_for_timeout(attempt * 1000)
        if await inputs.count() < 4:
            raise RuntimeError("自定义导出字段未完整加载")
        all_link = export_page.get_by_text("全选", exact=True).first
        if await all_link.count():
            try:
                await all_link.click(force=True)
            except Exception:
                await all_link.evaluate("e => e.click()")
        for index in range(await inputs.count()):
            item = inputs.nth(index)
            if not await item.is_checked():
                try:
                    await item.check(force=True)
                except Exception:
                    await item.evaluate(
                        "e => {e.checked=true; e.dispatchEvent(new Event('change',{bubbles:true}));}"
                    )
        checked = await inputs.evaluate_all("els => els.filter(e => e.checked).length")
        if checked != await inputs.count():
            raise RuntimeError(f"自定义导出字段仅选中 {checked}/{await inputs.count()}")

    async def download_xls(self, export_page: Page, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        await self.select_all_export_fields(export_page)
        button = export_page.locator("#litoexcel").first
        if not await button.count():
            button = export_page.get_by_text("xls", exact=True).first
        if not await button.count():
            raise RuntimeError("未找到知网 XLS 导出按钮")
        async with export_page.expect_download(timeout=180000) as download_info:
            try:
                await button.click(force=True)
            except Exception:
                await button.evaluate("e => e.click()")
        download = await download_info.value
        await download.save_as(destination)
        failure = await download.failure()
        if failure:
            raise RuntimeError(f"知网 XLS 下载失败：{failure}")
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("知网 XLS 下载文件为空")

    async def goto_page_number(self, page: Page, target: int) -> None:
        current, _ = await self.page_info(page)
        while current < target:
            if not await self.next_page(page):
                raise RuntimeError(f"未能前往第 {target} 页，停在第 {current} 页")
            current, _ = await self.page_info(page)

    async def set_page_size_stable(self, page: Page, total: int, size: int = 50) -> tuple[int, int]:
        expected_rows = min(size, total)
        expected_pages = math.ceil(total / size)
        for trigger in range(2):
            rows = await self.current_row_count(page)
            _, pages = await self.page_info(page)
            if rows == expected_rows and pages == expected_pages:
                return pages, rows
            if trigger == 0:
                dropdown = page.locator("#perPageDiv .sort-default").first
                option = page.locator(f"#perPageDiv ul.sort-list li[data-val='{size}'] a").first
                if await dropdown.count() and await option.count():
                    try:
                        await dropdown.click(force=True)
                        await option.click(force=True)
                    except Exception:
                        await option.evaluate("e => e.click()")
            for _ in range(80):
                await page.wait_for_timeout(250)
                await self.wait_for_human_verification(page)
                rows = await self.current_row_count(page)
                _, pages = await self.page_info(page)
                if rows == expected_rows and pages == expected_pages:
                    return pages, rows
        rows = await self.current_row_count(page)
        _, pages = await self.page_info(page)
        raise RuntimeError(
            f"每页数量切换未稳定：应为 {expected_rows} 条/{expected_pages} 页，实际 {rows} 条/{pages} 页"
        )

    async def search_collection(
        self,
        page: Page,
        collection: dict[str, Any],
        *,
        max_results_per_slice: int = 6000,
    ) -> tuple[int, int, int]:
        last_error: Exception | None = None
        for attempt in range(1, 5):
            try:
                await self.submit_professional_query(
                    page, str(collection["query"]), collection.get("publication_time")
                )
                total = await self.result_count(page)
                if total > 0:
                    if total > max_results_per_slice:
                        _, pages = await self.page_info(page)
                        return total, pages, await self.current_row_count(page)
                    pages, per_page = await self.set_page_size_stable(page, total, 50)
                else:
                    _, pages = await self.page_info(page)
                    per_page = await self.current_row_count(page)
                if total > 0 and per_page > 0:
                    return total, pages, per_page
                body = await self._body(page)
                self.log(
                    f"{collection['id']} 第 {attempt} 次检索未读取到有效结果"
                    f"（命中 {total}，当前页 {per_page} 条），准备重试"
                )
                (self.logs_dir / f"{collection['id']}-search-attempt-{attempt}.txt").write_text(
                    body[:20000], encoding="utf-8"
                )
            except Exception as exc:
                last_error = exc
                self.log(f"{collection['id']} 第 {attempt} 次检索失败：{exc}")
            await page.wait_for_timeout(attempt * 3000)
        if last_error:
            raise RuntimeError(f"{collection['id']} 多次检索失败") from last_error
        raise RuntimeError(f"{collection['id']} 多次检索均未返回有效记录")

    async def export_native_collection(
        self,
        collection: dict[str, Any],
        *,
        batch_size: int = 500,
        max_batches: int | None = None,
        max_results_per_slice: int = 6000,
        _write_manifest: bool = True,
    ) -> dict[str, Any]:
        if self.context is None:
            raise RuntimeError("浏览器未启动")
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        cid = str(collection["id"])
        native_dir = self.output_dir / "native_exports" / cid
        native_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = native_dir / "manifest.json"
        publication_time = collection.get("publication_time")
        if max_results_per_slice < 1:
            raise ValueError("max_results_per_slice 必须大于 0")
        file_prefix = cid
        if publication_time is not None:
            file_prefix += f"_{publication_time['start']}_{publication_time['end']}"

        self.log(f"{cid} 开始知网原生元数据导出：{collection['name']}")
        total, pages, per_page = await self.search_collection(
            page, collection, max_results_per_slice=max_results_per_slice
        )
        if total > max_results_per_slice:
            if publication_time is None:
                raise RuntimeError(
                    f"{cid} 命中 {total} 条，超过单个时间片上限 {max_results_per_slice}；"
                    "自动切片需要在检索配置中指定 publication_time，"
                    "或关闭 --all-time 后使用默认月份范围"
                )
            slices = split_publication_time_range(publication_time)
            if slices is None:
                raise RuntimeError(
                    f"{cid} 的发表时间范围 {publication_time['start']} 至 "
                    f"{publication_time['end']} 已细化到单日，但仍命中 {total} 条，"
                    f"超过单个时间片上限 {max_results_per_slice}；"
                    "请进一步缩小检索式或提高 --max-results-per-slice"
                )

            self.log(
                f"{cid} 时间范围 {publication_time['start']} 至 {publication_time['end']} "
                f"命中 {total} 条，超过上限 {max_results_per_slice}，"
                f"拆分为 {slices[0]['start']} 至 {slices[0]['end']} 和 "
                f"{slices[1]['start']} 至 {slices[1]['end']}"
            )
            await self.clear_selection(page)
            slice_manifests: list[dict[str, Any]] = []
            for date_slice in slices:
                slice_collection = {
                    **collection,
                    "publication_time": date_slice,
                }
                slice_manifests.append(
                    await self.export_native_collection(
                        slice_collection,
                        batch_size=batch_size,
                        max_batches=max_batches,
                        max_results_per_slice=max_results_per_slice,
                        _write_manifest=False,
                    )
                )
            sliced_total = sum(int(item["result_count"]) for item in slice_manifests)
            if sliced_total != total:
                raise RuntimeError(
                    f"{cid} 时间切片合计命中 {sliced_total} 条，"
                    f"原始范围为 {total} 条；检索结果可能在切片过程中发生变化"
                )
            exported_batches: list[dict[str, Any]] = []
            record_offset = 0
            for item in slice_manifests:
                for batch in item["exported_batches"]:
                    exported_batches.append(
                        {
                            **batch,
                            "first_record": int(batch["first_record"]) + record_offset,
                            "last_record": int(batch["last_record"]) + record_offset,
                        }
                    )
                record_offset += int(item["result_count"])
            manifest = {
                "collection": cid,
                "name": collection["name"],
                "query": collection["query"],
                "publication_time": publication_time,
                "result_count": sliced_total,
                "pages": sum(int(item["pages"]) for item in slice_manifests),
                "batch_size": batch_size,
                "max_results_per_slice": max_results_per_slice,
                "exported_batches": exported_batches,
                "time_slices": [
                    {
                        "publication_time": item["publication_time"],
                        "result_count": item["result_count"],
                        "pages": item["pages"],
                    }
                    for item in slice_manifests
                ],
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            if _write_manifest:
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            self.log(
                f"{cid} 时间切片导出完成：{len(slice_manifests)} 个子区间，"
                f"共 {sliced_total} 条"
            )
            return manifest

        self.log(f"{cid} 命中 {total} 条，共 {pages} 页，每页 {per_page} 条；按每批 {batch_size} 条导出")
        await self.clear_selection(page)

        if total == 0 or per_page == 0:
            manifest = {
                "collection": cid,
                "name": collection["name"],
                "query": collection["query"],
                "publication_time": publication_time,
                "result_count": total,
                "pages": pages,
                "batch_size": batch_size,
                "max_results_per_slice": max_results_per_slice,
                "exported_batches": [],
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            if _write_manifest:
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            self.log(f"{cid} 无命中记录")
            return manifest

        pages_per_batch = max(1, batch_size // per_page)
        batch_count = math.ceil(pages / pages_per_batch) if pages else 0
        if max_batches is not None:
            batch_count = min(batch_count, max_batches)
        exported: list[dict[str, Any]] = []

        for batch_index in range(batch_count):
            first_page = batch_index * pages_per_batch + 1
            last_page = min(pages, first_page + pages_per_batch - 1)
            first_record = (first_page - 1) * per_page + 1
            last_record = min(total, last_page * per_page)
            destination = native_dir / f"{file_prefix}_{first_record:05d}-{last_record:05d}.xls"
            if destination.exists() and destination.stat().st_size > 0:
                self.log(f"{cid} 已存在批次 {batch_index + 1}/{batch_count}：{destination.name}")
                exported.append(
                    {
                        "batch": batch_index + 1,
                        "first_record": first_record,
                        "last_record": last_record,
                        "expected_records": last_record - first_record + 1,
                        "file": destination.relative_to(self.root).as_posix(),
                        "bytes": destination.stat().st_size,
                        "status": "existing",
                    }
                )
                await self.goto_page_number(page, last_page)
                if last_page < pages:
                    await self.next_page(page)
                continue

            await self.goto_page_number(page, first_page)
            await self.clear_selection(page)
            selected_expected = 0
            for pno in range(first_page, last_page + 1):
                current, _ = await self.page_info(page)
                if current != pno:
                    raise RuntimeError(f"批次页码异常：应为 {pno}，实际为 {current}")
                added = await self.select_current_page(page)
                selected_expected += added
                selected_now = await self.selected_count(page)
                self.log(
                    f"{cid} 批次 {batch_index + 1}/{batch_count}：第 {pno}/{pages} 页，已选 {selected_now} 条"
                )
                await page.wait_for_timeout(1200)
                if pno < last_page:
                    if not await self.next_page(page):
                        raise RuntimeError(f"{cid} 第 {pno} 页后无法翻页")
            selected = await self.selected_count(page)
            expected = last_record - first_record + 1
            if selected != expected or selected_expected != expected:
                raise RuntimeError(
                    f"{cid} 批次选择数量异常：页面显示 {selected}，"
                    f"累计新增 {selected_expected}，应为 {expected}"
                )

            last_export_error: Exception | None = None
            for export_attempt in range(1, 5):
                export_page: Page | None = None
                try:
                    export_page = await self.open_custom_export(page)
                    await self.download_xls(export_page, destination)
                    last_export_error = None
                    break
                except Exception as exc:
                    last_export_error = exc
                    self.log(f"{cid} 批次 {batch_index + 1} 第 {export_attempt} 次 XLS 导出失败：{exc}")
                    if export_page is not None:
                        try:
                            await export_page.screenshot(
                                path=str(
                                    self.logs_dir
                                    / f"{cid}-batch-{batch_index + 1}-export-{export_attempt}.png"
                                ),
                                full_page=True,
                            )
                        except Exception:
                            pass
                    await page.wait_for_timeout(export_attempt * 3000)
                finally:
                    if export_page is not None and not export_page.is_closed():
                        await export_page.close()
            if last_export_error is not None:
                raise RuntimeError(f"{cid} 批次 {batch_index + 1} 多次 XLS 导出失败") from last_export_error
            exported.append(
                {
                    "batch": batch_index + 1,
                    "first_record": first_record,
                    "last_record": last_record,
                    "expected_records": expected,
                    "file": destination.relative_to(self.root).as_posix(),
                    "bytes": destination.stat().st_size,
                    "status": "downloaded",
                }
            )
            self.log(f"{cid} 批次 {batch_index + 1}/{batch_count} 已下载：{destination.name}")
            await self.clear_selection(page)
            if last_page < pages:
                await self.next_page(page)

        manifest = {
            "collection": cid,
            "name": collection["name"],
            "query": collection["query"],
            "publication_time": publication_time,
            "result_count": total,
            "pages": pages,
            "batch_size": batch_size,
            "max_results_per_slice": max_results_per_slice,
            "exported_batches": exported,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        if _write_manifest:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        self.log(f"{cid} 原生导出完成：{len(exported)} 个批次")
        return manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CNKI 自定义 XLS 批量元数据导出器")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--queries", type=Path, help="JSON 检索集合配置")
    source.add_argument("--docx", type=Path, help="包含检索策略的 DOCX")
    p.add_argument("--docx-start", default="四 CNKI", help="DOCX 中 CNKI 章节起始标题")
    p.add_argument("--docx-end", default="五 Web of Science", help="DOCX 中下一章节标题")
    p.add_argument("--root", type=Path, default=Path.cwd(), help="运行数据目录")
    p.add_argument("--collection", action="append", help="集合编号，可重复；默认全部")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument(
        "--max-results-per-slice",
        type=int,
        default=6000,
        help="单个发表时间片的最大结果数；超过后自动递归按日期拆分",
    )
    p.add_argument(
        "--all-time",
        action="store_true",
        help="不为未配置发表时间的集合自动限制到上一个完整自然月",
    )
    p.add_argument("--max-batches", type=int, default=None, help="测试时限制每个集合的批次数")
    p.add_argument("--headless", action="store_true")
    return p


def _completed_batch_files(root: Path, collection_id: str) -> list[dict[str, Any]]:
    native_dir = root / "output" / "native_exports" / collection_id
    if not native_dir.exists():
        return []

    batches: list[dict[str, Any]] = []
    for path in sorted(native_dir.glob("*.xls")):
        if path.stat().st_size == 0:
            continue
        match = re.search(r"_(\d{5})-(\d{5})\.xls$", path.name)
        if match is None:
            continue
        batches.append(
            {
                "file": path.relative_to(root).as_posix(),
                "first_record": int(match.group(1)),
                "last_record": int(match.group(2)),
                "bytes": path.stat().st_size,
                "status": "existing",
            }
        )
    return batches


def _log_run_progress(exporter: CNKINativeExporter, results: list[dict[str, Any]]) -> None:
    exporter.log("全部检索集合执行完毕，批次进度汇总：")
    for result in results:
        latest = result["last_completed_batch"]
        if latest is not None:
            progress = (
                f"已完成 {result['completed_batch_count']} 个批次，最新完成 "
                f"{Path(latest['file']).name}（记录 {latest['first_record']}-"
                f"{latest['last_record']}）"
            )
        else:
            progress = "尚无已完成批次"
        status = "完成" if result["status"] == "completed" else "中断两次后跳过"
        exporter.log(f"{result['collection']}：{status}；{progress}")


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
    if not args.all_time:
        default_range = last_complete_month_range()
        for collection in queries:
            if collection.get("publication_time") is None:
                collection["publication_time"] = default_range
        (root / "data" / "queries.resolved.json").write_text(
            json.dumps(queries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    selected = set(args.collection or [str(item["id"]) for item in queries])
    collections = [item for item in queries if str(item["id"]) in selected]
    exporter = CNKINativeExporter(root, root / ".cnki-profile", headless=args.headless)
    await exporter.launch()
    try:
        summaries: list[dict[str, Any]] = []
        skipped_collections = False
        for index, collection in enumerate(collections):
            manifest = None
            last_error: Exception | None = None
            interruptions = 0
            for collection_attempt in range(1, 3):
                try:
                    manifest = await exporter.export_native_collection(
                        collection,
                        batch_size=args.batch_size,
                        max_batches=args.max_batches,
                        max_results_per_slice=args.max_results_per_slice,
                    )
                    last_error = None
                    break
                except CaptchaPending:
                    raise
                except Exception as exc:
                    last_error = exc
                    interruptions += 1
                    exporter.log(
                        f"{collection['id']} 第 {collection_attempt} 次集合执行中断：{exc}；"
                        + (
                            "已达到中断上限，跳到下一个检索集合"
                            if interruptions == 2
                            else "等待 45 秒后从已有批次续接"
                        )
                    )
                    if interruptions == 1:
                        await asyncio.sleep(45)
            if last_error is not None or manifest is None:
                skipped_collections = True
                summaries.append(
                    {
                        "collection": str(collection["id"]),
                        "name": str(collection["name"]),
                        "status": "skipped_after_two_interruptions",
                        "interruptions": 2,
                        "last_error": str(last_error),
                        "completed_at": datetime.now().isoformat(timespec="seconds"),
                        "completed_batches": _completed_batch_files(root, str(collection["id"])),
                    }
                )
            else:
                completed_batches = manifest.get("exported_batches", [])
                summaries.append(
                    {
                        **manifest,
                        "status": "completed",
                        "interruptions": interruptions,
                        "completed_batches": completed_batches,
                    }
                )
            if index + 1 < len(collections):
                exporter.log("等待 20 秒后执行下一个检索集合")
                await asyncio.sleep(20                )
        for summary in summaries:
            batches = summary["completed_batches"]
            summary["completed_batch_count"] = len(batches)
            summary["last_completed_batch"] = batches[-1] if batches else None
        (root / "output" / "native-export-summary.json").write_text(
            json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _log_run_progress(exporter, summaries)
        return 1 if skipped_collections else 0
    except CaptchaPending as exc:
        exporter.log(str(exc))
        return 2
    finally:
        await exporter.close()


def main() -> None:
    raise SystemExit(asyncio.run(run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
