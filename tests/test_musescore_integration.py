"""Opt-in round-trip tests against the installed MuseScore desktop exporter."""

import copy
import os
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from music_score_toolkit import retarget_instrument_mscz, transpose_mscz
from music_score_toolkit.export_keys import native_key_plan
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


def native_written_keys(root):
    staff = root.find("Score/Staff")
    first_voice = staff.find("Measure/voice")
    keys = []
    for item in first_voice:
        if item.tag == "KeySig":
            break
        if item.tag == "Chord":
            keys.append(0)  # MuseScore may omit the initial C signature.
            break
    keys.extend(int(k.findtext("actualKey", k.findtext("concertKey", "0")))
                for k in staff.iter("KeySig"))
    return keys


def exported_key_events(root):
    """Read key positions independently of the repair implementation."""
    part = root.find('part')
    staff_count = max([int(s.text) for s in part.findall('.//attributes/staves')] or [1])
    events = {staff: [] for staff in range(1, staff_count + 1)}
    divisions = 1
    for index, measure in enumerate(part.findall('measure')):
        position = Fraction(0)
        for item in measure:
            if item.tag == 'attributes':
                divisions = int(item.findtext('divisions', str(divisions)))
                for key in item.findall('key'):
                    staves = [int(key.get('number'))] if 'number' in key.attrib else list(events)
                    for staff in staves:
                        events[staff].append((index, position, int(key.findtext('fifths'))))
            elif item.tag in {'note', 'backup', 'forward'}:
                if item.find('chord') is None and item.find('grace') is None:
                    duration = Fraction(int(item.findtext('duration')), 4 * divisions)
                    position += -duration if item.tag == 'backup' else duration
    return events


