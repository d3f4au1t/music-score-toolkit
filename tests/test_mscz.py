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


def test_custom_key_signature_is_left_untouched():
    xml = b"""<museScore><Score><KeySig><custom>1</custom><concertKey>0</concertKey>
    <CustDef><sym>accidentalSharp</sym></CustDef></KeySig></Score></museScore>"""

    rendered, report = transpose_mscx(xml, "C", "D")

    assert ET.fromstring(rendered).find(".//concertKey").text == "0"
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


def test_missing_primary_tpc_gets_target_key_fallback_without_adding_tpc2():
    xml = b"<museScore><Score><Note><pitch>60</pitch></Note></Score></museScore>"

    rendered, _ = transpose_mscx(xml, "C", "D")
    note = ET.fromstring(rendered).find(".//Note")

    assert note.findtext("pitch") == "62"
    assert note.findtext("tpc") == "16"
    assert note.find("tpc2") is None


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
    xml = b"<museScore><Score><Note><pitch>127</pitch></Note></Score></museScore>"
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
        archive.writestr("META-INF/container.xml", b"container-marker")
        archive.writestr("Thumbnails/thumbnail.png", b"image-marker")

    report = transpose_mscz(source, output, "Bb", "C")

    with zipfile.ZipFile(output) as archive:
        assert archive.read("META-INF/container.xml") == b"container-marker"
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
