import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from music_score_toolkit import (
    ScoreFormatError,
    retarget_instrument_mscx,
    retarget_instrument_mscz,
)
from music_score_toolkit.cli import main

CONCERT_C = b"""<museScore version="4.0"><Score>
  <Style><concertPitch>1</concertPitch></Style>
  <Part id="1"><Staff id="1"/><trackName>Flute</trackName><Instrument id="flute">
    <longName>Flute</longName><Channel><program value="73"/></Channel>
  </Instrument></Part>
  <Staff id="1"><VBox><Text><text>Original title</text></Text></VBox><Measure number="42">
    <voice><Clef/><TimeSig/><Harmony><root>14</root><base>18</base><name>maj7</name></Harmony>
    <Dynamic><subtype>p</subtype></Dynamic><Tempo><tempo>2</tempo></Tempo>
    <Chord><durationType>half</durationType><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord>
    <Chord><durationType>half</durationType><Note><pitch>64</pitch><tpc>18</tpc>
      <Accidental><subtype>accidentalNatural</subtype><role>1</role><bracket>1</bracket></Accidental>
    </Note></Chord></voice>
  </Measure></Staff>
</Score></museScore>"""


def test_concert_c_to_bb_preserves_sound_and_score_markings():
    rendered, report = retarget_instrument_mscx(CONCERT_C, "B♭", part_name="Clarinet in B♭")
    root = ET.fromstring(rendered)
    original = ET.fromstring(CONCERT_C)

    assert [n.findtext("pitch") for n in root.iter("Note")] == ["60", "64"]
    assert [n.findtext("tpc") for n in root.iter("Note")] == ["14", "18"]
    assert [n.findtext("tpc2") for n in root.iter("Note")] == ["16", "20"]
    assert root.findtext(".//KeySig/concertKey") == "0"
    assert root.findtext(".//KeySig/actualKey") == "2"
    assert root.findtext(".//Instrument/transposeDiatonic") == "-1"
    assert root.findtext(".//Instrument/transposeChromatic") == "-2"
    assert root.findtext(".//Style/concertPitch") == "0"
    assert root.findtext(".//Part/trackName") == "Clarinet in B♭"
    assert root.findtext(".//Harmony/root") == "16"
    assert root.findtext(".//Harmony/base") == "20"
    assert root.findtext(".//Accidental/subtype") == "accidentalSharp"
    assert root.findtext(".//Accidental/role") == "1"
    assert root.findtext(".//Accidental/bracket") == "1"
    assert root.find(".//Measure").attrib == original.find(".//Measure").attrib
    for tag in ("VBox", "Dynamic", "Tempo", "Channel", "TimeSig"):
        assert ET.tostring(root.find(f".//{tag}")) == ET.tostring(original.find(f".//{tag}"))
    assert report.target_pitch == "Bb"
    assert report.written_notes_changed == 2
    assert report.chord_symbols_changed == 1


@pytest.mark.parametrize(
    ("target", "chromatic", "tonic", "written_key"),
    [("C", 0, 14, 0), ("Bb", -2, 16, 2), ("A", -3, 11, -3),
     ("F", -7, 15, 1), ("Eb", -9, 17, 3)],
)
def test_instrument_presets_preserve_sounding_pitch(target, chromatic, tonic, written_key):
    rendered, _ = retarget_instrument_mscx(CONCERT_C, target)
    root = ET.fromstring(rendered)
    assert root.findtext(".//Note/pitch") == "60"
    assert root.findtext(".//Note/tpc2") == str(tonic)
    assert root.findtext(".//KeySig/actualKey") == str(written_key)
    assert root.findtext(".//Instrument/transposeChromatic") == str(chromatic)
    repeated, report = retarget_instrument_mscx(rendered, target)
    assert repeated == rendered
    assert report.score_entries_changed == 0


def test_bb_to_c_round_trip_preserves_music_and_chords():
    bb, _ = retarget_instrument_mscx(CONCERT_C)
    back, _ = retarget_instrument_mscx(bb, "C")
    root = ET.fromstring(back)
    assert [n.findtext("pitch") for n in root.iter("Note")] == ["60", "64"]
    assert [n.findtext("tpc2") for n in root.iter("Note")] == ["14", "18"]
    assert root.findtext(".//Harmony/root") == "14"
    assert root.findtext(".//Harmony/base") == "18"
    assert root.findtext(".//Instrument/transposeChromatic") == "0"


