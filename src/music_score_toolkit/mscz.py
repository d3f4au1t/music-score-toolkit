"""Loss-minimizing transposition for MuseScore MSCX and MSCZ scores."""

from __future__ import annotations

import copy
import lzma
import os
import re
import shutil
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

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


@dataclass(frozen=True, slots=True)
class _MscxDocument:
    root: ET.Element
    prolog: tuple[ET.Element, ...]
    epilog: tuple[ET.Element, ...]
    has_xml_declaration: bool
    xml_version: str
    standalone: str | None
    has_doctype: bool
    has_namespaces: bool


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

_SCORE_BOUNDARIES = frozenset({"Part", "SharedPart", "Staff", "Score"})

_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_ARCHIVE_MEMBER_SIZE = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_SIZE = 4 * 1024 * 1024 * 1024
_MAX_COMPRESSED_RATIO = 500
_MAX_SCORE_XML_SIZE = 256 * 1024 * 1024
_MAX_METADATA_XML_SIZE = 16 * 1024 * 1024
_COPY_CHUNK_SIZE = 1024 * 1024

_ARCHIVE_READ_ERRORS = (
    EOFError,
    UnicodeError,
    lzma.LZMAError,
    NotImplementedError,
    OSError,
    RuntimeError,
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
)

_XML_DECLARATION_RE = re.compile(
    r"\A\ufeff?[ \t\r\n]*<\?xml(?=[ \t\r\n]).*?\?>",
    flags=re.DOTALL,
)
_XML_DECLARATION_START_RE = re.compile(
    r"\A\ufeff?[ \t\r\n]*<\?xml(?=[ \t\r\n])",
)
_XML_ENCODING_RE = re.compile(
    r"(\bencoding\s*=\s*)(['\"])[^'\"]*\2",
    flags=re.IGNORECASE,
)
_XML_VERSION_RE = re.compile(
    r"\bversion\s*=\s*(['\"])([^'\"]+)\1",
    flags=re.IGNORECASE,
)
_XML_STANDALONE_RE = re.compile(
    r"\bstandalone\s*=\s*(['\"])(yes|no)\1",
    flags=re.IGNORECASE,
)


class _MscxTreeBuilder:
    """Build an ElementTree while retaining document-level comments and PIs."""

    def __init__(self) -> None:
        self._builder = ET.TreeBuilder(insert_comments=True, insert_pis=True)
        self._depth = 0
        self._root_closed = False
        self.prolog: list[ET.Element] = []
        self.epilog: list[ET.Element] = []
        self.has_doctype = False
        self.has_namespaces = False

    def start(self, tag: str, attributes: dict[str, str]) -> ET.Element:
        self._depth += 1
        return self._builder.start(tag, attributes)

    def end(self, tag: str) -> ET.Element:
        element = self._builder.end(tag)
        self._depth -= 1
        if self._depth == 0:
            self._root_closed = True
        return element

    def data(self, data: str) -> None:
        self._builder.data(data)

    def comment(self, text: str) -> None:
        if self._depth:
            self._builder.comment(text)
            return
        destination = self.epilog if self._root_closed else self.prolog
        destination.append(ET.Comment(text))

    def pi(self, target: str, text: str) -> None:
        if self._depth:
            self._builder.pi(target, text)
            return
        destination = self.epilog if self._root_closed else self.prolog
        destination.append(ET.ProcessingInstruction(target, text))

    def doctype(
        self,
        name: str,
        public_id: str | None,
        system_id: str | None,
    ) -> None:
        del name, public_id, system_id
        self.has_doctype = True

    def start_ns(self, prefix: str | None, uri: str) -> None:
        del prefix, uri
        self.has_namespaces = True

    def end_ns(self, prefix: str | None) -> None:
        del prefix

    def close(self) -> ET.Element:
        return self._builder.close()


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


def _score_contexts(
    root: ET.Element,
    fallback_concert_pitch: bool | None,
) -> list[tuple[ET.Element, bool | None]]:
    """Return each Score with its inherited concert/written-pitch view."""

    contexts: list[tuple[ET.Element, bool | None]] = []
    stack = [(child, fallback_concert_pitch) for child in reversed(root)]
    while stack:
        element, inherited_setting = stack.pop()
        context_setting = inherited_setting
        if element.tag == "Score":
            explicit_setting = _concert_pitch_setting(element)
            if explicit_setting is not None:
                context_setting = explicit_setting
            contexts.append((element, context_setting))
        stack.extend((child, context_setting) for child in reversed(element))
    return contexts


