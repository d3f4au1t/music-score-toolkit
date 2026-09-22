import os
import stat
import struct
import warnings
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from music_score_toolkit.mscz import (
    PitchRangeError,
    ScoreFormatError,
    transpose_mscx,
    transpose_mscz,
)

SCORE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<museScore version="4.0">
  <Score>
    <Staff><Measure>
      <KeySig><accidental>-2</accidental></KeySig>
      <Chord id="keep-me"><durationType>quarter</durationType>
        <Note><pitch>70</pitch><tpc>12</tpc><accidental>flat</accidental>
          <Accidental><subtype>accidentalFlat</subtype><eid>keep-accidental</eid></Accidental>
          <Tie/>
        </Note>
        <Note><pitch>74</pitch><tpc>16</tpc></Note>
      </Chord>
      <Rest><durationType>half</durationType></Rest>
    </Measure></Staff>
  </Score>
</museScore>
"""

MUSESCORE_4_KEY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<museScore version="4.6">
  <Score><Staff>
    <Measure>
      <KeySig><concertKey>0</concertKey><actualKey>2</actualKey></KeySig>
      <Chord><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord>
    </Measure>
    <Measure>
      <KeySig><concertKey>-1</concertKey></KeySig>
      <Dynamic><subtype>f</subtype></Dynamic>
    </Measure>
  </Staff></Score>
</museScore>
"""

CONTAINER_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<container><rootfiles><rootfile full-path="score.mscx"/></rootfiles></container>
"""


def test_transposes_notes_and_key_without_removing_structure():
    rendered, report = transpose_mscx(SCORE_XML, "Bb", "C")
    root = ET.fromstring(rendered)

    assert [int(item.text) for item in root.iter("pitch")] == [72, 76]
    assert root.find(".//KeySig/accidental").text == "0"
    assert root.find(".//Chord").attrib["id"] == "keep-me"
    assert root.find(".//Tie") is not None
    assert root.find(".//Rest/durationType").text == "half"
    assert root.find(".//Note/accidental").text == "flat"
    assert root.find(".//Note/Accidental/subtype").text == "accidentalNatural"
    assert root.find(".//Note/Accidental/eid").text == "keep-accidental"
    assert report.notes_changed == 2
    assert report.key_signatures_changed == 1
    assert report.semitone_shift == 2


def test_musescore_4_key_signatures_follow_whole_step_and_preserve_key_changes():
    rendered, report = transpose_mscx(MUSESCORE_4_KEY_XML, "C", "D")
    root = ET.fromstring(rendered)

    assert [int(item.text) for item in root.iter("pitch")] == [62]
    assert [int(item.text) for item in root.iter("concertKey")] == [2, 1]
    assert [int(item.text) for item in root.iter("actualKey")] == [4]
    assert root.find(".//Dynamic/subtype").text == "f"
    assert report.key_signatures_changed == 2


def test_custom_key_signature_transposes_base_key_and_preserves_definition():
    xml = b"""<museScore><Score><KeySig><custom>1</custom><concertKey>0</concertKey>
    <CustDef><sym>accidentalSharp</sym></CustDef></KeySig></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")

    root = ET.fromstring(rendered)

    assert root.find(".//concertKey").text == "2"
    assert root.find(".//CustDef/sym").text == "accidentalSharp"
    assert report.key_signatures_changed == 1


def test_atonal_key_signature_is_left_untouched_while_notes_transpose():
    xml = b"""<museScore><Score><KeySig><custom>1</custom><mode>none</mode></KeySig>
    <Note><pitch>60</pitch><tpc>14</tpc></Note></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    root = ET.fromstring(rendered)

    assert root.findtext(".//mode") == "none"
    assert root.findtext(".//pitch") == "62"
    assert report.key_signatures_changed == 0


def test_preserves_explicit_flat_spelling_across_named_interval():
    rendered, _ = transpose_mscx(SCORE_XML, "Bb", "Db")
    tpcs = [int(item.text) for item in ET.fromstring(rendered).iter("tpc")]
    assert tpcs == [9, 13]
    assert ET.fromstring(rendered).find(".//Accidental/subtype").text == "accidentalFlat"


@pytest.mark.parametrize(
    ("subtype", "pitch", "tpc"),
    [
        ("accidentalNaturalSharp", 68, 22),
        ("accidentalSharpSharp", 69, 29),
    ],
)
def test_preserves_alternate_accidental_glyph_when_alteration_is_unchanged(
    subtype: str,
    pitch: int,
    tpc: int,
):
    xml = f"""<museScore><Score><Note>
    <Accidental><subtype>{subtype}</subtype></Accidental>
    <pitch>{pitch}</pitch><tpc>{tpc}</tpc>
    </Note></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "D")

    assert ET.fromstring(rendered).findtext(".//Accidental/subtype") == subtype


def test_preserves_standard_accidental_that_matches_neither_stored_tpc():
    xml = b"""<museScore><Score><Note>
    <Accidental><subtype>accidentalFlat</subtype></Accidental>
    <pitch>60</pitch><tpc>14</tpc>
    </Note></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "D")

    assert ET.fromstring(rendered).findtext(".//Accidental/subtype") == "accidentalFlat"


def test_transposes_concert_and_written_tpc_without_adding_missing_tpc2():
    xml = b"""<museScore><Score>
    <Note><pitch>58</pitch><tpc>12</tpc><tpc2>14</tpc2></Note>
    <Note><pitch>66</pitch><tpc>20</tpc></Note>
    </Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    notes = list(ET.fromstring(rendered).iter("Note"))

    assert [notes[0].findtext(field) for field in ("pitch", "tpc", "tpc2")] == [
        "60",
        "14",
        "16",
    ]
    assert notes[1].findtext("tpc") == "22"
    assert notes[1].find("tpc2") is None
    assert report.notes_changed == 2


def test_missing_primary_tpc_is_rejected_instead_of_guessing_spelling():
    xml = b"<museScore><Score><Note><pitch>60</pitch></Note></Score></museScore>"

    with pytest.raises(ScoreFormatError, match="primary TPC"):
        transpose_mscx(xml, "C", "D")