@pytest.mark.parametrize(
    ("target", "written_tpc", "symbol"),
    [("Bb", "16", "accidentalQuarterToneSharpStein"),
     ("A", "18", "accidentalQuarterToneFlatStein")],
)
def test_microtonal_conversion_preserves_sounding_pitch_and_correct_symbol(
    target, written_tpc, symbol,
):
    xml = b"""<museScore version="4.0"><Score><Part><Staff id="1"/><Instrument/></Part>
      <Staff id="1"><Measure><Note><pitch>60</pitch><tpc>14</tpc><centOffset>50</centOffset>
        <Accidental><subtype>accidentalQuarterToneSharpStein</subtype></Accidental>
      </Note></Measure></Staff></Score></museScore>"""
    rendered, _ = retarget_instrument_mscx(xml, target)
    root = ET.fromstring(rendered)
    sounding = int(root.findtext(".//pitch")) + float(root.findtext(".//centOffset")) / 100
    assert sounding == 60.5
    assert root.findtext(".//tpc2") == written_tpc
    assert root.findtext(".//Accidental/subtype") == symbol
    restored, _ = retarget_instrument_mscx(rendered, "C")
    root = ET.fromstring(restored)
    assert root.findtext(".//pitch") == "60"
    assert root.findtext(".//centOffset") == "50"
    assert root.findtext(".//Accidental/subtype") == "accidentalQuarterToneSharpStein"


@pytest.mark.parametrize("version", ["2.06", "3.02", "5.0"])
def test_non_musescore_4_formats_require_resaving_before_instrument_conversion(version):
    with pytest.raises(ScoreFormatError, match="requires MuseScore 4 format"):
        retarget_instrument_mscx(CONCERT_C.replace(b'4.0', version.encode()))


@pytest.mark.parametrize("group", ["percussion", "tablature"])
def test_non_pitched_selected_staff_is_rejected(group):
    xml = CONCERT_C.replace(
        b'<Staff id="1"/>', f'<Staff id="1"><StaffType group="{group}"/></Staff>'.encode(),
    )
    with pytest.raises(ScoreFormatError, match="pitched staves only"):
        retarget_instrument_mscx(xml)


def test_source_enharmonic_written_chords_are_converted_to_concert_spelling():
    xml = b"""<museScore><Score><Part><Staff id="1"/><Instrument>
      <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
    </Instrument></Part><Staff id="1"><Measure><KeySig><concertKey>5</concertKey>
      <actualKey>-5</actualKey></KeySig><Harmony><harmonyInfo><root>9</root></harmonyInfo></Harmony>
      <Note><pitch>59</pitch><tpc>19</tpc><tpc2>9</tpc2></Note>
    </Measure></Staff></Score></museScore>"""
    rendered, _ = retarget_instrument_mscx(xml, "C")
    root = ET.fromstring(rendered)
    assert root.findtext(".//Note/pitch") == "59"
    assert root.findtext(".//Note/tpc2") == "19"
    assert root.findtext(".//harmonyInfo/root") == "19"
    assert root.findtext(".//KeySig/actualKey") == "5"


def test_legacy_written_key_is_replaced_with_concert_and_written_pair():
    xml = b"""<museScore><Score><Part><Staff id="1"/><Instrument>
      <transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic>
    </Instrument></Part><Staff id="1"><Measure><KeySig><accidental>2</accidental>
      <showCourtesy>0</showCourtesy></KeySig><Note><pitch>60</pitch><tpc>14</tpc><tpc2>16</tpc2>
    </Note></Measure></Staff></Score></museScore>"""
    rendered, _ = retarget_instrument_mscx(xml, "Eb")
    root = ET.fromstring(rendered)
    assert root.findtext(".//KeySig/concertKey") == "0"
    assert root.findtext(".//KeySig/actualKey") == "3"
    assert root.find(".//KeySig/accidental") is None
    assert root.findtext(".//KeySig/showCourtesy") == "0"
    assert root.findtext(".//Note/tpc2") == "17"


def multipart_score():
    root = ET.fromstring(CONCERT_C)
    score = root.find("Score")
    score.find("Style/concertPitch").text = "0"
    score.append(ET.fromstring('<Part id="2"><Staff id="2"/><Instrument/></Part>'))
    score.append(ET.fromstring('<Staff id="2"><Measure><Note><pitch>67</pitch><tpc>15</tpc>'
                               '</Note></Measure></Staff>'))
    return root


