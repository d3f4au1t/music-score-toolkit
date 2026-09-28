"""Source-aligned, lossless repair of native-to-MusicXML key changes."""

import subprocess
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction

import pytest

from music_score_toolkit.export_keys import native_key_plan, restore_exported_keys
from music_score_toolkit.tools import convert_score

NATIVE = b'''<museScore version="4.0"><Score>
<Part><Staff id="1"/><Instrument/></Part><Staff id="1"><Measure><voice>
<TimeSig><sigN>4</sigN><sigD>4</sigD></TimeSig>
<KeySig><concertKey>0</concertKey></KeySig>
<Chord><durationType>quarter</durationType><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord>
<KeySig><concertKey>3</concertKey><mode>major</mode></KeySig>
<Chord><durationType>quarter</durationType><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord>
<KeySig><concertKey>5</concertKey></KeySig>
<Chord><durationType>quarter</durationType><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord>
<KeySig><concertKey>7</concertKey><visible>0</visible></KeySig>
<Chord><durationType>quarter</durationType><Note><pitch>60</pitch><tpc>14</tpc></Note></Chord>
</voice></Measure></Staff></Score></museScore>'''

NOTE = b'<note><pitch><step>C</step><octave>4</octave></pitch><duration>12</duration><type>quarter</type></note>'
EXPORTED = (b'''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">
<!--keep this exactly--><?preserve this?>
<score-partwise version="4.0"><work><work-title>Original title</work-title></work>
<part-list><score-part id="P1"><part-name>Flute</part-name></score-part></part-list>
<part id="P1"><measure number="1"><attributes><divisions>12</divisions><key><fifths>0</fifths></key></attributes>
<direction><direction-type><words>Keep this text</words></direction-type></direction>'''
            + NOTE * 4 + b'</measure></part></score-partwise>')


def plan_for(tmp_path, content=NATIVE):
    source = tmp_path / 'source.mscx'
    source.write_bytes(content)
    return native_key_plan(source)


def test_restore_missing_keys_preserves_every_original_byte(tmp_path):
    plan = plan_for(tmp_path)
    assert [k.position for k in plan[0].measures[0].keys] == [Fraction(1, 4), Fraction(1, 2), Fraction(3, 4)]
    result = restore_exported_keys(EXPORTED, plan)
    additions = [b'<attributes><key><fifths>3</fifths><mode>major</mode></key></attributes>',
                 b'<attributes><key><fifths>5</fifths></key></attributes>',
                 b'<attributes><key print-object="no"><fifths>7</fifths></key></attributes>']
    stripped = result
    for addition in additions:
        assert stripped.count(addition) == 1
        stripped = stripped.replace(addition, b'')
    assert stripped == EXPORTED
    assert restore_exported_keys(result, plan) == result


@pytest.mark.parametrize(('before', 'after', 'message'), [
    (b'<step>C</step>', b'<step>D</step>', 'pitches or rhythmic positions'),
    (b'<duration>12</duration>', b'<duration>6</duration>', 'pitches or rhythmic positions'),
    (b'<divisions>12</divisions>', b'<divisions>0</divisions>', 'positive'),
    (b'<duration>12</duration>', b'<duration>-12</duration>', 'duration'),
    (b'<octave>4</octave>', b'<octave>4</octave><octave>5</octave>', 'Duplicate'),
    (b'<measure number="1">', b'<measure number="0"/><measure number="1">', 'measure count'),
])
def test_refuse_to_guess_when_output_does_not_align(tmp_path, before, after, message):
    with pytest.raises(ValueError, match=message):
        restore_exported_keys(EXPORTED.replace(before, after, 1), plan_for(tmp_path))


def test_refuse_conflicting_existing_key(tmp_path):
    plan = plan_for(tmp_path)
    result = restore_exported_keys(EXPORTED, plan).replace(b'<fifths>5</fifths>', b'<fifths>-1</fifths>')
    with pytest.raises(ValueError, match='disagrees'):
        restore_exported_keys(result, plan)


