import pytest

from music_score_toolkit.keys import (
    KeyNameError,
    calculate_shift,
    normalize_key,
    spelling_for_key,
    tpc_for_pitch,
    transpose_key_signature,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("bb", "Bb"), (" F♯ ", "F#"), ("c", "C"), ("gB", "Gb")],
)
def test_normalize_key(raw, expected):
    assert normalize_key(raw) == expected


def test_rejects_unknown_key():
    with pytest.raises(KeyNameError):
        normalize_key("H")


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


def test_transposes_conventional_key_signatures_by_interval():
    assert transpose_key_signature(0, 2, "sharp") == 2  # C major -> D major
    assert transpose_key_signature(-1, 2, "sharp") == 1  # F major -> G major


def test_key_signature_transposition_honors_enharmonic_spelling():
    assert transpose_key_signature(0, 6, "flat") == -6
    assert transpose_key_signature(0, 6, "sharp") == 6
