# Changelog

## Unreleased

- Made MuseScore conversions atomic so failed exports preserve existing destination files.
- Transpose MuseScore 4 `concertKey` signatures by interval, including mid-score key changes.
- Wait for stable, structurally valid MusicXML/MXL before converting SmartScore exports.
- Preserve enharmonic note spelling, transpose written `tpc2` values, and keep
  structured accidental metadata consistent during transposition; reject key
  names that cannot be represented by conventional MuseScore signatures.
- Treat a complete MuseScore export as a success when MuseScore aborts during
  headless teardown, and fail only when no usable output file is created.

## 0.1.0 - 2026-08-15

- Consolidated `auto-transpose` and `Auto-Music-Transpose`.
- Added installable package and `music-score` CLI.
- Added validated, atomic MSCZ transposition.
- Retained MuseScore conversion and SmartScore-assisted recognition.
- Added regression coverage using both original sample scores.