def test_written_tpc_controls_ambiguous_accidental_by_default():
    xml = b"""<museScore><Score><Note>
    <Accidental><subtype>accidentalSharp</subtype></Accidental>
    <pitch>61</pitch><tpc>21</tpc><tpc2>23</tpc2>
    </Note></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "B")
    root = ET.fromstring(rendered)

    assert root.findtext(".//tpc") == "26"
    assert root.findtext(".//tpc2") == "28"
    assert root.findtext(".//Accidental/subtype") == "accidentalDoubleSharp"


def test_inline_concert_pitch_style_controls_ambiguous_accidental():
    xml = b"""<museScore><Score>
    <Style><concertPitch>1</concertPitch></Style>
    <Note><Accidental><subtype>accidentalSharp</subtype></Accidental>
      <pitch>61</pitch><tpc>21</tpc><tpc2>23</tpc2></Note>
    </Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "B")

    assert ET.fromstring(rendered).findtext(".//Accidental/subtype") == "accidentalSharp"


def test_nested_score_inherits_outer_concert_pitch_style():
    xml = b"""<museScore><Score>
    <Style><concertPitch>1</concertPitch></Style>
    <Excerpt><Score><Note>
      <Accidental><subtype>accidentalSharp</subtype></Accidental>
      <pitch>61</pitch><tpc>21</tpc><tpc2>23</tpc2>
    </Note></Score></Excerpt>
    </Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "B")

    assert ET.fromstring(rendered).findtext(".//Accidental/subtype") == "accidentalSharp"


@pytest.mark.parametrize("value", ["", "yes", "2"])
def test_invalid_inline_concert_pitch_style_is_rejected(value: str):
    xml = f"""<museScore><Score>
    <Style><concertPitch>{value}</concertPitch></Style>
    <Note><pitch>60</pitch><tpc>14</tpc></Note>
    </Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="concertPitch"):
        transpose_mscx(xml, "C", "D")


def test_true_no_op_is_byte_identical_and_preserves_enharmonic_spelling():
    xml = b"""<museScore><Score><Note>
    <Accidental><subtype>accidentalFlat</subtype><eid>A</eid></Accidental>
    <pitch>61</pitch><tpc>9</tpc><tpc2>11</tpc2>
    </Note></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "C")

    assert rendered == xml
    assert report.notes_changed == 0
    assert report.key_signatures_changed == 0
    assert report.score_entries_changed == 0


@pytest.mark.parametrize(
    "invalid_field",
    ["<pitch>not-a-pitch</pitch>", "<pitch>60</pitch><tpc>41</tpc>", "<pitch>60</pitch><tpc2>x</tpc2>"],
)
def test_true_no_op_still_validates_note_fields(invalid_field: str):
    xml = f"<museScore><Score><Note>{invalid_field}</Note></Score></museScore>"

    with pytest.raises((ScoreFormatError, PitchRangeError)):
        transpose_mscx(xml, "C", "C")


def test_true_no_op_still_validates_key_signature_fields():
    xml = b"<museScore><Score><KeySig><concertKey>8</concertKey></KeySig></Score></museScore>"

    with pytest.raises(ScoreFormatError):
        transpose_mscx(xml, "C", "C")


def test_unchanged_rest_only_score_is_returned_byte_identically():
    xml = b"<museScore><Score><Rest><durationType>whole</durationType></Rest></Score></museScore>"

    rendered, report = transpose_mscx(xml, "C", "D")

    assert rendered == xml
    assert report.score_entries_changed == 0


def test_zero_semitone_enharmonic_change_respells_notes_and_key():
    xml = b"""<museScore><Score>
    <KeySig><concertKey>7</concertKey></KeySig>
    <Note><pitch>61</pitch><tpc>21</tpc></Note>
    </Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C#", "Db")
    root = ET.fromstring(rendered)

    assert root.findtext(".//pitch") == "61"
    assert root.findtext(".//tpc") == "9"
    assert root.findtext(".//concertKey") == "-5"
    assert report.semitone_shift == 0
    assert report.notes_changed == 1
    assert report.key_signatures_changed == 1


def test_user_accidental_subtype_changes_without_losing_metadata():
    xml = b"""<museScore><Score><Note>
    <Accidental><subtype>accidentalSharp</subtype><role>1</role>
      <bracket>1</bracket><eid>A</eid></Accidental>
    <pitch>66</pitch><tpc>20</tpc>
    </Note></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "Db")
    accidental = ET.fromstring(rendered).find(".//Accidental")

    assert ET.fromstring(rendered).findtext(".//pitch") == "67"
    assert ET.fromstring(rendered).findtext(".//tpc") == "15"
    assert accidental.findtext("subtype") == "accidentalNatural"
    assert accidental.findtext("role") == "1"
    assert accidental.findtext("bracket") == "1"
    assert accidental.findtext("eid") == "A"


def test_microtonal_accidental_is_preserved_without_guessing_its_spelling():
    xml = b"""<museScore><Score><Note>
    <Accidental><subtype>accidentalQuarterToneSharpStein</subtype>
      <role>1</role><eid>microtonal</eid></Accidental>
    <pitch>60</pitch><tpc>14</tpc>
    </Note></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "D")
    accidental = ET.fromstring(rendered).find(".//Accidental")

    assert accidental.findtext("subtype") == "accidentalQuarterToneSharpStein"
    assert accidental.findtext("role") == "1"
    assert accidental.findtext("eid") == "microtonal"


def test_rejects_out_of_range_pitch_by_default():
    xml = b"<museScore><Score><Note><pitch>127</pitch><tpc>15</tpc></Note></Score></museScore>"
    with pytest.raises(PitchRangeError):
        transpose_mscx(xml, "C", "D")


