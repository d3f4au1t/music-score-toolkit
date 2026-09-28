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
- `instruments.py` changes a selected part's instrument transposition and
  written notation while preserving sounding pitches; it shares the validated,
  atomic archive-rewrite path with key transposition.
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
- legacy and modern `Harmony` root/bass TPC fields

Each conventional key signature is shifted by the same interval as the notes,
so mid-score key changes are retained instead of being flattened to one key.
Missing initial C signatures are materialized when the destination needs a
visible signature. Custom key-signature definitions remain structurally
untouched while their conventional base key moves; atonal signatures and
percussion staves remain unchanged.

Local key regions keep concert notes, written notes, and chord symbols aligned
when a modulation requires an enharmonic signature. A measure-boundary key
applies to every voice even when its XML is serialized after another voice's
notes. A sequential single-voice change applies at its onset; grace chords and
harmonies at that onset share the new key. Instrument conversion uses the same
regions while preserving sounding pitches. Key definitions are rewritten only
after all source note/harmony contexts have been read.

This is deliberately not a full MuseScore tick evaluator. Changing-key staves
with explicit cursor moves, multi-voice mid-measure changes, and trailing keys
that might be courtesy announcements fail closed when local respelling is
needed. A uniform transposition that requires no key-dependent respelling can
still use one interval across those notes. The ordering rules follow MuseScore's
[measure reader](https://github.com/musescore/MuseScore/blob/v4.7.4/src/engraving/rw/read400/measurerw.cpp):
voices restart at the measure tick, grace chords do not advance it, and keys at
the measure's end are courtesy announcements rather than key-map changes.

TPC values move along MuseScore's line-of-fifths representation instead of
being regenerated from MIDI pitch alone. This preserves enharmonic intent,
including zero-semitone respellings such as C-sharp to D-flat. Existing
accidental nodes keep their role, bracket, EID, and layout metadata.
Stein-Zimmermann quarter-tone symbols follow changes in the displayed note's
alteration. Their MIDI/TPC base and cent offset are adjusted together to preserve
the transposed sounding pitch. Unknown microtonal subtypes are kept
only when their alteration stays the same; unsupported respellings fail closed.
The toolkit's double-accidental
readability policy is applied when an interval would otherwise create a
triple accidental.

Other archive members are copied with their original `ZipInfo` metadata. This
keeps images, styles, audio settings, view settings, and container metadata
outside the transformation boundary.

Staff definitions are resolved independently inside the master score and each
embedded score. Operations that need MuseScore's engraving engine—TAB
refretting, fret-diagram regeneration, and mid-score instrument/staff-type
changes—fail before output rather than leaving contradictory pitch data.

## Atomicity

An output archive is built in the destination directory, closed, and moved
into place with `os.replace`. Validation failures remove the temporary file and
leave any existing destination untouched.

Before publication, every ZIP member is CRC-checked, duplicate names and
invalid MSCX/container structures are rejected, archive comments and output
permissions are retained, and a semantic no-op copies the source byte for
byte.
