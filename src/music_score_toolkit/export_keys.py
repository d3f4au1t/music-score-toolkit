"""Restore conventional mid-measure keys lost by MuseScore's MusicXML exporter.

Only verified native-score exports are repaired. XML is patched by inserting
new attributes at existing musical boundaries, leaving every original byte
(including comments, DOCTYPE, layout and note notation) untouched.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from xml.parsers import expat

from . import mscz
from .timing import advances_position, opening_meter, read_measure_timing

_MAX_XML = 256 * 1024 * 1024
_MODES = {"major", "minor", "dorian", "phrygian", "lydian", "mixolydian", "aeolian", "ionian", "locrian"}
_STEPS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
# staff, whole-note onset, whole-note duration, sounding MIDI pitch
NoteIdentity = tuple[int, Fraction, Fraction, int]


@dataclass(frozen=True)
class KeyChange:
    staff: int
    position: Fraction
    concert: int
    written: int
    mode: str | None
    visible: bool


@dataclass(frozen=True)
class KeyMeasure:
    length: Fraction
    keys: tuple[KeyChange, ...]
    notes: Counter[NoteIdentity]


@dataclass(frozen=True)
class KeyPart:
    staff_count: int
    measure_count: int
    measures: dict[int, KeyMeasure]


def _potential_interior(measure: ET.Element) -> bool:
    for voice in measure.findall("voice") or [measure]:
        moved = False
        for item in voice:
            if item.tag == "KeySig" and moved:
                return True
            moved |= advances_position(item) or item.tag in {"location", "tick"}
    return False


def _integer(value: str | None, label: str) -> int:
    if value is None or not re.fullmatch(r"-?[0-9]{1,10}", value.strip()):
        raise ValueError(f"Invalid or missing {label} while checking exported key changes.")
    return int(value)


def _field(element: ET.Element, name: str, default: str | None = None) -> str | None:
    fields = element.findall(name)
    if len(fields) > 1:
        raise ValueError(f"Duplicate {name} while checking exported key changes.")
    return fields[0].text if fields else default


def native_key_plan(source: Path) -> tuple[KeyPart, ...]:
    """Read a bounded native source; ordinary boundary-only scores are a no-op."""
    if source.suffix.lower() not in {".mscx", ".mscz"}:
        return ()
    concert_pitch = None
    if source.suffix.lower() == ".mscx":
        with source.open("rb") as stream:
            contents = [stream.read(_MAX_XML + 1)]
        if len(contents[0]) > _MAX_XML:
            raise ValueError("Native score is too large to verify exported key changes.")
    else:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            members = mscz._validate_archive_members(infos)
            mscz._validate_mscz_manifest(archive, members)
            concert_pitch = mscz._archive_concert_pitch_setting(archive, infos)
            contents = [mscz._read_archive_member(archive, info, maximum_size=_MAX_XML)
                        for info in infos if info.filename.lower().endswith(".mscx") and not info.is_dir()]
    documents = [mscz._parse_mscx(content) for content in contents]
    if not any(_potential_interior(m) for doc in documents for m in doc.root.iter("Measure")):
        return ()
    if len(documents) != 1:
        raise ValueError("Export key repair requires a standalone score without linked excerpts.")
    root = documents[0].root
    contexts = mscz._score_contexts(root, concert_pitch)
    if not root.get("version", "").startswith("4.") or len(contexts) != 1:
        raise ValueError("Export key repair requires a standalone MuseScore 4 score.")
    score, concert_pitch = contexts[0]
    if any(e.tag in {"InstrumentChange", "StaffState", "StaffTypeChange"} for e in score.iter()):
        raise ValueError("Export key repair cannot verify mid-score instrument or staff changes.")
    scopes = mscz._staff_scopes(score)
    parts = [p for p in score if p.tag in {"Part", "SharedPart"}]
    if not parts or sum(len(p.findall("Staff")) for p in parts) != len(scopes):
        raise ValueError("Export key repair needs explicit native part/staff definitions.")
    # _staff_scopes validates IDs; match definition order (MusicXML part-list
    # order), never the order in which content Staff elements are serialized.
    scope_by_id = {s.content.get("id"): s for s in scopes}
    result = []
    staff_index = 0
    for part in parts:
        group = []
        for definition in part.findall("Staff"):
            staff_index += 1
            group.append(scope_by_id[definition.get("id", str(staff_index))])
        measure_lists = [s.content.findall("Measure") for s in group]
        if len({len(ms) for ms in measure_lists}) != 1:
            raise ValueError("Native staves have different measure counts; export alignment is unsafe.")
        candidates = {i for measures in measure_lists for i, m in enumerate(measures)
                      if _potential_interior(m)}
        meters_by_staff = []
        for measures in measure_lists:
            meters = ()
            history = []
            for measure in measures:
                meters = opening_meter(measure, meters)
                history.append(meters)
            meters_by_staff.append(history)
        planned = {}
        for index in sorted(candidates):
            keys = {}
            notes: Counter[NoteIdentity] = Counter()
            lengths = set()
            for staff, (scope, measures, meters) in enumerate(zip(group, measure_lists, meters_by_staff), 1):
                if scope.group != "pitched":
                    raise ValueError("Export key repair requires pitched, non-TAB staves.")
                measure = measures[index]
                timing = read_measure_timing(measure, meters[index])
                lengths.add(timing.length)
                if set(measure.iter("KeySig")) - set(timing.positions):
                    raise ValueError("Unsupported nested key signature in native source.")
                for item, position in timing.positions.items():
                    if item.tag == "KeySig" and 0 < position < timing.length:
                        if mscz._key_signature_kind(item) != "conventional":
                            raise ValueError("Custom or atonal interior keys cannot be repaired safely.")
                        context = mscz._key_context(scope, item, concert_pitch)
                        written = mscz._display_key(scope, context, False)
                        assert context.signature is not None and written is not None
                        mode = _field(item, "mode")
                        if mode in {"unknown", "none"}:
                            mode = None
                        if mode is not None and mode not in _MODES:
                            raise ValueError(f"Unsupported key mode {mode!r}.")
                        visible = _field(item, "visible", "1")
                        if visible not in {"0", "1"}:
                            raise ValueError("Unsupported native key visibility.")
                        key = KeyChange(staff, position, context.signature, written, mode, visible == "1")
                        identity = staff, position
                        if identity in keys and keys[identity] != key:
                            raise ValueError("Conflicting native key signatures at the same position.")
                        keys[identity] = key
                    elif item.tag == "Chord":
                        for note in item.findall("Note"):
                            if _field(note, "tuning", "0") not in {"0", "0.0"}:
                                raise ValueError("Microtonal note tuning cannot be verified for export key repair.")
                            pitch = _integer(_field(note, "pitch"), "native pitch")
                            notes[(staff, position, timing.durations[item], pitch)] += 1
            if len(lengths) != 1:
                raise ValueError("Native staves have different measure lengths.")
            if keys:
                planned[index] = KeyMeasure(lengths.pop(), tuple(keys.values()), notes)
        result.append(KeyPart(len(group), len(measure_lists[0]), planned))
    return tuple(result) if any(p.measures for p in result) else ()


def _xml_offsets(payload: bytes, root: ET.Element) -> dict[ET.Element, int]:
    """Expat byte offsets allow lossless insertions without XML reserialization."""
    parser = expat.ParserCreate()
    offsets = []
    encodings = []
    parser.StartElementHandler = lambda name, attrs: offsets.append(parser.CurrentByteIndex)
    parser.XmlDeclHandler = lambda version, encoding, standalone: encodings.append(encoding)
    parser.Parse(payload, True)
    if b"\x00" in payload or any(e and e.lower() not in {"utf-8", "us-ascii", "ascii"} for e in encodings):
        raise ValueError("Export key repair requires UTF-8 or ASCII MusicXML.")
    return dict(zip(root.iter(), offsets, strict=True))


def restore_exported_keys(payload: bytes, plan: tuple[KeyPart, ...]) -> bytes:
    """Insert only missing keys after exact note/timing and existing-key checks."""
    if not plan:
        return payload
    root = ET.fromstring(payload)
    if root.tag != "score-partwise":
        raise ValueError("Export key repair requires unnamespaced partwise MusicXML.")
    concert = root.find("defaults/concert-score") is not None
    declared = root.findall("part-list/score-part")
    parts = {p.get("id"): p for p in root.findall("part")}
    if len(declared) != len(plan) or len(parts) != len(plan):
        raise ValueError("Exported part count differs from the native score.")
    insertions: dict[ET.Element, list[ET.Element]] = {}
    for declaration, part_plan in zip(declared, plan):
        measures = parts[declaration.get("id")].findall("measure")
        if len(measures) != part_plan.measure_count:
            raise ValueError("Exported measure count differs from the native score.")
        divisions, staves = 0, 1
        shifts = {staff: 0 for staff in range(1, part_plan.staff_count + 1)}
        for index, measure in enumerate(measures):
            expected = part_plan.measures.get(index)
            cursor = Fraction(0)
            previous_onset = None
            anchors = {}
            actual_keys = {}
            notes: Counter[NoteIdentity] = Counter()
            for item in measure:
                is_chord = item.tag == "note" and item.find("chord") is not None
                if not is_chord:
                    anchors.setdefault(cursor, item)
                if item.tag == "attributes":
                    if item.find("divisions") is not None:
                        divisions = _integer(_field(item, "divisions"), "MusicXML divisions")
                        if divisions <= 0:
                            raise ValueError("MusicXML divisions must be positive.")
                    staves = _integer(_field(item, "staves", str(staves)), "staff count")
                    for transposition in item.findall("transpose"):
                        targets = ([_integer(transposition.get("number"), "transpose staff")]
                                   if "number" in transposition.attrib else list(shifts))
                        shift = _integer(_field(transposition, "chromatic"), "chromatic transposition")
                        shift += 12 * _integer(_field(transposition, "octave-change", "0"), "octave transposition")
                        if transposition.find("double") is not None:
                            raise ValueError("Doubled instrument transposition is unsupported for key repair.")
                        for staff in targets:
                            if staff not in shifts:
                                raise ValueError("Exported transposition refers to an unknown staff.")
                            shifts[staff] = shift
                    if expected:
                        for key in item.findall("key"):
                            targets = ([_integer(key.get("number"), "key staff")]
                                       if "number" in key.attrib else list(shifts))
                            for staff in targets:
                                if staff not in shifts:
                                    raise ValueError("Exported key refers to an unknown staff.")
                                identity = staff, cursor
                                if identity in actual_keys:
                                    raise ValueError("Duplicate exported keys at the same musical position.")
                                actual_keys[identity] = key
                if expected is None:
                    continue
                # A print/layout item may precede initial attributes.
                if (divisions <= 0 or staves != part_plan.staff_count) and item.tag in {"note", "backup", "forward"}:
                    raise ValueError("Missing divisions or mismatched exported staff count.")
                if item.tag in {"note", "backup", "forward"}:
                    grace = item.tag == "note" and item.find("grace") is not None
                    duration = (Fraction(0) if grace else
                                Fraction(_integer(_field(item, "duration"), "duration"), 4 * divisions))
                    if duration < 0 or (not grace and duration == 0):
                        raise ValueError("Invalid MusicXML duration while checking key changes.")
                    if item.tag == "backup":
                        cursor -= duration
                        previous_onset = None
                    elif item.tag == "forward":
                        cursor += duration
                        previous_onset = None
                    else:
                        onset = previous_onset if is_chord else cursor
                        if onset is None or onset + duration > expected.length:
                            raise ValueError("Invalid chord onset or overfull exported note.")
                        staff = _integer(_field(item, "staff", "1"), "note staff")
                        if staff not in shifts:
                            raise ValueError("Exported note refers to an unknown staff.")
                        pitch = item.find("pitch")
                        if pitch is not None:
                            step = _field(pitch, "step")
                            if step not in _STEPS:
                                raise ValueError("Invalid exported pitch step.")
                            sound = 12 * (_integer(_field(pitch, "octave"), "pitch octave") + 1)
                            sound += _STEPS[step]
                            sound += _integer(_field(pitch, "alter", "0"), "pitch alteration") + shifts[staff]
                            notes[(staff, onset, duration, sound)] += 1
                        elif item.find("rest") is None:
                            raise ValueError("Unpitched exported note cannot be aligned for key repair.")
                        previous_onset = onset
                        if not is_chord:
                            cursor += duration
                    if not 0 <= cursor <= expected.length:
                        raise ValueError("Exported cursor leaves its native measure.")
            if expected is None:
                continue
            if notes != expected.notes:
                raise ValueError("Exported pitches or rhythmic positions differ from the native score; key repair is unsafe.")
            wanted = {(key.staff, key.position): key for key in expected.keys}
            for identity in actual_keys:
                if 0 < identity[1] < expected.length and identity not in wanted:
                    raise ValueError("Unexpected exported interior key signature.")
            for identity, key in wanted.items():
                fifths = key.concert if concert else key.written
                existing = actual_keys.get(identity)
                if existing is not None:
                    if _integer(_field(existing, "fifths"), "key fifths") != fifths:
                        raise ValueError("Exported key signature disagrees with the native score.")
                    if key.mode and _field(existing, "mode") != key.mode:
                        raise ValueError("Exported key mode disagrees with the native score.")
                    if (existing.get("print-object", "yes") != "no") != key.visible:
                        raise ValueError("Exported key visibility disagrees with the native score.")
                    continue
                anchor = anchors.get(key.position)
                if anchor is None:
                    raise ValueError("Missing key has no verified MusicXML insertion boundary.")
                attributes = ET.Element("attributes")
                key_attrs = {"number": str(key.staff)} if part_plan.staff_count > 1 else {}
                if not key.visible:
                    key_attrs["print-object"] = "no"
                restored = ET.SubElement(attributes, "key", key_attrs)
                ET.SubElement(restored, "fifths").text = str(fifths)
                if key.mode:
                    ET.SubElement(restored, "mode").text = key.mode
                insertions.setdefault(anchor, []).append(attributes)
    if not insertions:
        return payload
    offsets = _xml_offsets(payload, root)
    edits = sorted((offsets[anchor], b"".join(ET.tostring(e, encoding="utf-8") for e in elements))
                   for anchor, elements in insertions.items())
    chunks = []
    previous = 0
    for offset, addition in edits:
        chunks.extend((payload[previous:offset], addition))
        previous = offset
    chunks.append(payload[previous:])
    result = b"".join(chunks)
    # Reparse and verify every repaired key; a second pass must be a byte no-op.
    if restore_exported_keys(result, plan) != result:
        raise ValueError("Export key repair did not reach a stable verified result.")
    return result