def test_ignore_trailing_courtesy_and_boundary_only_keys(tmp_path):
    root = ET.fromstring(NATIVE)
    voice = root.find('Score/Staff/Measure/voice')
    keys = voice.findall('KeySig')
    for key in keys[1:]:
        voice.remove(key)
    assert plan_for(tmp_path, ET.tostring(root)) == ()
    voice.append(keys[-1])
    assert plan_for(tmp_path, ET.tostring(root)) == ()


def test_written_and_concert_pitch_export(tmp_path):
    native = NATIVE.replace(b'<Instrument/>', b'<Instrument><transposeDiatonic>-1</transposeDiatonic><transposeChromatic>-2</transposeChromatic></Instrument>')
    plan = plan_for(tmp_path, native)
    written = EXPORTED.replace(b'<step>C</step>', b'<step>D</step>').replace(
        b'<divisions>12</divisions>', b'<divisions>12</divisions><transpose><chromatic>-2</chromatic></transpose>')
    result = restore_exported_keys(written, plan)
    assert [k.text for k in ET.fromstring(result).findall('.//key/fifths')][1:] == ['5', '-5', '-3']
    concert = EXPORTED.replace(b'<part-list>', b'<defaults><concert-score/></defaults><part-list>')
    result = restore_exported_keys(concert, plan)
    assert [k.text for k in ET.fromstring(result).findall('.//key/fifths')][1:] == ['3', '5', '7']


@pytest.mark.parametrize('suffix', ['.musicxml', '.xml', '.mxl'])
def test_converter_repairs_native_export_atomically(tmp_path, monkeypatch, suffix):
    source = tmp_path / 'source.mscz'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('score.mscx', NATIVE)
    destination = tmp_path / f'result{suffix}'
    destination.write_bytes(b'previous output')
    executable = tmp_path / 'musescore'
    executable.touch(mode=0o755)
    package = {}

    def export(command, *, check):
        from pathlib import Path
        output = Path(command[-1])
        if suffix == '.mxl':
            with zipfile.ZipFile(output, 'w') as archive:
                archive.comment = b'archive comment'
                archive.writestr('META-INF/container.xml', b'<container><rootfiles><rootfile full-path="scores/main.musicxml"/></rootfiles></container>')
                archive.writestr('scores/main.musicxml', EXPORTED)
                extra = zipfile.ZipInfo('image.png', (2020, 1, 1, 1, 1, 0))
                extra.comment = b'asset comment'
                extra.external_attr = 0o644 << 16
                archive.writestr(extra, b'preserved asset')
            with zipfile.ZipFile(output) as archive:
                package.update({i.filename: (i.date_time, i.comment, i.external_attr, archive.read(i))
                                for i in archive.infolist()})
        else:
            output.write_bytes(EXPORTED)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr('music_score_toolkit.tools.subprocess.run', export)
    convert_score(source, destination, musescore=executable)
    if suffix == '.mxl':
        with zipfile.ZipFile(destination) as archive:
            assert archive.comment == b'archive comment'
            result = archive.read('scores/main.musicxml')
            for info in archive.infolist():
                original = package[info.filename]
                assert (info.date_time, info.comment, info.external_attr) == original[:3]
                if info.filename != 'scores/main.musicxml':
                    assert archive.read(info) == original[3]
    else:
        result = destination.read_bytes()
    assert [k.text for k in ET.fromstring(result).findall('.//key/fifths')] == ['0', '3', '5', '7']
    assert not list(tmp_path.glob('.music-score-*'))


def test_converter_does_not_publish_uncertain_repair(tmp_path, monkeypatch):
    plan_for(tmp_path)
    destination = tmp_path / 'result.musicxml'
    destination.write_bytes(b'previous output')
    executable = tmp_path / 'musescore'
    executable.touch(mode=0o755)

    def export(command, *, check):
        from pathlib import Path
        Path(command[-1]).write_bytes(EXPORTED.replace(b'<step>C</step>', b'<step>D</step>'))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr('music_score_toolkit.tools.subprocess.run', export)
    with pytest.raises(RuntimeError, match='staged output was not published'):
        convert_score(tmp_path / 'source.mscx', destination, musescore=executable)
    assert destination.read_bytes() == b'previous output'
    assert not list(tmp_path.glob('.music-score-*'))
