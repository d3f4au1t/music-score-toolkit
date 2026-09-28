import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest

from music_score_toolkit.timing import ScoreTimingError, opening_meter, read_measure_timing


def read(body, *, length="1"):
    measure = ET.fromstring(f'<Measure len="{length}">{body}</Measure>')
    return read_measure_timing(measure, opening_meter(measure))


def chord(duration="quarter", extra=""):
    return f"<Chord>{extra}<durationType>{duration}</durationType><Note/></Chord>"


def tuplet(normal=2, actual=3, base="eighth"):
    return (f"<Tuplet><normalNotes>{normal}</normalNotes><actualNotes>{actual}</actualNotes>"
            f"<baseNote>{base}</baseNote></Tuplet>")


def key_times(timing):
    return [tick for item, tick in timing.positions.items() if item.tag == "KeySig"]


def test_each_voice_restarts_at_zero_and_dotted_notes_have_exact_duration():
    timing = read(f"<voice>{chord('quarter', '<dots>1</dots>')}<KeySig/></voice>"
                  f"<voice>{chord('eighth')}<KeySig/></voice>")
    assert key_times(timing) == [Fraction(3, 8), Fraction(1, 8)]


def test_triplets_align_exactly_with_quarter_beat_in_other_voice():
    timing = read(f"<voice>{tuplet()}{chord('eighth') * 3}<endTuplet/><KeySig/></voice>"
                  f"<voice>{chord()}<KeySig/></voice>")
    assert key_times(timing) == [Fraction(1, 4), Fraction(1, 4)]


def test_nested_tuplets_multiply_ratios_and_validate_their_spans():
    inner = f"{tuplet(base='16')}{chord('16') * 3}<endTuplet/>"
    timing = read(f"<voice>{tuplet()}{inner}{chord('eighth') * 2}<endTuplet/><KeySig/></voice>")
    assert key_times(timing) == [Fraction(1, 4)]


def test_grace_does_not_consume_beats_or_tuplet_duration():
    timing = read(f"<voice>{tuplet()}{chord('eighth', '<acciaccatura/>')}"
                  f"{chord('eighth') * 3}<endTuplet/><KeySig/></voice>")
    assert key_times(timing) == [Fraction(1, 4)]


def test_pickup_explicit_rest_duration_and_inherited_meter():
    first = ET.fromstring("<Measure len='1/4'><voice><TimeSig><sigN>3</sigN><sigD>4</sigD>"
                         "</TimeSig><Rest><durationType>measure</durationType><duration>1/4</duration>"
                         "</Rest><KeySig/></voice></Measure>")
    meter = opening_meter(first)
    assert key_times(read_measure_timing(first, meter)) == [Fraction(1, 4)]
    second = ET.fromstring("<Measure><voice><Rest><durationType>measure</durationType>"
                          "</Rest><KeySig/></voice></Measure>")
    assert key_times(read_measure_timing(second, opening_meter(second, meter))) == [Fraction(3, 4)]


def test_relative_cursor_repositions_without_changing_stored_rhythm():
    timing = read(f"<voice>{chord()}<location><fractions>1/4</fractions></location>"
                  "<KeySig/><location><fractions>-1/2</fractions></location><Harmony/></voice>")
    assert key_times(timing) == [Fraction(1, 2)]
    assert next(tick for item, tick in timing.positions.items() if item.tag == "Harmony") == 0


@pytest.mark.parametrize("body", [
    "<Chord/>", "<Chord><durationType>unknown</durationType></Chord>",
    "<Rest><durationType>quarter</durationType><duration>0</duration></Rest>",
    "<Rest><durationType>quarter</durationType><duration>1/0</duration></Rest>",
    "<Chord><durationType>quarter</durationType><durationType>half</durationType></Chord>",
    "<Chord><durationType>quarter</durationType><dots>1</dots></Chord>",
    "<Chord><dots>-1</dots><durationType>quarter</durationType></Chord>",
    "<Chord><durationType>quarter</durationType><staffMove>1</staffMove></Chord>",
    "<Chord><durationType>quarter</durationType><Tuplet>1</Tuplet></Chord>",
    "<location><fractions>-1/4</fractions></location>",
    "<location><fractions>2</fractions></location>",
    "<location><fractions>1/2</fractions><voices>1</voices></location>",
    "<location><fractions>1/2</fractions><unexpected>0</unexpected></location>",
    "<location><fractions>1/2</fractions><fractions>1/2</fractions></location>",
    "<tick>480</tick>", "<endTuplet/>", "<Note/>", "<MeasureRepeat/>",
    tuplet() + chord('eighth') + "<endTuplet/>",
    tuplet() + chord('eighth') * 3,
    tuplet() + "<location><fractions>1/4</fractions></location><endTuplet/>",
    tuplet(actual=0) + chord('eighth') * 3 + "<endTuplet/>",
    chord("whole") * 2, chord("eighth", "<acciaccatura/>"),
    chord("quarter", "<track>4</track>"),
    chord("eighth", "<acciaccatura/><staffMove>1</staffMove>"),
])
def test_incomplete_or_unsupported_timing_is_rejected(body):
    with pytest.raises(ScoreTimingError):
        read(f"<voice>{body}</voice>")


@pytest.mark.parametrize("length", ["0", "-1/4", "1/0", "bad", "1/256"])
def test_invalid_measure_lengths_are_rejected(length):
    with pytest.raises(ScoreTimingError):
        read(f"<voice>{chord()}</voice>", length=length)


@pytest.mark.parametrize("time_signature", [
    "<sigN>4</sigN><sigD>0</sigD>",
    "<sigN>4</sigN><sigD>4</sigD><stretchN>2</stretchN><stretchD>1</stretchD>",
    "<sigN>4</sigN><sigN>3</sigN><sigD>4</sigD>",
])
def test_invalid_or_stretched_meters_are_not_guessed(time_signature):
    with pytest.raises(ScoreTimingError):
        read(f"<voice><TimeSig>{time_signature}</TimeSig>{chord()}<KeySig/></voice>")


def test_extra_voice_cannot_silently_overflow_into_another_staff():
    with pytest.raises(ScoreTimingError, match="four voices"):
        read(f"<voice>{chord()}</voice>" * 5)
