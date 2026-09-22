import pytest

from music_score_toolkit.keys import (
    KeyNameError,
    calculate_shift,
    calculate_tpc_shift,
    normalize_conventional_key,
    normalize_key,
    spelling_for_key,
    tonic_tpc,
    tpc_alteration,
    tpc_for_pitch,
    tpc_pitch_class,
    transpose_key_signature,
    transpose_key_signature_by_tpc,
    transpose_tpc,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("bb", "Bb"), (" F♯ ", "F#"), ("c", "C"), ("gB", "Gb")],
)
def test_normalize_key(raw, expected):
    assert normalize_key(raw) == expected


def test_rejects_unknown_key():
    with pytest.raises(KeyNameError, match="D#"):
        normalize_key("H")


def test_conventional_key_validation_rejects_theoretical_signatures():
    assert normalize_key("D#") == "D#"
    with pytest.raises(KeyNameError):
        normalize_conventional_key("D#")


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [("Bb", "C", 2), ("C", "B", -1), ("F#", "C", -6), ("C", "Gb", 6)],
)
def test_calculate_nearest_shift(source, target, expected):
    assert calculate_shift(source, target) == expected


def test_target_key_controls_default_spelling():
    assert spelling_for_key("Eb") == "flat"
    assert spelling_for_key("E") == "sharp"
    assert tpc_for_pitch(61, "flat") != tpc_for_pitch(61, "sharp")


@pytest.mark.parametrize("pitch", [-1, 128])
def test_tpc_for_pitch_rejects_out_of_range_values(pitch):
    with pytest.raises(ValueError, match="MIDI pitch"):
        tpc_for_pitch(pitch, "sharp")


@pytest.mark.parametrize("pitch", [60.5, True])
def test_tpc_for_pitch_rejects_non_integer_values(pitch):
    with pytest.raises(TypeError, match="MIDI pitch"):
        tpc_for_pitch(pitch, "sharp")


@pytest.mark.parametrize("spelling", ["", "FLAT", "bogus", None])
def test_tpc_for_pitch_rejects_unknown_spelling(spelling):
    with pytest.raises(ValueError, match="Pitch spelling"):
        tpc_for_pitch(60, spelling)


def test_named_key_interval_controls_tpc_spelling():
    assert tonic_tpc("Cb") == 7
    assert calculate_tpc_shift("C", "D") == 2
    assert calculate_tpc_shift("C#", "Db") == -12
    assert transpose_tpc(20, 2) == 22  # F-sharp -> G-sharp
    assert transpose_tpc(8, 2) == 10  # G-flat -> A-flat


def test_tpc_transposition_respells_beyond_double_accidentals():
    assert transpose_tpc(40, 7) == 23
    assert tpc_alteration(23) == 1
    assert transpose_tpc(-8, -15) == 25
    assert transpose_tpc(40, 0) == 40


def test_tpc_transposition_properties_cover_full_musescore_range():
    for tpc in range(-8, 41):
        for shift in range(-7, 8):
            updated = transpose_tpc(tpc, shift)
            if shift == 0:
                assert updated == tpc
            else:
                assert -1 <= updated <= 33
                assert tpc_pitch_class(updated) == (
                    tpc_pitch_class(tpc) + (7 * shift)
                ) % 12


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        (transpose_tpc, (14.0, 1)),
        (transpose_tpc, (14, 1.0)),
        (tpc_pitch_class, (14.0,)),
        (tpc_alteration, (True,)),
        (transpose_key_signature, (0.0, 2, "sharp")),
        (transpose_key_signature, (0, False, "sharp")),
        (transpose_key_signature_by_tpc, (0, 2.0)),
    ],
)
def test_tonal_helpers_reject_non_integer_numeric_inputs(operation, args):
    with pytest.raises(TypeError, match="integer"):
        operation(*args)


def test_transposes_conventional_key_signatures_by_interval():
    assert transpose_key_signature(0, 2, "sharp") == 2  # C major -> D major
    assert transpose_key_signature(-1, 2, "sharp") == 1  # F major -> G major


def test_key_signature_transposition_honors_enharmonic_spelling():
    assert transpose_key_signature(0, 6, "flat") == -6
    assert transpose_key_signature(0, 6, "sharp") == 6


def test_spelled_interval_controls_each_key_signature_change():
    assert transpose_key_signature_by_tpc(-7, 2) == -5  # C-flat -> D-flat
    assert transpose_key_signature_by_tpc(7, -12) == -5  # C-sharp -> D-flat
