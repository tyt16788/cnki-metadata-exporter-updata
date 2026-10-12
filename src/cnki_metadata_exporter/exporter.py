from __future__ import annotations

import asyncio
import csv
import json
import random
import re
import unicodedata
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, Locator, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

CNKI_ADVANCED = "https://kns.cnki.net/kns8s/AdvSearch"
CAPTCHA_MARKERS = ("安全验证", "拖动下方拼图完成验证", "访问过于频繁", "系统检测到异常访问")


@dataclass
class Record:
    来源数据库: str = ""
    题名: str = ""
    作者: str = ""
    机构: str = ""
    来源: str = ""
    关键词: str = ""
    摘要: str = ""
    发表时间: str = ""
    被引量: str = ""
    下载量: str = ""
    第一责任人: str = ""
    基金: str = ""
    年份: str = ""
    DOI: str = ""
    详情链接: str = ""
    检索集合: str = ""
    集合名称: str = ""
    结果页码: int = 0
    集合内序号: int = 0
    采集状态: str = ""
    采集时间: str = ""


class CaptchaPending(RuntimeError):
    pass


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def number_text(value: str | None) -> str:
    match = re.search(r"\d+(?:,\d+)*", value or "")
    return match.group(0).replace(",", "") if match else ""


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def record_key(record: dict[str, Any] | Record) -> str:
    data = asdict(record) if isinstance(record, Record) else record
    doi = clean_text(str(data.get("DOI", ""))).lower()
    if doi:
        return "doi:" + doi.removeprefix("https://doi.org/").removeprefix("doi:")
    parts = [
        normalize_title(str(data.get("题名", ""))),
        clean_text(str(data.get("作者", ""))).lower(),
        clean_text(str(data.get("来源", ""))).lower(),
        clean_text(str(data.get("发表时间", ""))),
        clean_text(str(data.get("来源数据库", ""))),
    ]
    return "meta:" + "|".join(parts)


def raw_record_key(record: dict[str, Any] | Record) -> str:
    data = asdict(record) if isinstance(record, Record) else record
    return "raw:{collection}:{page}:{rank}".format(
        collection=data.get("检索集合", ""),
        page=data.get("结果页码", ""),
        rank=data.get("集合内序号", ""),
    )


async def locator_text(locator: Locator) -> str:
    try:
        if await locator.count():
            return clean_text(await locator.first.inner_text(timeout=3000))
    except Exception:
        pass
    return ""


async def meta_content(page: Page, names: list[str]) -> str:
    for name in names:
        for selector in (f'meta[name="{name}"]', f'meta[property="{name}"]'):
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    value = clean_text(await locator.get_attribute("content"))
                    if value:
                        return value
            except Exception:
                continue
    return ""


async def meta_contents(page: Page, names: list[str]) -> list[str]:
    output: list[str] = []
    for name in names:
        for selector in (f'meta[name="{name}"]', f'meta[property="{name}"]'):
            try:
                values = await page.locator(selector).evaluate_all(
                    "els => els.map(e => e.getAttribute('content') || '').filter(Boolean)"
                )
                output.extend(clean_text(str(v)) for v in values if clean_text(str(v)))
            except Exception:
                continue
    return list(dict.fromkeys(output))