@pytest.mark.parametrize("layout", ["two_voices", "single_voice", "polyphonic", "tuplets", "two_staves", "concert_pitch"])
def test_modulating_score_keeps_all_voices_keys_chords_and_markings(tmp_path, layout):
    fixture = Path(__file__).parent / "fixtures" / "modulating-two-voices.musicxml"
    if layout != "two_voices":
        # Put the first note of each section into one measure. This exercises
        # actual in-measure modulation serialization, not just synthetic MSCX.
        tree = ET.parse(fixture)
        part = tree.getroot().find("part")
        measures = list(part)
        contents = []
        bass_notes = []
        for measure in measures:
            first_note = measure.find("note")
            first_note.find("duration").text = "12"
            bass_notes.append(copy.deepcopy(measure.findall("note")[-1]))
            contents.extend(child for child in measure if child.tag != "backup"
                            and (child.tag != "note" or child is first_note))
            part.remove(measure)
        measure = ET.SubElement(part, "measure", {"number": "1"})
        measure.extend(contents)
        measure.find("attributes/divisions").text = "12"
        if layout in {"polyphonic", "tuplets", "two_staves"}:
            ET.SubElement(ET.SubElement(measure, "backup"), "duration").text = "48"
            if layout != "tuplets":
                rhythm = [(bass, 12, "quarter") for bass in bass_notes]
            else:
                rhythm = [(bass_notes[0], 4, "eighth")] * 3 + [
                    (bass_notes[1], 18, "quarter"), (bass_notes[2], 6, "eighth"),
                    (bass_notes[3], 12, "quarter"),
                ]
            for index, (bass, duration, kind) in enumerate(rhythm):
                bass = copy.deepcopy(bass)
                bass.find("duration").text = str(duration)
                bass.find("type").text = kind
                if layout == "tuplets" and index < 3:
                    modification = ET.SubElement(bass, "time-modification")
                    ET.SubElement(modification, "actual-notes").text = "3"
                    ET.SubElement(modification, "normal-notes").text = "2"
                    ET.SubElement(modification, "normal-type").text = "eighth"
                    if index in (0, 2):
                        ET.SubElement(ET.SubElement(bass, "notations"), "tuplet", {
                            "type": "start" if index == 0 else "stop", "number": "1",
                        })
                if layout == "tuplets" and index == 3:
                    bass.insert(list(bass).index(bass.find("type")) + 1, ET.Element("dot"))
                if layout == "two_staves":
                    bass.find("voice").text = "5"
                    ET.SubElement(bass, "staff").text = "2"
                measure.append(bass)
            if layout == "two_staves":
                attributes = measure.find("attributes")
                attributes.insert(list(attributes).index(attributes.find("clef")), ET.Element("staves"))
                attributes.find("staves").text = "2"
                attributes.find("clef").set("number", "1")
                clef = ET.SubElement(attributes, "clef", {"number": "2"})
                ET.SubElement(clef, "sign").text = "F"
                ET.SubElement(clef, "line").text = "4"
        fixture = tmp_path / f"{layout}.musicxml"
        tree.write(fixture, encoding="utf-8", xml_declaration=True)
    source, bb, raised, restored = [tmp_path / f"{name}.mscz" for name in (
        "source", "bb", "raised", "restored",
    )]
    convert_score(fixture, source)
    baseline = tmp_path / "baseline.musicxml"
    convert_score(source, baseline)
    original = ET.parse(baseline).getroot()
    notes = sounding_notes(original)
    assert len(notes) == {"two_voices": 20, "single_voice": 4, "polyphonic": 8, "tuplets": 10,
                          "two_staves": 8, "concert_pitch": 4}[layout]
    retarget_instrument_mscz(source, bb)
    transpose_mscz(bb, raised, "C", "D")
    retarget_instrument_mscz(bb, restored, "C")
    if layout == "concert_pitch":
        # An instrument can be shown at concert pitch. MuseScore exports that
        # view, so the inserted signatures must be concert rather than written.
        native = tmp_path / 'concert.mscx'
        convert_score(bb, native)
        tree = ET.parse(native)
        score = tree.getroot().find('Score')
        style = score.find('Style')
        if style is None:
            style = ET.SubElement(score, 'Style')
        concert = style.find('concertPitch')
        if concert is None:
            concert = ET.SubElement(style, 'concertPitch')
        concert.text = '1'
        # MuseScore's actualKey stores the current displayed signature. A
        # real view switch updates it along with the style flag.
        for key in score.iter('KeySig'):
            actual = key.find('actualKey')
            if actual is not None:
                actual.text = key.findtext('concertKey')
        tree.write(native, encoding='utf-8', xml_declaration=True)
        output = tmp_path / 'concert.musicxml'
        convert_score(native, output)
        root = ET.parse(output).getroot()
        assert root.find('defaults/concert-score') is not None
        assert sounding_notes(root) == notes
        assert [int(k.text) for k in root.findall('.//key/fifths')] == [0, 3, 5, 7]
    for score, shift, keys, chords in [
        (bb, 0, [2, 5, -5, -3], [("D", "0"), ("B", "0"), ("D", "-1"), ("E", "-1")]),
        (raised, 2, [4, -5, -3, -1], [("E", "0"), ("D", "-1"), ("E", "-1"), ("F", "0")]),
        (restored, 0, [0, 3, 5, 7], [("C", "0"), ("A", "0"), ("B", "0"), ("C", "1")]),
    ]:
        output = score.with_suffix(".musicxml")
        convert_score(score, output)
        root = ET.parse(output).getroot()
        assert sounding_notes(root) == [(m, v, d, pitch + shift) for m, v, d, pitch in notes]
        native = score.with_name(f"{score.stem}-reopened.mscx")
        convert_score(score, native)
        native_root = ET.parse(native).getroot()
        assert native_written_keys(native_root) == keys
        # Every note of the last voice is a local tonic in this fixture. Verify
        # its actual spelling, not only sounding pitch and printed signatures.
        spelled = [int(note.findtext("tpc2", note.findtext("tpc")))
                   for measure in native_root.findall("Score/Staff/Measure")
                   for note in measure.findall("voice")[-1].iter("Note")]
        expected_spelling = [14 + value for value in keys]
        if layout == "tuplets":
            expected_spelling = expected_spelling[:1] * 3 + expected_spelling[1:]
        assert spelled == expected_spelling * (2 if layout == "two_staves" else 1)
        expected_events = [(i if layout == 'two_voices' else 0,
                            Fraction(0) if layout == 'two_voices' else Fraction(i, 4), key)
                           for i, key in enumerate(keys)]
        assert exported_key_events(root) == {
            staff: expected_events for staff in range(1, 3 if layout == 'two_staves' else 2)
        }
        assert [(h.findtext("root/root-step"), h.findtext("root/root-alter", "0"))
                for h in root.iter("harmony")] == chords
        for tag in ("work-title", "words", "per-minute"):
            assert [e.text for e in root.iter(tag)] == [e.text for e in original.iter(tag)]
        assert [e.tag for d in root.iter("dynamics") for e in d] == [
            e.tag for d in original.iter("dynamics") for e in d
        ]
        if score == bb:
            compressed = tmp_path / 'bb.mxl'
            convert_score(score, compressed)
            # Re-import both formats using the real application. Verify native
            # key positions and pitches, not just XML element presence.
            for exported in (output, compressed):
                imported = tmp_path / f'reimported-{exported.suffix[1:]}.mscx'
                convert_score(exported, imported)
                assert native_written_keys(ET.parse(imported).getroot()) == keys
                expected_plan = native_key_plan(native)
                actual_plan = native_key_plan(imported)
                assert len(actual_plan) == len(expected_plan)
                for actual, expected in zip(actual_plan, expected_plan):
                    assert actual.staff_count == expected.staff_count
                    assert actual.measure_count == expected.measure_count
                    assert actual.measures.keys() == expected.measures.keys()
                    for index, measure in expected.measures.items():
                        reopened = actual.measures[index]
                        assert reopened.length == measure.length
                        assert reopened.notes == measure.notes
                        # MusicXML stores written keys, not the independent
                        # concert enharmonic spelling. MuseScore may derive
                        # concert C-flat from written D-flat instead of B.
                        assert [(k.staff, k.position, k.written, k.mode, k.visible, k.concert % 12)
                                for k in reopened.keys] == [
                                    (k.staff, k.position, k.written, k.mode, k.visible, k.concert % 12)
                                    for k in measure.keys
                                ]