def _xml_prefix(content: bytes | str) -> str:
    if isinstance(content, str):
        initial = content[:4096]
        if _XML_DECLARATION_START_RE.match(initial) is None:
            return initial
        declaration_end = content.find("?>")
        return content if declaration_end < 0 else content[: declaration_end + 2]

    signature = content[:4]
    encodings = (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
        (b"\x00\x00\x00<", "utf-32-be"),
        (b"<\x00\x00\x00", "utf-32-le"),
        (b"\x00<\x00?", "utf-16-be"),
        (b"<\x00?\x00", "utf-16-le"),
    )
    encoding = next(
        (name for marker, name in encodings if signature.startswith(marker)),
        "ascii",
    )
    sample_size = min(len(content), 16384)
    while True:
        decoded = content[:sample_size].decode(encoding, errors="ignore")
        if _XML_DECLARATION_START_RE.match(decoded) is None:
            return decoded
        declaration_end = decoded.find("?>")
        if declaration_end >= 0:
            return decoded[: declaration_end + 2]
        if sample_size == len(content):
            return decoded
        sample_size = min(len(content), sample_size * 2)


def _xml_declaration_fields(
    content: bytes | str,
) -> tuple[bool, str, str | None]:
    declaration = _XML_DECLARATION_RE.match(_xml_prefix(content))
    if declaration is None:
        return False, "1.0", None
    version_match = _XML_VERSION_RE.search(declaration.group(0))
    standalone_match = _XML_STANDALONE_RE.search(declaration.group(0))
    version = version_match.group(2) if version_match is not None else "1.0"
    standalone = standalone_match.group(2).lower() if standalone_match is not None else None
    return True, version, standalone


def _encode_xml_text(content: str) -> bytes:
    declaration = _XML_DECLARATION_RE.match(content)
    if declaration is None:
        return content.encode("utf-8")
    normalized = _XML_ENCODING_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}utf-8{match.group(2)}",
        declaration.group(0),
        count=1,
    )
    return (normalized + content[declaration.end() :]).encode("utf-8")


def _parse_mscx(content: bytes | str) -> _MscxDocument:
    target = _MscxTreeBuilder()
    try:
        parser = ET.XMLParser(target=target)
        root = ET.fromstring(content, parser=parser)
    except (ET.ParseError, LookupError, RecursionError, ValueError) as exc:
        raise ScoreFormatError(f"Invalid MSCX XML: {exc}") from exc

    if root.tag != "museScore":
        raise ScoreFormatError("Invalid MSCX XML: root element must be <museScore>.")
    direct_scores = root.findall("Score")
    if len(direct_scores) != 1:
        raise ScoreFormatError(
            "Invalid MSCX XML: <museScore> must contain exactly one direct <Score>."
        )
    has_declaration, xml_version, standalone = _xml_declaration_fields(content)
    return _MscxDocument(
        root=root,
        prolog=tuple(target.prolog),
        epilog=tuple(target.epilog),
        has_xml_declaration=has_declaration,
        xml_version=xml_version,
        standalone=standalone,
        has_doctype=target.has_doctype,
        has_namespaces=target.has_namespaces,
    )


