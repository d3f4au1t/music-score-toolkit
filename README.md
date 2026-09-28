# Music Score Toolkit

[![CI](https://github.com/d3f4au1t/music-score-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/d3f4au1t/music-score-toolkit/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

A loss-minimizing Python toolkit focused on reliable automatic transposition
of MuseScore MSCZ files, plus explicit MuseScore/SmartScore conversion
workflows.

This repository consolidates the maintained functionality of
[`auto-transpose`](https://github.com/jzjzzzzzzz/auto-transpose) and
[`Auto-Music-Transpose`](https://github.com/jzjzzzzzzz/Auto-Music-Transpose).
The original repositories remain available as read-only archives.

## Why this repository exists

The original prototypes demonstrated direct MSCX editing and desktop score
conversion. This consolidation keeps those workflows while adding a package
boundary, a real CLI, atomic output writes, actionable dependency discovery,
and regression tests against both original MSCZ samples.

## Features

- Transpose every `.mscx` entry inside a MuseScore `.mscz` archive.
- Rewrite a selected part for C, B-flat, A, F, or E-flat instrument pitch
  while preserving its sounding pitches and updating written notation.
- Update MIDI pitch, concert and written MuseScore TPC spelling, and both
  MuseScore 4 and legacy conventional key signatures while preserving key
  changes and source-note enharmonic intent.
- Add a readable destination key signature when a C-major score omitted its
  implicit initial signature, and transpose legacy and modern chord symbols.
- Resolve each staff independently: pitched notation is transposed,
  percussion mappings are preserved, and unsupported TAB/fret-diagram cases
  stop safely instead of producing contradictory notation.
- Preserve chords, rests, ties, rhythm, lyrics, layout files, thumbnails, and
  other archive members, including archive comments and file permissions.
- Abort before writing output when a transposition exceeds MIDI `0..127`.
- Convert MusicXML, MSCZ, and other MuseScore-supported inputs through the
  MuseScore 4 CLI.
- Launch SmartScore for manual PDF recognition, wait for a stable and valid
  MusicXML/MXL export, and complete the conversion to MSCZ.
- Run without third-party Python packages for core transposition.

## Installation

```bash
git clone https://github.com/d3f4au1t/music-score-toolkit.git
cd music-score-toolkit
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.venv\Scripts\activate`.

## Quick start

Transpose a B-flat score to C:

```bash
music-score transpose input.mscz output.mscz \
  --from-key Bb \
  --to-key C
```

The command prints a machine-readable report:

```json
{
  "from_key": "Bb",
  "to_key": "C",
  "semitone_shift": 2,
  "notes_changed": 184,
  "key_signatures_changed": 1,
  "score_entries_changed": 1,
  "chord_symbols_changed": 12
}
```

Transpose and export a PDF through MuseScore:

```bash
music-score transpose input.mscz output.mscz \
  --from-key Bb --to-key C --export-pdf output.pdf
```

`--from-key` and `--to-key` describe musical keys, not instrument names. When
the score has an unambiguous opening concert key, the command checks that it
matches `--from-key` so a typo cannot silently transpose by the wrong interval.
For an intentionally ambiguous or partial score, that check can be bypassed
with `--ignore-source-key`.

To prepare concert-C music for a B-flat instrument while keeping its original
sounding pitch, use the instrument conversion command:

```bash
music-score instrument concert-c.mscz b-flat-part.mscz \
  --to-instrument Bb --part-name "B-flat part" --export-pdf b-flat-part.pdf
```

The output is displayed in written pitch: a sounding C becomes a written D,
and concert C major gets a written D-major signature. Playback stays at the
original pitch. Instrument conversion also updates written chord symbols,
accidentals, and the instrument's transposition settings. The existing playback
sound and clef are retained; this command sets instrument **pitch**, not a new
sound-library instrument. Supported pitches are C, Bb (the default), A, F, and
Eb, with sounding intervals of 0, -2, -3, -7, and -9 semitones respectively.
Octave-transposing variants such as bass clarinet are not separate presets yet.

For a multi-part score, add `--part 2` (part numbers start at 1). Omitting it
lists the available choices in an error. Multi-part input must already be in
written-pitch view; the other parts are preserved. The command currently
requires MuseScore 4 format, a score without linked excerpts, and a selected part without
mid-score instrument changes, custom or atonal keys, percussion, TAB, or fret diagrams.
Key changes are supported between and inside measures, including multiple
voices. Exact timing handles dotted rhythms, modern nested tuplets, grace
notes, full-measure rests, pickups, and relative cursor moves within a measure.
Notes and chord symbols use the key at their own onset; end-of-measure courtesy
signatures do not change the following measure's key. When timing is needed,
the score must provide a time signature or explicit measure length. Missing or
malformed rhythms, legacy absolute ticks, cursor moves between staves/voices,
local meter stretching, measure repeats, and ambiguous tremolos still fail safely.
Cursor moves inside tuplets or grace groups also remain unsupported.
Keep MSCZ as the master file: MuseScore 4.7.4's MusicXML exporter was observed to
omit mid-measure key signatures, even from unmodified input. Native MSCZ/MSCX
round trips retain those changes; this toolkit does not fix that external exporter.
Export a standalone part from MuseScore when an archive includes linked excerpts.
Open and save older score files in MuseScore 4 before instrument conversion.

Use `transpose --from-key C --to-key D` when you actually want to raise the
music's sounding pitch by a whole step instead.

Convert MusicXML to PDF or MSCZ:

```bash
music-score convert score.musicxml score.pdf
music-score convert score.musicxml score.mscz
```

Run the retained SmartScore-assisted recognition workflow:

```bash
music-score recognize scan.pdf ./recognized --timeout 1800
```

SmartScore recognition remains a manual proofreading step. Save the exported
`.mxl`, `.musicxml`, or `.xml` file in the requested output directory; the
toolkit waits until its size and timestamps stop changing, validates its
MusicXML score structure, and only then asks MuseScore to create the final MSCZ
file. Unrelated XML and partial exports are ignored while the workflow waits.

## Desktop dependencies

Core MSCZ transposition uses only the Python standard library. Conversion and
recognition commands discover optional desktop tools in this order:

1. `MUSESCORE_PATH` or `SMARTSCORE_PATH`
2. an executable on `PATH`
3. common macOS and Windows installation locations

Example:

```bash
export MUSESCORE_PATH="/Applications/MuseScore 4.app/Contents/MacOS/mscore"
```

## Python API

```python
from music_score_toolkit import retarget_instrument_mscz, transpose_mscz

report = transpose_mscz(
    "input.mscz",
    "output.mscz",
    from_key="Bb",
    to_key="C",
)
print(report.notes_changed)

# Prepare a B-flat part without raising playback pitch.
retarget_instrument_mscz("concert-part.mscz", "bb-part.mscz", "Bb")
```

## Reliability boundaries

- Major keys in conventional sharp/flat notation are supported.
- Note spelling follows the full named interval, so enharmonic distinctions
  such as F-sharp versus G-flat survive transposition. Standard explicit
  accidental metadata is retained and its glyph is updated when unambiguous.
- For readable output, spellings that would require more than a
  double accidental are reduced to a readable enharmonic equivalent.
- The toolkit changes score semantics conservatively, but it is not an
  engraving engine. Review complex notation in MuseScore after conversion.
- Percussion staves and atonal signatures are deliberately left untouched.
  Custom key-signature definitions are preserved while their base key moves.
- Stein-Zimmermann quarter-tone accidental glyphs follow the transposed note
  spelling while preserving the transposed sounding pitch and display metadata. Other microtonal
  symbols remain unchanged when their alteration stays the same; unsupported
  or ambiguous respellings stop before output is written. Tablature,
  fret diagrams, mid-score staff/instrument changes, and ambiguous
  key-change timing fail closed when direct XML editing could
  make their visual and sounding representations disagree.
- MSCZ members, CRCs, duplicate names, XML shape, and container references are
  validated before output replaces an existing file.
- PDF optical music recognition is delegated to SmartScore. For a dedicated
  Audiveris workflow, see
  [`PDFtoMSCZ`](https://github.com/jzjzzzzzzz/PDFtoMSCZ).

## Development

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest
```

Tests include generated XML cases and both MSCZ files from the source
repositories. External desktop applications are isolated from unit tests. To
also verify barline and polyphonic mid-measure modulations, triplets, dotted
rhythms, written spelling, instrument conversion, sounding pitches, and score
markings using an installed MuseScore 4 exporter:

```bash
MUSIC_SCORE_MUSESCORE_TESTS=1 pytest tests/test_musescore_integration.py
```

## Repository map

```text
src/music_score_toolkit/   maintained package
tests/                     unit and source-fixture regression tests
legacy/                    original script snapshots for provenance
docs/                      architecture and maintenance documentation
MIGRATION.md               old-command migration guide
```

## Project status

Active consolidation release. New issues and changes belong in this
repository; the two source repositories are retained as archived historical
records.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Original MIT
and Apache-2.0 source provenance is documented in [MIGRATION.md](MIGRATION.md).
