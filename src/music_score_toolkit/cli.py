"""Command-line interface for score transposition and conversion."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .instruments import retarget_instrument_mscz
from .mscz import transpose_mscz
from .tools import convert_score
from .workflows import recognize_pdf_with_smartscore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-score",
        description="Transpose MuseScore files and run explicit desktop conversion workflows.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    transpose = commands.add_parser("transpose", help="Transpose an MSCZ score.")
    transpose.add_argument("input", type=Path)
    transpose.add_argument("output", type=Path)
    transpose.add_argument("--from-key", required=True)
    transpose.add_argument("--to-key", required=True)
    transpose.add_argument(
        "--allow-pitch-clipping",
        action="store_true",
        help="Clip pitches outside MIDI 0..127 instead of aborting.",
    )
    transpose.add_argument(
        "--ignore-source-key",
        action="store_true",
        help="Skip checking --from-key against an unambiguous opening concert key.",
    )
    transpose.add_argument(
        "--export-pdf",
        type=Path,
        help="After transposition, ask MuseScore to create this PDF.",
    )

    instrument = commands.add_parser(
        "instrument", help="Rewrite a part for another instrument pitch without changing its sound.",
    )
    instrument.add_argument("input", type=Path)
    instrument.add_argument("output", type=Path)
    instrument.add_argument("--to-instrument", default="Bb", help="C, Bb (default), A, F, or Eb.")
    instrument.add_argument("--part", type=int, help="One-based part number; required for multi-part scores.")
    instrument.add_argument("--part-name", help="Printed part label; defaults to the destination pitch.")
    instrument.add_argument("--export-pdf", type=Path)

    convert = commands.add_parser("convert", help="Convert a score through MuseScore 4.")
    convert.add_argument("input", type=Path)
    convert.add_argument("output", type=Path)

    recognize = commands.add_parser(
        "recognize",
        help="Launch SmartScore for manual PDF recognition, then create MSCZ.",
    )
    recognize.add_argument("pdf", type=Path)
    recognize.add_argument("output_directory", type=Path)
    recognize.add_argument("--timeout", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "transpose":
            report = transpose_mscz(
                args.input,
                args.output,
                args.from_key,
                args.to_key,
                strict_pitch_range=not args.allow_pitch_clipping,
                validate_source_key=not args.ignore_source_key,
            )
            if args.export_pdf:
                convert_score(args.output, args.export_pdf)
            print(json.dumps(asdict(report), indent=2))
        elif args.command == "instrument":
            report = retarget_instrument_mscz(
                args.input, args.output, args.to_instrument,
                part=args.part, part_name=args.part_name,
            )
            if args.export_pdf:
                convert_score(args.output, args.export_pdf)
            print(json.dumps(asdict(report), indent=2))
        elif args.command == "convert":
            print(convert_score(args.input, args.output))
        else:
            print(
                recognize_pdf_with_smartscore(
                    args.pdf,
                    args.output_directory,
                    timeout=args.timeout,
                )
            )
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        print(f"music-score: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
