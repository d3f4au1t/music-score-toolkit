"""Opt-in round-trip tests against the installed MuseScore desktop exporter."""

import os
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from music_score_toolkit import retarget_instrument_mscz, transpose_mscz
from music_score_toolkit.tools import convert_score

pytestmark = pytest.mark.skipif(
    os.environ.get("MUSIC_SCORE_MUSESCORE_TESTS") != "1",
    reason="Set MUSIC_SCORE_MUSESCORE_TESTS=1 to run the installed MuseScore exporter.",
)


def sounding_notes(root):
    result = []
    steps = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    for part in root.findall("part"):
        shift, divisions = 0, 1
        for measure in part.findall("measure"):
            for element in measure:
                if element.tag == "attributes":
                    divisions = int(element.findtext("divisions", str(divisions)))
                    transposition = element.find("transpose")
                    if transposition is not None:
                        shift = int(transposition.findtext("chromatic", "0")) + 12 * int(
                            transposition.findtext("octave-change", "0"),
                        )
                pitch = element.find("pitch") if element.tag == "note" else None
                if pitch is not None:
                    sound = 12 * (int(pitch.findtext("octave")) + 1)
                    sound += steps[pitch.findtext("step")] + Fraction(pitch.findtext("alter", "0"))
                    result.append((measure.get("number"), element.findtext("voice"),
                                   Fraction(int(element.findtext("duration")), divisions),
                                   sound + shift))
    return result


def test_modulating_score_keeps_all_voices_keys_chords_and_markings(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "modulating-two-voices.musicxml"
    source, bb, raised, restored = [tmp_path / f"{name}.mscz" for name in (
        "source", "bb", "raised", "restored",
    )]
    convert_score(fixture, source)
    baseline = tmp_path / "baseline.musicxml"
    convert_score(source, baseline)
    original = ET.parse(baseline).getroot()
    notes = sounding_notes(original)
    assert len(notes) == 20
    retarget_instrument_mscz(source, bb)
    transpose_mscz(bb, raised, "C", "D")
    retarget_instrument_mscz(bb, restored, "C")
    for score, shift, keys, chords in [
        (bb, 0, [2, 5, -5, -3], [("D", "0"), ("B", "0"), ("D", "-1"), ("E", "-1")]),
        (raised, 2, [4, -5, -3, -1], [("E", "0"), ("D", "-1"), ("E", "-1"), ("F", "0")]),
        (restored, 0, [0, 3, 5, 7], [("C", "0"), ("A", "0"), ("B", "0"), ("C", "1")]),
    ]:
        output = score.with_suffix(".musicxml")
        convert_score(score, output)
        root = ET.parse(output).getroot()
        assert sounding_notes(root) == [(m, v, d, pitch + shift) for m, v, d, pitch in notes]
        assert [int(k.text) for k in root.findall(".//attributes/key/fifths")] == keys
        assert [(h.findtext("root/root-step"), h.findtext("root/root-alter", "0"))
                for h in root.iter("harmony")] == chords
        for tag in ("work-title", "words", "per-minute"):
            assert [e.text for e in root.iter(tag)] == [e.text for e in original.iter(tag)]
        assert [e.tag for d in root.iter("dynamics") for e in d] == [
            e.tag for d in original.iter("dynamics") for e in d
        ]