def test_allowed_pitch_clipping_keeps_tpc_fields_consistent():
    xml = b"""<museScore><Score><Note>
    <pitch>127</pitch><tpc>15</tpc><tpc2>17</tpc2>
    </Note></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D", strict_pitch_range=False)
    note = ET.fromstring(rendered).find(".//Note")

    assert [note.findtext(field) for field in ("pitch", "tpc", "tpc2")] == [
        "127",
        "15",
        "17",
    ]
    assert report.notes_changed == 0


def test_transposes_archive_and_preserves_other_entries(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("META-INF/container.xml", CONTAINER_XML)
        archive.writestr("Thumbnails/thumbnail.png", b"image-marker")

    report = transpose_mscz(source, output, "Bb", "C")

    with zipfile.ZipFile(output) as archive:
        assert archive.read("META-INF/container.xml") == CONTAINER_XML
        assert archive.read("Thumbnails/thumbnail.png") == b"image-marker"
        pitches = [int(item.text) for item in ET.fromstring(archive.read("score.mscx")).iter("pitch")]
    assert pitches == [72, 76]
    assert report.score_entries_changed == 1


def test_archive_style_controls_written_or_concert_accidental(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    score = b"""<museScore><Score><Note>
    <Accidental><subtype>accidentalSharp</subtype></Accidental>
    <pitch>61</pitch><tpc>21</tpc><tpc2>23</tpc2>
    </Note></Score></museScore>"""
    style = b"<museScore><Style><concertPitch>1</concertPitch></Style></museScore>"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", score)
        archive.writestr("score_style.mss", style)

    report = transpose_mscz(source, output, "C", "B")

    with zipfile.ZipFile(output) as archive:
        root = ET.fromstring(archive.read("score.mscx"))
    assert root.findtext(".//Accidental/subtype") == "accidentalSharp"
    assert report.score_entries_changed == 1


@pytest.mark.parametrize(
    "style",
    [
        b"not XML",
        b"<Style><concertPitch>1</concertPitch></Style>",
        b"<museScore><Style><concertPitch>maybe</concertPitch></Style></museScore>",
    ],
)
def test_invalid_archive_style_is_rejected_atomically(tmp_path: Path, style: bytes):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("score_style.mss", style)
    output.write_bytes(b"existing-output")

    with pytest.raises(ScoreFormatError, match="score_style|concertPitch"):
        transpose_mscz(source, output, "Bb", "C")

    assert output.read_bytes() == b"existing-output"


def test_case_variant_archive_styles_are_rejected_as_ambiguous(tmp_path: Path):
    source = tmp_path / "source.mscz"
    style = b"<museScore><Style><concertPitch>1</concertPitch></Style></museScore>"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("score_style.mss", style)
        archive.writestr("SCORE_STYLE.MSS", style)

    with pytest.raises(ScoreFormatError, match="colliding member|ambiguous score_style"):
        transpose_mscz(source, tmp_path / "out.mscz", "Bb", "C")


def test_score_transposition_rejects_theoretical_key_signature_names():
    with pytest.raises(ValueError, match="conventional major key"):
        transpose_mscx(b"<museScore><Score/></museScore>", "C", "D#")


def test_invalid_archive_is_reported(tmp_path: Path):
    source = tmp_path / "invalid.mscz"
    source.write_text("not a zip")
    with pytest.raises(ScoreFormatError):
        transpose_mscz(source, tmp_path / "out.mscz", "C", "D")


def test_invalid_tpc_preserves_existing_archive_destination(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "score.mscx",
            b"<museScore><Score><Note><pitch>60</pitch><tpc>41</tpc></Note></Score></museScore>",
        )
    output.write_bytes(b"existing-output")

    with pytest.raises(ScoreFormatError):
        transpose_mscz(source, output, "C", "D")

    assert output.read_bytes() == b"existing-output"


def test_staff_aware_transposition_skips_percussion_and_updates_harmony():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument/></Part>
      <Part><Staff id="2"><StaffType group="percussion"/></Staff>
        <Instrument><useDrumset>1</useDrumset></Instrument></Part>
      <Staff id="1"><Measure><voice>
        <KeySig><concertKey>0</concertKey></KeySig>
        <Harmony><root>14</root><base>18</base><name>C/E</name></Harmony>
        <Harmony><harmonyInfo><root>15</root><bass>19</bass><name>G/B</name>
          </harmonyInfo></Harmony>
        <Chord><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord>
      </voice></Measure></Staff>
      <Staff id="2"><Measure><voice><Chord><Note>
        <pitch>41</pitch><tpc>13</tpc><fret>41</fret><string>4</string>
      </Note></Chord></voice></Measure></Staff>
    </Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    root = ET.fromstring(rendered)
    staves = root.findall(".//Score/Staff")

    assert staves[0].findtext(".//pitch") == "62"
    assert staves[0].findtext(".//KeySig/concertKey") == "2"
    harmonies = staves[0].findall(".//Harmony")
    assert [harmonies[0].findtext(name) for name in ("root", "base")] == ["16", "20"]
    assert [harmonies[1].findtext(f".//{name}") for name in ("root", "bass")] == [
        "17",
        "21",
    ]
    assert [staves[1].findtext(f".//{name}") for name in ("pitch", "tpc", "fret", "string")] == [
        "41",
        "13",
        "41",
        "4",
    ]
    assert report.notes_changed == 1
    assert report.chord_symbols_changed == 2


def test_explicit_staff_id_must_match_its_instrument_definition():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="percussion"/></Staff>
        <Instrument><useDrumset>1</useDrumset></Instrument></Part>
      <Staff id="99"><Measure>
        <Note><pitch>60</pitch><tpc>14</tpc></Note>
      </Measure></Staff>
    </Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="staff id '99'.*no matching"):
        transpose_mscx(xml, "C", "D")


def test_staff_scoped_score_rejects_unscoped_notes_instead_of_skipping_them():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument/></Part>
      <Staff id="1"><Measure/></Staff>
      <Note><pitch>60</pitch><tpc>14</tpc></Note>
    </Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="mixes staff-scoped and unscoped"):
        transpose_mscx(xml, "C", "D")


def test_adds_missing_initial_key_signature_and_recomputes_written_pitch():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
      </Instrument></Part>
      <Staff id="1"><Measure><voice><Clef/><TimeSig/>
        <Chord><Note><pitch>60</pitch><tpc>14</tpc><tpc2>16</tpc2></Note></Chord>
      </voice></Measure></Staff>
    </Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    root = ET.fromstring(rendered)
    voice = root.find(".//Score/Staff/Measure/voice")

    assert [child.tag for child in voice] == ["Clef", "KeySig", "TimeSig", "Chord"]
    assert voice.findtext("KeySig/concertKey") == "2"
    assert voice.findtext("KeySig/actualKey") == "4"
    assert [voice.findtext(f".//{name}") for name in ("pitch", "tpc", "tpc2")] == [
        "62",
        "16",
        "18",
    ]
    assert report.key_signatures_changed == 1


def test_adds_missing_written_tpc_for_transposing_instrument():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
      </Instrument></Part>
      <Staff id="1"><Measure><KeySig><concertKey>0</concertKey><actualKey>2</actualKey>
        </KeySig><Note><pitch>60</pitch><tpc>14</tpc></Note></Measure></Staff>
    </Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "D")

    assert ET.fromstring(rendered).findtext(".//Note/tpc2") == "18"


def test_exact_no_op_does_not_add_or_respell_written_tpc():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
      </Instrument></Part>
      <Staff id="1"><Measure><KeySig><concertKey>0</concertKey><actualKey>2</actualKey>
        </KeySig><Note><pitch>60</pitch><tpc>14</tpc></Note>
        <Note><pitch>61</pitch><tpc>21</tpc><tpc2>23</tpc2></Note>
      </Measure></Staff>
    </Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "C")

    assert rendered == xml
    assert report.notes_changed == 0
    assert report.score_entries_changed == 0


def test_b_flat_staff_recomputes_enharmonic_written_notes_and_harmony():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
      </Instrument></Part>
      <Staff id="1"><Measure><voice>
        <KeySig><concertKey>0</concertKey><actualKey>2</actualKey></KeySig>
        <Harmony><root>16</root><name>D</name></Harmony>
        <Chord><Note><pitch>60</pitch><tpc>14</tpc><tpc2>16</tpc2></Note></Chord>
      </voice></Measure></Staff>
    </Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "B")
    root = ET.fromstring(rendered)

    assert [root.findtext(f".//{name}") for name in ("pitch", "tpc", "tpc2")] == [
        "59",
        "19",
        "9",
    ]
    assert [root.findtext(f".//KeySig/{name}") for name in ("concertKey", "actualKey")] == [
        "5",
        "-5",
    ]
    assert root.findtext(".//Harmony/root") == "9"
    assert report.chord_symbols_changed == 1


def test_actual_key_is_recomputed_from_raw_instrument_not_old_enharmonic_relation():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
      </Instrument></Part>
      <Staff id="1"><Measure><KeySig><concertKey>5</concertKey><actualKey>-5</actualKey>
      </KeySig></Measure></Staff>
    </Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "B", "A")

    assert [
        ET.fromstring(rendered).findtext(f".//KeySig/{name}")
        for name in ("concertKey", "actualKey")
    ] == ["3", "5"]


@pytest.mark.parametrize(
    ("concert_pitch", "opening_accidental", "expected"),
    [("1", "0", "2"), ("0", "2", "4")],
)
def test_legacy_accidental_respects_concert_pitch_view(
    concert_pitch: str,
    opening_accidental: str,
    expected: str,
):
    xml = f"""<museScore><Score><Style><concertPitch>{concert_pitch}</concertPitch></Style>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
      </Instrument></Part>
      <Staff id="1"><Measure><KeySig><accidental>{opening_accidental}</accidental>
      </KeySig></Measure></Staff>
    </Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "D")

    assert ET.fromstring(rendered).findtext(".//KeySig/accidental") == expected


