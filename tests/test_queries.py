import json
from datetime import date
from pathlib import Path

import pytest

from cnki_metadata_exporter.queries import (
    last_complete_month_range,
    load_queries,
    split_publication_time_range,
    validate_queries,
)


def test_load_example_queries() -> None:
    path = Path(__file__).parents[1] / "examples" / "queries.example.json"
    queries = load_queries(path)
    assert [item["id"] for item in queries] == ["Q1", "Q2"]
    assert queries[0]["field"] == "SU"
    assert queries[1]["tier"] == "性能研究"


def test_wrapped_queries_are_supported(tmp_path: Path) -> None:
    path = tmp_path / "queries.json"
    path.write_text(
        json.dumps({"queries": [{"id": "A1", "name": "示例", "query": "SU=('钢')"}]}),
        encoding="utf-8",
    )
    assert load_queries(path)[0]["expression"] == "('钢')"


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="重复"):
        validate_queries(
            [
                {"id": "Q1", "name": "一", "query": "SU=('钢')"},
                {"id": "Q1", "name": "二", "query": "SU=('铁')"},
            ]
        )


def test_publication_time_range_is_validated_and_preserved() -> None:
    queries = validate_queries(
        [
            {
                "id": "Q1",
                "name": "时间范围示例",
                "query": "SU=('示例')",
                "publication_time": {"start": "2010-01-01", "end": "2018-12-31"},
            }
        ]
    )
    assert queries[0]["publication_time"] == {"start": "2010-01-01", "end": "2018-12-31"}


@pytest.mark.parametrize(
    ("publication_time", "message"),
    [
        ({"start": "2010", "end": "2018"}, "YYYY-MM-DD"),
        ({"start": "2018-01-01", "end": "2010-12-31"}, "不能晚于"),
        ({"start": "2010-01-01"}, "YYYY-MM-DD"),
    ],
)
def test_invalid_publication_time_is_rejected(
    publication_time: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_queries(
            [
                {
                    "id": "Q1",
                    "name": "错误时间范围",
                    "query": "SU=('示例')",
                    "publication_time": publication_time,
                }
            ]
        )


def test_last_complete_month_range_handles_year_boundary() -> None:
    assert last_complete_month_range(date(2026, 1, 15)) == {
        "start": "2025-12-01",
        "end": "2025-12-31",
    }


def test_publication_time_range_splits_without_gaps_or_overlap() -> None:
    result = split_publication_time_range({"start": "2023-01-01", "end": "2023-12-31"})
    assert result is not None
    left, right = result
    assert left == {"start": "2023-01-01", "end": "2023-07-02"}
    assert right == {"start": "2023-07-03", "end": "2023-12-31"}


def test_single_day_publication_range_cannot_be_split() -> None:
    assert split_publication_time_range(
        {"start": "2023-06-15", "end": "2023-06-15"}
    ) is None
