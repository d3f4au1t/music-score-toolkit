"""Conservative, exact measure timing for MuseScore's modern voice format.

Fractions are whole-note units, as in MSCX ``duration`` and ``location``.
This reader never modifies rhythm or repairs incomplete rhythmic structures.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction


class ScoreTimingError(ValueError):
    """A key change cannot be located without guessing musical timing."""


GRACE_TAGS = frozenset({
    "appoggiatura", "acciaccatura", "grace4", "grace16", "grace32",
    "grace8after", "grace16after", "grace32after",
})
_DURATIONS = {
    "long": Fraction(4), "breve": Fraction(2), "whole": Fraction(1),
    "half": Fraction(1, 2), "quarter": Fraction(1, 4), "eighth": Fraction(1, 8),
    **{str(n): Fraction(1, n) for n in (16, 32, 64, 128, 256, 512, 1024)},
}


@dataclass(frozen=True)
class MeasureTiming:
    length: Fraction
    positions: dict[ET.Element, Fraction]


def _field(element: ET.Element, tag: str, default: str | None = None) -> str | None:
    values = element.findall(tag)
    if not values:
        return default
    if len(values) != 1 or not (values[0].text or "").strip():
        raise ScoreTimingError(f"Missing or duplicate timing field {tag}.")
    return values[0].text.strip()


def _fraction(value: str | None, label: str, *, positive: bool = False) -> Fraction:
    if value is None or not re.fullmatch(r"-?[0-9]{1,10}(?:/[0-9]{1,10})?", value.strip()):
        raise ScoreTimingError(f"Invalid or missing {label} for tick-aware key changes.")
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ScoreTimingError(f"Invalid {label} for tick-aware key changes.") from exc
    if positive and result <= 0:
        raise ScoreTimingError(f"{label} must be positive.")
    return result


def _integer(element: ET.Element, tag: str, default: str | None = None) -> int:
    value = _fraction(_field(element, tag, default), tag)
    if value.denominator != 1:
        raise ScoreTimingError(f"{tag} must be an integer.")
    return value.numerator


def advances_position(element: ET.Element) -> bool:
    if element.tag == "Chord":
        return not any(child.tag in GRACE_TAGS for child in element)
    return element.tag in {"Rest", "Note", "MeasureRepeat", "RepeatMeasure"}


def opening_meter(measure: ET.Element, previous: tuple[ET.Element, ...] = ()) -> tuple[ET.Element, ...]:
    """Carry meter declarations without requiring timing for simple scores."""

    found = []
    for voice in measure.findall("voice") or [measure]:
        for item in voice:
            if item.tag == "TimeSig":
                found.append(item)
            if advances_position(item) or item.tag in {"location", "tick"}:
                break
    return tuple(found) if found else previous


def _meter_length(meters: tuple[ET.Element, ...]) -> Fraction | None:
    values = set()
    for meter in meters:
        if _integer(meter, "stretchN", "1") != 1 or _integer(meter, "stretchD", "1") != 1:
            raise ScoreTimingError("Local time-signature stretching is not supported for key timing.")
        numerator, denominator = _integer(meter, "sigN"), _integer(meter, "sigD")
        if numerator <= 0 or denominator <= 0:
            raise ScoreTimingError("Time-signature values must be positive.")
        values.add(Fraction(numerator, denominator))
    if len(values) > 1:
        raise ScoreTimingError("Conflicting simultaneous time signatures.")
    return next(iter(values)) if values else None


def _dotted_duration(name: str | None, dots: int) -> Fraction:
    if name not in _DURATIONS or not 0 <= dots <= 4:
        raise ScoreTimingError("Unsupported duration type or dot count for key timing.")
    return _DURATIONS[name] * (2 - Fraction(1, 2 ** dots))


def _duration(item: ET.Element, meter: Fraction | None, length: Fraction) -> Fraction:
    if item.tag in {"MeasureRepeat", "RepeatMeasure"}:
        raise ScoreTimingError("Measure repeats need expanded music for tick-aware key changes.")
    if item.tag == "Note":
        raise ScoreTimingError("Standalone notes have no chord duration for tick-aware key changes.")
    if any(item.find(tag) is not None for tag in ("Tuplet", "ticklen", "Tremolo", "TremoloTwoChord")):
        raise ScoreTimingError("Legacy tuplets/timing or tremolos require MuseScore timing support.")
    if _integer(item, "staffMove", "0") != 0:
        raise ScoreTimingError("Cross-staff notes need staff-aware timing support.")
    name = _field(item, "durationType")
    dots = _integer(item, "dots", "0")
    # MuseScore applies dots when reading durationType, and an explicit
    # duration when it encounters that field. Reject contradictory ordering.
    tags = [child.tag for child in item]
    if "dots" in tags and "durationType" in tags and tags.index("dots") > tags.index("durationType"):
        raise ScoreTimingError("Noncanonical dotted-duration order; re-save in MuseScore first.")
    explicit = _field(item, "duration")
    if explicit is not None:
        if "durationType" in tags and tags.index("duration") < tags.index("durationType"):
            raise ScoreTimingError("Noncanonical explicit-duration order; re-save in MuseScore first.")
        return _fraction(explicit, "duration", positive=True)
    if name == "measure" and item.tag == "Rest":
        if meter is None or length != meter or dots:
            raise ScoreTimingError("An irregular full-measure rest needs an explicit duration.")
        return meter
    return _dotted_duration(name, dots)


def read_measure_timing(
    measure: ET.Element,
    meters: tuple[ET.Element, ...],
) -> MeasureTiming:
    """Read independently serialized voices into one exact local timeline.

    Supports modern nested tuplets and relative fractional cursor movements
    confined to this measure/staff. Absolute legacy ticks, cross-staff moves,
    stretched meters, and malformed/incomplete timing fail closed.
    """

    meter = _meter_length(meters)
    length = (_fraction(measure.get("len"), "measure length", positive=True)
              if "len" in measure.attrib else meter)
    if length is None:
        raise ScoreTimingError("Tick-aware key changes require an explicit measure length or time signature.")
    if measure.find("multiMeasureRest") is not None:
        raise ScoreTimingError("Condensed multi-measure rests need expanded music for key timing.")
    voices = measure.findall("voice")
    if not voices:
        raise ScoreTimingError("Tick-aware key changes require modern MuseScore voice containers.")
    positions: dict[ET.Element, Fraction] = {}
    for voice in voices:
        cursor = Fraction(0)
        factor = Fraction(1)
        tuplets: list[tuple[Fraction, Fraction, Fraction]] = []
        pending_grace = False
        for item in voice:
            if item.tag in {"Chord", "Rest", "Note", "Harmony", "KeySig"} and (
                item.find("track") is not None
                or any(note.find("track") is not None for note in item.findall("Note"))
            ):
                raise ScoreTimingError("Explicit event track routing is unsupported for key timing.")
            if item.tag == "tick":
                raise ScoreTimingError("Absolute legacy ticks are unsupported; re-save in MuseScore 4.")
            if item.tag == "location":
                if tuplets or pending_grace:
                    raise ScoreTimingError("Cursor movements inside tuplets or grace groups are unsupported.")
                for child in item:
                    if child.tag != "fractions" and isinstance(child.tag, str):
                        if child.tag not in {"staves", "voices", "measures", "grace", "notes"}:
                            raise ScoreTimingError("Unknown cursor movement field.")
                        if _integer(item, child.tag) != 0:
                            raise ScoreTimingError("Cursor movement leaves its voice/staff/measure context.")
                cursor += _fraction(_field(item, "fractions", "0"), "cursor fraction")
            elif item.tag == "Tuplet":
                if len(tuplets) >= 16:
                    raise ScoreTimingError("Tuplet nesting exceeds the supported limit.")
                normal, actual = _integer(item, "normalNotes"), _integer(item, "actualNotes")
                if not 1 <= normal <= 1024 or not 1 <= actual <= 1024:
                    raise ScoreTimingError("Invalid tuplet ratio.")
                base = _dotted_duration(_field(item, "baseNote"), _integer(item, "baseDots", "0"))
                tuplets.append((cursor, base * normal * factor, factor))
                factor *= Fraction(normal, actual)
            elif item.tag == "endTuplet":
                if not tuplets:
                    raise ScoreTimingError("Unmatched endTuplet marker.")
                start, expected, factor = tuplets.pop()
                if cursor - start != expected:
                    raise ScoreTimingError("Incomplete or overfull tuplet; key timing would be ambiguous.")
            elif item.tag == "TimeSig":
                # Non-opening signatures are courtesy only. At the opening,
                # do not silently adopt a declaration our meter pass missed.
                if cursor == 0 and item not in meters:
                    raise ScoreTimingError("Time signature reached through a cursor movement is unsupported.")
            else:
                positions[item] = cursor
                if item.tag == "Chord" and not advances_position(item):
                    if _integer(item, "staffMove", "0") != 0:
                        raise ScoreTimingError("Cross-staff grace notes need staff-aware timing support.")
                    pending_grace = True
                elif advances_position(item):
                    if pending_grace and item.tag != "Chord":
                        raise ScoreTimingError("Grace notes have no following main chord.")
                    pending_grace = False
                    duration = _duration(item, meter, length) * factor
                    if cursor >= length or duration <= 0 or cursor + duration > length:
                        raise ScoreTimingError("Musical duration falls outside its measure.")
                    cursor += duration
                elif item.tag == "Harmony" and cursor >= length:
                    raise ScoreTimingError("Chord symbol at the measure boundary has ambiguous timing.")
            if not 0 <= cursor <= length:
                raise ScoreTimingError("Cursor position falls outside its measure.")
        if tuplets or pending_grace:
            raise ScoreTimingError("Unclosed tuplet or grace group; key timing is incomplete.")
    return MeasureTiming(length, positions)
