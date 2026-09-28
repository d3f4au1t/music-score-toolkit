"""Music Score Toolkit public API."""

from .instruments import (
    InstrumentConversionReport,
    retarget_instrument_mscx,
    retarget_instrument_mscz,
)
from .keys import KeyNameError, calculate_shift, normalize_key
from .mscz import PitchRangeError, ScoreFormatError, TransposeReport, transpose_mscx, transpose_mscz

__all__ = [
    "InstrumentConversionReport",
    "KeyNameError",
    "PitchRangeError",
    "ScoreFormatError",
    "TransposeReport",
    "calculate_shift",
    "normalize_key",
    "retarget_instrument_mscx",
    "retarget_instrument_mscz",
    "transpose_mscx",
    "transpose_mscz",
]
__version__ = "0.1.0"
