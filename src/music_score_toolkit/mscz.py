"""Loss-minimizing transposition for MuseScore MSCX and MSCZ scores."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .keys import (
    KEY_SIGNATURES,
    calculate_shift,
    calculate_tpc_shift,
    normalize_conventional_key,
    spelling_for_key,
    tpc_alteration,
    tpc_for_pitch,
    tpc_pitch_class,
    transpose_key_signature_by_tpc,
    transpose_tpc,
)


class ScoreFormatError(ValueError):
    """Raised when a score container or XML document is unsupported."""


class PitchRangeError(ValueError):
    """Raised instead of silently clipping a note outside the MIDI range."""


@dataclass(frozen=True, slots=True)
class TransposeReport:
    from_key: str
    to_key: str
    semitone_shift: int
    notes_changed: int
    key_signatures_changed: int
    score_entries_changed: int = 1
    chord_symbols_changed: int = 0


@dataclass(frozen=True, slots=True)
class _StaffScope:
    content: ET.Element
    group: str
    instrument: ET.Element | None
    key_preference: str = "auto"


@dataclass(frozen=True, slots=True)
class _OpeningKey:
    signature: int | None
    key_signature: ET.Element | None
    is_custom: bool = False
    has_measure: bool = True


_ACCIDENTAL_TO_ALTERATION = {
    "accidentalTripleFlat": -3,
    "accidentalDoubleFlat": -2,
    "accidentalFlat": -1,
    "accidentalNaturalFlat": -1,
    "accidentalNatural": 0,
    "accidentalNaturalSharp": 1,
    "accidentalSharp": 1,
    "accidentalDoubleSharp": 2,
    "accidentalSharpSharp": 2,
    "accidentalTripleSharp": 3,
    "double flat": -2,
    "flat": -1,
    "natural": 0,
    "sharp": 1,
    "double sharp": 2,
}

_ALTERATION_TO_ACCIDENTAL = {
    -3: "accidentalTripleFlat",
    -2: "accidentalDoubleFlat",
    -1: "accidentalFlat",
    0: "accidentalNatural",
    1: "accidentalSharp",
    2: "accidentalDoubleSharp",
    3: "accidentalTripleSharp",
}

_ALTERATION_TO_LEGACY_ACCIDENTAL = {
    -2: "double flat",
    -1: "flat",
    0: "natural",
    1: "sharp",
    2: "double sharp",
}

_SCORE_BOUNDARIES = frozenset({"Part", "Staff", "Score"})


def _read_tpc(note: ET.Element, field_name: str) -> tuple[ET.Element | None, int | None]:
    fields = note.findall(field_name)
    if len(fields) > 1:
        raise ScoreFormatError(f"MuseScore Note contains duplicate {field_name} fields.")
    if not fields:
        return None, None
    field = fields[0]
    if field.text is None:
        raise ScoreFormatError(f"Invalid MuseScore {field_name} value: missing text")
    try:
        value = int(field.text)
        tpc_alteration(value)
    except ValueError as exc:
        raise ScoreFormatError(
            f"Invalid MuseScore {field_name} value: {field.text!r}"
        ) from exc
    return field, value


def _transpose_accidental_subtype(
    note: ET.Element,
    old_tpc: int | None,
    new_tpc: int | None,
    old_tpc2: int | None,
    new_tpc2: int | None,
    concert_pitch: bool | None,
) -> bool:
    """Update one standard accidental glyph without discarding its metadata."""

    accidental = note.find("Accidental")
    if accidental is None:
        return False
    subtype = accidental.find("subtype")
    if subtype is None or subtype.text not in _ACCIDENTAL_TO_ALTERATION:
        # Microtonal and extension accidentals remain outside the normalization
        # boundary; keep the complete node structurally intact.
        return False

    current_alteration = _ACCIDENTAL_TO_ALTERATION[subtype.text]
    matching_new_alterations = [
        (field_name, tpc_alteration(updated))
        for field_name, current, updated in (
            ("tpc", old_tpc, new_tpc),
            ("tpc2", old_tpc2, new_tpc2),
        )
        if current is not None
        and updated is not None
        and tpc_alteration(current) == current_alteration
    ]
    distinct_alterations = {value for _, value in matching_new_alterations}

    if len(distinct_alterations) == 1:
        updated_alteration = distinct_alterations.pop()
    elif matching_new_alterations:
        preferred_field = "tpc" if concert_pitch else "tpc2"
        preferred = [
            value for field_name, value in matching_new_alterations if field_name == preferred_field
        ]
        if not preferred:
            return False
        updated_alteration = preferred[0]
    elif old_tpc is None and old_tpc2 is None:
        preferred_value = new_tpc if concert_pitch or new_tpc2 is None else new_tpc2
        if preferred_value is None:
            return False
        updated_alteration = tpc_alteration(preferred_value)
    else:
        # A standard glyph that matches neither stored TPC may be deliberate
        # notation. Preserve it instead of guessing.
        return False

    if updated_alteration == current_alteration:
        return False
    if subtype.text.startswith("accidental"):
        subtype.text = _ALTERATION_TO_ACCIDENTAL[updated_alteration]
    elif updated_alteration in _ALTERATION_TO_LEGACY_ACCIDENTAL:
        subtype.text = _ALTERATION_TO_LEGACY_ACCIDENTAL[updated_alteration]
    else:
        # The legacy spelling dialect has no standard triple-accidental token.
        return False
    return True


def _concert_pitch_setting(root: ET.Element) -> bool | None:
    fields = root.findall("./Style/concertPitch")
    if len(fields) > 1:
        raise ScoreFormatError("Duplicate MuseScore concertPitch style fields.")
    if not fields:
        return None
    field = fields[0]
    if field.text is None:
        raise ScoreFormatError("Invalid MuseScore concertPitch value: missing text")
    value = field.text.strip().lower()
    if value in {"1", "true"}:
        return True
    if value in {"0", "false"}:
        return False
    raise ScoreFormatError(f"Invalid MuseScore concertPitch value: {field.text!r}")


def _parse_mscx(content: bytes | str) -> ET.Element:
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(content, parser=parser)
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise ScoreFormatError(f"Invalid MSCX XML: {exc}") from exc

    if root.tag != "museScore":
        raise ScoreFormatError("Invalid MSCX XML: root element must be <museScore>.")
    direct_scores = root.findall("Score")
    if len(direct_scores) != 1:
        raise ScoreFormatError(
            "Invalid MSCX XML: <museScore> must contain exactly one direct <Score>."
        )
    return root


def _key_signature_kind(key_signature: ET.Element) -> str:
    custom = key_signature.find("custom")
    custom_enabled = custom is not None and custom.text not in {None, "", "0"}
    has_key_field = any(
        key_signature.find(name) is not None
        for name in ("concertKey", "actualKey", "accidental")
    )
    if key_signature.findtext("mode", "").strip().lower() == "none" or (
        custom_enabled and key_signature.find("CustDef") is None and not has_key_field
    ):
        return "atonal"
    if key_signature.find("CustDef") is not None or custom_enabled:
        return "custom"
    return "conventional"


def _read_integer_field(parent: ET.Element, field_name: str) -> int | None:
    fields = parent.findall(field_name)
    if len(fields) > 1:
        raise ScoreFormatError(f"Duplicate MuseScore {field_name} fields.")
    if not fields:
        return None
    field = fields[0]
    if field.text is None:
        raise ScoreFormatError(f"Invalid MuseScore {field_name} value: missing text")
    try:
        return int(field.text)
    except ValueError as exc:
        raise ScoreFormatError(
            f"Invalid MuseScore {field_name} value: {field.text!r}"
        ) from exc


def _instrument_interval(instrument: ET.Element | None) -> tuple[int, int] | None:
    if instrument is None:
        return None
    chromatic = _read_integer_field(instrument, "transposeChromatic")
    diatonic = _read_integer_field(instrument, "transposeDiatonic")
    if chromatic is None and diatonic is None:
        return None
    if chromatic is None or diatonic is None:
        raise ScoreFormatError(
            "Instrument transposition must include both transposeChromatic and "
            "transposeDiatonic."
        )
    return diatonic, chromatic


def _instrument_tpc_shift(instrument: ET.Element | None) -> int:
    interval = _instrument_interval(instrument)
    if interval is None:
        return 0
    diatonic, chromatic = interval
    return chromatic * 7 - diatonic * 12


def _effective_key_preference(scope: _StaffScope) -> str:
    interval = _instrument_interval(scope.instrument)
    if interval is None:
        return scope.key_preference
    diatonic, chromatic = interval
    if chromatic % 12 == 0 and diatonic % 7 == 0:
        return "none"
    return scope.key_preference


def _staff_group(definition: ET.Element | None, instrument: ET.Element | None) -> str:
    group = None
    if definition is not None:
        staff_type = definition.find("StaffType")
        if staff_type is not None:
            group = staff_type.get("group")
    if instrument is not None:
        use_drumset = instrument.findtext("useDrumset", "").strip().lower()
        if use_drumset not in {"", "0", "false"}:
            return "percussion"
    return (group or "pitched").strip().lower()


def _staff_scopes(score: ET.Element) -> list[_StaffScope]:
    definitions: list[tuple[str | None, ET.Element, ET.Element | None, str]] = []
    definitions_by_id: dict[str, tuple[ET.Element, ET.Element | None, str]] = {}
    for part in score.findall("Part"):
        instrument = part.find("Instrument")
        preference = part.findtext("preferSharpFlat", "").strip().lower()
        if preference not in {"sharps", "flats", "none"}:
            preference = "auto"
        part_staves = part.findall("Staff")
        for definition in part_staves:
            staff_id = definition.get("id")
            if staff_id is None and len(part_staves) == 1:
                staff_id = part.get("id")
            definitions.append((staff_id, definition, instrument, preference))
            if staff_id is not None:
                if staff_id in definitions_by_id:
                    raise ScoreFormatError(
                        f"Invalid MSCX XML: duplicate staff definition id {staff_id!r}."
                    )
                definitions_by_id[staff_id] = (definition, instrument, preference)

    content_staves = score.findall("Staff")
    content_ids = [staff.get("id") for staff in content_staves if staff.get("id")]
    duplicate_ids = [item for item, count in Counter(content_ids).items() if count > 1]
    if duplicate_ids:
        raise ScoreFormatError(
            f"Invalid MSCX XML: duplicate score staff id {duplicate_ids[0]!r}."
        )

    scopes: list[_StaffScope] = []
    for index, content in enumerate(content_staves):
        match = definitions_by_id.get(content.get("id", ""))
        if match is None and index < len(definitions):
            _, definition, instrument, preference = definitions[index]
        elif match is None:
            definition = content
            instrument = None
            preference = "auto"
        else:
            definition, instrument, preference = match
        scopes.append(
            _StaffScope(
                content=content,
                group=_staff_group(definition, instrument),
                instrument=instrument,
                key_preference=preference,
            )
        )
    return scopes


def _bounded_descendants(
    parent: ET.Element,
    *,
    boundaries: frozenset[str] = _SCORE_BOUNDARIES,
) -> Iterator[ET.Element]:
    """Yield descendants without crossing into another score/staff context."""

    stack = list(reversed(parent))
    while stack:
        element = stack.pop()
        if element.tag in boundaries:
            continue
        yield element
        stack.extend(reversed(element))


def _bounded_elements(parent: ET.Element, tag: str) -> list[ET.Element]:
    return [element for element in _bounded_descendants(parent) if element.tag == tag]


def _unscoped_elements(score: ET.Element, tag: str) -> list[ET.Element]:
    return _bounded_elements(score, tag)


def _first_measure(staff: ET.Element) -> ET.Element | None:
    return next((child for child in staff if child.tag == "Measure"), None)


def _opening_key_signature(measure: ET.Element) -> ET.Element | None:
    music_has_started = False
    for element in _bounded_descendants(measure):
        if element.tag == "KeySig":
            return None if music_has_started else element
        if element.tag in {"Chord", "Rest", "Note"}:
            music_has_started = True
    return None


def _opening_key(
    scope: _StaffScope,
    concert_pitch: bool | None = None,
) -> _OpeningKey:
    measure = _first_measure(scope.content)
    if measure is None:
        return _OpeningKey(None, None, has_measure=False)
    key_signature = _opening_key_signature(measure)
    if key_signature is None:
        return _OpeningKey(0, None)
    if _key_signature_kind(key_signature) == "atonal":
        return _OpeningKey(None, key_signature, is_custom=True)

    concert = _read_integer_field(key_signature, "concertKey")
    if concert is not None:
        try:
            transpose_key_signature_by_tpc(concert, 0)
        except ValueError as exc:
            raise ScoreFormatError(
                f"Invalid MuseScore concertKey value: {concert}"
            ) from exc
        return _OpeningKey(concert, key_signature)

    actual = _read_integer_field(key_signature, "actualKey")
    legacy = _read_integer_field(key_signature, "accidental")
    if actual is None and legacy is None:
        raise ScoreFormatError("Conventional MuseScore KeySig has no key value.")
    if actual is not None:
        written = actual
        instrument_shift = _instrument_tpc_shift(scope.instrument)
    else:
        assert legacy is not None
        written = legacy
        instrument_shift = 0 if concert_pitch else _instrument_tpc_shift(scope.instrument)
    try:
        concert = transpose_key_signature_by_tpc(
            written,
            instrument_shift,
        )
    except ValueError as exc:
        raise ScoreFormatError(f"Invalid MuseScore key-signature value: {written}") from exc
    return _OpeningKey(concert, key_signature)


def _insert_initial_key_signature(
    scope: _StaffScope,
    tpc_shift: int,
    concert_pitch: bool | None = None,
) -> bool:
    opening = _opening_key(scope, concert_pitch)
    if not opening.has_measure or opening.key_signature is not None:
        return False
    measure = _first_measure(scope.content)
    assert measure is not None
    parent = next((child for child in measure if child.tag == "voice"), measure)
    key_signature = ET.Element("KeySig")
    concert_key = transpose_key_signature_by_tpc(0, tpc_shift)
    ET.SubElement(key_signature, "concertKey").text = str(concert_key)
    instrument_shift = _instrument_tpc_shift(scope.instrument)
    if instrument_shift and not concert_pitch:
        named_actual = transpose_key_signature_by_tpc(concert_key, -instrument_shift)
        actual_key = _normalize_written_signature(
            named_actual,
            _effective_key_preference(scope),
        )
        ET.SubElement(key_signature, "actualKey").text = str(actual_key)

    insertion_index = 0
    children = list(parent)
    while insertion_index < len(children) and children[insertion_index].tag in {
        "Clef",
        "Ambitus",
    }:
        insertion_index += 1
    parent.insert(insertion_index, key_signature)
    return True


def _normalize_written_signature(
    named_signature: int,
    preference: str = "auto",
) -> int:
    """Apply MuseScore's sharp/flat preference to a written key signature."""

    if preference == "sharps" and named_signature <= -5:
        return named_signature + 12
    if preference == "flats" and named_signature >= 5:
        return named_signature - 12
    if preference == "auto" and named_signature == 7:
        return -5
    return named_signature