def test_octave_transposer_keeps_c_sharp_spelling_instead_of_auto_respell():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-7</transposeDiatonic><transposeChromatic>-12</transposeChromatic>
      </Instrument></Part>
      <Staff id="1"><Measure><KeySig><concertKey>0</concertKey><actualKey>0</actualKey>
        </KeySig><Note><pitch>60</pitch><tpc>14</tpc><tpc2>14</tpc2></Note>
      </Measure></Staff>
    </Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "C#")
    root = ET.fromstring(rendered)

    assert [root.findtext(f".//KeySig/{name}") for name in ("concertKey", "actualKey")] == [
        "7",
        "7",
    ]
    assert [root.findtext(f".//Note/{name}") for name in ("tpc", "tpc2")] == [
        "21",
        "21",
    ]


def test_nontransposing_actual_key_stays_equal_to_concert_key():
    xml = b"""<museScore><Score><Staff><Measure><KeySig>
    <concertKey>0</concertKey><actualKey>0</actualKey>
    </KeySig></Measure></Staff></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "C#")

    assert [
        ET.fromstring(rendered).findtext(f".//KeySig/{name}")
        for name in ("concertKey", "actualKey")
    ] == ["7", "7"]


def test_each_key_change_follows_the_named_interval_not_global_accidental_family():
    xml = b"""<museScore><Score><Staff>
      <Measure><KeySig><concertKey>0</concertKey></KeySig></Measure>
      <Measure><KeySig><concertKey>-7</concertKey></KeySig></Measure>
    </Staff></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")

    assert [int(item.text) for item in ET.fromstring(rendered).iter("concertKey")] == [
        2,
        -5,
    ]
    assert report.key_signatures_changed == 2


