"""Local key spelling must follow musical position, not XML voice order."""

import xml.etree.ElementTree as ET
import zipfile

import pytest

from music_score_toolkit import (
    ScoreFormatError,
    retarget_instrument_mscx,
    retarget_instrument_mscz,
    transpose_mscx,
    transpose_mscz,
)
from music_score_toolkit.instruments import INSTRUMENT_INTERVALS
from music_score_toolkit.keys import KEY_SIGNATURES, calculate_shift, tpc_pitch_class


def note(pitch=60, tpc=14, written=16):
    return (f"<Chord><durationType>quarter</durationType><Note><pitch>{pitch}</pitch>"
            f"<tpc>{tpc}</tpc><tpc2>{written}</tpc2></Note></Chord>")


def key(concert, written):
    return f"<KeySig><concertKey>{concert}</concertKey><actualKey>{written}</actualKey></KeySig>"


def score(measures, *, concert=False, instrument="Bb"):
    diatonic, chromatic = INSTRUMENT_INTERVALS[instrument]
    return f"""<museScore version="4.0"><Score>
      <Style><concertPitch>{int(concert)}</concertPitch></Style>
      <Part><Staff id="1"/><Instrument><transposeDiatonic>{diatonic}</transposeDiatonic>
        <transposeChromatic>{chromatic}</transposeChromatic></Instrument></Part>
      <Staff id="1">{measures}</Staff></Score></museScore>"""


def transform(xml, operation):
    if operation == "instrument":
        return retarget_instrument_mscx(xml, "C")
    return transpose_mscx(xml, "C", "D")


@pytest.mark.parametrize("operation", ["transpose", "instrument"])
def test_barline_key_in_later_voice_applies_to_all_voices_and_next_measure(operation):
    xml = score(
        f"<Measure><voice>{key(0, 2)}{note()}</voice></Measure>"
        f"<Measure><voice>{note(59, 19, 9)}</voice>"
        f"<voice>{key(5, -5)}{note(71, 19, 9)}</voice></Measure>"
        f"<Measure><voice>{note(59, 19, 9)}</voice></Measure>"
    )
    rendered, report = transform(xml, operation)
    root = ET.fromstring(rendered)
    expected = [18, 11, 11, 11] if operation == "transpose" else [14, 19, 19, 19]
    assert [int(n.findtext("tpc2")) for n in root.iter("Note")] == expected
    assert report.key_signatures_changed == 2


def test_opening_key_in_second_voice_is_not_implicit_c():
    xml = score(f"<Measure><voice>{note(59, 19, 9)}</voice>"
                f"<voice>{key(5, -5)}{note(71, 19, 9)}</voice></Measure>")
    rendered, _ = transpose_mscx(xml, "B", "C")
    root = ET.fromstring(rendered)
    assert len(list(root.iter("KeySig"))) == 1
    assert [n.findtext("tpc2") for n in root.iter("Note")] == ["16", "16"]


@pytest.mark.parametrize("operation", ["transpose", "instrument"])
def test_single_voice_interior_key_and_harmony_at_same_onset(operation):
    xml = score(f"""<Measure><voice>{key(0, 2)}{note()}
      <Harmony><root>9</root><base>13</base><name>maj7</name></Harmony>
      {key(5, -5)}<Dynamic><subtype>ff</subtype></Dynamic>{note(59, 19, 9)}
      <Rest><durationType>half</durationType></Rest></voice></Measure>
      <Measure><voice>{note(71, 19, 9)}</voice></Measure>""")
    rendered, _ = transform(xml, operation)
    root = ET.fromstring(rendered)
    expected = [18, 11, 11] if operation == "transpose" else [14, 19, 19]
    assert [int(n.findtext("tpc2")) for n in root.iter("Note")] == expected
    assert root.findtext(".//Harmony/root") == ("11" if operation == "transpose" else "19")
    assert root.findtext(".//Harmony/base") == ("15" if operation == "transpose" else "23")
    assert root.findtext(".//Harmony/name") == "maj7"
    assert root.findtext(".//Dynamic/subtype") == "ff"
    assert root.findtext(".//Rest/durationType") == "half"


