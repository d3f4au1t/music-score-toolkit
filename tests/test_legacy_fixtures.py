import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from music_score_toolkit.keys import transpose_tpc
from music_score_toolkit.mscz import transpose_mscz

FIXTURES = Path(__file__).parent / "fixtures"


def _masked_score_tree(element: ET.Element, parent_tag: str | None = None) -> tuple:
    mutable_text = (
        (parent_tag == "Note" and element.tag in {"pitch", "tpc", "tpc2"})
        or (
            parent_tag == "KeySig"
            and element.tag in {"concertKey", "actualKey", "accidental"}
        )
        or (parent_tag == "Accidental" and element.tag == "subtype")
    )
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        "<transposed>" if mutable_text else element.text,
        element.tail,
        tuple(_masked_score_tree(child, element.tag) for child in element),
    )


@pytest.mark.parametrize(
    "fixture",
    ["auto-transpose-sample.mscz", "auto-music-transpose-sample.mscz"],
)
def test_original_mscz_samples_transpose_successfully(fixture: str, tmp_path: Path):
    source = FIXTURES / fixture
    output = tmp_path / fixture
    with zipfile.ZipFile(source) as before:
        original_entries = set(before.namelist())
        score_name = next(name for name in before.namelist() if name.endswith(".mscx"))
        original_score = ET.fromstring(before.read(score_name))

    # The archived samples begin in A major.  A -> B preserves the prototypes'
    # intended whole-step interval while exercising source-key validation.
    report = transpose_mscz(source, output, "A", "B")

    with zipfile.ZipFile(output) as after:
        assert set(after.namelist()) == original_entries
        assert after.testzip() is None
        transposed_score = ET.fromstring(after.read(score_name))
        concert_keys = [
            int(item.text) for item in transposed_score.iter("concertKey")
        ]

    original_notes = list(original_score.iter("Note"))
    transposed_notes = list(transposed_score.iter("Note"))
    assert len(transposed_notes) == len(original_notes)
    for original, transposed in zip(original_notes, transposed_notes):
        assert int(transposed.findtext("pitch")) == int(original.findtext("pitch")) + 2
        assert int(transposed.findtext("tpc")) == transpose_tpc(
            int(original.findtext("tpc")), 2
        )

    assert _masked_score_tree(transposed_score) == _masked_score_tree(original_score)
    assert [ET.tostring(item) for item in transposed_score.iter("Accidental")] == [
        ET.tostring(item) for item in original_score.iter("Accidental")
    ]

    assert report.notes_changed > 0
    assert concert_keys == [5]
    assert report.key_signatures_changed == 1
    assert report.score_entries_changed >= 1