def _transpose_key_signature(
    key_signature: ET.Element,
    tpc_shift: int,
    *,
    instrument_tpc_shift: int = 0,
    key_preference: str = "auto",
    concert_pitch: bool | None = None,
    write_changes: bool = True,
) -> bool:
    """Transpose MuseScore 4 and legacy conventional key fields in place."""

    kind = _key_signature_kind(key_signature)
    if kind == "atonal":
        return False

    fields: dict[str, tuple[ET.Element, int]] = {}
    if key_signature.find("accidental") is not None and (
        key_signature.find("concertKey") is not None
        or key_signature.find("actualKey") is not None
    ):
        raise ScoreFormatError(
            "MuseScore KeySig mixes legacy and modern key fields; no output was written."
        )
    for field_name in ("concertKey", "actualKey", "accidental"):
        matching_fields = key_signature.findall(field_name)
        if len(matching_fields) > 1:
            raise ScoreFormatError(f"Duplicate MuseScore {field_name} fields.")
        if not matching_fields:
            continue
        field = matching_fields[0]
        if field.text is None:
            raise ScoreFormatError(f"Invalid MuseScore {field_name} value: missing text")
        try:
            current = int(field.text)
        except ValueError as exc:
            raise ScoreFormatError(
                f"Invalid MuseScore {field_name} value: {field.text!r}"
            ) from exc
        try:
            transpose_key_signature_by_tpc(current, 0)
        except ValueError as exc:
            raise ScoreFormatError(f"Invalid MuseScore {field_name} value: {current}") from exc
        fields[field_name] = (field, current)

    if not fields and kind == "conventional":
        raise ScoreFormatError(
            "Conventional MuseScore KeySig has no supported key value; "
            "legacy subtype-only signatures are not safe to transpose."
        )
    if kind == "custom" and not fields and tpc_shift:
        raise ScoreFormatError(
            "Custom key signature has no base key that can be transposed safely; "
            "no output was written."
        )

    concert = fields.get("concertKey")
    updated_concert = (
        transpose_key_signature_by_tpc(concert[1], tpc_shift)
        if concert is not None
        else None
    )
    changed = False
    for field_name, (field, current) in fields.items():
        if field_name == "concertKey":
            assert updated_concert is not None
            updated = updated_concert
        elif field_name == "accidental" and concert_pitch:
            updated = transpose_key_signature_by_tpc(current, tpc_shift)
        elif concert is not None:
            # Recompute written/actual key from the raw instrument interval;
            # infer that relation from stored fields only for context-free XML.
            relation = (
                -instrument_tpc_shift
                if instrument_tpc_shift
                else current - concert[1]
            )
            assert updated_concert is not None
            named_written = transpose_key_signature_by_tpc(updated_concert, relation)
            updated = (
                named_written
                if relation == 0
                else _normalize_written_signature(named_written, key_preference)
            )
        elif instrument_tpc_shift:
            old_concert = transpose_key_signature_by_tpc(
                current,
                instrument_tpc_shift,
            )
            new_concert = transpose_key_signature_by_tpc(old_concert, tpc_shift)
            named_written = transpose_key_signature_by_tpc(
                new_concert,
                -instrument_tpc_shift,
            )
            updated = _normalize_written_signature(named_written, key_preference)
        else:
            updated = transpose_key_signature_by_tpc(current, tpc_shift)
        if updated != current and write_changes:
            field.text = str(updated)
            changed = True
    return changed