def _render_mscx(document: _MscxDocument) -> bytes:
    if document.has_doctype:
        raise ScoreFormatError(
            "MSCX documents with a DOCTYPE cannot be rewritten safely; "
            "no output was written."
        )
    if document.has_namespaces:
        raise ScoreFormatError(
            "MSCX documents with XML namespaces cannot be rewritten without "
            "changing namespace prefixes; no output was written."
        )
    try:
        body = b"".join(
            ET.tostring(element, encoding="utf-8", xml_declaration=False)
            for element in (*document.prolog, document.root, *document.epilog)
        )
    except (LookupError, RecursionError, ValueError) as exc:
        raise ScoreFormatError(f"Unable to serialize MSCX XML safely: {exc}") from exc
    if document.has_xml_declaration:
        standalone = (
            f" standalone='{document.standalone}'" if document.standalone is not None else ""
        )
        declaration = (
            f"<?xml version='{document.xml_version}' encoding='utf-8'{standalone}?>\n"
        ).encode("ascii")
        return declaration + body
    return body


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
    for part in score:
        if part.tag not in {"Part", "SharedPart"}:
            continue
        instrument = part.find("Instrument")
        preference = part.findtext("preferSharpFlat", "").strip().lower()
        if preference not in {"sharps", "flats", "none"}:
            preference = "auto"
        definitions.extend(
            (definition.get("id"), definition, instrument, preference)
            for definition in part.findall("Staff")
        )

    content_staves = score.findall("Staff")
    content_ids = [staff.get("id") for staff in content_staves if staff.get("id")]
    duplicate_ids = [item for item, count in Counter(content_ids).items() if count > 1]
    if duplicate_ids:
        raise ScoreFormatError(
            f"Invalid MSCX XML: duplicate score staff id {duplicate_ids[0]!r}."
        )

    if definitions and content_staves and len(definitions) != len(content_staves):
        raise ScoreFormatError(
            "Invalid MSCX XML: staff definition count does not match score staff count."
        )

    explicit_definition_ids = [staff_id is not None for staff_id, *_ in definitions]
    if any(explicit_definition_ids) and not all(explicit_definition_ids):
        raise ScoreFormatError(
            "Invalid MSCX XML: staff definitions mix explicit and idless IDs."
        )

    scopes: list[_StaffScope] = []
    if definitions and not any(explicit_definition_ids):
        actual_ids = [staff.get("id") for staff in content_staves]
        expected_ids = [str(index) for index in range(1, len(content_staves) + 1)]
        if actual_ids != expected_ids:
            raise ScoreFormatError(
                "Invalid MSCX XML: idless staff definitions require canonical score "
                "staff IDs 1..N."
            )
        matches = [
            (content, definition, instrument, preference)
            for content, (_, definition, instrument, preference) in zip(
                content_staves,
                definitions,
                strict=True,
            )
        ]
    elif definitions:
        definitions_by_id: dict[str, tuple[ET.Element, ET.Element | None, str]] = {}
        for staff_id, definition, instrument, preference in definitions:
            assert staff_id is not None
            if staff_id in definitions_by_id:
                raise ScoreFormatError(
                    f"Invalid MSCX XML: duplicate staff definition id {staff_id!r}."
                )
            definitions_by_id[staff_id] = (definition, instrument, preference)
        matches = []
        for content in content_staves:
            content_id = content.get("id")
            match = definitions_by_id.get(content_id or "")
            if match is None:
                raise ScoreFormatError(
                    f"Invalid MSCX XML: score staff id {content_id!r} has no matching "
                    "staff definition."
                )
            definition, instrument, preference = match
            matches.append((content, definition, instrument, preference))
    else:
        matches = [(content, content, None, "auto") for content in content_staves]

    for content, definition, instrument, preference in matches:
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


def _key_signature_folds_enharmonically(
    key_signature: ET.Element,
    tpc_shift: int,
) -> bool:
    """Return whether a conventional signature must cross the fifths boundary."""

    if _key_signature_kind(key_signature) != "conventional":
        return False
    signature = None
    for field_name in ("concertKey", "actualKey", "accidental"):
        signature = _read_integer_field(key_signature, field_name)
        if signature is not None:
            break
    if signature is None:
        return False
    try:
        named_tonic = transpose_tpc(14 + signature, tpc_shift)
        normalized_tonic = 14 + transpose_key_signature_by_tpc(
            signature,
            tpc_shift,
        )
    except ValueError as exc:
        raise ScoreFormatError(
            f"Invalid MuseScore key-signature value: {signature}"
        ) from exc
    return named_tonic != normalized_tonic


def _transpose_harmony(harmony: ET.Element, tpc_shift: int) -> bool:
    changed = False
    for field_name in ("root", "base", "bass"):
        boundaries = _SCORE_BOUNDARIES | {"Harmony"}
        for field in _bounded_descendants(harmony, boundaries=boundaries):
            if field.tag != field_name:
                continue
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
    key_signature = None
    music_has_started = False
    for element in _bounded_descendants(score):
        if element.tag == "KeySig":
            if not music_has_started:
                key_signature = element
            break
        if element.tag in {"Chord", "Rest", "Note"}:
            music_has_started = True

    if key_signature is not None:
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
    if music_has_started or _unscoped_elements(score, "Measure"):
        return 0
    return None


