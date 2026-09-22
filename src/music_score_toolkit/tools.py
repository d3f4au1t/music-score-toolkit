"""External-tool discovery and score conversion helpers."""

from __future__ import annotations

import lzma
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


class ExecutableNotFoundError(FileNotFoundError):
    """Raised when an optional desktop dependency cannot be located."""


MUSESCORE_COMMANDS = ("mscore", "musescore", "MuseScore4")
MUSESCORE_MAC_PATHS = ("/Applications/MuseScore 4.app/Contents/MacOS/mscore",)
MUSESCORE_WINDOWS_PATHS = (
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
_MAX_ZIP_MEMBERS = 10_000
_MAX_ZIP_MEMBER_SIZE = 2 * 1024 * 1024 * 1024
_MAX_ZIP_TOTAL_SIZE = 4 * 1024 * 1024 * 1024
_MAX_ZIP_COMPRESSION_RATIO = 500
_MAX_MUSICXML_SIZE = 256 * 1024 * 1024
_MAX_CONTAINER_XML_SIZE = 16 * 1024 * 1024
_ZIP_READ_CHUNK_SIZE = 1024 * 1024
_MUSICXML_MIMETYPE = b"application/vnd.recordare.musicxml"
_MUSICXML_ROOTFILE_MEDIA_TYPE = "application/vnd.recordare.musicxml+xml"
_XML_WHITESPACE_RE = re.compile(r"[ \t\r\n]+")


class _ZipValidationError(ValueError):
    """Raised when ZIP metadata or a bounded member read is unsafe."""


def _is_teardown_abort(returncode: int) -> bool:
    return returncode in {-signal.SIGABRT, 128 + signal.SIGABRT}


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


def _collapse_xml_token(value: str) -> str:
    return _XML_WHITESPACE_RE.sub(" ", value).strip(" ")


def _is_musicxml_rootfile_media_type(value: str) -> bool:
    essence = value.partition(";")[0].strip().casefold()
    return not essence or essence == _MUSICXML_ROOTFILE_MEDIA_TYPE


def _is_xml_ncname_start(character: str) -> bool:
    codepoint = ord(character)
    return (
        character == "_"
        or "A" <= character <= "Z"
        or "a" <= character <= "z"
        or 0xC0 <= codepoint <= 0xD6
        or 0xD8 <= codepoint <= 0xF6
        or 0xF8 <= codepoint <= 0x2FF
        or 0x370 <= codepoint <= 0x37D
        or 0x37F <= codepoint <= 0x1FFF
        or 0x200C <= codepoint <= 0x200D
        or 0x2070 <= codepoint <= 0x218F
        or 0x2C00 <= codepoint <= 0x2FEF
        or 0x3001 <= codepoint <= 0xD7FF
        or 0xF900 <= codepoint <= 0xFDCF
        or 0xFDF0 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0xEFFFF
    )


def _is_xml_ncname(value: str) -> bool:
    if not value or not _is_xml_ncname_start(value[0]):
        return False
    return all(
        _is_xml_ncname_start(character)
        or character in {"-", "."}
        or "0" <= character <= "9"
        or ord(character) == 0xB7
        or 0x300 <= ord(character) <= 0x36F
        or 0x203F <= ord(character) <= 0x2040
        for character in value[1:]
    )


def _musicxml_ids(elements: list[ET.Element], *, label: str) -> list[str]:
    identifiers = [
        _collapse_xml_token(element.get("id", "")) for element in elements
    ]
    if any(not _is_xml_ncname(identifier) for identifier in identifiers):
        raise ValueError(f"MusicXML {label} has a missing or invalid id")
    duplicates = [
        identifier
        for identifier, count in Counter(identifiers).items()
        if count > 1
    ]
    if duplicates:
        raise ValueError(f"MusicXML {label} contains duplicate id {duplicates[0]!r}")
    return identifiers


def _validate_musicxml_root(root: ET.Element) -> None:
    root_name = _local_name(root.tag)
    if root_name not in MUSICXML_ROOTS:
        raise ValueError(f"expected a MusicXML score root, found <{root_name}>")

    children = list(root)
    part_lists = [
        child for child in children if _local_name(child.tag) == "part-list"
    ]
    if len(part_lists) != 1:
        raise ValueError("MusicXML score must contain one populated <part-list>")
    part_list = part_lists[0]
    body_name = "part" if root_name == "score-partwise" else "measure"
    if any(
        _local_name(child.tag) == body_name
        for child in children[: children.index(part_list)]
    ):
        raise ValueError(
            f"MusicXML <part-list> must precede all <{body_name}> elements"
        )
    score_parts = [
        child for child in part_list if _local_name(child.tag) == "score-part"
    ]
    if not score_parts:
        raise ValueError("MusicXML score has no populated <part-list>")
    if any(
        len(
            [
                child
                for child in score_part
                if _local_name(child.tag) == "part-name"
            ]
        )
        != 1
        for score_part in score_parts
    ):
        raise ValueError(
            "MusicXML <score-part> must contain one direct <part-name>"
        )
    declared_ids = _musicxml_ids(score_parts, label="<score-part>")

    if root_name == "score-partwise":
        parts = [child for child in children if _local_name(child.tag) == "part"]
        part_ids = _musicxml_ids(parts, label="<part>")
        if set(part_ids) != set(declared_ids):
            raise ValueError(
                "MusicXML <part> IDs do not match <score-part> declarations"
            )
        measures_by_part = [
            [child for child in part if _local_name(child.tag) == "measure"]
            for part in parts
        ]
        complete = bool(parts) and all(measures_by_part)
        measures = [measure for group in measures_by_part for measure in group]
    else:
        measures = [child for child in children if _local_name(child.tag) == "measure"]
        complete = bool(measures)
        for measure in measures:
            parts = [child for child in measure if _local_name(child.tag) == "part"]
            part_ids = _musicxml_ids(parts, label="<part>")
            if set(part_ids) != set(declared_ids):
                raise ValueError(
                    "MusicXML measure <part> IDs do not match <score-part> declarations"
                )
    if any("number" not in measure.attrib for measure in measures):
        raise ValueError("MusicXML <measure> is missing its required number")
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


def _validated_zip_members(
    archive: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > _MAX_ZIP_MEMBERS:
        raise _ZipValidationError(
            f"ZIP archive contains too many members ({len(infos)}; "
            f"maximum {_MAX_ZIP_MEMBERS})"
        )

    members: dict[str, zipfile.ZipInfo] = {}
    normalized_names: dict[str, str] = {}
    total_size = 0
    for info in infos:
        if info.filename in members:
            raise _ZipValidationError(
                f"ZIP archive contains duplicate member {info.filename!r}"
            )
        raw_name = info.orig_filename
        name = info.filename
        if raw_name != name or not name:
            raise _ZipValidationError(
                f"ZIP archive contains an invalid member name {raw_name!r}"
            )
        if "\\" in name or any(ord(character) < 32 for character in name):
            raise _ZipValidationError(
                f"ZIP archive contains an unsafe member path {name!r}"
            )
        path_text = name.removesuffix("/")
        parts = path_text.split("/")
        if (
            not path_text
            or name.startswith("/")
            or PurePosixPath(path_text).is_absolute()
            or (
                len(path_text) >= 3
                and path_text[0].isalpha()
                and path_text[1:3] == ":/"
            )
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise _ZipValidationError(
                f"ZIP archive contains an unsafe member path {name!r}"
            )
        previous = normalized_names.get(path_text)
        if previous is not None:
            raise _ZipValidationError(
                f"ZIP archive contains colliding members {previous!r} and {name!r}"
            )
        normalized_names[path_text] = name

        unix_mode = info.external_attr >> 16
        file_type = stat.S_IFMT(unix_mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise _ZipValidationError(
                f"ZIP archive member {name!r} has an unsupported special-file type"
            )
        if (file_type == stat.S_IFDIR) != info.is_dir() and file_type != 0:
            raise _ZipValidationError(
                f"ZIP archive member {name!r} has inconsistent file metadata"
            )
        members[info.filename] = info
        if info.flag_bits & 0x1:
            raise _ZipValidationError(
                f"ZIP archive member {info.filename!r} is encrypted"
            )
        if (
            info.file_size < 0
            or info.compress_size < 0
            or info.file_size > _MAX_ZIP_MEMBER_SIZE
        ):
            raise _ZipValidationError(
                f"ZIP archive member {info.filename!r} exceeds the "
                f"{_MAX_ZIP_MEMBER_SIZE}-byte limit"
            )
        total_size += info.file_size
        if total_size > _MAX_ZIP_TOTAL_SIZE:
            raise _ZipValidationError(
                f"ZIP archive expands beyond {_MAX_ZIP_TOTAL_SIZE} bytes"
            )
        if (
            info.compress_type == zipfile.ZIP_STORED
            and info.compress_size != info.file_size
        ):
            raise _ZipValidationError(
                f"ZIP member {info.filename!r} has inconsistent stored-size metadata"
            )
        if (
            info.file_size >= 1024 * 1024
            and info.file_size
            > max(info.compress_size, 1) * _MAX_ZIP_COMPRESSION_RATIO
        ):
            raise _ZipValidationError(
                f"ZIP archive member {info.filename!r} has a suspicious compression ratio"
            )

    for path_text, name in normalized_names.items():
        parts = path_text.split("/")
        for index in range(1, len(parts)):
            ancestor_name = normalized_names.get("/".join(parts[:index]))
            if ancestor_name is not None and not members[ancestor_name].is_dir():
                raise _ZipValidationError(
                    "ZIP archive contains a file/directory path collision between "
                    f"{ancestor_name!r} and {name!r}"
                )
    return members


def _validate_mxl_package_members(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> None:
    for info in members.values():
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise _ZipValidationError(
                f"MXL member {info.filename!r} uses an unsupported compression method"
            )

    mimetype = members.get("mimetype")
    if mimetype is None:
        return
    if mimetype.compress_type != zipfile.ZIP_STORED or mimetype.extra:
        raise _ZipValidationError(
            "MXL mimetype must be stored without compression or extra fields"
        )
    if _read_zip_member(
        archive,
        mimetype,
        maximum_size=len(_MUSICXML_MIMETYPE),
    ) != _MUSICXML_MIMETYPE:
        raise _ZipValidationError("MXL mimetype has invalid content")


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_size: int,
) -> bytes:
    if info.is_dir() or info.file_size > maximum_size:
        raise _ZipValidationError(
            f"ZIP member {info.filename!r} is not a file within the "
            f"{maximum_size}-byte limit"
        )
    chunks: list[bytes] = []
    size = 0
    try:
        with archive.open(info) as member:
            while chunk := member.read(_ZIP_READ_CHUNK_SIZE):
                size += len(chunk)
                if size > maximum_size or size > info.file_size:
                    raise _ZipValidationError(
                        f"ZIP member {info.filename!r} expands beyond its declared size"
                    )
                chunks.append(chunk)
    except _ZipValidationError:
        raise
    except (
        EOFError,
        UnicodeError,
        lzma.LZMAError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ) as exc:
        raise _ZipValidationError(
            f"Unable to read or validate ZIP member {info.filename!r}: {exc}"
        ) from exc
    if size != info.file_size:
        raise _ZipValidationError(
            f"ZIP member {info.filename!r} size does not match its metadata"
        )
    return b"".join(chunks)


def _validate_zip(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        members = _validated_zip_members(archive)
        if not any(not info.is_dir() for info in members.values()):
            raise ValueError("ZIP archive contains no files")
        for info in members.values():
            if not info.is_dir():
                _read_zip_member(
                    archive,
                    info,
                    maximum_size=_MAX_ZIP_MEMBER_SIZE,
                )
        return list(members)


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
            direct_scores = [
                element for element in root if _local_name(element.tag) == "Score"
            ]
            if len(direct_scores) != 1:
                raise ValueError(
                    f"MSCX member {score_name!r} must contain one direct <Score>"
                )


def _validate_mxl(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        members = _validated_zip_members(archive)
        _validate_mxl_package_members(archive, members)
        container_info = members.get(MUSICXML_CONTAINER)
        if container_info is None or container_info.is_dir():
            raise ValueError(f"MXL archive must contain one {MUSICXML_CONTAINER}")
        container = ET.fromstring(
            _read_zip_member(
                archive,
                container_info,
                maximum_size=_MAX_CONTAINER_XML_SIZE,
            )
        )
        if _local_name(container.tag) != "container":
            raise ValueError("MXL container has an invalid root")
        if container.attrib or len(container) != 1:
            raise ValueError("MXL container must contain only one direct <rootfiles>")
        rootfiles_containers = [
            child for child in container if _local_name(child.tag) == "rootfiles"
        ]
        if len(rootfiles_containers) != 1:
            raise ValueError("MXL container must contain one direct <rootfiles>")
        rootfiles = [
            child
            for child in rootfiles_containers[0]
            if _local_name(child.tag) == "rootfile"
        ]
        all_rootfiles = [
            element
            for element in container.iter()
            if _local_name(element.tag) == "rootfile"
        ]
        if (
            rootfiles_containers[0].attrib
            or len(rootfiles) != len(rootfiles_containers[0])
            or not rootfiles
            or len(all_rootfiles) != len(rootfiles)
        ):
            raise ValueError(
                "MXL container must contain valid direct <rootfile> entries only"
            )
        references: set[str] = set()
        for index, rootfile in enumerate(rootfiles):
            if (
                list(rootfile)
                or (rootfile.text or "").strip()
                or set(rootfile.attrib) - {"full-path", "media-type"}
            ):
                raise ValueError("MXL container has an invalid <rootfile> element")
            reference = _collapse_xml_token(rootfile.get("full-path", ""))
            media_type = _collapse_xml_token(rootfile.get("media-type", ""))
            if index == 0 and not _is_musicxml_rootfile_media_type(media_type):
                raise ValueError(
                    "MXL first rootfile has a non-MusicXML media-type"
                )
            member_path = PurePosixPath(reference)
            try:
                member_info = members[reference]
            except KeyError:
                member_info = None
            if (
                not reference
                or member_path.is_absolute()
                or ".." in member_path.parts
                or reference in references
                or member_info is None
                or member_info.is_dir()
            ):
                raise ValueError(
                    f"MXL container references an invalid rootfile {reference!r}"
                )
            references.add(reference)
        first_rootfile = _collapse_xml_token(rootfiles[0].get("full-path", ""))
        _validate_musicxml_root(
            ET.fromstring(
                _read_zip_member(
                    archive,
                    members[first_rootfile],
                    maximum_size=_MAX_MUSICXML_SIZE,
                )
            )
        )


def _validate_mscx(path: Path) -> None:
    root = ET.parse(path).getroot()
    if _local_name(root.tag) != "museScore":
        raise ValueError(f"expected a MuseScore document root, found <{_local_name(root.tag)}>")
    direct_scores = [element for element in root if _local_name(element.tag) == "Score"]
    if len(direct_scores) != 1:
        raise ValueError("MuseScore document must contain one direct <Score>")


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
        candidate = Path(configured).expanduser().resolve()
        problem = _executable_problem(candidate)
        if problem is None:
            return candidate
        raise ExecutableNotFoundError(
            f"{env_var} points to an unusable executable: {candidate} {problem}"
        )

    for command in commands:
        discovered = shutil.which(command)
        if discovered:
            candidate = Path(discovered).resolve()
            if _executable_problem(candidate) is None:
                return candidate

    for path in known_paths:
        candidate = Path(path).expanduser().resolve()
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


def require_executable(path: str | Path, *, label: str) -> Path:
    """Return an explicit executable path or raise an actionable error."""

    executable = Path(path).expanduser().resolve()
    problem = _executable_problem(executable)
    if problem is not None:
        raise ExecutableNotFoundError(
            f"{label} executable is unusable: {executable} {problem}"
        )
    return executable


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
    try:
        destination_stat = destination.stat()
    except FileNotFoundError:
        output_mode = stat.S_IMODE(source_stat.st_mode)
    except OSError as exc:
        raise RuntimeError(f"Unable to inspect output path {destination}: {exc}") from exc
    else:
        output_mode = stat.S_IMODE(destination_stat.st_mode)
    executable = (
        require_executable(musescore, label="MuseScore")
        if musescore
        else find_musescore()
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Unable to create output directory {destination.parent}: {exc}"
        ) from exc

    try:
        staging_directory = Path(
            tempfile.mkdtemp(
                dir=destination.parent,
                prefix=".music-score-",
            )
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to stage output beside {destination}: {exc}") from exc

    # MuseScore treats a hidden MSCX filename as an unpack directory instead
    # of a score export. Keep the basename visible and isolate any sidecars it
    # creates so the destination directory stays clean.
    temporary_output = staging_directory / f"output{destination.suffix}"

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
            EOFError,
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

        # A valid known-format file can survive MuseScore's known teardown
        # SIGABRT. Other failures can still leave plausible-looking partial
        # output and must not be published.
        if completed.returncode != 0 and (
            not known_format or not _is_teardown_abort(completed.returncode)
        ):
            raise RuntimeError(
                f"MuseScore failed to convert {source} to {destination} "
                f"(exit code {completed.returncode}); staged output was not published."
            )
        try:
            os.chmod(temporary_output, output_mode)
            os.replace(temporary_output, destination)
        except OSError as exc:
            raise RuntimeError(f"Unable to publish converted score {destination}: {exc}") from exc
    finally:
        # Best-effort cleanup must not mask the conversion or publication error.
        shutil.rmtree(staging_directory, ignore_errors=True)
    return destination