def test_mid_measure_key_change_does_not_hide_implicit_opening_c_key():
    xml = b"""<museScore><Score><Staff><Measure><voice>
      <Chord><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord>
      <KeySig><concertKey>1</concertKey></KeySig>
    </voice></Measure></Staff></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    voice = ET.fromstring(rendered).find(".//voice")

    assert [child.tag for child in voice][:2] == ["KeySig", "Chord"]
    assert [int(item.text) for item in voice.iter("concertKey")] == [2, 3]
    assert report.key_signatures_changed == 2


def test_source_key_mismatch_is_actionable_and_can_be_explicitly_overridden():
    with pytest.raises(ScoreFormatError, match=r"--from-key Bb.*opening concert key \(C\)"):
        transpose_mscx(MUSESCORE_4_KEY_XML, "Bb", "C")

    rendered, _ = transpose_mscx(
        MUSESCORE_4_KEY_XML,
        "Bb",
        "C",
        validate_source_key=False,
    )

    assert ET.fromstring(rendered).findtext(".//concertKey") == "2"


def test_unscoped_later_key_change_is_not_mistaken_for_opening_key():
    xml = b"""<museScore><Score>
    <Note><pitch>60</pitch><tpc>14</tpc></Note>
    <KeySig><concertKey>1</concertKey></KeySig>
    </Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    root = ET.fromstring(rendered)

    assert root.findtext(".//pitch") == "62"
    assert root.findtext(".//concertKey") == "3"
    assert report.key_signatures_changed == 1


@pytest.mark.parametrize(
    "xml",
    [
        b"<settings><Note><pitch>60</pitch><tpc>14</tpc></Note></settings>",
        b"<Score><Note><pitch>60</pitch><tpc>14</tpc></Note></Score>",
        b"<museScore><Style/></museScore>",
        b"<museScore><Score/><Score/></museScore>",
    ],
)
def test_rejects_non_mscx_xml_shapes(xml: bytes):
    with pytest.raises(ScoreFormatError):
        transpose_mscx(xml, "C", "D")


def test_nested_score_is_transposed_once_not_again_through_outer_staff():
    xml = b"""<museScore><Score><Staff><Measure><Score>
    <Note><pitch>60</pitch><tpc>14</tpc></Note>
    </Score></Measure></Staff></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    note = ET.fromstring(rendered).find(".//Note")

    assert note.findtext("pitch") == "62"
    assert note.findtext("tpc") == "16"
    assert report.notes_changed == 1


def test_nested_score_harmony_is_not_transposed_through_outer_harmony():
    xml = b"""<museScore><Score><Staff><Measure>
    <Harmony><root>14</root><Score>
      <Harmony><root>14</root></Harmony>
    </Score></Harmony>
    </Measure></Staff></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    roots = [int(element.text) for element in ET.fromstring(rendered).iter("root")]

    assert roots == [16, 16]
    assert report.chord_symbols_changed == 2


def test_preserves_comments_and_processing_instructions_inside_score():
    xml = b"""<?xml version="1.0"?>
    <museScore><!--keep-comment--><Score><?proof keep?>
    <Note><pitch>60</pitch><tpc>14</tpc></Note></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "D")

    assert b"<!--keep-comment-->" in rendered
    assert b"<?proof keep?>" in rendered


def test_preserves_document_level_comments_and_processing_instructions():
    xml = b"""<?xml version="1.0"?><?pre?><!--before--><museScore>
    <Score><Note><pitch>60</pitch><tpc>14</tpc></Note></Score>
    </museScore><!--after--><?post done?>"""

    rendered, _ = transpose_mscx(xml, "C", "D")

    markers = [b"<?pre?>", b"<!--before-->", b"<museScore", b"<!--after-->", b"<?post done?>"]
    assert all(marker in rendered for marker in markers)
    assert [rendered.index(marker) for marker in markers] == sorted(
        rendered.index(marker) for marker in markers
    )
    assert ET.fromstring(rendered).findtext(".//pitch") == "62"


def test_changed_utf16_document_is_rewritten_as_valid_utf8_with_envelope():
    text = """<?xml version="1.0" encoding="UTF-16"?>
    <!--préface--><museScore><Score><metaTag>café</metaTag>
    <Note><pitch>60</pitch><tpc>14</tpc></Note></Score></museScore><!--après-->"""

    rendered, _ = transpose_mscx(text.encode("utf-16"), "C", "D")
    root = ET.fromstring(rendered)

    assert rendered.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    assert b"pr\xc3\xa9face" in rendered
    assert b"apr\xc3\xa8s" in rendered
    assert root.findtext(".//metaTag") == "café"
    assert root.findtext(".//pitch") == "62"


def test_changed_document_preserves_xml_version_and_standalone_declaration():
    xml = b"""<?xml version="1.1" encoding="UTF-8" standalone="yes"?>
    <museScore><Score><Note><pitch>60</pitch><tpc>14</tpc></Note></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "D")

    assert rendered.startswith(
        b"<?xml version='1.1' encoding='utf-8' standalone='yes'?>"
    )
    assert ET.fromstring(rendered).findtext(".//pitch") == "62"


def test_long_valid_xml_declaration_is_not_lost_on_rewrite():
    padding = " " * 20_000
    xml = (
        f"<?xml{padding}version='1.0'{padding}encoding='UTF-8'?>"
        "<museScore><Score><Note><pitch>60</pitch><tpc>14</tpc></Note>"
        "</Score></museScore>"
    )

    rendered, _ = transpose_mscx(xml, "C", "D")

    assert rendered.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    assert ET.fromstring(rendered).findtext(".//pitch") == "62"


@pytest.mark.parametrize("declared_encoding", ["ISO-8859-1", "UTF-16"])
def test_string_no_op_normalizes_encoding_declaration_to_utf8(
    declared_encoding: str,
):
    xml = f"""<?xml version='1.0' encoding='{declared_encoding}'?>
    <museScore><Score><metaTag>café</metaTag></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "C")

    assert b"encoding='utf-8'" in rendered
    assert ET.fromstring(rendered).findtext(".//metaTag") == "café"
    assert report.score_entries_changed == 0


def test_xml_stylesheet_pi_is_not_mistaken_for_an_xml_declaration():
    xml = """<?xml-stylesheet encoding="ISO-8859-1" href="score.xsl"?>
    <museScore><Score/></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "C")

    assert b'encoding="ISO-8859-1"' in rendered
    assert not rendered.startswith(b"<?xml version=")
    assert report.score_entries_changed == 0