class CNKIMetadataExporter:
    def __init__(
        self,
        root: Path,
        profile_dir: Path,
        *,
        headless: bool = False,
        detail_delay: tuple[float, float] = (1.0, 1.8),
        page_delay: tuple[float, float] = (1.0, 2.0),
    ) -> None:
        self.root = root
        self.profile_dir = profile_dir
        self.headless = headless
        self.detail_delay = detail_delay
        self.page_delay = page_delay
        self.output_dir = root / "output"
        self.logs_dir = root / "logs"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.logs_dir / "采集日志.txt"
        self.context: BrowserContext | None = None

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {message}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def launch(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self.context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            accept_downloads=True,
            ignore_https_errors=True,
            locale="zh-CN",
            viewport={"width": 1440, "height": 960},
            args=[
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if getattr(self, "_playwright", None):
            await self._playwright.stop()

    async def _body(self, page: Page) -> str:
        try:
            return await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    async def _visible_text(self, page: Page, text: str) -> bool:
        locator = page.get_by_text(text, exact=False)
        for index in range(await locator.count()):
            try:
                if await locator.nth(index).is_visible(timeout=200):
                    return True
            except Exception:
                continue
        return False

    async def captcha_visible(self, page: Page) -> bool:
        # Normal CNKI pages retain a rendered verification component even when no
        # challenge is active, so the verification route is the reliable signal.
        return "verify/" in page.url

    async def wait_for_human_verification(self, page: Page, timeout_minutes: int = 20) -> None:
        if not await self.captcha_visible(page):
            return
        if self.headless:
            raise CaptchaPending("检测到知网安全验证，需要使用可见浏览器完成")
        self.log("检测到知网安全验证；浏览器已停留在验证页面，等待人工完成拼图")
        deadline = asyncio.get_running_loop().time() + timeout_minutes * 60
        while asyncio.get_running_loop().time() < deadline:
            await page.wait_for_timeout(1000)
            if not await self.captcha_visible(page):
                self.log("安全验证已完成，继续执行")
                return
        raise CaptchaPending("人工验证等待超时，进度已保存")

    async def open_professional_search(self, page: Page) -> None:
        await page.goto(CNKI_ADVANCED, wait_until="domcontentloaded", timeout=60000)
        await self.wait_for_human_verification(page)
        try:
            await page.get_by_text("专业检索", exact=True).last.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("未找到知网专业检索入口") from exc
        tabs = page.get_by_text("专业检索", exact=True)
        clicked = False
        for index in range(await tabs.count()):
            tab = tabs.nth(index)
            try:
                if await tab.is_visible():
                    await tab.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            raise RuntimeError("知网专业检索入口当前不可见")
        await page.locator("textarea.textarea-major, textarea.majorSearch, textarea").first.wait_for(
            state="visible", timeout=15000
        )

    async def fill_publication_time(self, page: Page, publication_time: dict[str, str]) -> None:
        date_inputs = {
            "#datebox0": publication_time["start"],
            "#datebox1": publication_time["end"],
        }
        missing = [selector for selector in date_inputs if not await page.locator(selector).count()]
        if missing:
            raise RuntimeError(
                "已配置发表时间，但未找到知网日期控件 "
                f"{', '.join(missing)}；请确认专业检索页面已加载或页面结构是否更新"
            )

        for selector, value in date_inputs.items():
            field = page.locator(selector)
            if not await field.is_visible():
                raise RuntimeError(f"知网发表时间控件不可见：{selector}")
            await field.evaluate(
                """(input, date) => {
                    input.value = date;
                    input.setAttribute("txt", date);
                    input.setAttribute("condition", `(${date})`);
                    input.dispatchEvent(new Event("input", { bubbles: true }));
                    input.dispatchEvent(new Event("change", { bubbles: true }));
                }""",
                value,
            )
            actual = await field.evaluate(
                """input => ({
                    value: input.value,
                    txt: input.getAttribute("txt"),
                    condition: input.getAttribute("condition")
                })"""
            )
            expected = {"value": value, "txt": value, "condition": f"({value})"}
            if actual != expected:
                raise RuntimeError(
                    f"知网发表时间控件写入失败：{selector} 应为 {expected}，实际 {actual}"
                )
        self.log(
            f"已填入发表时间范围：{publication_time['start']} 至 {publication_time['end']}"
        )

    async def submit_professional_query(
        self,
        page: Page,
        query: str,
        publication_time: dict[str, str] | None = None,
    ) -> None:
        await self.open_professional_search(page)
        candidates = [
            "textarea",
            "#expertvalue",
            "#txt_SearchText",
            "input[name='expertvalue']",
            "input[type='text']",
        ]
        query_box: Locator | None = None
        for selector in candidates:
            locator = page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    query_box = locator
                    break
            except Exception:
                continue
        if query_box is None:
            raise RuntimeError("未找到知网专业检索式输入框")
        await query_box.fill(query)
        if publication_time is not None:
            await self.fill_publication_time(page, publication_time)
        bilingual = page.locator('.gradeSearch input[data-id="EN"]:checked').first
        if await bilingual.count():
            await bilingual.uncheck()
        button = page.locator(".search-middle input.btn-search:visible, input.btn-search:visible").first
        if not await button.count():
            button = page.get_by_role("button", name=re.compile(r"检索|搜索")).first
        if not await button.count():
            raise RuntimeError("未找到知网专业检索按钮")
        await button.click()
        try:
            await page.locator("span.pagerTitleCell, table.result-table-list, p.no-content").first.wait_for(
                state="visible", timeout=45000
            )
        except PlaywrightTimeoutError:
            await self.wait_for_human_verification(page)
        await self.wait_for_human_verification(page)

    async def result_count(self, page: Page) -> int:
        for selector in ("span.pagerTitleCell em", "span.pagerTitleCell"):
            text = await locator_text(page.locator(selector))
            number = number_text(text)
            if number:
                return int(number)
        return 0

    async def page_info(self, page: Page) -> tuple[int, int]:
        text = await locator_text(page.locator("span.countPageMark"))
        match = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if match:
            return int(match.group(1)), int(match.group(2))
        try:
            current = await page.locator("div.pagesnums a.cur[data-curpage]").first.get_attribute(
                "data-curpage"
            )
            pages = await page.locator("div.pagesnums a[data-curpage]").evaluate_all(
                "els => els.map(e => Number(e.getAttribute('data-curpage'))).filter(Number.isFinite)"
            )
            current_i = int(current or 1)
            return current_i, max([current_i, *[int(v) for v in pages]])
        except Exception:
            return 1, 1

    async def set_page_size(self, page: Page, size: int = 50) -> None:
        current = await locator_text(page.locator("#perPageDiv .sort-default span"))
        if current == str(size):
            return
        dropdown = page.locator("#perPageDiv .sort-default").first
        option = page.locator(f"#perPageDiv ul.sort-list li[data-val='{size}'] a").first
        if not await dropdown.count() or not await option.count():
            return
        await dropdown.click()
        await option.click()
        await page.wait_for_timeout(800)
        await page.locator("table.result-table-list tbody tr, p.no-content").first.wait_for(
            state="visible", timeout=30000
        )

    async def parse_result_page(self, page: Page, collection: dict[str, Any]) -> list[Record]:
        current_page, _ = await self.page_info(page)
        rows = page.locator("table.result-table-list tbody tr")
        if not await rows.count():
            rows = page.locator(".result-table-list tbody tr")
        records: list[Record] = []
        for index in range(await rows.count()):
            row = rows.nth(index)
            title_link = row.locator("a.fz14, td.name a, a[href*='kcms2/article'], a[href*='Detail']").first
            if not await title_link.count():
                continue
            title = clean_text(await title_link.inner_text())
            href = await title_link.get_attribute("href") or ""
            if not title or not href:
                continue
            author = await locator_text(row.locator("td.author"))
            source = await locator_text(row.locator("td.source"))
            publish_time = await locator_text(row.locator("td.date, td.time"))
            cited = await locator_text(row.locator("td.quote"))
            downloaded = await locator_text(row.locator("td.download"))
            cells = row.locator("td")
            if not cited and await cells.count() >= 6:
                cited = await locator_text(cells.nth(5))
            if not downloaded and await cells.count() >= 7:
                downloaded = await locator_text(cells.nth(6))
            row_text = await locator_text(row)
            badge = ""
            try:
                badge = " ".join(
                    await row.locator("em, [title], img[alt]").evaluate_all(
                        "els => els.map(e => e.textContent || "
                        "e.getAttribute('title') || e.getAttribute('alt') || '').filter(Boolean)"
                    )
                )
            except Exception:
                pass
            resource_type = self.detect_resource_type(" ".join((href, row_text, badge)))
            year_match = re.search(r"(?:19|20)\d{2}", publish_time)
            records.append(
                Record(
                    来源数据库=resource_type,
                    题名=title,
                    作者=author,
                    来源=source,
                    发表时间=publish_time,
                    被引量=number_text(cited),
                    下载量=number_text(downloaded),
                    第一责任人=re.split(r"[;；,，\s]+", author)[0] if author else "",
                    年份=year_match.group(0) if year_match else "",
                    详情链接=urljoin(CNKI_ADVANCED, href),
                    检索集合=str(collection["id"]),
                    集合名称=str(collection["name"]),
                    结果页码=current_page,
                    集合内序号=(current_page - 1) * 50 + index + 1,
                    采集状态="列表页",
                    采集时间=datetime.now().isoformat(timespec="seconds"),
                )
            )
        return records

    @staticmethod
    def detect_resource_type(text: str) -> str:
        upper = text.upper()
        if any(token in text for token in ("学位论文", "博士", "硕士")) or any(
            token in upper for token in ("CDMD", "CMFD", "CDFD", "DISSERTATION")
        ):
            return "学位论文"
        if "会议" in text or any(token in upper for token in ("CPFD", "CONFERENCE")):
            return "会议论文"
        if "报纸" in text or "CCND" in upper:
            return "报纸"
        if "期刊" in text or any(token in upper for token in ("CJFD", "CJFQ", "JOURNAL")):
            return "学术期刊"
        return "CNKI"

    async def enrich_record(self, context: BrowserContext, record: Record) -> Record:
        detail = await context.new_page()
        try:
            await detail.goto(record.详情链接, wait_until="domcontentloaded", timeout=60000)
            await self.wait_for_human_verification(detail)
            try:
                await detail.locator(".wx-tit h1, div.doc h1, h1").first.wait_for(
                    state="visible", timeout=10000
                )
            except Exception:
                pass
            body = await self._body(detail)
            record.题名 = (
                await meta_content(detail, ["citation_title", "DC.Title"])
                or await locator_text(detail.locator(".wx-tit h1, div.doc h1, div.main h1, h1"))
                or record.题名
            )
            authors = await meta_contents(detail, ["citation_author", "DC.Creator"])
            author_text = await locator_text(detail.locator(".author, .doc-author, .author-text"))
            if authors:
                record.作者 = "；".join(authors)
            elif author_text:
                record.作者 = author_text
            record.第一责任人 = re.split(r"[;；,，\s]+", record.作者)[0] if record.作者 else ""
            institution = await locator_text(
                detail.locator(".orgn, .affiliation, .author-affiliation, #catalog_ORG")
            )
            if institution:
                record.机构 = institution
            source = await meta_content(
                detail, ["citation_journal_title", "citation_conference_title"]
            ) or await locator_text(detail.locator(".sourcename, .journal-name"))
            if source:
                record.来源 = source
            abstract = await meta_content(
                detail, ["description", "DC.Description", "citation_abstract"]
            ) or await locator_text(
                detail.locator("#ChDivSummary, span.abstract-text, .abstract-text, .abstract")
            )
            if abstract:
                record.摘要 = re.sub(r"^摘要[:：]?", "", abstract).strip()
            keywords = await meta_content(detail, ["keywords", "DC.Subject"]) or await locator_text(
                detail.locator("p.keywords, .keywords, .keyword-list, #catalog_KEYWORD")
            )
            if keywords:
                record.关键词 = re.sub(r"^关键词[:：]?", "", keywords).strip()
            fund = await locator_text(
                detail.locator("p.funds, .funds, .fund-info, .funding, .fund, #catalog_FUND")
            )
            if fund:
                record.基金 = re.sub(r"^基金[:：]?", "", fund).strip()
            doi = await meta_content(detail, ["citation_doi", "DC.Identifier"])
            if not doi:
                match = re.search(r"DOI\s*[:：]\s*([^\s;；]+)", body, flags=re.I)
                doi = match.group(1) if match else ""
            record.DOI = clean_text(doi)
            publish_date = await meta_content(detail, ["citation_publication_date", "DC.Date"])
            if publish_date:
                record.发表时间 = publish_date
            year_match = re.search(r"(?:19|20)\d{2}", record.发表时间 or body)
            if year_match:
                record.年份 = year_match.group(0)
            detected = self.detect_resource_type(record.详情链接 + " " + body[:4000])
            if detected != "CNKI":
                record.来源数据库 = detected
            record.采集状态 = "详情完成"
            record.采集时间 = datetime.now().isoformat(timespec="seconds")
            return record
        finally:
            await detail.close()

    async def next_page(self, page: Page) -> bool:
        current, total = await self.page_info(page)
        if current >= total:
            return False
        button = page.locator("#PageNext").first
        if not await button.count():
            button = page.locator(f"a[data-curpage='{current + 1}']").first
        if not await button.count():
            return False
        await button.click()
        for _ in range(40):
            await page.wait_for_timeout(500)
            await self.wait_for_human_verification(page)
            new_page, _ = await self.page_info(page)
            if new_page == current + 1:
                return True
        return False

    def paths_for(self, collection_id: str) -> tuple[Path, Path, Path]:
        base = self.output_dir / collection_id
        return base.with_suffix(".list.jsonl"), base.with_suffix(".metadata.jsonl"), base.with_suffix(".csv")

    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        if not path.exists():
            return output
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                output.append(json.loads(line))
            except Exception:
                continue
        return output

    @classmethod
    def load_existing(cls, path: Path) -> dict[str, dict[str, Any]]:
        return {record_key(item): item for item in cls.read_jsonl(path)}

    @staticmethod
    def append_jsonl(path: Path, record: Record) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    @staticmethod
    def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
        columns = [field.name for field in fields(Record)]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)

    async def export_collection(
        self, collection: dict[str, Any], *, list_only: bool = False
    ) -> dict[str, Any]:
        if self.context is None:
            raise RuntimeError("浏览器未启动")
        page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        collection_id = str(collection["id"])
        list_path, metadata_path, csv_path = self.paths_for(collection_id)
        existing = self.load_existing(metadata_path)
        self.log(f"{collection_id} 开始专业检索：{collection['name']}")
        await self.submit_professional_query(
            page, str(collection["query"]), collection.get("publication_time")
        )
        total = await self.result_count(page)
        await self.set_page_size(page, 50)
        current, pages = await self.page_info(page)
        self.log(f"{collection_id} 知网页面命中 {total} 条，共 {pages} 页")
        seen_list: set[str] = {raw_record_key(item) for item in self.read_jsonl(list_path)}
        harvested = 0
        completed = len(existing)
        while True:
            await self.wait_for_human_verification(page)
            current, pages = await self.page_info(page)
            page_records = await self.parse_result_page(page, collection)
            self.log(f"{collection_id} 第 {current}/{pages} 页解析 {len(page_records)} 条")
            for record in page_records:
                raw_key = raw_record_key(record)
                if raw_key not in seen_list:
                    self.append_jsonl(list_path, record)
                    seen_list.add(raw_key)
                    harvested += 1
                key = record_key(record)
                if list_only or key in existing:
                    continue
                try:
                    enriched = await self.enrich_record(self.context, record)
                    self.append_jsonl(metadata_path, enriched)
                    existing[record_key(enriched)] = asdict(enriched)
                    completed += 1
                    self.log(f"{collection_id} 详情 {completed}/{total or '?'}：{enriched.题名[:45]}")
                except CaptchaPending:
                    raise
                except Exception as exc:
                    record.采集状态 = "详情失败"
                    record.采集时间 = datetime.now().isoformat(timespec="seconds")
                    self.append_jsonl(metadata_path, record)
                    existing[key] = asdict(record)
                    self.log(f"{collection_id} 详情失败：{record.题名[:45]} | {exc}")
                await asyncio.sleep(random.uniform(*self.detail_delay))
            if current >= pages or not await self.next_page(page):
                break
            await asyncio.sleep(random.uniform(*self.page_delay))
        records = (
            self.read_jsonl(list_path) if list_only else list(self.load_existing(metadata_path).values())
        )
        self.write_csv(csv_path, records)
        stats = {
            "collection": collection_id,
            "name": collection["name"],
            "query": collection["query"],
            "result_count": total,
            "pages": pages,
            "list_records_this_run": harvested,
            "metadata_records": len(records),
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        (self.output_dir / f"{collection_id}.stats.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.log(f"{collection_id} 完成：输出 {len(records)} 条")
        return stats
