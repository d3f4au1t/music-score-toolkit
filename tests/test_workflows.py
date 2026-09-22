import os
import stat
import zipfile
from pathlib import Path

import pytest

from music_score_toolkit import workflows
from music_score_toolkit.tools import ExecutableNotFoundError
from music_score_toolkit.workflows import (
    ScoreExportError,
    recognize_pdf_with_smartscore,
    validate_score_file,
    wait_for_score_file,
)

FIXTURES = Path(__file__).parent / "fixtures"

PARTWISE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Music</part-name></score-part></part-list>
  <part id="P1"><measure number="1"><note><rest/><duration>1</duration></note></measure></part>
</score-partwise>
"""

TIMEWISE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<score-timewise version="4.0">
  <part-list><score-part id="P1"><part-name>Music</part-name></score-part></part-list>
  <measure number="1"><part id="P1"><note><rest/><duration>1</duration></note></part></measure>
</score-timewise>
"""

REORDERED_PARTWISE_XML = b"""<score-partwise>
  <part-list>
    <score-part id="P1"><part-name>One</part-name></score-part>
    <score-part id="P2"><part-name>Two</part-name></score-part>
  </part-list>
  <part id="P2"><measure number="1"/></part>
  <part id="P1"><measure number="1"/></part>
</score-partwise>
"""

CONTAINER_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="score.musicxml" media-type="application/vnd.recordare.musicxml+xml"/>
  </rootfiles>