@pytest.mark.parametrize("concert", [False, True])
def test_legacy_modulations_derive_concert_key_using_instrument_and_view(concert):
    xml = score(f"<Measure><KeySig><accidental>{0 if concert else 2}</accidental></KeySig>"
                f"{note()}</Measure><Measure><KeySig><accidental>"
                f"{3 if concert else 5}</accidental><mode>minor</mode><showCourtesy>0</showCourtesy>"
                f"</KeySig>{note(69, 17, 19)}</Measure>", concert=concert)
    rendered, _ = retarget_instrument_mscx(xml, "Eb")
    root = ET.fromstring(rendered)
    assert [k.findtext("concertKey") for k in root.iter("KeySig")] == ["0", "3"]
    assert [k.findtext("actualKey") for k in root.iter("KeySig")] == ["3", "6"]
    assert [n.findtext("tpc2") for n in root.iter("Note")] == ["17", "20"]
    assert root.find(".//KeySig/accidental") is None
    assert root.findtext(".//KeySig/mode") == "minor"
    assert root.findtext(".//KeySig/showCourtesy") == "0"


@pytest.mark.parametrize("operation", ["transpose", "instrument"])
@pytest.mark.parametrize("extra", [
    "</voice><voice><Rest><durationType>whole</durationType></Rest>",
    "<location><fractions>-1/4</fractions></location>",
    "<tick>480</tick>",
])
def test_ambiguous_interior_changes_fail_closed(operation, extra):
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}{key(5, -5)}"
                f"{note(59, 19, 9)}{extra}</voice></Measure>")
    with pytest.raises(ScoreFormatError, match="tick-aware"):
        transform(xml, operation)
    # An unchanged-key operation remains a byte-preserving inspection.
    rendered, report = transpose_mscx(xml, "C", "C")
    assert rendered == xml.encode()
    assert report.score_entries_changed == 0


def test_nested_spanner_location_is_not_a_voice_cursor_move():
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}"
                "<Spanner type='Slur'><next><location><fractions>1/4</fractions>"
                "</location></next></Spanner>"
                f"{key(5, -5)}{note(59, 19, 9)}</voice></Measure>")
    rendered, _ = transpose_mscx(xml, "C", "D")
    root = ET.fromstring(rendered)
    assert root.findtext(".//Spanner/next/location/fractions") == "1/4"
    assert [n.findtext("tpc2") for n in root.iter("Note")] == ["18", "11"]


@pytest.mark.parametrize("operation", ["transpose", "instrument"])
def test_conflicting_simultaneous_keys_are_rejected(operation):
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}</voice>"
                f"<voice>{key(5, -5)}{note(59, 19, 9)}</voice></Measure>")
    with pytest.raises(ScoreFormatError, match="Conflicting key signatures"):
        transform(xml, operation)


@pytest.mark.parametrize("operation", ["transpose", "instrument"])
def test_identical_simultaneous_keys_do_not_double_transpose_notes(operation):
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}</voice>"
                f"<voice>{key(0, 2)}{note()}</voice></Measure>")
    rendered, _ = transform(xml, operation)
    root = ET.fromstring(rendered)
    assert [n.findtext("pitch") for n in root.iter("Note")] == (
        ["62", "62"] if operation == "transpose" else ["60", "60"]
    )


@pytest.mark.parametrize("extra", [
    "<unknown><KeySig><concertKey>5</concertKey></KeySig></unknown>",
    "<unknown><Chord><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord></unknown>",
])
def test_unknown_nested_musical_positions_fail_closed(extra):
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}{extra}</voice></Measure>")
    with pytest.raises(ScoreFormatError, match="position"):
        retarget_instrument_mscx(xml)


