# Architecture

## Design goals

1. Keep core transposition standard-library only.
2. Never silently corrupt a score when validation can fail closed.
3. Isolate optional desktop applications behind explicit workflows.
4. Preserve source-repository behavior through real regression fixtures.

## Modules

- `keys.py` normalizes major keys and contains semitone, key-signature, and
  MuseScore TPC mappings.
- `mscz.py` performs XML transformation and atomic MSCZ repacking.
- `tools.py` discovers MuseScore/SmartScore and runs MuseScore conversion.
- `workflows.py` coordinates the manual SmartScore export loop, including
  stable-file detection and MusicXML/MXL container validation.
- `cli.py` provides stable user-facing commands and JSON reports.

## Transformation boundary

The engine parses the MSCX entry and only changes:

- `Note/pitch`
- `Note/tpc`
- existing `Note/tpc2`
- standard `Note/Accidental/subtype` when its displayed TPC is unambiguous
- `KeySig/concertKey` and `KeySig/actualKey` (MuseScore 4), or
  `KeySig/accidental` (legacy scores)

Each conventional key signature is shifted by the same interval as the notes,
so mid-score key changes are retained instead of being flattened to one key.
Custom key-signature definitions remain untouched.

TPC values move along MuseScore's line-of-fifths representation instead of
being regenerated from MIDI pitch alone. This preserves enharmonic intent,
including zero-semitone respellings such as C-sharp to D-flat. Existing
accidental nodes keep their role, bracket, EID, and layout metadata; unknown
microtonal subtypes are left unchanged. MuseScore 4's double-accidental limit
is applied when an interval would otherwise create a triple accidental.

Other archive members are copied with their original `ZipInfo` metadata. This
keeps images, styles, audio settings, view settings, and container metadata
outside the transformation boundary.

## Atomicity

An output archive is built in the destination directory, closed, and moved
into place with `os.replace`. Validation failures remove the temporary file and
leave any existing destination untouched.