def _validate_source_key(
    score_contexts: list[tuple[ET.Element, bool | None]],
    source_key: str,
) -> None:
    observed: list[int] = []
    for score, context_concert_pitch in score_contexts:
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
    tpc_shift: int,
) -> list[_StaffScope]:
    scopes = _staff_scopes(score)
    if scopes:
        unscoped_tags = [
            tag
            for tag in ("Note", "Harmony", "KeySig")
            if _unscoped_elements(score, tag)
        ]
        if unscoped_tags:
            rendered = ", ".join(unscoped_tags)
            raise ScoreFormatError(
                "MuseScore Score mixes staff-scoped and unscoped musical content "
                f"({rendered}); no output was written."
            )
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
            first_measure = _first_measure(scope.content)
            opening_key_signature = (
                _opening_key_signature(first_measure)
                if first_measure is not None
                else None
            )
            if has_content and any(
                key_signature is not opening_key_signature
                for key_signature in key_signatures
            ):
                raise ScoreFormatError(
                    "Transposing-instrument staves with mid-score key changes require "
                    "tick-aware written-pitch respelling; no output was written."
                )
        if scope.group == "pitched" and has_content and any(
            _key_signature_folds_enharmonically(key_signature, tpc_shift)
            for key_signature in _bounded_elements(scope.content, "KeySig")
        ):
            raise ScoreFormatError(
                "A key change crosses the conventional enharmonic-signature boundary "
                "and requires tick-aware note respelling; no output was written."
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
        unscoped_has_content = bool(_unscoped_elements(score, "Note")) or bool(
            _unscoped_elements(score, "Harmony")
        )
        if unscoped_has_content and any(
            _key_signature_folds_enharmonically(key_signature, tpc_shift)
            for key_signature in _unscoped_elements(score, "KeySig")
        ):
            raise ScoreFormatError(
                "A key change crosses the conventional enharmonic-signature boundary "
                "and requires tick-aware note respelling; no output was written."
            )
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
    document = _parse_mscx(content)
    root = document.root
    score_contexts = _score_contexts(root, concert_pitch)
    if validate_source_key:
        _validate_source_key(score_contexts, source_key)

    transposition_changes_spelling = bool(shift or note_tpc_shift)
    scoped_contexts = [
        (
            score,
            context_concert_pitch,
            _validate_score_context(
                score,
                transposition_changes_spelling=transposition_changes_spelling,
                tpc_shift=note_tpc_shift,
            ),
        )
        for score, context_concert_pitch in score_contexts
    ]

    note_count = 0
    key_signature_count = 0
    harmony_count = 0
    target_spelling = spelling_for_key(target_key)
    for score, context_concert_pitch, scopes in scoped_contexts:
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
        original = content if isinstance(content, bytes) else _encode_xml_text(content)
        return original, TransposeReport(
            from_key=source_key,
            to_key=target_key,
            semitone_shift=shift,
            notes_changed=0,
            key_signatures_changed=0,
            score_entries_changed=0,
            chord_symbols_changed=0,
        )

    rendered = _render_mscx(document)
    return rendered, TransposeReport(
        from_key=source_key,
        to_key=target_key,
        semitone_shift=shift,
        notes_changed=note_count,
        key_signatures_changed=key_signature_count,
        score_entries_changed=1,
        chord_symbols_changed=harmony_count,
    )


def _archive_member_key(info: zipfile.ZipInfo) -> str:
    name = info.filename
    raw_name = info.orig_filename
    if raw_name != name or not name:
        raise ScoreFormatError(f"MSCZ archive contains an invalid member name {raw_name!r}.")
    if "\\" in name or any(ord(character) < 32 for character in name):
        raise ScoreFormatError(f"MSCZ archive contains an unsafe member path {name!r}.")

    path_text = name.removesuffix("/")
    parts = path_text.split("/")
    if (
        not path_text
        or name.startswith("/")
        or PurePosixPath(path_text).is_absolute()
        or PureWindowsPath(path_text).drive
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ScoreFormatError(f"MSCZ archive contains an unsafe member path {name!r}.")

    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ScoreFormatError(
            f"MSCZ archive member {name!r} has an unsupported special-file type."
        )
    if (file_type == stat.S_IFDIR) != info.is_dir() and file_type != 0:
        raise ScoreFormatError(
            f"MSCZ archive member {name!r} has inconsistent file metadata."
        )
    if info.flag_bits & 0x1:
        raise ScoreFormatError(f"MSCZ archive member {name!r} is encrypted.")

    return path_text


def _validate_archive_members(infos: list[zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise ScoreFormatError(
            f"MSCZ archive contains too many members ({len(infos)}; "
            f"maximum {_MAX_ARCHIVE_MEMBERS})."
        )

    total_size = 0
    normalized_names: dict[str, str] = {}
    members: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        if info.filename in members:
            raise ScoreFormatError(
                f"MSCZ archive contains duplicate member {info.filename!r}."
            )
        key = _archive_member_key(info)
        previous = normalized_names.get(key)
        if previous is not None:
            raise ScoreFormatError(
                "MSCZ archive contains colliding member names "
                f"{previous!r} and {info.filename!r}."
            )
        normalized_names[key] = info.filename
        members[info.filename] = info

        if (
            info.file_size < 0
            or info.compress_size < 0
            or info.file_size > _MAX_ARCHIVE_MEMBER_SIZE
        ):
            raise ScoreFormatError(
                f"MSCZ archive member {info.filename!r} is too large "
                f"({info.file_size} bytes; maximum {_MAX_ARCHIVE_MEMBER_SIZE})."
            )
        total_size += info.file_size
        if total_size > _MAX_ARCHIVE_TOTAL_SIZE:
            raise ScoreFormatError(
                f"MSCZ archive expands beyond {_MAX_ARCHIVE_TOTAL_SIZE} bytes."
            )
        if (
            info.compress_type == zipfile.ZIP_STORED
            and info.compress_size != info.file_size
        ):
            raise ScoreFormatError(
                f"MSCZ member {info.filename!r} stored size does not match "
                "its ZIP metadata."
            )
        if (
            info.file_size >= 1024 * 1024
            and info.file_size > max(info.compress_size, 1) * _MAX_COMPRESSED_RATIO
        ):
            raise ScoreFormatError(
                f"MSCZ archive member {info.filename!r} has a suspicious "
                "compression ratio."
            )

    for singleton in ("META-INF/container.xml", "score_style.mss"):
        variants = [name for name in members if name.casefold() == singleton.casefold()]
        if len(variants) > 1:
            raise ScoreFormatError(
                f"MSCZ archive contains ambiguous {singleton} members."
            )
    for key, name in normalized_names.items():
        parts = key.split("/")
        for index in range(1, len(parts)):
            ancestor_name = normalized_names.get("/".join(parts[:index]))
            if ancestor_name is not None and not members[ancestor_name].is_dir():
                raise ScoreFormatError(
                    "MSCZ archive contains a file/directory path collision between "
                    f"{ancestor_name!r} and {name!r}."
                )
    return members


def _read_archive_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_size: int | None = None,
) -> bytes:
    if maximum_size is not None and info.file_size > maximum_size:
        raise ScoreFormatError(
            f"MSCZ member {info.filename!r} exceeds the {maximum_size}-byte limit."
        )
    try:
        payload = archive.read(info)
    except _ARCHIVE_READ_ERRORS as exc:
        raise ScoreFormatError(
            f"Unable to read or validate MSCZ member {info.filename!r}: {exc}"
        ) from exc
    if len(payload) != info.file_size:
        raise ScoreFormatError(
            f"MSCZ member {info.filename!r} size does not match its ZIP metadata."
        )
    return payload


def _copy_archive_member(
    source: zipfile.ZipFile,
    target: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> None:
    expected_size = info.file_size
    target_info = copy.copy(info)
    if info.is_dir():
        if expected_size != 0 or info.CRC != 0:
            raise ScoreFormatError(
                f"MSCZ directory member {info.filename!r} has invalid content metadata."
            )
        payload = _read_archive_member(source, info)
        if payload:
            raise ScoreFormatError(
                f"MSCZ directory member {info.filename!r} contains file data."
            )
        try:
            target.writestr(target_info, b"")
        except _ARCHIVE_READ_ERRORS as exc:
            raise ScoreFormatError(
                f"Unable to preserve MSCZ member {info.filename!r}: {exc}"
            ) from exc
        return
    try:
        with (
            source.open(info, "r") as source_member,
            target.open(target_info, "w") as target_member,
        ):
            copied = 0
            while chunk := source_member.read(_COPY_CHUNK_SIZE):
                target_member.write(chunk)
                copied += len(chunk)
    except _ARCHIVE_READ_ERRORS as exc:
        raise ScoreFormatError(
            f"Unable to read or validate MSCZ member {info.filename!r}: {exc}"
        ) from exc
    if copied != expected_size:
        raise ScoreFormatError(
            f"MSCZ member {info.filename!r} size does not match its ZIP metadata."
        )


def _file_snapshot(item_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        item_stat.st_dev,
        item_stat.st_ino,
        item_stat.st_size,
        item_stat.st_mtime_ns,
        item_stat.st_ctime_ns,
    )


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

    payload = _read_archive_member(
        archive,
        style_infos[0],
        maximum_size=_MAX_METADATA_XML_SIZE,
    )
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
    members: dict[str, zipfile.ZipInfo],
) -> None:
    container_info = members.get("META-INF/container.xml")
    if container_info is None:
        return
    payload = _read_archive_member(
        archive,
        container_info,
        maximum_size=_MAX_METADATA_XML_SIZE,
    )
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise ScoreFormatError(f"Invalid MSCZ container manifest: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1] != "container":
        raise ScoreFormatError("Invalid MSCZ container manifest root.")

    rootfiles_containers = [
        child
        for child in root
        if isinstance(child.tag, str)
        and child.tag.rsplit("}", 1)[-1] == "rootfiles"
    ]
    if len(rootfiles_containers) != 1:
        raise ScoreFormatError(
            "MSCZ container manifest must contain one direct <rootfiles>."
        )
    rootfiles = [
        child
        for child in rootfiles_containers[0]
        if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1] == "rootfile"
    ]
    all_rootfiles = [
        element
        for element in root.iter()
        if isinstance(element.tag, str)
        and element.tag.rsplit("}", 1)[-1] == "rootfile"
    ]
    if not rootfiles or len(all_rootfiles) != len(rootfiles):
        raise ScoreFormatError(
            "MSCZ container manifest has invalid <rootfile> structure."
        )
    references: list[str] = []
    for rootfile in rootfiles:
        reference = rootfile.get("full-path", "")
        referenced_info = members.get(reference)
        if not reference or referenced_info is None or referenced_info.is_dir():
            raise ScoreFormatError(
                f"MSCZ container manifest references an invalid member {reference!r}."
            )
        if reference in references:
            raise ScoreFormatError(
                f"MSCZ container manifest repeats rootfile {reference!r}."
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
            prefix=".music-score-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name

        try:
            source_handle = source.open("rb")
        except OSError as exc:
            raise ScoreFormatError(f"Unable to open MSCZ archive {source}: {exc}") from exc

        with source_handle:
            initial_snapshot = _file_snapshot(os.fstat(source_handle.fileno()))
            try:
                source_zip = zipfile.ZipFile(source_handle, "r")
            except _ARCHIVE_READ_ERRORS as exc:
                raise ScoreFormatError(f"Not a valid MSCZ/ZIP archive: {source}") from exc

            with source_zip:
                infos = source_zip.infolist()
                members = _validate_archive_members(infos)
                score_entries = [
                    info
                    for info in infos
                    if not info.is_dir() and info.filename.lower().endswith(".mscx")
                ]
                if not score_entries:
                    raise ScoreFormatError(
                        "MSCZ archive does not contain an .mscx score entry."
                    )
                _validate_mscz_manifest(source_zip, members)
                concert_pitch = _archive_concert_pitch_setting(source_zip, infos)
                score_names = {info.filename for info in score_entries}

                with zipfile.ZipFile(temp_name, "w") as target_zip:
                    target_zip.comment = source_zip.comment
                    for info in infos:
                        if info.filename not in score_names:
                            _copy_archive_member(source_zip, target_zip, info)
                            continue
                        payload = _read_archive_member(
                            source_zip,
                            info,
                            maximum_size=_MAX_SCORE_XML_SIZE,
                        )
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

            if _file_snapshot(os.fstat(source_handle.fileno())) != initial_snapshot:
                raise ScoreFormatError(
                    f"MSCZ archive changed while it was being processed: {source}"
                )
            if score_count == 0:
                source_handle.seek(0)
                with Path(temp_name).open("wb") as staged_output:
                    shutil.copyfileobj(
                        source_handle,
                        staged_output,
                        _COPY_CHUNK_SIZE,
                    )
                if _file_snapshot(os.fstat(source_handle.fileno())) != initial_snapshot:
                    raise ScoreFormatError(
                        f"MSCZ archive changed while it was being copied: {source}"
                    )
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
