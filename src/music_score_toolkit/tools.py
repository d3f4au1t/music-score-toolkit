"""External-tool discovery and score conversion helpers."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


class ExecutableNotFoundError(FileNotFoundError):
    """Raised when an optional desktop dependency cannot be located."""


MUSESCORE_COMMANDS = ("mscore", "musescore", "MuseScore4")
MUSESCORE_MAC_PATHS = ("/Applications/MuseScore 4.app/Contents/MacOS/mscore",)
MUSESCORE_WINDOWS_PATHS = (
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
)

SMARTSCORE_COMMANDS = ("SmartScore", "smartscore")
SMARTSCORE_MAC_PATHS = (
    "/Applications/SmartScore 64 Pro.app/Contents/MacOS/SmartScore 64 Pro",
    "/Applications/SmartScore.app/Contents/MacOS/SmartScore",
)
SMARTSCORE_WINDOWS_PATHS = (
    r"C:\Program Files (x86)\Musitek\SmartScore X2 Professional Edition\SmartScore_pro.exe",
)

MUSICXML_ROOTS = {"score-partwise", "score-timewise"}
MUSICXML_CONTAINER = "META-INF/container.xml"


def _executable_problem(path: Path) -> str | None:
    try:
        path_stat = path.stat()
    except FileNotFoundError:
        return "does not exist"
    except OSError as exc:
        return f"cannot be accessed ({exc})"
    if not stat.S_ISREG(path_stat.st_mode):
        return "is not a file"
    if not os.access(path, os.X_OK):
        return "is not executable"
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _validate_musicxml_root(root: ET.Element) -> None:
    root_name = _local_name(root.tag)
    if root_name not in MUSICXML_ROOTS:
        raise ValueError(f"expected a MusicXML score root, found <{root_name}>")

    children = list(root)
    part_list = next(
        (child for child in children if _local_name(child.tag) == "part-list"),
        None,
    )
    if part_list is None or not any(_local_name(child.tag) == "score-part" for child in part_list):
        raise ValueError("MusicXML score has no populated <part-list>")

    if root_name == "score-partwise":
        parts = [child for child in children if _local_name(child.tag) == "part"]
        complete = bool(parts) and all(
            any(_local_name(child.tag) == "measure" for child in part) for part in parts
        )
    else:
        measures = [child for child in children if _local_name(child.tag) == "measure"]
        complete = bool(measures) and all(
            any(_local_name(child.tag) == "part" for child in measure) for measure in measures
        )
    if not complete:
        raise ValueError("MusicXML score has an incomplete part/measure structure")


def _validate_musicxml(path: Path) -> None:
    _validate_musicxml_root(ET.parse(path).getroot())


def _validate_pdf(path: Path, *, size: int) -> None:
    with path.open("rb") as stream:
        header = stream.read(8)
        if (
            len(header) != 8
            or not header.startswith(b"%PDF-")
            or not header[5:6].isdigit()
            or header[6:7] != b"."
            or not header[7:8].isdigit()
        ):
            raise ValueError("PDF header is missing or invalid")
        stream.seek(max(0, size - 4096))
        trailer = stream.read()
    stripped_trailer = trailer.rstrip()
    if b"startxref" not in trailer or not stripped_trailer.endswith(b"%%EOF"):
        raise ValueError("PDF trailer is incomplete")
    startxref = stripped_trailer.rsplit(b"startxref", 1)[-1].splitlines()
    try:
        xref_offset = int(next(line for line in startxref if line.strip()).strip())
    except (StopIteration, ValueError) as exc:
        raise ValueError("PDF startxref offset is invalid") from exc
    if not 0 <= xref_offset < size:
        raise ValueError("PDF startxref offset is outside the file")


def _validate_zip(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise ValueError(f"ZIP member {bad_member!r} failed its CRC check")
        names = archive.namelist()
        if not any(not name.endswith("/") for name in names):
            raise ValueError("ZIP archive contains no files")
        return names


def _validate_mscz(path: Path) -> None:
    names = _validate_zip(path)
    score_names = [name for name in names if name.lower().endswith(".mscx")]
    if not score_names:
        raise ValueError("MSCZ archive contains no MSCX score")
    with zipfile.ZipFile(path) as archive:
        for score_name in score_names:
            root = ET.fromstring(archive.read(score_name))
            if _local_name(root.tag) != "museScore":
                raise ValueError(f"MSCX member {score_name!r} has an invalid root")
            if not any(_local_name(element.tag) == "Score" for element in root):
                raise ValueError(f"MSCX member {score_name!r} contains no <Score>")


def _validate_mxl(path: Path) -> None:
    names = _validate_zip(path)
    if names.count(MUSICXML_CONTAINER) != 1:
        raise ValueError(f"MXL archive must contain one {MUSICXML_CONTAINER}")
    with zipfile.ZipFile(path) as archive:
        container = ET.fromstring(archive.read(MUSICXML_CONTAINER))
        if _local_name(container.tag) != "container":
            raise ValueError("MXL container has an invalid root")
        rootfile = next(
            (
                element.get("full-path", "")
                for element in container.iter()
                if _local_name(element.tag) == "rootfile"
            ),
            "",
        )
        member_path = PurePosixPath(rootfile)
        if (
            not rootfile
            or member_path.is_absolute()
            or ".." in member_path.parts
            or names.count(rootfile) != 1
        ):
            raise ValueError(f"MXL container references an invalid rootfile {rootfile!r}")
        _validate_musicxml_root(ET.fromstring(archive.read(rootfile)))


def _validate_mscx(path: Path) -> None:
    root = ET.parse(path).getroot()
    if _local_name(root.tag) != "museScore":
        raise ValueError(f"expected a MuseScore document root, found <{_local_name(root.tag)}>")
    if not any(_local_name(element.tag) == "Score" for element in root):
        raise ValueError("MuseScore document contains no <Score>")


def _validate_generated_output(path: Path, *, size: int) -> bool:
    """Validate known output formats and report whether the suffix is known."""

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        _validate_pdf(path, size=size)
    elif suffix == ".mscz":
        _validate_mscz(path)
    elif suffix == ".mxl":
        _validate_mxl(path)
    elif suffix in {".musicxml", ".xml"}:
        _validate_musicxml(path)
    elif suffix == ".mscx":
        _validate_mscx(path)
    elif suffix == ".zip":
        _validate_zip(path)
    else:
        return False
    return True


def find_executable(
    *,
    label: str,
    env_var: str,
    commands: Iterable[str],
    known_paths: Iterable[str],
) -> Path:
    configured = os.environ.get(env_var)
    if configured:
        candidate = Path(configured).expanduser()
        problem = _executable_problem(candidate)
        if problem is None:
            return candidate
        raise ExecutableNotFoundError(
            f"{env_var} points to an unusable executable: {candidate} {problem}"
        )

    for command in commands:
        discovered = shutil.which(command)
        if discovered and _executable_problem(Path(discovered)) is None:
            return Path(discovered)

    for path in known_paths:
        candidate = Path(path)
        if _executable_problem(candidate) is None:
            return candidate

    raise ExecutableNotFoundError(
        f"{label} was not found. Install it, add it to PATH, or set {env_var}."
    )


def find_musescore() -> Path:
    return find_executable(
        label="MuseScore 4",
        env_var="MUSESCORE_PATH",
        commands=MUSESCORE_COMMANDS,
        known_paths=(*MUSESCORE_MAC_PATHS, *MUSESCORE_WINDOWS_PATHS),
    )


def find_smartscore() -> Path:
    return find_executable(
        label="SmartScore",
        env_var="SMARTSCORE_PATH",
        commands=SMARTSCORE_COMMANDS,
        known_paths=(*SMARTSCORE_MAC_PATHS, *SMARTSCORE_WINDOWS_PATHS),
    )


def convert_score(
    input_path: str | Path,
    output_path: str | Path,
    *,
    musescore: str | Path | None = None,
) -> Path:
    """Convert a score through MuseScore's command-line interface."""

    source = Path(input_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    try:
        source_stat = source.stat()
    except FileNotFoundError:
        raise FileNotFoundError(f"Input score does not exist: {source}")
    except OSError as exc:
        raise RuntimeError(f"Unable to access input score {source}: {exc}") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise FileNotFoundError(f"Input score does not exist: {source}")
    executable = Path(musescore).expanduser() if musescore else find_musescore()
    executable_problem = _executable_problem(executable)
    if executable_problem is not None:
        raise ExecutableNotFoundError(
            f"MuseScore executable is unusable: {executable} {executable_problem}"
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Unable to create output directory {destination.parent}: {exc}"
        ) from exc

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=destination.suffix,
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to stage output beside {destination}: {exc}") from exc
    temporary_output = Path(temporary_name)
    try:
        os.close(descriptor)
        temporary_output.unlink()
    except OSError as exc:
        try:
            temporary_output.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"Unable to prepare staged output {temporary_output}: {exc}") from exc

    try:
        try:
            completed = subprocess.run(
                [str(executable), str(source), "-o", str(temporary_output)],
                check=False,
            )
        except OSError as exc:
            raise RuntimeError(f"Unable to run MuseScore executable {executable}: {exc}") from exc

        try:
            output_stat = temporary_output.stat()
        except FileNotFoundError:
            raise RuntimeError(
                f"MuseScore exited without creating the requested output: {destination} "
                f"(exit code {completed.returncode})."
            )
        except OSError as exc:
            raise RuntimeError(
                f"Unable to inspect MuseScore output for {destination}: {exc}"
            ) from exc
        if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_size == 0:
            raise RuntimeError(
                f"MuseScore exited without creating a nonempty output file: {destination} "
                f"(exit code {completed.returncode})."
            )

        try:
            known_format = _validate_generated_output(
                temporary_output,
                size=output_stat.st_size,
            )
        except (
            ET.ParseError,
            KeyError,
            LookupError,
            NotImplementedError,
            OSError,
            RuntimeError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            raise RuntimeError(
                f"MuseScore produced an invalid {destination.suffix or 'unknown-format'} "
                f"output for {destination}: {exc} (exit code {completed.returncode})."
            ) from exc

        # A valid known-format file can survive MuseScore's common teardown
        # SIGABRT. For an unknown format, a nonzero status cannot be safely
        # distinguished from a partial output.
        if completed.returncode != 0 and not known_format:
            raise RuntimeError(
                f"MuseScore failed to convert {source} to {destination} "
                f"(exit code {completed.returncode}); the output format cannot be validated."
            )
        try:
            os.replace(temporary_output, destination)
        except OSError as exc:
            raise RuntimeError(f"Unable to publish converted score {destination}: {exc}") from exc
    finally:
        try:
            temporary_output.unlink(missing_ok=True)
        except OSError:
            # Best-effort cleanup must not mask the conversion or publication error.
            pass
    return destination