def _transpose_note(
    note: ET.Element,
    semitone_shift: int,
    tpc_shift: int,
    target_spelling: str,
    *,
    strict_pitch_range: bool,
    concert_pitch: bool | None,
    instrument_tpc_shift: int = 0,
    written_tpc_adjustment: int = 0,
    has_instrument_transposition: bool = False,
) -> bool:
    pitches = note.findall("pitch")
    if len(pitches) != 1 or pitches[0].text is None:
        if len(pitches) > 1:
            raise ScoreFormatError("Pitched MuseScore Note contains duplicate pitch fields.")
        raise ScoreFormatError("Pitched MuseScore Note has no MIDI pitch value.")
    pitch = pitches[0]
    try:
        current = int(pitch.text)
    except ValueError as exc:
        raise ScoreFormatError(f"Invalid MuseScore pitch value: {pitch.text!r}") from exc
    if not 0 <= current <= 127:
        raise ScoreFormatError(
            f"Invalid MuseScore MIDI pitch {current}; expected a value from 0 to 127."
        )

    tpc, old_tpc = _read_tpc(note, "tpc")
    tpc2, old_tpc2 = _read_tpc(note, "tpc2")
    if old_tpc is None:
        raise ScoreFormatError(
            "Pitched MuseScore Note has no primary TPC spelling; no output was written."
        )
    if old_tpc is not None and tpc_pitch_class(old_tpc) != current % 12:
        raise ScoreFormatError(
            f"MuseScore pitch {current} conflicts with concert TPC {old_tpc}; "
            "no output was written."
        )
    if old_tpc2 is not None and has_instrument_transposition:
        expected_written = transpose_tpc(old_tpc, -instrument_tpc_shift)
        if tpc_pitch_class(old_tpc2) != tpc_pitch_class(expected_written):
            raise ScoreFormatError(
                f"MuseScore written TPC {old_tpc2} conflicts with the instrument "
                "transposition; no output was written."
            )

    # An exact named-key no-op is also an exact document no-op. Validation
    # above still runs, but existing enharmonic choices and absent optional
    # tpc2 fields must not be normalized merely by inspecting the score.
    if semitone_shift == 0 and tpc_shift == 0:
        return False

    updated = current + semitone_shift
    was_clipped = False
    if not 0 <= updated <= 127:
        if strict_pitch_range:
            raise PitchRangeError(
                f"Transposition moves MIDI pitch {current} to {updated}; "
                "no output was written."
            )
        updated = min(127, max(0, updated))
        was_clipped = True

    changed = False
    if updated != current:
        pitch.text = str(updated)
        changed = True

    if was_clipped:
        new_tpc = tpc_for_pitch(updated, target_spelling)
        if new_tpc != old_tpc:
            assert tpc is not None
            tpc.text = str(new_tpc)
            changed = True
    else:
        new_tpc = transpose_tpc(old_tpc, tpc_shift)
        if new_tpc != old_tpc:
            assert tpc is not None
            tpc.text = str(new_tpc)
            changed = True

    new_tpc2 = None
    if old_tpc2 is not None:
        if has_instrument_transposition:
            recomputed = transpose_tpc(new_tpc, -instrument_tpc_shift)
            new_tpc2 = (
                transpose_tpc(recomputed, written_tpc_adjustment)
                if written_tpc_adjustment
                else recomputed
            )
        elif was_clipped:
            written_pitch_class = tpc_pitch_class(old_tpc2) + (updated - current)
            new_tpc2 = tpc_for_pitch(written_pitch_class, target_spelling)
        else:
            new_tpc2 = transpose_tpc(old_tpc2, tpc_shift)
        if new_tpc2 != old_tpc2:
            assert tpc2 is not None
            tpc2.text = str(new_tpc2)
            changed = True
    elif has_instrument_transposition:
        recomputed = transpose_tpc(new_tpc, -instrument_tpc_shift)
        new_tpc2 = (
            transpose_tpc(recomputed, written_tpc_adjustment)
            if written_tpc_adjustment
            else recomputed
        )
        tpc2 = ET.Element("tpc2")
        tpc2.text = str(new_tpc2)
        assert tpc is not None
        note.insert(list(note).index(tpc) + 1, tpc2)
        changed = True

    if _transpose_accidental_subtype(
        note,
        old_tpc,
        new_tpc,
        old_tpc2,
        new_tpc2,
        concert_pitch,
    ):
        changed = True
    return changed


