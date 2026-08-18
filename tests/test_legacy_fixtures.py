import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from music_score_toolkit.mscz import transpose_mscz

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "fixture",
    ["auto-transpose-sample.mscz", "auto-music-transpose-sample.mscz"],
)
def test_original_mscz_samples_transpose_successfully(fixture: str, tmp_path: Path):
    source = FIXTURES / fixture
    output = tmp_path / fixture
    with zipfile.ZipFile(source) as before:
        original_entries = set(before.namelist())

    report = transpose_mscz(source, output, "Bb", "C")

    with zipfile.ZipFile(output) as after:
        assert set(after.namelist()) == original_entries
        assert after.testzip() is None
        score_name = next(name for name in after.namelist() if name.endswith(".mscx"))
        concert_keys = [
            int(item.text) for item in ET.fromstring(after.read(score_name)).iter("concertKey")
        ]
    assert report.notes_changed > 0
    assert concert_keys == [5]
    assert report.key_signatures_changed == 1
    assert report.score_entries_changed >= 1
