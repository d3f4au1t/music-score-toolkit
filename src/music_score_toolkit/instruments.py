"""Change a part's written instrument pitch while preserving its sounding pitches."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .keys import normalize_key, transpose_key_signature_by_tpc, transpose_tpc
from .mscz import (
    ScoreFormatError,
    _bounded_elements,
    _display_key,
    _encode_xml_text,
    _instrument_interval,
    _instrument_tpc_shift,
    _key_context,
    _key_signature_kind,
    _normalize_written_signature,
    _opening_key,
    _parse_mscx,
    _read_tpc,
    _render_mscx,
    _rewrite_mscz,
    _score_contexts,
    _staff_key_regions,
    _staff_scopes,
    _transpose_accidental_subtype,
    _transpose_harmony,
    _transpose_key_signature,
    _transpose_note,
    _unscoped_elements,
)

# Sounding interval below written pitch. Octave-transposing variants need their
# own explicit presets; a key name alone does not imply bass-clarinet register.
INSTRUMENT_INTERVALS = {"C": (0, 0), "Bb": (-1, -2), "A": (-2, -3),
                        "F": (-4, -7), "Eb": (-5, -9)}


@dataclass(frozen=True, slots=True)
class InstrumentConversionReport:
    target_pitch: str
    part: int
    written_notes_changed: int
    key_signatures_changed: int
    chord_symbols_changed: int
    score_entries_changed: int


def _set_text(parent: ET.Element, tag: str, text: str) -> None:
    fields = parent.findall(tag)
    if len(fields) > 1:
        raise ScoreFormatError(f"Duplicate MuseScore {tag} fields.")
    field = fields[0] if fields else ET.SubElement(parent, tag)
    field.text = text


def _written_key(concert_key: int, interval: int, preference: str) -> int:
    key = transpose_key_signature_by_tpc(concert_key, -interval)
    return _normalize_written_signature(key, preference) if interval else key


def retarget_instrument_mscx(
    content: bytes | str,
    to_instrument: str = "Bb",
    *,
    part: int | None = None,
    part_name: str | None = None,
    concert_pitch: bool | None = None,
) -> tuple[bytes, InstrumentConversionReport]:
    """Set one part's instrument transposition and show it in written pitch.

    ``part`` is one-based and required for multi-part scores. This changes the
    transposition and part labels, not the playback sound or clef. Linked
    excerpts and instrument changes require a standalone part prepared in
    MuseScore. Key changes are supported at measure boundaries across voices,
    and within a single voice without explicit cursor movements.
    """

    target = normalize_key(to_instrument)
    if target not in INSTRUMENT_INTERVALS:
        raise ValueError("Supported instrument pitches: C, Bb, A, F, Eb.")
    if part is not None and (not isinstance(part, int) or isinstance(part, bool) or part < 1):
        raise ValueError("--part must be a positive, one-based part number.")
    if part_name is not None and not part_name.strip():
        raise ValueError("Part name cannot be empty.")
    document = _parse_mscx(content)
    if not document.root.get("version", "4.0").startswith("4."):
        raise ScoreFormatError(
            "Instrument conversion requires MuseScore 4 format; "
            "open and save this score in MuseScore 4 first."
        )
    contexts = _score_contexts(document.root, concert_pitch)
    if len(contexts) != 1 or document.root.find(".//Excerpt") is not None:
        raise ScoreFormatError(
            "Instrument conversion requires a score without linked excerpts; "
            "export the desired part as a standalone MSCZ first."
        )
    score, source_concert_pitch = contexts[0]
    parts = [child for child in score if child.tag in {"Part", "SharedPart"}]
    if not parts:
        raise ScoreFormatError("Instrument conversion requires explicit part definitions.")
    if part is None:
        if len(parts) != 1:
            choices = "; ".join(
                f"{index}: {item.findtext('trackName') or 'Unnamed part'}"
                for index, item in enumerate(parts, 1)
            )
            raise ScoreFormatError(f"Choose a part with --part NUMBER ({choices}).")
        part = 1
    if part > len(parts):
        raise ScoreFormatError(f"Part {part} does not exist; the score has {len(parts)} parts.")
    if source_concert_pitch and len(parts) > 1:
        raise ScoreFormatError(
            "Switch this multi-part score to written pitch in MuseScore first, "
            "or export the desired part as a standalone MSCZ."
        )
    selected_part = parts[part - 1]
    if len(selected_part.findall("Instrument")) != 1:
        raise ScoreFormatError("The selected part must have one instrument definition.")
    instrument = selected_part.find("Instrument")
    assert instrument is not None
    scopes = [scope for scope in _staff_scopes(score) if scope.instrument is instrument]
    if not scopes:
        raise ScoreFormatError("The selected part has no score staves.")
    if any(_unscoped_elements(score, tag) for tag in ("Note", "KeySig", "Harmony")):
        raise ScoreFormatError("Instrument conversion requires staff-scoped musical content.")

    old_interval = _instrument_interval(instrument) or (0, 0)
    old_tpc_interval = _instrument_tpc_shift(instrument)
    new_diatonic, new_chromatic = INSTRUMENT_INTERVALS[target]
    new_tpc_interval = new_chromatic * 7 - new_diatonic * 12
    before = ET.tostring(document.root)
    note_count = key_count = harmony_count = 0
    for scope in scopes:
        if scope.group != "pitched":
            raise ScoreFormatError("Instrument conversion supports pitched staves only.")
        for tag in ("InstrumentChange", "StaffState", "StaffTypeChange", "FretDiagram"):
            if _bounded_elements(scope.content, tag):
                raise ScoreFormatError(f"Instrument conversion does not yet support {tag}.")
        notes = _bounded_elements(scope.content, "Note")
        if any(note.find(tag) is not None for note in notes for tag in ("fret", "string")):
            raise ScoreFormatError("Instrument conversion cannot safely refret tablature.")
        opening = _opening_key(scope, source_concert_pitch)
        signatures = _bounded_elements(scope.content, "KeySig")
        if any(_key_signature_kind(key) != "conventional" for key in signatures):
            raise ScoreFormatError("Instrument conversion requires conventional key signatures.")
        preference = scope.key_preference
        for region in _staff_key_regions(scope, source_concert_pitch):
            if not region.elements:
                continue
            concert_key = region.key.signature
            if concert_key is None:
                raise ScoreFormatError("Instrument conversion requires measure-scoped music.")
            destination_key = _written_key(concert_key, new_tpc_interval, preference)
            source_display_key = _display_key(scope, region.key, source_concert_pitch)
            assert source_display_key is not None
            for element in region.elements:
                if element.tag == "Harmony":
                    harmony_count += _transpose_harmony(
                        element, destination_key - source_display_key,
                    )
                    continue
                note = element
                original_note = ET.tostring(note)
                _transpose_note(
                    note, 0, 0, "sharp", strict_pitch_range=True,
                    concert_pitch=source_concert_pitch,
                    instrument_tpc_shift=old_tpc_interval,
                    has_instrument_transposition=old_interval != (0, 0),
                )
                _, concert_tpc = _read_tpc(note, "tpc")
                _, source_written_tpc = _read_tpc(note, "tpc2")
                assert concert_tpc is not None
                source_display_tpc = concert_tpc
                if not source_concert_pitch:
                    source_display_tpc = (
                        source_written_tpc if source_written_tpc is not None
                        else transpose_tpc(concert_tpc, source_display_key - concert_key)
                    )
                destination_tpc = transpose_tpc(concert_tpc, destination_key - concert_key)
                _set_text(note, "tpc2", str(destination_tpc))
                _transpose_accidental_subtype(
                    note, source_display_tpc, destination_tpc, None, None, True,
                )
                note_count += ET.tostring(note) != original_note
        for key in signatures:
            concert_key = _key_context(scope, key, source_concert_pitch).signature
            assert concert_key is not None
            destination_key = _written_key(concert_key, new_tpc_interval, preference)
            original_key = ET.tostring(key)
            # Validate old key fields before translating legacy notation to the
            # modern concert/written pair. Keep courtesy/layout/identity fields.
            _transpose_key_signature(key, 0, write_changes=False)
            for field in key.findall("accidental"):
                key.remove(field)
            _set_text(key, "concertKey", str(concert_key))
            _set_text(key, "actualKey", str(destination_key))
            key_count += ET.tostring(key) != original_key
        if opening.key_signature is None and opening.has_measure:
            concert_key = 0
            destination_key = _written_key(concert_key, new_tpc_interval, preference)
            measure = next(child for child in scope.content if child.tag == "Measure")
            parent = measure.find("voice")
            if parent is None:
                parent = measure
            key = ET.Element("KeySig")
            _set_text(key, "concertKey", str(concert_key))
            _set_text(key, "actualKey", str(destination_key))
            insertion_index = 0
            while insertion_index < len(parent) and parent[insertion_index].tag in {"Clef", "Ambitus"}:
                insertion_index += 1
            parent.insert(insertion_index, key)
            key_count += 1

    _set_text(instrument, "transposeDiatonic", str(new_diatonic))
    _set_text(instrument, "transposeChromatic", str(new_chromatic))
    display_pitch = target.replace("b", "♭")
    name = part_name.strip() if part_name is not None else f"{display_pitch} part"
    _set_text(selected_part, "trackName", name)
    for field in ("longName", "shortName", "trackName"):
        _set_text(instrument, field, name)
    styles = score.findall("Style")
    if len(styles) > 1:
        raise ScoreFormatError("Instrument conversion requires one unambiguous score style.")
    style = styles[0] if styles else None
    if style is None:
        style = ET.Element("Style")
        score.insert(0, style)
    _set_text(style, "concertPitch", "0")
    changed = ET.tostring(document.root) != before
    rendered = (
        _render_mscx(document) if changed
        else content if isinstance(content, bytes) else _encode_xml_text(content)
    )
    return rendered, InstrumentConversionReport(
        target, part, note_count, key_count, harmony_count, int(changed),
    )


def retarget_instrument_mscz(
    input_path: str | Path,
    output_path: str | Path,
    to_instrument: str = "Bb",
    *,
    part: int | None = None,
    part_name: str | None = None,
) -> InstrumentConversionReport:
    """Atomically rewrite a selected part while preserving archive assets."""

    reports = _rewrite_mscz(
        input_path, output_path,
        lambda payload, concert_pitch: retarget_instrument_mscx(
            payload, to_instrument, part=part, part_name=part_name, concert_pitch=concert_pitch,
        ),
        single_score=True,
    )
    return reports[0]