def _written_tpc_adjustment(
    scope: _StaffScope,
    tpc_shift: int,
    concert_pitch: bool | None = None,
) -> int:
    instrument_shift = _instrument_tpc_shift(scope.instrument)
    if not instrument_shift:
        return 0
    opening = _opening_key(scope, concert_pitch)
    if opening.signature is None:
        return 0
    target_concert = transpose_key_signature_by_tpc(opening.signature, tpc_shift)
    named_written = transpose_key_signature_by_tpc(target_concert, -instrument_shift)
    normalized_written = _normalize_written_signature(
        named_written,
        _effective_key_preference(scope),
    )
    return normalized_written - named_written


def _transpose_harmony(harmony: ET.Element, tpc_shift: int) -> bool:
    changed = False
    for field_name in ("root", "base", "bass"):
        for field in harmony.iter(field_name):
            if field.text is None:
                raise ScoreFormatError(
                    f"Invalid MuseScore Harmony {field_name} value: missing text"
                )
            try:
                current = int(field.text)
            except ValueError as exc:
                raise ScoreFormatError(
                    f"Invalid MuseScore Harmony {field_name} value: {field.text!r}"
                ) from exc
            if current == -9:  # MuseScore's invalid/absent TPC sentinel.
                continue
            try:
                updated = transpose_tpc(current, tpc_shift)
            except ValueError as exc:
                raise ScoreFormatError(
                    f"Invalid MuseScore Harmony {field_name} value: {current}"
                ) from exc
            if updated != current:
                field.text = str(updated)
                changed = True
    return changed


