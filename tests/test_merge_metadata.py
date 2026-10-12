from pathlib import Path

from cnki_metadata_exporter.merge_metadata import deduplicate, parse_native_xls


def test_parse_cnki_html_xls(tmp_path: Path) -> None:
    path = tmp_path / "sample.xls"
    path.write_text(
        """<table><tr><th>SrcDatabase-来源库</th><th>Title-题名</th><th>Author-作者</th>"
        "<th>DOI-DOI</th></tr><tr><td>期刊</td><td>示例论文</td><td>张三</td>"
        "<td>10.1000/demo</td></tr></table>""",
        encoding="utf-8",
    )
    rows = parse_native_xls(path)
    assert rows == [{"来源库": "期刊", "题名": "示例论文", "作者": "张三", "DOI": "10.1000/demo"}]


def test_deduplicate_merges_collection_membership() -> None:
    rows = [
        {
            "来源库": "期刊",
            "题名": "同一篇论文",
            "作者": "张三",
            "DOI": "10.1000/demo",
            "检索集合": "Q1",
            "集合名称": "集合一",
            "数据层级": "核心",
        },
        {
            "来源库": "期刊",
            "题名": "同一篇论文",
            "作者": "张三",
            "DOI": "https://doi.org/10.1000/demo",
            "检索集合": "Q2",
            "集合名称": "集合二",
            "数据层级": "扩展",
        },
    ]
    result = deduplicate(rows)
    assert len(result) == 1
    assert result[0]["检索集合"] == "Q1；Q2"
    assert result[0]["原始记录数"] == 2