</container>
"""


def _write_mxl(
    path: Path,
    *,
    container: bytes = CONTAINER_XML,
    score: bytes = PARTWISE_XML,
    include_container: bool = True,
) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        if include_container:
            archive.writestr("META-INF/container.xml", container)
        archive.writestr("score.musicxml", score)


@pytest.mark.parametrize("payload", [PARTWISE_XML, TIMEWISE_XML])
def test_validates_partwise_and_timewise_musicxml(tmp_path: Path, payload: bytes):
    score = tmp_path / "score.musicxml"
    score.write_bytes(payload)

    validate_score_file(score)


def test_validates_musicxml_parts_serialized_in_non_display_order(tmp_path: Path):
    score = tmp_path / "score.musicxml"
    score.write_bytes(REORDERED_PARTWISE_XML)

    validate_score_file(score)


def test_validates_bundled_smartscore_musicxml():
    validate_score_file(FIXTURES / "sample.musicxml")


@pytest.mark.parametrize(
    "payload",
    [
        b"""<score-partwise>
        <part-list><score-part id="P1"><part-name>One</part-name></score-part></part-list>
        <part id="P2"><measure number="1"/></part>
        </score-partwise>""",
        b"""<score-partwise>
        <part-list><score-part id="P1"><part-name>One</part-name></score-part></part-list>
        <part id="P1"><measure number="1"/></part>
        <part id="P2"><measure number="1"/></part>
        </score-partwise>""",
        b"""<score-partwise>
        <part-list>
        <score-part id="P1"><part-name>One</part-name></score-part>
        <score-part id="P1"><part-name>Two</part-name></score-part>
        </part-list>
        <part id="P1"><measure number="1"/></part>
        </score-partwise>""",
        b"""<score-partwise>
        <part-list><score-part><part-name>One</part-name></score-part></part-list>
        <part><measure number="1"/></part>
        </score-partwise>""",
        b"""<score-timewise>
        <part-list><score-part id="P1"><part-name>One</part-name></score-part></part-list>
        <measure number="1"><part id="P2"/></measure>
        </score-timewise>""",
    ],
)
def test_rejects_mismatched_or_invalid_musicxml_part_ids(
    tmp_path: Path,
    payload: bytes,
):
    score = tmp_path / "score.musicxml"
    score.write_bytes(payload)

    with pytest.raises(ScoreExportError, match="id|IDs"):
        validate_score_file(score)


def test_musicxml_ids_use_xml_schema_whitespace_and_ncname_rules(tmp_path: Path):
    score = tmp_path / "score.musicxml"
    score.write_bytes(
        """<score-partwise>
        <part-list><score-part id="  Pärt\t">
        <part-name>Music</part-name></score-part></part-list>
        <part id="Pärt"><measure number="1"/></part>
        </score-partwise>""".encode()
    )

    validate_score_file(score)

    score.write_bytes(
        b"""<score-partwise>
        <part-list><score-part id="1 bad"><part-name>Music</part-name></score-part></part-list>
        <part id="1 bad"><measure number="1"/></part>
        </score-partwise>"""
    )
    with pytest.raises(ScoreExportError, match="invalid id"):
        validate_score_file(score)


def test_rejects_multiple_musicxml_part_lists(tmp_path: Path):
    score = tmp_path / "score.musicxml"
    score.write_bytes(
        b"""<score-partwise>
        <part-list><score-part id="P1"/></part-list>
        <part-list><score-part id="P2"/></part-list>
        <part id="P1"><measure number="1"/></part>
        </score-partwise>"""
    )

    with pytest.raises(ScoreExportError, match="one populated <part-list>"):
        validate_score_file(score)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            b"""<score-partwise>
            <part-list><score-part id="P1"/></part-list>
            <part id="P1"><measure number="1"/></part>
            </score-partwise>""",
            "part-name",
        ),
        (
            b"""<score-partwise>
            <part-list><score-part id="P1"><part-name>One</part-name></score-part></part-list>
            <part id="P1"><measure/></part>
            </score-partwise>""",
            "required number",
        ),
        (
            b"""<score-partwise>
            <part id="P1"><measure number="1"/></part>
            <part-list><score-part id="P1"><part-name>One</part-name></score-part></part-list>
            </score-partwise>""",
            "must precede",
        ),
    ],
)
def test_rejects_missing_or_misordered_required_musicxml_structure(
    tmp_path: Path,
    payload: bytes,
    message: str,
):
    score = tmp_path / "score.musicxml"
    score.write_bytes(payload)

    with pytest.raises(ScoreExportError, match=message):
        validate_score_file(score)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"<score-partwise>",
        b"<settings><option>not music</option></settings>",
        b"<score-partwise><part-list/></score-partwise>",
    ],
)
def test_rejects_incomplete_or_non_musicxml(tmp_path: Path, payload: bytes):
    score = tmp_path / "score.xml"
    score.write_bytes(payload)

    with pytest.raises(ScoreExportError):
        validate_score_file(score)


def test_rejects_unknown_xml_encoding_as_an_export_error(tmp_path: Path):
    score = tmp_path / "score.musicxml"
    score.write_bytes(PARTWISE_XML.replace(b'encoding="UTF-8"', b'encoding="unknown"'))

    with pytest.raises(ScoreExportError):
        validate_score_file(score)


def test_validates_mxl_without_optional_legacy_mimetype(tmp_path: Path):
    score = tmp_path / "score.mxl"
    _write_mxl(score)

    validate_score_file(score)


def test_validates_mxl_with_alternate_rootfile_renditions(tmp_path: Path):
    score = tmp_path / "score.mxl"
    container = b"""<container><rootfiles>
      <rootfile full-path="score.musicxml"/>
      <rootfile full-path="preview.pdf" media-type="application/pdf"/>
    </rootfiles></container>"""
    with zipfile.ZipFile(score, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("score.musicxml", PARTWISE_XML)
        archive.writestr("preview.pdf", b"%PDF-1.7\n%%EOF\n")

    validate_score_file(score)


def test_validates_mxl_with_standard_mimetype_and_tokenized_path(tmp_path: Path):
    score = tmp_path / "score.mxl"
    container = b"""<container><rootfiles>
    <rootfile full-path="  score.musicxml\t"
      media-type=" application/vnd.recordare.musicxml+xml "/>
    </rootfiles></container>"""
    with zipfile.ZipFile(score, "w") as archive:
        archive.writestr(
            "mimetype",
            b"application/vnd.recordare.musicxml",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("score.musicxml", PARTWISE_XML)

    validate_score_file(score)


def test_rejects_mxl_with_high_ratio_extra_member(tmp_path: Path):
    score = tmp_path / "score.mxl"
    with zipfile.ZipFile(score, "w") as archive:
        archive.writestr("META-INF/container.xml", CONTAINER_XML)
        archive.writestr("score.musicxml", PARTWISE_XML)
        archive.writestr(
            "oversized-extra.bin",
            b"\0" * (2 * 1024 * 1024),
            compress_type=zipfile.ZIP_DEFLATED,
        )

    with pytest.raises(ScoreExportError, match="suspicious compression ratio"):
        validate_score_file(score)


@pytest.mark.parametrize(
    "member_name",
    ["../extra.bin", "/extra.bin", "C:/extra.bin", "dir\\extra.bin", "dir//extra.bin"],
)
def test_rejects_mxl_with_unsafe_member_paths(
    tmp_path: Path,
    member_name: str,
):
    score = tmp_path / "score.mxl"
    with zipfile.ZipFile(score, "w") as archive:
        archive.writestr("META-INF/container.xml", CONTAINER_XML)
        archive.writestr("score.musicxml", PARTWISE_XML)
        archive.writestr(member_name, b"extra")

    with pytest.raises(ScoreExportError, match="unsafe member path"):
        validate_score_file(score)


@pytest.mark.parametrize(
    ("container", "message"),
    [
        (
            b"""<container><rootfiles><rootfile full-path="score.musicxml"
            media-type="application/pdf"/></rootfiles></container>""",
            "non-MusicXML media-type",
        ),
        (
            b"""<container><rootfiles><rootfile full-path="score.musicxml">
            unexpected</rootfile></rootfiles></container>""",
            "invalid <rootfile>",
        ),
        (
            b"""<container><rootfiles><rootfile full-path="score.musicxml"/></rootfiles>
            <unexpected/></container>""",
            "only one direct <rootfiles>",
        ),
    ],
)
def test_rejects_nonconforming_mxl_container_structure(
    tmp_path: Path,
    container: bytes,
    message: str,
):
    score = tmp_path / "score.mxl"
    _write_mxl(score, container=container)

    with pytest.raises(ScoreExportError, match=message):
        validate_score_file(score)


@pytest.mark.parametrize(
    ("first", "content", "compression", "message"),
    [
        (False, b"application/vnd.recordare.musicxml", zipfile.ZIP_STORED, "first"),
        (True, b"wrong/type", zipfile.ZIP_STORED, "invalid content"),
        (
            True,
            b"application/vnd.recordare.musicxml",
            zipfile.ZIP_DEFLATED,
            "without compression",
        ),
    ],
)
def test_rejects_invalid_mxl_mimetype(
    tmp_path: Path,
    first: bool,
    content: bytes,
    compression: int,
    message: str,
):
    score = tmp_path / "score.mxl"
    with zipfile.ZipFile(score, "w") as archive:
        if not first:
            archive.writestr("score.musicxml", PARTWISE_XML)
        archive.writestr("mimetype", content, compress_type=compression)
        archive.writestr("META-INF/container.xml", CONTAINER_XML)
        if first:
            archive.writestr("score.musicxml", PARTWISE_XML)

    with pytest.raises(ScoreExportError, match=message):
        validate_score_file(score)


@pytest.mark.parametrize(
    "container",
    [
        b"""<container><rootfiles>
        <rootfile full-path="score.musicxml"/>
        <rootfile full-path="missing.pdf"/>
        </rootfiles></container>""",
        b"""<container><rootfiles>
        <rootfile full-path="score.musicxml"/>
        <rootfile full-path="score.musicxml"/>
        </rootfiles></container>""",
    ],
)
def test_rejects_invalid_alternate_mxl_rootfiles(
    tmp_path: Path,
    container: bytes,
):
    score = tmp_path / "score.mxl"
    _write_mxl(score, container=container)

    with pytest.raises(ScoreExportError, match="invalid rootfile"):
        validate_score_file(score)


@pytest.mark.parametrize(
    ("container", "score_payload", "include_container"),
    [
        (CONTAINER_XML, PARTWISE_XML, False),
        (
            CONTAINER_XML.replace(b"score.musicxml", b"missing.musicxml"),
            PARTWISE_XML,
            True,
        ),
        (CONTAINER_XML, b"<score-partwise>", True),
        (
            b"""<container><junk><rootfile full-path="score.musicxml"/></junk>
            <rootfiles><rootfile full-path="missing.musicxml"/></rootfiles></container>""",
            PARTWISE_XML,
            True,
        ),
    ],
)
def test_rejects_invalid_mxl_containers(
    tmp_path: Path,
    container: bytes,
    score_payload: bytes,
    include_container: bool,
):
    score = tmp_path / "score.mxl"
    _write_mxl(
        score,
        container=container,
        score=score_payload,
        include_container=include_container,
    )

    with pytest.raises(ScoreExportError):
        validate_score_file(score)


def test_rejects_truncated_non_zip_mxl(tmp_path: Path):
    score = tmp_path / "score.mxl"
    score.write_bytes(b"PK\x03\x04partial archive")

    with pytest.raises(ScoreExportError):
        validate_score_file(score)


def test_validation_rejects_a_file_changed_during_read(monkeypatch, tmp_path: Path):
    score = tmp_path / "score.musicxml"
    score.write_bytes(PARTWISE_XML)
    original_read_bytes = Path.read_bytes

    def read_then_change(path: Path) -> bytes:
        payload = original_read_bytes(path)
        path.write_bytes(payload + b"\n")
        return payload

    monkeypatch.setattr(Path, "read_bytes", read_then_change)

    with pytest.raises(ScoreExportError, match="changed while"):
        validate_score_file(score)


def test_waits_for_partial_export_to_finish(monkeypatch, tmp_path: Path):
    score = tmp_path / "score.musicxml"
    score.write_bytes(PARTWISE_XML[:-24])
    sleep_calls = 0

    def finish_after_validation_attempt(_interval: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            score.write_bytes(PARTWISE_XML)

    monkeypatch.setattr(workflows.time, "sleep", finish_after_validation_attempt)

    match = wait_for_score_file(tmp_path, timeout=1, poll_interval=0.01)

    assert match == score
    assert sleep_calls == 3


def test_newer_invalid_xml_does_not_mask_valid_export(tmp_path: Path):
    valid = tmp_path / "score.musicxml"
    invalid = tmp_path / "settings.xml"
    valid.write_bytes(PARTWISE_XML)
    invalid.write_text("<settings/>")
    os.utime(valid, ns=(1_000_000_000, 1_000_000_000))
    os.utime(invalid, ns=(2_000_000_000, 2_000_000_000))

    assert wait_for_score_file(tmp_path, timeout=1, poll_interval=0.001) == valid


def test_transient_validation_error_is_retried(monkeypatch, tmp_path: Path):
    score = tmp_path / "score.musicxml"
    score.write_bytes(PARTWISE_XML)
    original_validate = workflows.validate_score_file
    attempts = 0

    def fail_once(path, *, expected_fingerprint=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ScoreExportError("exporter still has the file locked")
        return original_validate(path, expected_fingerprint=expected_fingerprint)

    monkeypatch.setattr(workflows, "validate_score_file", fail_once)

    assert wait_for_score_file(tmp_path, timeout=1, poll_interval=0.001) == score
    assert attempts == 2


def test_preexisting_export_must_change_after_baseline(monkeypatch, tmp_path: Path):
    score = tmp_path / "score.musicxml"
    score.write_bytes(PARTWISE_XML)
    baseline = workflows._score_file_snapshot(tmp_path)
    sleep_calls = 0

    def overwrite_existing_export(_interval: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            score.write_bytes(PARTWISE_XML + b"\n")

    monkeypatch.setattr(workflows.time, "sleep", overwrite_existing_export)

    match = wait_for_score_file(
        tmp_path,
        timeout=1,
        poll_interval=0.01,
        baseline=baseline,
    )

    assert match == score
    assert sleep_calls == 3


def test_timeout_explains_that_candidate_is_invalid(tmp_path: Path):
    (tmp_path / "score.musicxml").write_text("<settings/>")

    with pytest.raises(TimeoutError, match="Last candidate was invalid"):
        wait_for_score_file(tmp_path, timeout=0.01, poll_interval=0.001)


def test_recognize_rejects_nonexecutable_explicit_smartscore(tmp_path: Path):
    source = tmp_path / "scan.pdf"
    executable = tmp_path / "SmartScore"
    source.write_bytes(b"%PDF")
    executable.write_text("not executable")
    executable.chmod(0o600)

    with pytest.raises(ExecutableNotFoundError, match="is not executable"):
        recognize_pdf_with_smartscore(
            source,
            tmp_path / "output",
            smartscore=executable,
        )


def test_recognize_wraps_smartscore_launch_error(monkeypatch, tmp_path: Path):
    source = tmp_path / "scan.pdf"
    executable = tmp_path / "SmartScore"
    source.write_bytes(b"%PDF")
    executable.write_text("executable")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    def deny_launch(*args, **kwargs):
        raise PermissionError("launch denied")

    monkeypatch.setattr(workflows.subprocess, "Popen", deny_launch)

    with pytest.raises(RuntimeError, match="Unable to launch SmartScore.*launch denied"):
        recognize_pdf_with_smartscore(
            source,
            tmp_path / "output",
            smartscore=executable,
        )