def _opening_unscoped_key(score: ET.Element) -> int | None:
    key_signatures = _unscoped_elements(score, "KeySig")
    if key_signatures:
        key_signature = key_signatures[0]
        if _key_signature_kind(key_signature) == "atonal":
            return None
        for field_name in ("concertKey", "actualKey", "accidental"):
            value = _read_integer_field(key_signature, field_name)
            if value is None:
                continue
            try:
                transpose_key_signature_by_tpc(value, 0)
            except ValueError as exc:
                raise ScoreFormatError(
                    f"Invalid MuseScore {field_name} value: {value}"
                ) from exc
            return value
        raise ScoreFormatError("Conventional MuseScore KeySig has no key value.")
    if _unscoped_elements(score, "Note") or _unscoped_elements(score, "Measure"):
        return 0
    return None


def _validate_source_key(
    score_contexts: list[ET.Element],
    source_key: str,
    fallback_concert_pitch: bool | None = None,
) -> None:
    observed: list[int] = []
    for score in score_contexts:
        context_concert_pitch = _concert_pitch_setting(score)
        if context_concert_pitch is None:
            context_concert_pitch = fallback_concert_pitch
        scopes = _staff_scopes(score)
        for scope in scopes:
            if scope.group != "pitched":
                continue
            opening = _opening_key(scope, context_concert_pitch)
            if opening.signature is not None:
                observed.append(opening.signature)
        if not scopes:
            opening = _opening_unscoped_key(score)
            if opening is not None:
                observed.append(opening)

    if not observed:
        return
    distinct = set(observed)
    if len(distinct) > 1:
        rendered = ", ".join(str(item) for item in sorted(distinct))
        raise ScoreFormatError(
            "Opening concert key signatures disagree across pitched staves "
            f"({rendered}); use an unambiguous score or disable source-key validation."
        )
    expected = KEY_SIGNATURES[source_key]
    actual = observed[0]
    if actual != expected:
        actual_name = next(
            (name for name, signature in KEY_SIGNATURES.items() if signature == actual),
            str(actual),
        )
        raise ScoreFormatError(
            f"--from-key {source_key} does not match the score's opening concert key "
            f"({actual_name}). Correct --from-key or use --ignore-source-key."
        )


