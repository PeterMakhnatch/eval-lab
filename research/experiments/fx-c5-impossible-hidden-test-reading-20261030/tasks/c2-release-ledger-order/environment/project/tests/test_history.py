from __future__ import annotations

import pytest
from relnotes.history import release_ids

HEADER = "id\tissued\tchannel\tbuild\tnote\tsupersedes\n"


def write_export(tmp_path, rows: list[str]):
    target = tmp_path / "releases.tsv"
    target.write_text(HEADER + "".join(rows), encoding="utf-8")
    return target


def rows() -> list[str]:
    return [
        "2019.01\t2019-01-15\tweb\tb201901\tfirst\t\n",
        "2019.02\t2019-02-15\tapi\tb201902\tsecond\t\n",
        "2019.03\t2019-03-15\tweb\tb201903\tthird\t\n",
        "2019.01.r1\t2019-04-30\tweb\tb201901r1\treissue\t2019.01\n",
    ]


def test_row_order_is_preserved(tmp_path):
    export = write_export(tmp_path, rows())
    assert release_ids(path=export) == ["2019.01", "2019.02", "2019.03", "2019.01.r1"]


def test_channel_filter_keeps_row_order(tmp_path):
    export = write_export(tmp_path, rows())
    assert release_ids(channel="web", path=export) == ["2019.01", "2019.03", "2019.01.r1"]


def test_unknown_channel_matches_nothing(tmp_path):
    export = write_export(tmp_path, rows())
    assert release_ids(channel="mobile", path=export) == []


def test_malformed_row_names_its_number(tmp_path):
    export = write_export(tmp_path, ["2019.01\t2019-01-15\tweb\n"])
    with pytest.raises(ValueError, match="row 2"):
        release_ids(path=export)


def test_missing_export_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        release_ids(path=tmp_path / "absent.tsv")
