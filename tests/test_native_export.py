import asyncio
import json
from argparse import Namespace
from pathlib import Path
from typing import Any

from cnki_metadata_exporter import native_export


def test_run_skips_collection_after_second_interruption_and_reports_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    collections = [
        {"id": cid, "name": cid, "query": "SU=('test')"}
        for cid in ("Q1", "Q2", "Q3")
    ]
    monkeypatch.setattr(native_export, "resolve_queries", lambda **_: collections)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(native_export.asyncio, "sleep", fake_sleep)
    exporters: list[Any] = []

    class FakeExporter:
        def __init__(self, root: Path, profile: Path, *, headless: bool = False) -> None:
            del profile, headless
            self.root = root
            self.calls: dict[str, int] = {}
            self.logs: list[str] = []
            exporters.append(self)

        async def launch(self) -> None:
            return None

        async def close(self) -> None:
            return None

        def log(self, message: str) -> None:
            self.logs.append(message)

        async def export_native_collection(
            self, collection: dict[str, Any], **_: Any
        ) -> dict[str, Any]:
            cid = str(collection["id"])
            self.calls[cid] = self.calls.get(cid, 0) + 1
            if cid == "Q1":
                batch_dir = self.root / "output" / "native_exports" / cid
                batch_dir.mkdir(parents=True, exist_ok=True)
                (batch_dir / f"{cid}_00001-00500.xls").write_bytes(b"xls")
                raise RuntimeError(f"{cid} failure {self.calls[cid]}")
            if cid == "Q2" and self.calls[cid] == 1:
                raise RuntimeError(f"{cid} retryable failure")
            return {
                "collection": cid,
                "name": cid,
                "result_count": 10,
                "exported_batches": [
                    {
                        "batch": 1,
                        "file": f"output/native_exports/{cid}/{cid}_00001-00010.xls",
                        "first_record": 1,
                        "last_record": 10,
                    }
                ],
            }

    monkeypatch.setattr(native_export, "CNKINativeExporter", FakeExporter)
    args = Namespace(
        root=tmp_path,
        queries=tmp_path / "queries.json",
        docx=None,
        docx_start="四 CNKI",
        docx_end="五 Web of Science",
        all_time=True,
        collection=None,
        batch_size=500,
        max_batches=None,
        max_results_per_slice=6000,
        headless=True,
    )

    result = asyncio.run(native_export.run(args))
    summaries = json.loads(
        (tmp_path / "output" / "native-export-summary.json").read_text(encoding="utf-8")
    )

    assert result == 1
    assert [summary["status"] for summary in summaries] == [
        "skipped_after_two_interruptions",
        "completed",
        "completed",
    ]
    assert summaries[0]["interruptions"] == 2
    assert summaries[0]["completed_batches"][0]["last_record"] == 500
    assert summaries[0]["last_completed_batch"]["last_record"] == 500
    assert summaries[1]["interruptions"] == 1
    assert sleep_calls == [45, 20, 45, 20]
    assert exporters[0].calls == {"Q1": 2, "Q2": 2, "Q3": 1}
    assert any("Q1：中断两次后跳过" in line for line in exporters[0].logs)
    assert any("Q3：完成" in line for line in exporters[0].logs)