def _validate_score_context(
    score: ET.Element,
    *,
    transposition_changes_spelling: bool,
) -> list[_StaffScope]:
    scopes = _staff_scopes(score)
    if not transposition_changes_spelling:
        return scopes

    for scope in scopes:
        if _bounded_elements(scope.content, "InstrumentChange"):
            raise ScoreFormatError(
                "Scores with mid-score instrument changes cannot be transposed safely yet."
            )
        if _bounded_elements(scope.content, "StaffTypeChange"):
            raise ScoreFormatError(
                "Scores with mid-score staff-type changes cannot be transposed safely yet."
            )
        for staff_state in _bounded_elements(scope.content, "StaffState"):
            subtype = staff_state.findtext("subtype", "").strip().lower()
            if subtype == "instrument" or staff_state.find(".//Instrument") is not None:
                raise ScoreFormatError(
                    "Scores with legacy mid-score instrument changes cannot be "
                    "transposed safely yet."
                )
        has_content = (
            bool(_bounded_elements(scope.content, "Note"))
            or bool(_bounded_elements(scope.content, "Harmony"))
        )
        if scope.group == "tablature" and has_content:
            raise ScoreFormatError(
                "Tablature requires chord-aware refretting and cannot be transposed safely; "
                "no output was written."
            )
        if scope.group not in {"pitched", "percussion", "tablature"} and has_content:
            raise ScoreFormatError(
                f"Unsupported MuseScore staff group {scope.group!r}; no output was written."
            )
        if scope.group == "pitched" and any(
            note.find("fret") is not None or note.find("string") is not None
            for note in _bounded_elements(scope.content, "Note")
        ):
            raise ScoreFormatError(
                "Fretted notes require chord-aware refretting and cannot be transposed "
                "safely; no output was written."
            )
        if scope.group == "pitched" and _instrument_tpc_shift(scope.instrument):
            key_signatures = _bounded_elements(scope.content, "KeySig")
            if len(key_signatures) > 1 and has_content:
                raise ScoreFormatError(
                    "Transposing-instrument staves with mid-score key changes require "
                    "tick-aware written-pitch respelling; no output was written."
                )
        if (
            scope.group == "pitched"
            and _bounded_elements(scope.content, "FretDiagram")
        ):
            raise ScoreFormatError(
                "Fret diagrams cannot be regenerated safely during direct XML transposition; "
                "no output was written."
            )
    if not scopes:
        if any(
            note.find("fret") is not None or note.find("string") is not None
            for note in _unscoped_elements(score, "Note")
        ):
            raise ScoreFormatError(
                "Fretted notes require chord-aware refretting and cannot be transposed "
                "safely; no output was written."
            )
        if _unscoped_elements(score, "FretDiagram"):
            raise ScoreFormatError(
                "Fret diagrams cannot be regenerated safely during direct XML transposition; "
                "no output was written."
            )
    return scopes


