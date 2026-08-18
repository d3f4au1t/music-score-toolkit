"""Loss-minimizing transposition for MuseScore MSCX and MSCZ scores."""

from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .keys import (
    calculate_shift,
    calculate_tpc_shift,
    normalize_conventional_key,
    spelling_for_key,
    tpc_alteration,
    tpc_for_pitch,
    tpc_pitch_class,
    transpose_key_signature,
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


def _read_tpc(note: ET.Element, field_name: str) -> tuple[ET.Element | None, int | None]:
    field = note.find(field_name)
    if field is None:
        return None, None
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
    subtype.text = _ALTERATION_TO_ACCIDENTAL[updated_alteration]
    return True


def _concert_pitch_setting(root: ET.Element) -> bool | None:
    field = root.find(".//Style/concertPitch")
    if field is None or field.text is None:
        return None
    value = field.text.strip().lower()
    if value in {"1", "true"}:
        return True
    if value in {"0", "false"}:
        return False
    return None


def _archive_concert_pitch_setting(archive: zipfile.ZipFile) -> bool | None:
    for info in archive.infolist():
        if Path(info.filename).name != "score_style.mss":
            continue
        try:
            style_root = ET.fromstring(archive.read(info))
        except (ET.ParseError, OSError, ValueError):
            return None
        return _concert_pitch_setting(style_root)
    return None


def _transpose_key_signature(
    key_signature: ET.Element,
    semitone_shift: int,
    target_spelling: str,
    *,
    write_changes: bool = True,
) -> bool:
    """Transpose MuseScore 4 and legacy conventional key fields in place."""

    custom = key_signature.find("custom")
    if key_signature.find("CustDef") is not None or (
        custom is not None and custom.text not in {None, "", "0"}
    ):
        return False

    changed = False
    for field_name in ("concertKey", "actualKey", "accidental"):
        field = key_signature.find(field_name)
        if field is None or field.text is None:
            continue
        try:
            current = int(field.text)
        except ValueError as exc:
            raise ScoreFormatError(
                f"Invalid MuseScore {field_name} value: {field.text!r}"
            ) from exc
        try:
            updated = transpose_key_signature(current, semitone_shift, target_spelling)
        except ValueError as exc:
            raise ScoreFormatError(f"Invalid MuseScore {field_name} value: {current}") from exc
        if updated != current and write_changes:
            field.text = str(updated)
            changed = True
    return changed


def transpose_mscx(
    content: bytes | str,
    from_key: str,
    to_key: str,
    *,
    strict_pitch_range: bool = True,
    concert_pitch: bool | None = None,
) -> tuple[bytes, TransposeReport]:
    """Transpose one MuseScore XML document and return bytes plus a report.

    Only pitch, TPC spelling, and conventional key-signature fields are
    modified. Chords, durations, rests, ties, lyrics, layout, and other score
    elements remain structurally untouched. ``concert_pitch`` selects which
    TPC controls an ambiguous explicit accidental; when omitted, inline style
    is used and MuseScore's written-pitch view is the fallback.
    """

    source_key = normalize_conventional_key(from_key)
    target_key = normalize_conventional_key(to_key)
    shift = calculate_shift(source_key, target_key)
    note_tpc_shift = calculate_tpc_shift(source_key, target_key)
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ScoreFormatError(f"Invalid MSCX XML: {exc}") from exc

    if concert_pitch is None:
        concert_pitch = _concert_pitch_setting(root)

    note_count = 0
    target_spelling = spelling_for_key(target_key)
    for note in root.iter("Note"):
        pitch = note.find("pitch")
        if pitch is None or pitch.text is None:
            continue
        try:
            current = int(pitch.text)
        except ValueError as exc:
            raise ScoreFormatError(f"Invalid MuseScore pitch value: {pitch.text!r}") from exc
        updated = current + shift
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

        tpc, old_tpc = _read_tpc(note, "tpc")
        tpc2, old_tpc2 = _read_tpc(note, "tpc2")
        if old_tpc is None and not was_clipped and shift == 0 and note_tpc_shift == 0:
            new_tpc = None
        elif old_tpc is None or was_clipped:
            new_tpc = tpc_for_pitch(updated, target_spelling)
            if tpc is None:
                tpc = ET.SubElement(note, "tpc")
                tpc.text = str(new_tpc)
                changed = True
            elif new_tpc != old_tpc:
                tpc.text = str(new_tpc)
                changed = True
        else:
            new_tpc = transpose_tpc(old_tpc, note_tpc_shift)
            if new_tpc != old_tpc:
                assert tpc is not None
                tpc.text = str(new_tpc)
                changed = True

        new_tpc2 = None
        if old_tpc2 is not None:
            if was_clipped:
                written_pitch_class = tpc_pitch_class(old_tpc2) + (updated - current)
                new_tpc2 = tpc_for_pitch(written_pitch_class, target_spelling)
            else:
                new_tpc2 = transpose_tpc(old_tpc2, note_tpc_shift)
            if new_tpc2 != old_tpc2:
                assert tpc2 is not None
                tpc2.text = str(new_tpc2)
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
        if changed:
            note_count += 1

    key_signature_count = 0
    for key_signature in root.iter("KeySig"):
        if source_key == target_key:
            _transpose_key_signature(
                key_signature,
                shift,
                target_spelling,
                write_changes=False,
            )
        elif _transpose_key_signature(key_signature, shift, target_spelling):
            key_signature_count += 1

    if note_count == 0 and key_signature_count == 0:
        original = content if isinstance(content, bytes) else content.encode("utf-8")
        return original, TransposeReport(
            from_key=source_key,
            to_key=target_key,
            semitone_shift=shift,
            notes_changed=0,
            key_signatures_changed=0,
            score_entries_changed=0,
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
    )


def transpose_mscz(
    input_path: str | Path,
    output_path: str | Path,
    from_key: str,
    to_key: str,
    *,
    strict_pitch_range: bool = True,
) -> TransposeReport:
    """Transpose every MSCX score entry inside an MSCZ archive atomically."""

    source = Path(input_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input MSCZ file does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    note_count = 0
    signature_count = 0
    score_count = 0
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
        except zipfile.BadZipFile as exc:
            raise ScoreFormatError(f"Not a valid MSCZ/ZIP archive: {source}") from exc

        with source_zip, zipfile.ZipFile(temp_name, "w") as target_zip:
            score_entries = [info.filename for info in source_zip.infolist() if info.filename.endswith(".mscx")]
            if not score_entries:
                raise ScoreFormatError("MSCZ archive does not contain an .mscx score entry.")
            concert_pitch = _archive_concert_pitch_setting(source_zip)

            for info in source_zip.infolist():
                payload = source_zip.read(info.filename)
                if info.filename.endswith(".mscx"):
                    payload, report = transpose_mscx(
                        payload,
                        from_key,
                        to_key,
                        strict_pitch_range=strict_pitch_range,
                        concert_pitch=concert_pitch,
                    )
                    note_count += report.notes_changed
                    signature_count += report.key_signatures_changed
                    score_count += report.score_entries_changed
                target_zip.writestr(info, payload)

        os.replace(temp_name, destination)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)

    return TransposeReport(
        from_key=normalize_conventional_key(from_key),
        to_key=normalize_conventional_key(to_key),
        semitone_shift=calculate_shift(from_key, to_key),
        notes_changed=note_count,
        key_signatures_changed=signature_count,
        score_entries_changed=score_count,
    )