def test_mixed_voice_and_measure_music_cannot_be_silently_skipped():
    xml = score(f"<Measure>{note()}<voice>{key(0, 2)}{note()}{key(5, -5)}"
                f"{note(59, 19, 9)}</voice></Measure>")
    with pytest.raises(ScoreFormatError, match="measure-scoped"):
        retarget_instrument_mscx(xml)


@pytest.mark.parametrize("operation", ["transpose", "instrument"])
def test_unsupported_key_timing_leaves_existing_archive_untouched(tmp_path, operation):
    source, destination = tmp_path / "source.mscz", tmp_path / "result.mscz"
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}{key(5, -5)}{note(59, 19, 9)}"
                "</voice><voice><Rest/></voice></Measure>")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", xml)
    destination.write_bytes(b"previous output")
    with pytest.raises(ScoreFormatError, match="Mid-measure"):
        if operation == "transpose":
            transpose_mscz(source, destination, "C", "D")
        else:
            retarget_instrument_mscz(source, destination)
    assert destination.read_bytes() == b"previous output"


def folded(signature):
    while signature < -7:
        signature += 12
    while signature > 7:
        signature -= 12
    return signature


def written_key(signature, instrument):
    diatonic, chromatic = INSTRUMENT_INTERVALS[instrument]
    result = folded(signature - (chromatic * 7 - diatonic * 12))
    return -5 if instrument != "C" and result == 7 else result


@pytest.mark.parametrize("source_instrument", list(INSTRUMENT_INTERVALS))
@pytest.mark.parametrize("concert", [False, True])
def test_all_conventional_modulations_preserve_pitch_and_match_local_signatures(
    source_instrument, concert,
):
    for modulation in KEY_SIGNATURES.values():
        measures = []
        for signature in (0, modulation, 0):
            actual = written_key(signature, source_instrument)
            tonic = 14 + signature
            root = tonic if concert else 14 + actual
            measures.append(f"<Measure><voice>{key(signature, actual)}"
                            f"<Harmony><root>{root}</root></Harmony>"
                            f"{note(60 + tpc_pitch_class(tonic), tonic, 14 + actual)}"
                            "</voice></Measure>")
        xml = score("".join(measures), concert=concert, instrument=source_instrument)
        source_pitches = [int(n.findtext("pitch")) for n in ET.fromstring(xml).iter("Note")]
        for target, shift in KEY_SIGNATURES.items():
            rendered, _ = transpose_mscx(xml, "C", target)
            root = ET.fromstring(rendered)
            for index, measure in enumerate(root.findall("./Score/Staff/Measure")):
                local = (0, modulation, 0)[index]
                expected = folded(local + shift)
                actual = written_key(expected, source_instrument)
                assert measure.findtext(".//KeySig/concertKey") == str(expected)
                assert measure.findtext(".//KeySig/actualKey") == str(actual)
                assert measure.findtext(".//Note/tpc") == str(14 + expected)
                # A true C->C no-op retains the source spellings exactly.
                assert measure.findtext(".//Note/tpc2") == str(14 + actual)
                assert int(measure.findtext(".//Note/pitch")) == (
                    source_pitches[index] + calculate_shift("C", target)
                )
                assert measure.findtext(".//Harmony/root") == str(14 + (expected if concert else actual))
        for target in INSTRUMENT_INTERVALS:
            rendered, _ = retarget_instrument_mscx(xml, target)
            repeated, report = retarget_instrument_mscx(rendered, target)
            assert repeated == rendered
            assert report.score_entries_changed == 0
            root = ET.fromstring(rendered)
            assert [int(n.findtext("pitch")) for n in root.iter("Note")] == source_pitches
            for index, measure in enumerate(root.findall("./Score/Staff/Measure")):
                local = (0, modulation, 0)[index]
                expected = written_key(local, target)
                assert measure.findtext(".//Note/tpc") == str(14 + local)
                assert measure.findtext(".//Note/tpc2") == str(14 + expected)
                assert measure.findtext(".//KeySig/actualKey") == str(expected)
                assert measure.findtext(".//Harmony/root") == str(14 + expected)


