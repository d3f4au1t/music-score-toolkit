import os
import zipfile
from pathlib import Path

import pytest

from music_score_toolkit import workflows
from music_score_toolkit.workflows import (
    ScoreExportError,
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


def test_validates_bundled_smartscore_musicxml():
    validate_score_file(FIXTURES / "sample.musicxml")


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