def test_changed_document_without_declaration_does_not_gain_one():
    xml = b"<museScore><Score><Note><pitch>60</pitch><tpc>14</tpc></Note></Score></museScore>"

    rendered, _ = transpose_mscx(xml, "C", "D")

    assert not rendered.startswith(b"<?xml")
    assert ET.fromstring(rendered).findtext(".//pitch") == "62"


def test_doctype_score_is_preserved_on_no_op_and_rejected_on_change():
    xml = b"""<?xml version="1.0"?><!DOCTYPE museScore SYSTEM "score.dtd">
    <museScore><Score><Note><pitch>60</pitch><tpc>14</tpc></Note></Score></museScore>"""

    unchanged, _ = transpose_mscx(xml, "C", "C")
    assert unchanged == xml

    with pytest.raises(ScoreFormatError, match="DOCTYPE"):
        transpose_mscx(xml, "C", "D")


def test_namespaced_score_is_preserved_on_no_op_and_rejected_on_change():
    xml = b"""<museScore xmlns:x="urn:extension"><Score>
    <x:Meta>prefix-sensitive</x:Meta>
    <Note><pitch>60</pitch><tpc>14</tpc></Note></Score></museScore>"""

    unchanged, _ = transpose_mscx(xml, "C", "C")
    assert unchanged == xml

    with pytest.raises(ScoreFormatError, match="namespace prefixes"):
        transpose_mscx(xml, "C", "D")


def test_excessive_xml_depth_is_reported_instead_of_escaping_recursion_error():
    depth = 1200
    xml = (
        "<museScore><Score>"
        + ("<layer>" * depth)
        + "<Note><pitch>60</pitch><tpc>14</tpc></Note>"
        + ("</layer>" * depth)
        + "</Score></museScore>"
    )

    with pytest.raises(ScoreFormatError, match="serialize MSCX"):
        transpose_mscx(xml, "C", "D")


def test_rejects_pitch_and_tpc_that_disagree():
    xml = b"<museScore><Score><Note><pitch>60</pitch><tpc>15</tpc></Note></Score></museScore>"

    with pytest.raises(ScoreFormatError, match="conflicts with concert TPC"):
        transpose_mscx(xml, "C", "D")


@pytest.mark.parametrize(
    "xml",
    [
        b"""<museScore><Score><Note><pitch>60</pitch><pitch>60</pitch>
        <tpc>14</tpc></Note></Score></museScore>""",
        b"""<museScore><Score><KeySig><concertKey>0</concertKey><accidental>0</accidental>
        </KeySig></Score></museScore>""",
    ],
)
def test_rejects_duplicate_or_mixed_pitch_fields(xml: bytes):
    with pytest.raises(ScoreFormatError, match="duplicate|mixes"):
        transpose_mscx(xml, "C", "D")


def test_updates_legacy_accidental_dialect_without_replacing_metadata():
    xml = b"""<museScore><Score><Note><pitch>66</pitch><tpc>20</tpc>
    <Accidental><subtype>sharp</subtype><eid>legacy</eid></Accidental>
    </Note></Score></museScore>"""

    rendered, _ = transpose_mscx(xml, "C", "Db")
    accidental = ET.fromstring(rendered).find(".//Accidental")

    assert accidental.findtext("subtype") == "natural"
    assert accidental.findtext("eid") == "legacy"


def test_rejects_legacy_subtype_only_key_signature():
    xml = b"""<museScore><Score><KeySig><subtype>1</subtype></KeySig>
    <Note><pitch>60</pitch><tpc>14</tpc></Note></Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="no key value"):
        transpose_mscx(xml, "C", "D")


@pytest.mark.parametrize(
    ("extra_definition", "extra_score"),
    [
        ('<StaffType group="tablature"/>', ""),
        ('<StaffType group="pitched"/>', "<StaffTypeChange/>"),
        ('<StaffType group="pitched"/>', "<InstrumentChange/>"),
        (
            '<StaffType group="pitched"/>',
            "<StaffState><subtype>instrument</subtype></StaffState>",
        ),
    ],
)
def test_fails_closed_for_context_changes_that_need_an_engraving_engine(
    extra_definition: str,
    extra_score: str,
):
    xml = f"""<museScore><Score><Part><Staff id="1">{extra_definition}</Staff>
    <Instrument/></Part><Staff id="1"><Measure>{extra_score}<Note>
    <pitch>64</pitch><tpc>18</tpc><fret>0</fret><string>0</string>
    </Note></Measure></Staff></Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="cannot be transposed safely|changes"):
        transpose_mscx(xml, "C", "D")


