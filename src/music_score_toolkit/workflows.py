"""High-level workflows retained from the original toolkit repositories."""

from __future__ import annotations

import os
import stat
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .tools import (
    _MAX_CONTAINER_XML_SIZE,
    _MAX_MUSICXML_SIZE,
    _MUSICXML_ROOTFILE_MEDIA_TYPE,
    _collapse_xml_token,
    _is_xml_ncname,
    _read_zip_member,
    _validate_mxl_package_members,
    _validated_zip_members,
    _ZipValidationError,
    convert_score,
    find_smartscore,
    require_executable,
)

SCORE_SUFFIXES = (".mxl", ".musicxml", ".xml")
MUSICXML_ROOTS = {"score-partwise", "score-timewise"}
MUSICXML_CONTAINER = "META-INF/container.xml"


class ScoreExportError(ValueError):
    """Raised when a SmartScore export is not a complete MusicXML score."""


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


def _fingerprint(item_stat: os.stat_result) -> _FileFingerprint:
    return _FileFingerprint(
        device=item_stat.st_dev,
        inode=item_stat.st_ino,
        size=item_stat.st_size,
        modified_ns=item_stat.st_mtime_ns,
        changed_ns=item_stat.st_ctime_ns,
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _musicxml_ids(
    elements: list[ET.Element],
    *,
    label: str,
    source: Path,
) -> list[str]:
    identifiers = [
        _collapse_xml_token(element.get("id", "")) for element in elements
    ]
    if any(not _is_xml_ncname(identifier) for identifier in identifiers):
        raise ScoreExportError(f"MusicXML {label} has a missing or invalid id: {source}")
    duplicates = [
        identifier
        for identifier, count in Counter(identifiers).items()
        if count > 1
    ]
    if duplicates:
        raise ScoreExportError(
            f"MusicXML {label} contains duplicate id {duplicates[0]!r}: {source}"
        )
    return identifiers


def _validate_musicxml_root(root: ET.Element, *, source: Path) -> None:
    root_name = _local_name(root.tag)
    if root_name not in MUSICXML_ROOTS:
        raise ScoreExportError(
            f"Expected a MusicXML score root in {source}, found <{root_name}>."
        )

    children = list(root)
    part_lists = [
        child for child in children if _local_name(child.tag) == "part-list"
    ]
    if len(part_lists) != 1:
        raise ScoreExportError(
            f"MusicXML score must contain one populated <part-list>: {source}"
        )
    part_list = part_lists[0]
    body_name = "part" if root_name == "score-partwise" else "measure"
    if any(
        _local_name(child.tag) == body_name
        for child in children[: children.index(part_list)]
    ):
        raise ScoreExportError(
            f"MusicXML <part-list> must precede all <{body_name}> elements: {source}"
        )
    score_parts = [
        child for child in part_list if _local_name(child.tag) == "score-part"
    ]
    if not score_parts:
        raise ScoreExportError(f"MusicXML score has no populated <part-list>: {source}")
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
        raise ScoreExportError(
            f"MusicXML <score-part> must contain one direct <part-name>: {source}"
        )
    declared_ids = _musicxml_ids(
        score_parts,
        label="<score-part>",
        source=source,
    )

    if root_name == "score-partwise":
        parts = [child for child in children if _local_name(child.tag) == "part"]
        part_ids = _musicxml_ids(parts, label="<part>", source=source)
        if set(part_ids) != set(declared_ids):
            raise ScoreExportError(
                "MusicXML <part> IDs do not match <score-part> declarations: "
                f"{source}"
            )
        measures_by_part = [
            [child for child in part if _local_name(child.tag) == "measure"]
            for part in parts
        ]
        has_music = bool(parts) and all(measures_by_part)
        measures = [measure for group in measures_by_part for measure in group]
    else:
        measures = [child for child in children if _local_name(child.tag) == "measure"]
        has_music = bool(measures)
        for measure in measures:
            parts = [child for child in measure if _local_name(child.tag) == "part"]
            part_ids = _musicxml_ids(parts, label="<part>", source=source)
            if set(part_ids) != set(declared_ids):
                raise ScoreExportError(
                    "MusicXML measure <part> IDs do not match <score-part> "
                    f"declarations: {source}"
                )
    if any("number" not in measure.attrib for measure in measures):
        raise ScoreExportError(
            f"MusicXML <measure> is missing its required number: {source}"
        )
    if not has_music:
        raise ScoreExportError(f"MusicXML score has an incomplete part/measure structure: {source}")


def _parse_musicxml(payload: bytes, *, source: Path) -> None:
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, LookupError) as exc:
        raise ScoreExportError(f"Incomplete or invalid MusicXML in {source}: {exc}") from exc
    _validate_musicxml_root(root, source=source)