def transpose_mscx(
    content: bytes | str,
    from_key: str,
    to_key: str,
    *,
    strict_pitch_range: bool = True,
    concert_pitch: bool | None = None,
    validate_source_key: bool = True,
) -> tuple[bytes, TransposeReport]:
    """Transpose one MuseScore XML document and return bytes plus a report.

    Pitch, TPC spelling, conventional key signatures, and chord-symbol roots
    are modified. Chords, durations, rests, ties, lyrics, layout, and other
    score elements remain structurally untouched. ``concert_pitch`` is a
    fallback for scores without an inline style setting. Source-key validation
    can be disabled only for deliberately ambiguous or partial scores.
    """

    source_key = normalize_conventional_key(from_key)
    target_key = normalize_conventional_key(to_key)
    shift = calculate_shift(source_key, target_key)
    note_tpc_shift = calculate_tpc_shift(source_key, target_key)
    root = _parse_mscx(content)
    score_contexts = list(root.iter("Score"))
    if validate_source_key:
        _validate_source_key(score_contexts, source_key, concert_pitch)

    transposition_changes_spelling = bool(shift or note_tpc_shift)
    scoped_contexts = [
        (
            score,
            _validate_score_context(
                score,
                transposition_changes_spelling=transposition_changes_spelling,
            ),
        )
        for score in score_contexts
    ]

    note_count = 0
    key_signature_count = 0
    harmony_count = 0
    target_spelling = spelling_for_key(target_key)
    for score, scopes in scoped_contexts:
        context_concert_pitch = _concert_pitch_setting(score)
        if context_concert_pitch is None:
            context_concert_pitch = concert_pitch

        for scope in scopes:
            if scope.group != "pitched":
                continue
            instrument_shift = _instrument_tpc_shift(scope.instrument)
            instrument_interval = _instrument_interval(scope.instrument)
            has_instrument_transposition = instrument_interval not in {None, (0, 0)}
            written_adjustment = _written_tpc_adjustment(
                scope,
                note_tpc_shift,
                context_concert_pitch,
            )
            for note in _bounded_elements(scope.content, "Note"):
                if _transpose_note(
                    note,
                    shift,
                    note_tpc_shift,
                    target_spelling,
                    strict_pitch_range=strict_pitch_range,
                    concert_pitch=context_concert_pitch,
                    instrument_tpc_shift=instrument_shift,
                    written_tpc_adjustment=written_adjustment,
                    has_instrument_transposition=has_instrument_transposition,
                ):
                    note_count += 1
            for harmony in _bounded_elements(scope.content, "Harmony"):
                harmony_shift = note_tpc_shift
                if not context_concert_pitch:
                    harmony_shift += written_adjustment
                if _transpose_harmony(harmony, harmony_shift):
                    harmony_count += 1
            for key_signature in _bounded_elements(scope.content, "KeySig"):
                if _transpose_key_signature(
                    key_signature,
                    note_tpc_shift,
                    instrument_tpc_shift=instrument_shift,
                    key_preference=_effective_key_preference(scope),
                    concert_pitch=context_concert_pitch,
                    write_changes=transposition_changes_spelling,
                ):
                    key_signature_count += 1
            if transposition_changes_spelling and _insert_initial_key_signature(
                scope,
                note_tpc_shift,
                context_concert_pitch,
            ):
                key_signature_count += 1

        if not scopes:
            for note in _unscoped_elements(score, "Note"):
                if _transpose_note(
                    note,
                    shift,
                    note_tpc_shift,
                    target_spelling,
                    strict_pitch_range=strict_pitch_range,
                    concert_pitch=context_concert_pitch,
                ):
                    note_count += 1
            for harmony in _unscoped_elements(score, "Harmony"):
                if _transpose_harmony(harmony, note_tpc_shift):
                    harmony_count += 1
            for key_signature in _unscoped_elements(score, "KeySig"):
                if _transpose_key_signature(
                    key_signature,
                    note_tpc_shift,
                    write_changes=transposition_changes_spelling,
                ):
                    key_signature_count += 1

    if note_count == 0 and key_signature_count == 0 and harmony_count == 0:
        original = content if isinstance(content, bytes) else content.encode("utf-8")
        return original, TransposeReport(
            from_key=source_key,
            to_key=target_key,
            semitone_shift=shift,
            notes_changed=0,
            key_signatures_changed=0,
            score_entries_changed=0,
            chord_symbols_changed=0,
        )

    had_declaration = (
        content.lstrip().startswith(b"<?xml")
        if isinstance(content, bytes)
        else content.lstrip().startswith("<?xml")
    )
    rendered = ET.tostring(root, encoding="utf-8", xml_declaration=had_declaration)
    return rendered, TransposeReport(
        from_key=source_key,
        to_key=target_key,
        semitone_shift=shift,
        notes_changed=note_count,
        key_signatures_changed=key_signature_count,
        score_entries_changed=1,
        chord_symbols_changed=harmony_count,
    )


def _read_archive_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        return archive.read(info)
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
    ) as exc:
        raise ScoreFormatError(
            f"Unable to read MSCZ member {info.filename!r}: {exc}"
        ) from exc