def test_fails_closed_when_fret_diagram_would_disagree_with_chord_symbol():
    xml = b"""<museScore><Score><Staff><Measure><FretDiagram>
    <Harmony><root>14</root><name>C</name></Harmony></FretDiagram>
    </Measure></Staff></Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="Fret diagrams"):
        transpose_mscx(xml, "C", "D")


@pytest.mark.parametrize(
    "xml",
    [
        b"""<museScore><Score><Part><Staff id="1"><StaffType group="pitched"/>
        </Staff><Instrument/></Part><Staff id="1"><Measure><Note>
        <pitch>64</pitch><tpc>18</tpc><fret>0</fret><string>0</string>
        </Note></Measure></Staff></Score></museScore>""",
        b"""<museScore><Score><Note><pitch>64</pitch><tpc>18</tpc>
        <fret>0</fret><string>0</string></Note></Score></museScore>""",
    ],
)
def test_fails_closed_for_fretted_notes_even_outside_tablature(xml: bytes):
    with pytest.raises(ScoreFormatError, match="Fretted notes"):
        transpose_mscx(xml, "C", "D")


def test_fails_closed_for_transposing_staff_harmony_after_key_change():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
      </Instrument></Part>
      <Staff id="1">
        <Measure><KeySig><concertKey>0</concertKey><actualKey>2</actualKey></KeySig></Measure>
        <Measure><KeySig><concertKey>5</concertKey><actualKey>-5</actualKey></KeySig>
          <Harmony><root>9</root><name>Db</name></Harmony></Measure>
      </Staff>
    </Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="tick-aware"):
        transpose_mscx(xml, "C", "D")


def test_fails_closed_for_transposing_staff_with_implicit_opening_key_change():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument>
        <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
      </Instrument></Part>
      <Staff id="1">
        <Measure><Note><pitch>60</pitch><tpc>14</tpc><tpc2>16</tpc2></Note></Measure>
        <Measure><KeySig><concertKey>3</concertKey><actualKey>5</actualKey></KeySig>
          <Note><pitch>61</pitch><tpc>21</tpc><tpc2>23</tpc2></Note></Measure>
      </Staff>
    </Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="tick-aware"):
        transpose_mscx(xml, "C", "D")


def test_fails_closed_when_key_change_needs_enharmonic_note_respelling():
    xml = b"""<museScore><Score><Staff>
      <Measure><KeySig><concertKey>0</concertKey></KeySig>
        <Note><pitch>60</pitch><tpc>14</tpc></Note></Measure>
      <Measure><KeySig><concertKey>7</concertKey></KeySig>
        <Note><pitch>61</pitch><tpc>21</tpc></Note></Measure>
    </Staff></Score></museScore>"""

    with pytest.raises(ScoreFormatError, match="enharmonic-signature boundary"):
        transpose_mscx(xml, "C", "D")


def test_embedded_score_staff_ids_are_resolved_in_their_own_context():
    xml = b"""<museScore><Score>
      <Part><Staff id="1"><StaffType group="pitched"/></Staff><Instrument/></Part>
      <Staff id="1"><Measure><KeySig><concertKey>0</concertKey></KeySig>
        <Note><pitch>60</pitch><tpc>14</tpc></Note></Measure></Staff>
      <Excerpt><Score>
        <Part><Staff id="1"><StaffType group="percussion"/></Staff>
          <Instrument><useDrumset>1</useDrumset></Instrument></Part>
        <Staff id="1"><Measure><Note><pitch>38</pitch><tpc>16</tpc></Note></Measure></Staff>
      </Score></Excerpt>
    </Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")
    scores = list(ET.fromstring(rendered).iter("Score"))

    assert scores[0].findtext("./Staff/Measure/Note/pitch") == "62"
    assert scores[1].findtext("./Staff/Measure/Note/pitch") == "38"
    assert report.notes_changed == 1


@pytest.mark.parametrize("duplicate_name", ["score.mscx", "asset.bin"])
def test_duplicate_archive_members_are_rejected_atomically(
    duplicate_name: str,
    tmp_path: Path,
):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with warnings.catch_warnings(), zipfile.ZipFile(source, "w") as archive:
        warnings.simplefilter("ignore", UserWarning)
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr(
            duplicate_name,
            SCORE_XML if duplicate_name.endswith(".mscx") else b"1",
        )
        archive.writestr(
            duplicate_name,
            SCORE_XML if duplicate_name.endswith(".mscx") else b"2",
        )
    output.write_bytes(b"existing")

    with pytest.raises(ScoreFormatError, match="duplicate member"):
        transpose_mscz(source, output, "Bb", "C")

    assert output.read_bytes() == b"existing"