def _validate_mxl(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = _validated_zip_members(archive)
            _validate_mxl_package_members(archive, members)
            container_info = members.get(MUSICXML_CONTAINER)
            if container_info is None or container_info.is_dir():
                raise ScoreExportError(
                    f"Compressed MusicXML must contain one {MUSICXML_CONTAINER}: {path}"
                )
            try:
                container = ET.fromstring(
                    _read_zip_member(
                        archive,
                        container_info,
                        maximum_size=_MAX_CONTAINER_XML_SIZE,
                    )
                )
            except (ET.ParseError, LookupError) as exc:
                raise ScoreExportError(f"Invalid MusicXML container in {path}: {exc}") from exc
            if _local_name(container.tag) != "container":
                raise ScoreExportError(f"Invalid MusicXML container root in {path}.")
            if container.attrib or len(container) != 1:
                raise ScoreExportError(
                    f"MusicXML container must contain only one direct <rootfiles>: {path}"
                )

            rootfiles_containers = [
                child for child in container if _local_name(child.tag) == "rootfiles"
            ]
            if len(rootfiles_containers) != 1:
                raise ScoreExportError(
                    f"MusicXML container must contain one direct <rootfiles>: {path}"
                )
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
                raise ScoreExportError(
                    "MusicXML container must contain valid direct <rootfile> "
                    f"entries only: {path}"
                )
            references: set[str] = set()
            for index, rootfile in enumerate(rootfiles):
                if (
                    list(rootfile)
                    or (rootfile.text or "").strip()
                    or set(rootfile.attrib) - {"full-path", "media-type"}
                ):
                    raise ScoreExportError(
                        f"MusicXML container has an invalid <rootfile>: {path}"
                    )
                root_path = _collapse_xml_token(rootfile.get("full-path", ""))
                media_type = _collapse_xml_token(rootfile.get("media-type", ""))
                if index == 0 and media_type not in {
                    "",
                    _MUSICXML_ROOTFILE_MEDIA_TYPE,
                }:
                    raise ScoreExportError(
                        "MusicXML first rootfile has a non-MusicXML media-type: "
                        f"{path}"
                    )
                member_path = PurePosixPath(root_path)
                try:
                    member_info = members[root_path]
                except KeyError:
                    member_info = None
                if (
                    not root_path
                    or member_path.is_absolute()
                    or ".." in member_path.parts
                    or root_path in references
                    or member_info is None
                    or member_info.is_dir()
                ):
                    raise ScoreExportError(
                        "MusicXML container references an invalid rootfile "
                        f"{root_path!r}: {path}"
                    )
                references.add(root_path)
            first_root_path = _collapse_xml_token(
                rootfiles[0].get("full-path", "")
            )
            _parse_musicxml(
                _read_zip_member(
                    archive,
                    members[first_root_path],
                    maximum_size=_MAX_MUSICXML_SIZE,
                ),
                source=path,
            )
    except ScoreExportError:
        raise
    except (
        EOFError,
        KeyError,
        NotImplementedError,
        OSError,
        RuntimeError,
        UnicodeError,
        _ZipValidationError,
        zipfile.BadZipFile,
    ) as exc:
        raise ScoreExportError(f"Incomplete or invalid compressed MusicXML {path}: {exc}") from exc


def validate_score_file(
    path: str | Path,
    *,
    expected_fingerprint: _FileFingerprint | None = None,
) -> None:
    """Validate one complete uncompressed or compressed MusicXML score."""

    score = Path(path).expanduser().resolve()
    if not score.is_file():
        raise ScoreExportError(f"MusicXML export does not exist: {score}")

    try:
        before = score.stat()
        if expected_fingerprint is not None and _fingerprint(before) != expected_fingerprint:
            raise ScoreExportError(
                f"MusicXML export changed before it could be validated: {score}"
            )
        if score.suffix.lower() == ".mxl":
            _validate_mxl(score)
        else:
            _parse_musicxml(score.read_bytes(), source=score)
        after = score.stat()
    except OSError as exc:
        raise ScoreExportError(f"Unable to read MusicXML export {score}: {exc}") from exc

    if _fingerprint(before) != _fingerprint(after):
        raise ScoreExportError(f"MusicXML export changed while it was being validated: {score}")


def _score_file_candidates(
    directory: Path,
    *,
    newer_than: float,
) -> list[tuple[Path, _FileFingerprint]]:
    candidates: list[tuple[int, str, Path, _FileFingerprint]] = []
    for item in directory.rglob("*"):
        if item.suffix.lower() not in SCORE_SUFFIXES:
            continue
        try:
            item_stat = item.stat()
        except OSError:
            continue
        if stat.S_ISREG(item_stat.st_mode) and item_stat.st_mtime >= newer_than:
            candidates.append(
                (item_stat.st_mtime_ns, str(item), item, _fingerprint(item_stat))
            )
    return [
        (item, fingerprint)
        for _, _, item, fingerprint in sorted(candidates, reverse=True)
    ]


def _score_file_snapshot(directory: Path) -> dict[Path, _FileFingerprint]:
    return dict(_score_file_candidates(directory, newer_than=0))


def newest_score_file(directory: Path, *, newer_than: float = 0) -> Path | None:
    candidates = _score_file_candidates(directory, newer_than=newer_than)
    return candidates[0][0] if candidates else None


def wait_for_score_file(
    directory: str | Path,
    *,
    newer_than: float = 0,
    timeout: float | None = None,
    poll_interval: float = 2,
    stable_polls: int = 2,
    baseline: Mapping[Path, _FileFingerprint] | None = None,
) -> Path:
    """Wait for a stable, structurally valid MusicXML-family export."""

    output_directory = Path(directory).expanduser().resolve()
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero.")
    if stable_polls < 2:
        raise ValueError("stable_polls must be at least 2.")
    if timeout is not None and timeout < 0:
        raise ValueError("timeout cannot be negative.")

    started = time.monotonic()
    observations: dict[Path, tuple[_FileFingerprint, int]] = {}
    validation_errors: dict[Path, tuple[_FileFingerprint, str]] = {}
    while True:
        candidates = [
            (path, fingerprint)
            for path, fingerprint in _score_file_candidates(
                output_directory,
                newer_than=newer_than,
            )
            if baseline is None or baseline.get(path) != fingerprint
        ]
        current_candidates = {path for path, _ in candidates}
        observations = {
            path: observation
            for path, observation in observations.items()
            if path in current_candidates
        }
        validation_errors = {
            path: error for path, error in validation_errors.items() if path in current_candidates
        }

        for match, signature in candidates:
            previous = observations.get(match)
            unchanged_polls = previous[1] + 1 if previous and previous[0] == signature else 1
            observations[match] = (signature, unchanged_polls)
            if unchanged_polls < stable_polls:
                continue

            try:
                validate_score_file(match, expected_fingerprint=signature)
            except ScoreExportError as exc:
                validation_errors[match] = (signature, str(exc))
                continue
            return match

        elapsed = time.monotonic() - started
        if timeout is not None and elapsed >= timeout:
            detail = ""
            if validation_errors:
                newest_invalid = next(
                    (path for path, _ in candidates if path in validation_errors),
                    next(iter(validation_errors)),
                )
                detail = f" Last candidate was invalid: {validation_errors[newest_invalid][1]}"
            raise TimeoutError(
                f"No complete MusicXML file appeared in {output_directory} within {timeout}s."
                f"{detail}"
            )
        remaining = timeout - elapsed if timeout is not None else poll_interval
        time.sleep(min(poll_interval, remaining))


def recognize_pdf_with_smartscore(
    pdf_path: str | Path,
    output_directory: str | Path,
    *,
    smartscore: str | Path | None = None,
    musescore: str | Path | None = None,
    timeout: float | None = None,
) -> Path:
    """Launch SmartScore, wait for manual MusicXML export, then create MSCZ."""

    source = Path(pdf_path).expanduser().resolve()
    destination_directory = Path(output_directory).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input PDF does not exist: {source}")
    try:
        destination_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Unable to create output directory {destination_directory}: {exc}"
        ) from exc
    executable = (
        require_executable(smartscore, label="SmartScore")
        if smartscore
        else find_smartscore()
    )

    baseline = _score_file_snapshot(destination_directory)
    try:
        subprocess.Popen([str(executable), str(source)])
    except OSError as exc:
        raise RuntimeError(
            f"Unable to launch SmartScore executable {executable}: {exc}"
        ) from exc
    exported = wait_for_score_file(
        destination_directory,
        timeout=timeout,
        baseline=baseline,
    )
    return convert_score(
        exported,
        destination_directory / f"{exported.stem}.mscz",
        musescore=musescore,
    )