def test_multi_part_scores_require_selection_and_preserve_other_parts():
    source = multipart_score()
    xml = ET.tostring(source)
    with pytest.raises(ScoreFormatError, match="Choose a part"):
        retarget_instrument_mscx(xml)
    rendered, report = retarget_instrument_mscx(xml, part=2)
    root = ET.fromstring(rendered)
    for path in ("./Score/Part[@id='1']", "./Score/Staff[@id='1']"):
        assert ET.tostring(root.find(path)) == ET.tostring(source.find(path))
    assert root.findtext("./Score/Staff[@id='2']//tpc2") == "17"
    assert report.part == 2


@pytest.mark.parametrize("part", [0, -1, True, 3])
def test_invalid_part_selection_is_rejected(part):
    with pytest.raises(ValueError):
        retarget_instrument_mscx(CONCERT_C, part=part)


@pytest.mark.parametrize(
    "extra", ["<InstrumentChange/>", "<StaffTypeChange/>", "<FretDiagram/>"],
)
def test_unsupported_context_changes_fail_closed(extra):
    xml = CONCERT_C.replace(b"</voice>", extra.encode() + b"</voice>")
    with pytest.raises(ScoreFormatError, match="does not yet support"):
        retarget_instrument_mscx(xml)


def test_linked_excerpts_and_concert_multi_part_scores_are_rejected():
    with pytest.raises(ScoreFormatError, match="linked excerpts"):
        retarget_instrument_mscx(CONCERT_C.replace(b"</Score>", b"<Excerpt/></Score>"))
    root = multipart_score()
    root.find(".//concertPitch").text = "1"
    with pytest.raises(ScoreFormatError, match="written pitch"):
        retarget_instrument_mscx(ET.tostring(root), part=1)


def test_archive_rewrite_preserves_assets_and_archive_comment(tmp_path):
    source, target = tmp_path / "source.mscz", tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.comment = b"preserve archive comment"
        archive.writestr("score.mscx", CONCERT_C)
        archive.writestr("score_style.mss", b"<museScore><Style><concertPitch>1</concertPitch>"
                         b"</Style></museScore>")
        archive.writestr("image.png", b"preserve asset bytes")
    retarget_instrument_mscz(source, target)
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target) as converted:
        assert converted.comment == original.comment
        assert converted.namelist() == original.namelist()
        assert converted.read("image.png") == original.read("image.png")
        assert ET.fromstring(converted.read("score.mscx")).findtext(".//concertPitch") == "0"
    repeated = tmp_path / "repeated.mscz"
    retarget_instrument_mscz(target, repeated)
    assert repeated.read_bytes() == target.read_bytes()


def test_multiple_score_archive_leaves_existing_destination_untouched(tmp_path):
    source, target = tmp_path / "source.mscz", tmp_path / "out.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", CONCERT_C)
        archive.writestr("parts/part.mscx", CONCERT_C)
    target.write_bytes(b"previous score")
    with pytest.raises(ScoreFormatError, match="linked excerpts"):
        retarget_instrument_mscz(source, target)
    assert target.read_bytes() == b"previous score"


def test_instrument_cli_converts_and_exports_pdf(tmp_path, monkeypatch, capsys):
    source, output, pdf = (tmp_path / name for name in ("in.mscz", "out.mscz", "out.pdf"))
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", CONCERT_C)
    exports = []
    monkeypatch.setattr("music_score_toolkit.cli.convert_score", lambda a, b: exports.append((a, b)))
    assert main(["instrument", str(source), str(output), "--part-name", "B-flat part",
                 "--export-pdf", str(pdf)]) == 0
    assert json.loads(capsys.readouterr().out)["target_pitch"] == "Bb"
    assert exports == [(output, pdf)]


def test_instrument_cli_reports_unsupported_target(tmp_path, capsys):
    source = tmp_path / "in.mscz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("score.mscx", CONCERT_C)
    assert main(["instrument", str(source), str(tmp_path / "out.mscz"),
                 "--to-instrument", "Db"]) == 2
    assert "Supported instrument pitches" in capsys.readouterr().err


@pytest.mark.parametrize("filename", ["auto-transpose-sample.mscz", "auto-music-transpose-sample.mscz"])
def test_original_score_fixtures_keep_all_sounding_pitches(tmp_path, filename):
    source = Path(__file__).parent / "fixtures" / filename
    output = tmp_path / filename
    retarget_instrument_mscz(source, output)
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as converted:
        for name in original.namelist():
            if name.endswith(".mscx"):
                old = ET.fromstring(original.read(name))
                new = ET.fromstring(converted.read(name))
                assert [n.findtext("pitch") for n in old.iter("Note")] == [
                    n.findtext("pitch") for n in new.iter("Note")
                ]
            else:
                assert original.read(name) == converted.read(name)