def test_corrupt_archive_member_is_wrapped_and_destination_is_preserved(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("asset.bin", b"asset-marker")
    raw = bytearray(source.read_bytes())
    marker = b"asset-marker"
    raw[raw.index(marker)] ^= 1
    source.write_bytes(raw)
    output.write_bytes(b"existing")

    with pytest.raises(ScoreFormatError, match="read or validate"):
        transpose_mscz(source, output, "Bb", "C")

    assert output.read_bytes() == b"existing"


def test_no_op_archive_is_byte_identical_and_preserves_comment_and_mode(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.comment = b"archive-comment"
        archive.writestr("score.mscx", SCORE_XML)
    source.chmod(0o640)
    original = source.read_bytes()

    report = transpose_mscz(source, output, "Bb", "Bb")

    assert output.read_bytes() == original
    assert stat.S_IMODE(output.stat().st_mode) == 0o640
    with zipfile.ZipFile(output) as archive:
        assert archive.comment == b"archive-comment"
    assert report.score_entries_changed == 0


def test_changed_archive_preserves_comment_and_existing_destination_mode(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.comment = b"archive-comment"
        archive.writestr("score.mscx", SCORE_XML)
    output.write_bytes(b"existing")
    output.chmod(0o604)

    transpose_mscz(source, output, "Bb", "C")

    assert stat.S_IMODE(output.stat().st_mode) == 0o604
    with zipfile.ZipFile(output) as archive:
        assert archive.comment == b"archive-comment"


def test_multiple_unique_score_entries_transpose_and_aggregate_report(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("one.mscx", SCORE_XML)
        archive.writestr("TWO.MSCX", SCORE_XML)

    report = transpose_mscz(source, output, "Bb", "C")

    with zipfile.ZipFile(output) as archive:
        assert all(
            [int(item.text) for item in ET.fromstring(archive.read(name)).iter("pitch")]
            == [72, 76]
            for name in ("one.mscx", "TWO.MSCX")
        )
    assert report.notes_changed == 4
    assert report.score_entries_changed == 2


def test_malformed_container_manifest_is_rejected_atomically(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("META-INF/container.xml", b"not XML")
    output.write_bytes(b"existing")

    with pytest.raises(ScoreFormatError, match="container manifest"):
        transpose_mscz(source, output, "Bb", "C")

    assert output.read_bytes() == b"existing"


@pytest.mark.parametrize(
    "manifest",
    [
        b'<container><rootfile full-path="score.mscx"/></container>',
        b'<container><junk><rootfile full-path="score.mscx"/></junk></container>',
        b"""<container><rootfiles/><junk>
        <rootfile full-path="score.mscx"/></junk></container>""",
        b"""<container><rootfiles>
        <rootfile full-path="score.mscx"/><rootfile full-path="score.mscx"/>
        </rootfiles></container>""",
    ],
)
def test_container_manifest_requires_direct_rootfiles_structure(
    tmp_path: Path,
    manifest: bytes,
):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("META-INF/container.xml", manifest)
    output.write_bytes(b"existing")

    with pytest.raises(ScoreFormatError, match="container manifest"):
        transpose_mscz(source, output, "Bb", "C")

    assert output.read_bytes() == b"existing"


@pytest.mark.parametrize(
    "score_name",
    ["../score.mscx", "dir/../../score.mscx", "..\\score.mscx", "C:/score.mscx"],
)
def test_unsafe_archive_member_paths_are_rejected_atomically(
    tmp_path: Path,
    score_name: str,
):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(score_name, SCORE_XML)
    output.write_bytes(b"existing")

    with pytest.raises(ScoreFormatError, match="unsafe member path"):
        transpose_mscz(source, output, "Bb", "C")

    assert output.read_bytes() == b"existing"


def test_safe_dot_prefixed_unicode_score_member_is_supported(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    score_name = "..bongo español.mscx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(score_name, SCORE_XML)

    transpose_mscz(source, output, "Bb", "C")

    with zipfile.ZipFile(output) as archive:
        pitches = [
            int(element.text)
            for element in ET.fromstring(archive.read(score_name)).iter("pitch")
        ]
    assert pitches == [72, 76]


def test_archive_symlink_member_is_rejected(tmp_path: Path):
    source = tmp_path / "source.mscz"
    link = zipfile.ZipInfo("score.mscx")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(link, SCORE_XML)

    with pytest.raises(ScoreFormatError, match="special-file type"):
        transpose_mscz(source, tmp_path / "out.mscz", "Bb", "C")


def test_archive_file_directory_prefix_collision_is_rejected(tmp_path: Path):
    source = tmp_path / "source.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("META-INF", b"file, not directory")
        archive.writestr("META-INF/container.xml", CONTAINER_XML)

    with pytest.raises(ScoreFormatError, match="file/directory path collision"):
        transpose_mscz(source, tmp_path / "out.mscz", "Bb", "C")


def test_suspicious_archive_compression_ratio_is_rejected(tmp_path: Path):
    source = tmp_path / "source.mscz"
    with zipfile.ZipFile(
        source,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("asset.bin", b"0" * (2 * 1024 * 1024))

    with pytest.raises(ScoreFormatError, match="compression ratio"):
        transpose_mscz(source, tmp_path / "out.mscz", "Bb", "C")


def test_deflate_decoder_error_is_wrapped_as_score_format_error(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(
        source,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.writestr("score.mscx", SCORE_XML)
    raw = bytearray(source.read_bytes())
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo("score.mscx")
    name_length, extra_length = struct.unpack_from("<HH", raw, info.header_offset + 26)
    payload_offset = info.header_offset + 30 + name_length + extra_length
    raw[payload_offset] ^= 0x02
    source.write_bytes(raw)
    output.write_bytes(b"existing")

    with pytest.raises(ScoreFormatError, match="read or validate"):
        transpose_mscz(source, output, "Bb", "C")

    assert output.read_bytes() == b"existing"


def test_unsupported_zip_extract_version_is_wrapped(tmp_path: Path):
    source = tmp_path / "source.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)
    raw = bytearray(source.read_bytes())
    central_offset = raw.index(b"PK\x01\x02")
    struct.pack_into("<H", raw, central_offset + 6, 84)
    source.write_bytes(raw)

    with pytest.raises(ScoreFormatError, match="valid MSCZ|read or validate"):
        transpose_mscz(source, tmp_path / "out.mscz", "Bb", "C")


def test_streamed_member_size_must_match_zip_metadata(tmp_path: Path):
    source = tmp_path / "source.mscz"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("score.mscx", SCORE_XML)
        archive.writestr("asset.bin", b"asset")
    raw = bytearray(source.read_bytes())
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo("asset.bin")
    struct.pack_into("<I", raw, info.header_offset + 22, info.file_size + 100)
    central_offset = 0
    while (central_offset := raw.find(b"PK\x01\x02", central_offset)) >= 0:
        name_length = struct.unpack_from("<H", raw, central_offset + 28)[0]
        extra_length = struct.unpack_from("<H", raw, central_offset + 30)[0]
        comment_length = struct.unpack_from("<H", raw, central_offset + 32)[0]
        name_offset = central_offset + 46
        if raw[name_offset : name_offset + name_length] == b"asset.bin":
            struct.pack_into("<I", raw, central_offset + 24, info.file_size + 100)
            break
        central_offset = name_offset + name_length + extra_length + comment_length
    source.write_bytes(raw)

    with pytest.raises(ScoreFormatError, match="size does not match"):
        transpose_mscz(source, tmp_path / "out.mscz", "Bb", "C")


def test_no_op_never_publishes_a_replaced_source_path(
    monkeypatch,
    tmp_path: Path,
):
    source = tmp_path / "source.mscz"
    output = tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)
    output.write_bytes(b"existing-output")

    from music_score_toolkit import mscz as mscz_module

    original_read = mscz_module._read_archive_member
    replaced = False

    def read_then_replace_path(*args, **kwargs):
        nonlocal replaced
        payload = original_read(*args, **kwargs)
        if not replaced:
            replacement = tmp_path / "replacement.mscz"
            replacement.write_bytes(b"replaced after validation")
            os.replace(replacement, source)
            replaced = True
        return payload

    monkeypatch.setattr(mscz_module, "_read_archive_member", read_then_replace_path)

    with pytest.raises(ScoreFormatError, match="changed while"):
        transpose_mscz(source, output, "Bb", "Bb")

    assert output.read_bytes() == b"existing-output"
    assert source.read_bytes() == b"replaced after validation"


def test_long_valid_output_basename_does_not_break_staging(tmp_path: Path):
    source = tmp_path / "source.mscz"
    output = tmp_path / (("o" * 240) + ".mscz")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", SCORE_XML)

    transpose_mscz(source, output, "Bb", "C")

    assert output.is_file()