@pytest.mark.parametrize("operation", ["transpose", "instrument"])
def test_trailing_courtesy_signature_is_not_treated_as_next_measures_key(operation):
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}{key(5, -5)}</voice></Measure>"
                f"<Measure><voice>{note()}</voice></Measure>")
    with pytest.raises(ScoreFormatError, match="courtesy"):
        transform(xml, operation)


def test_nontransposing_mid_measure_changes_keep_working_without_local_respelling():
    xml = score(f"<Measure><voice>{key(0, 0)}{note(written=14)}{key(1, 1)}"
                f"{note(67, 15, 15)}</voice><voice>{note(written=14)}</voice></Measure>",
                instrument="C")
    rendered, _ = transpose_mscx(xml, "C", "D")
    root = ET.fromstring(rendered)
    assert [n.findtext("tpc") for n in root.iter("Note")] == ["16", "17", "16"]


@pytest.mark.parametrize("grace", ["acciaccatura", "appoggiatura", "grace4", "grace16after"])
def test_grace_chords_share_key_at_following_normal_chords_onset(grace):
    grace_note = note(59, 19, 9).replace("<Chord>", f"<Chord><{grace}/>")
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}{grace_note}"
                f"{key(5, -5)}{note(71, 19, 9)}</voice></Measure>")
    rendered, _ = transpose_mscx(xml, "C", "D")
    root = ET.fromstring(rendered)
    assert [n.findtext("tpc2") for n in root.iter("Note")] == ["18", "11", "11"]
    assert root.find(f".//{grace}") is not None


def test_grace_before_initial_key_does_not_make_key_look_like_a_modulation():
    grace_note = note(59, 19, 9).replace("<Chord>", "<Chord><acciaccatura/>")
    xml = score(f"<Measure><voice>{grace_note}{key(5, -5)}{note(71, 19, 9)}</voice></Measure>")
    rendered, _ = transpose_mscx(xml, "B", "C")
    root = ET.fromstring(rendered)
    assert [n.findtext("tpc2") for n in root.iter("Note")] == ["16", "16"]
    assert len(list(root.iter("KeySig"))) == 1


def test_cursor_cannot_move_notes_across_a_barline_key_change():
    xml = score(f"<Measure><voice>{key(0, 2)}{note()}</voice></Measure>"
                f"<Measure><voice>{key(5, -5)}<location><fractions>-1/4</fractions>"
                f"</location>{note()}</voice></Measure>")
    with pytest.raises(ScoreFormatError, match="explicit cursor"):
        retarget_instrument_mscx(xml)


@pytest.mark.parametrize("operation", ["transpose", "instrument"])
def test_each_staff_tracks_its_own_modulations(operation):
    root = ET.fromstring(score(f"<Measure>{key(0, 2)}{note()}</Measure>"
                               f"<Measure>{key(5, -5)}{note(59, 19, 9)}</Measure>"))
    root.find("Score/Part").insert(1, ET.fromstring('<Staff id="2"/>'))
    root.find("Score").append(ET.fromstring(
        f'<Staff id="2"><Measure>{key(0, 2)}{note()}</Measure>'
        f'<Measure>{key(3, 5)}{note(69, 17, 19)}</Measure></Staff>'
    ))
    rendered, _ = transform(ET.tostring(root), operation)
    staves = ET.fromstring(rendered).findall("Score/Staff")
    assert [[n.findtext("tpc2") for n in staff.iter("Note")] for staff in staves] == (
        [["18", "11"], ["18", "9"]] if operation == "transpose"
        else [["14", "19"], ["14", "17"]]
    )