def _archive_concert_pitch_setting(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
) -> bool | None:
    style_infos = [
        info
        for info in infos
        if not info.is_dir() and info.filename.lower() == "score_style.mss"
    ]
    if not style_infos:
        return None
    if len(style_infos) > 1:
        raise ScoreFormatError("MSCZ archive contains ambiguous score_style.mss members.")

    payload = _read_archive_member(archive, style_infos[0])
    try:
        style_root = ET.fromstring(payload)
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise ScoreFormatError(f"Invalid MSCZ score_style.mss XML: {exc}") from exc
    if style_root.tag != "museScore":
        raise ScoreFormatError(
            "Invalid MSCZ score_style.mss XML: root element must be <museScore>."
        )
    return _concert_pitch_setting(style_root)


def _validate_mscz_manifest(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
) -> None:
    container_infos = [
        info for info in infos if info.filename == "META-INF/container.xml"
    ]
    if not container_infos:
        return
    container_info = container_infos[0]
    payload = _read_archive_member(archive, container_info)
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise ScoreFormatError(f"Invalid MSCZ container manifest: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1] != "container":
        raise ScoreFormatError("Invalid MSCZ container manifest root.")

    names = {info.filename for info in infos}
    rootfiles = [
        element
        for element in root.iter()
        if isinstance(element.tag, str) and element.tag.rsplit("}", 1)[-1] == "rootfile"
    ]
    if not rootfiles:
        raise ScoreFormatError("MSCZ container manifest has no rootfile entries.")
    references: list[str] = []
    for rootfile in rootfiles:
        reference = rootfile.get("full-path", "")
        path = PurePosixPath(reference)
        if (
            not reference
            or path.is_absolute()
            or ".." in path.parts
            or reference not in names
        ):
            raise ScoreFormatError(
                f"MSCZ container manifest references an invalid member {reference!r}."
            )
        references.append(reference)
    if not any(reference.lower().endswith(".mscx") for reference in references):
        raise ScoreFormatError("MSCZ container manifest does not reference an MSCX score.")


def transpose_mscz(
    input_path: str | Path,
    output_path: str | Path,
    from_key: str,
    to_key: str,
    *,
    strict_pitch_range: bool = True,
    validate_source_key: bool = True,
) -> TransposeReport:
    """Transpose every MSCX score entry inside an MSCZ archive atomically."""

    source = Path(input_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input MSCZ file does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    output_mode = stat.S_IMODE(
        (destination if destination.exists() else source).stat().st_mode
    )

    note_count = 0
    signature_count = 0
    score_count = 0
    harmony_count = 0
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name

        try:
            source_zip = zipfile.ZipFile(source, "r")
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise ScoreFormatError(f"Not a valid MSCZ/ZIP archive: {source}") from exc

        with source_zip:
            infos = source_zip.infolist()
            duplicates = [
                name
                for name, count in Counter(info.filename for info in infos).items()
                if count > 1
            ]
            if duplicates:
                raise ScoreFormatError(
                    f"MSCZ archive contains duplicate member {duplicates[0]!r}."
                )
            score_entries = [
                info
                for info in infos
                if not info.is_dir() and info.filename.lower().endswith(".mscx")
            ]
            if not score_entries:
                raise ScoreFormatError("MSCZ archive does not contain an .mscx score entry.")
            try:
                bad_member = source_zip.testzip()
            except (
                EOFError,
                NotImplementedError,
                OSError,
                RuntimeError,
                zipfile.BadZipFile,
            ) as exc:
                raise ScoreFormatError(f"Unable to validate MSCZ archive: {exc}") from exc
            if bad_member is not None:
                raise ScoreFormatError(
                    f"MSCZ archive contains a corrupt member {bad_member!r}."
                )
            _validate_mscz_manifest(source_zip, infos)
            concert_pitch = _archive_concert_pitch_setting(source_zip, infos)

            with zipfile.ZipFile(temp_name, "w") as target_zip:
                target_zip.comment = source_zip.comment
                for info in infos:
                    payload = _read_archive_member(source_zip, info)
                    if info in score_entries:
                        payload, report = transpose_mscx(
                            payload,
                            from_key,
                            to_key,
                            strict_pitch_range=strict_pitch_range,
                            concert_pitch=concert_pitch,
                            validate_source_key=validate_source_key,
                        )
                        note_count += report.notes_changed
                        signature_count += report.key_signatures_changed
                        score_count += report.score_entries_changed
                        harmony_count += report.chord_symbols_changed
                    target_zip.writestr(info, payload)

        if score_count == 0:
            shutil.copyfile(source, temp_name)
        os.chmod(temp_name, output_mode)
        os.replace(temp_name, destination)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                # Best-effort cleanup must not hide the score-processing error.
                pass

    return TransposeReport(
        from_key=normalize_conventional_key(from_key),
        to_key=normalize_conventional_key(to_key),
        semitone_shift=calculate_shift(from_key, to_key),
        notes_changed=note_count,
        key_signatures_changed=signature_count,
        score_entries_changed=score_count,
        chord_symbols_changed=harmony_count,
    )
