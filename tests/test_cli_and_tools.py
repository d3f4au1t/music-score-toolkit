import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from music_score_toolkit.cli import main
from music_score_toolkit.mscz import TransposeReport
from music_score_toolkit.tools import (
    ExecutableNotFoundError,
    convert_score,
    find_executable,
    require_executable,
)
from music_score_toolkit.workflows import newest_score_file, wait_for_score_file

VALID_PDF = b"%PDF-1.7\nstartxref\n0\n%%EOF\n"
VALID_MUSICXML = b"""<?xml version="1.0"?>
<score-partwise>
  <part-list><score-part id="P1"><part-name>Music</part-name></score-part></part-list>
  <part id="P1"><measure number="1"/></part>
</score-partwise>
"""
VALID_MSCX = b'<museScore version="4.0"><Score/></museScore>'
MXL_CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="score.musicxml"/></rootfiles>
</container>
"""


def make_executable(path: Path) -> None:
    path.write_text("executable")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def write_valid_output(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        path.write_bytes(VALID_PDF)
    elif suffix == ".mscz":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("score.mscx", VALID_MSCX)
    elif suffix == ".mxl":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META-INF/container.xml", MXL_CONTAINER)
            archive.writestr("score.musicxml", VALID_MUSICXML)
    elif suffix in {".musicxml", ".xml"}:
        path.write_bytes(VALID_MUSICXML)
    elif suffix == ".mscx":
        path.write_bytes(VALID_MSCX)
    elif suffix == ".zip":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("score.txt", "score")
    else:
        path.write_text("output")


def test_cli_reports_missing_input(capsys, tmp_path: Path):
    result = main(
        [
            "transpose",
            str(tmp_path / "missing.mscz"),
            str(tmp_path / "out.mscz"),
            "--from-key",
            "C",
            "--to-key",
            "D",
        ]
    )
    assert result == 2
    assert "does not exist" in capsys.readouterr().err


def test_cli_can_explicitly_override_source_key_validation(monkeypatch, capsys):
    captured = {}

    def transpose(*args, **kwargs):
        captured.update(kwargs)
        return TransposeReport("Bb", "C", 2, 1, 1)

    monkeypatch.setattr("music_score_toolkit.cli.transpose_mscz", transpose)

    result = main(
        [
            "transpose",
            "input.mscz",
            "output.mscz",
            "--from-key",
            "Bb",
            "--to-key",
            "C",
            "--ignore-source-key",
        ]
    )

    assert result == 0
    assert captured["validate_source_key"] is False
    assert '"chord_symbols_changed": 0' in capsys.readouterr().out


def test_configured_executable_is_respected(monkeypatch, tmp_path: Path):
    executable = tmp_path / "tool"
    make_executable(executable)
    monkeypatch.setenv("TEST_TOOL_PATH", str(executable))
    assert (
        find_executable(
            label="Test tool",
            env_var="TEST_TOOL_PATH",
            commands=(),
            known_paths=(),
        )
        == executable
    )


def test_missing_configured_executable_is_actionable(monkeypatch):
    monkeypatch.setenv("TEST_TOOL_PATH", os.devnull + "-missing")
    with pytest.raises(ExecutableNotFoundError, match="TEST_TOOL_PATH"):
        find_executable(
            label="Test tool",
            env_var="TEST_TOOL_PATH",
            commands=(),
            known_paths=(),
        )


def test_nonexecutable_configured_executable_is_rejected(monkeypatch, tmp_path: Path):
    executable = tmp_path / "tool"
    executable.write_text("not executable")
    executable.chmod(0o600)
    monkeypatch.setenv("TEST_TOOL_PATH", str(executable))

    with pytest.raises(ExecutableNotFoundError, match="is not executable"):
        find_executable(
            label="Test tool",
            env_var="TEST_TOOL_PATH",
            commands=(),
            known_paths=(),
        )


def test_relative_explicit_executable_is_resolved_before_launch(monkeypatch, tmp_path: Path):
    executable = tmp_path / "tool"
    make_executable(executable)
    monkeypatch.chdir(tmp_path)

    assert require_executable("tool", label="Test tool") == executable.resolve()


def test_convert_score_publishes_output_atomically(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mscz"
    destination = tmp_path / "score.pdf"
    executable = tmp_path / "musescore"
    source.write_text("score")
    destination.write_text("previous output")
    make_executable(executable)

    def run_musescore(command, *, check):
        assert check is False
        temporary_output = Path(command[-1])
        assert temporary_output != destination
        assert temporary_output.suffix == destination.suffix
        write_valid_output(temporary_output)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("music_score_toolkit.tools.subprocess.run", run_musescore)

    assert convert_score(source, destination, musescore=executable) == destination
    assert destination.read_bytes() == VALID_PDF
    assert list(tmp_path.glob(".score.*.pdf")) == []


def test_convert_score_accepts_output_written_before_a_crash_on_exit(monkeypatch, tmp_path: Path):
    """MuseScore 4 can write a complete file and still abort while shutting down."""

    source = tmp_path / "source.mscz"
    destination = tmp_path / "score.pdf"
    executable = tmp_path / "musescore"
    source.write_text("score")
    destination.write_text("previous output")
    make_executable(executable)

    def crash_after_writing_output(command, *, check):
        assert check is False
        write_valid_output(Path(command[-1]))
        return subprocess.CompletedProcess(command, -6)

    monkeypatch.setattr("music_score_toolkit.tools.subprocess.run", crash_after_writing_output)

    assert convert_score(source, destination, musescore=executable) == destination
    assert destination.read_bytes() == VALID_PDF
    assert list(tmp_path.glob(".score.*.pdf")) == []


def test_convert_score_rejects_valid_looking_output_from_regular_failure(
    monkeypatch,
    tmp_path: Path,
):
    source = tmp_path / "source.mscz"
    destination = tmp_path / "score.pdf"
    executable = tmp_path / "musescore"
    source.write_text("score")
    destination.write_text("previous output")
    make_executable(executable)

    def fail_after_writing_output(command, *, check):
        assert check is False
        write_valid_output(Path(command[-1]))
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr("music_score_toolkit.tools.subprocess.run", fail_after_writing_output)

    with pytest.raises(RuntimeError, match="exit code 3.*not published"):
        convert_score(source, destination, musescore=executable)

    assert destination.read_text() == "previous output"


def test_convert_score_preserves_existing_destination_mode(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mscz"
    destination = tmp_path / "score.pdf"
    executable = tmp_path / "musescore"
    source.write_text("score")
    destination.write_text("previous output")
    destination.chmod(0o604)
    make_executable(executable)

    def succeed(command, *, check):
        write_valid_output(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("music_score_toolkit.tools.subprocess.run", succeed)

    convert_score(source, destination, musescore=executable)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o604


def test_convert_score_preserves_destination_and_cleans_partial_output(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mscz"
    destination = tmp_path / "score.pdf"
    executable = tmp_path / "musescore"
    source.write_text("score")
    destination.write_text("previous output")
    make_executable(executable)

    def fail_conversion(command, *, check):
        assert check is False
        Path(command[-1]).write_text("nonempty but not a PDF")
        return subprocess.CompletedProcess(command, 3)

    monkeypatch.setattr("music_score_toolkit.tools.subprocess.run", fail_conversion)

    with pytest.raises(RuntimeError, match=r"invalid \.pdf.*exit code 3"):
        convert_score(source, destination, musescore=executable)

    assert destination.read_text() == "previous output"
    assert list(tmp_path.glob(".score.*.pdf")) == []


def test_convert_score_rejects_missing_new_output(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.mscz"
    destination = tmp_path / "score.pdf"
    executable = tmp_path / "musescore"
    source.write_text("score")
    destination.write_text("previous output")
    make_executable(executable)

    monkeypatch.setattr(
        "music_score_toolkit.tools.subprocess.run",
        lambda command, *, check: subprocess.CompletedProcess(command, 0),
    )

    with pytest.raises(RuntimeError, match="without creating"):
        convert_score(source, destination, musescore=executable)

    assert destination.read_text() == "previous output"
    assert list(tmp_path.glob(".score.*.pdf")) == []


def test_convert_score_rejects_nonexecutable_explicit_path(tmp_path: Path):
    source = tmp_path / "source.musicxml"
    executable = tmp_path / "musescore"
    source.write_bytes(VALID_MUSICXML)
    executable.write_text("not executable")
    executable.chmod(0o600)

    with pytest.raises(ExecutableNotFoundError, match="is not executable"):
        convert_score(source, tmp_path / "score.pdf", musescore=executable)


@pytest.mark.parametrize(
    "suffix",
    [".pdf", ".mscz", ".mxl", ".musicxml", ".xml", ".mscx", ".zip"],
)
def test_convert_score_accepts_valid_known_format_after_teardown_crash(
    suffix: str, monkeypatch, tmp_path: Path
):
    source = tmp_path / "source.musicxml"
    destination = tmp_path / f"score{suffix}"
    executable = tmp_path / "musescore"
    source.write_bytes(VALID_MUSICXML)
    destination.write_text("previous output")
    make_executable(executable)

    def crash_after_valid_output(command, *, check):
        assert check is False
        write_valid_output(Path(command[-1]))
        return subprocess.CompletedProcess(command, -6)

    monkeypatch.setattr(
        "music_score_toolkit.tools.subprocess.run",
        crash_after_valid_output,
    )

    assert convert_score(source, destination, musescore=executable) == destination
    assert destination.read_text(errors="ignore") != "previous output"


@pytest.mark.parametrize(
    "suffix",
    [".pdf", ".mscz", ".mxl", ".musicxml", ".xml", ".mscx", ".zip"],
)
def test_convert_score_rejects_nonempty_junk_for_known_format(
    suffix: str, monkeypatch, tmp_path: Path
):
    source = tmp_path / "source.musicxml"
    destination = tmp_path / f"score{suffix}"
    executable = tmp_path / "musescore"
    source.write_bytes(VALID_MUSICXML)
    destination.write_text("previous output")
    make_executable(executable)

    def write_junk(command, *, check):
        assert check is False
        Path(command[-1]).write_bytes(b"nonempty junk")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("music_score_toolkit.tools.subprocess.run", write_junk)

    with pytest.raises(RuntimeError, match="produced an invalid"):
        convert_score(source, destination, musescore=executable)

    assert destination.read_text() == "previous output"
    assert not list(tmp_path.glob(f".score.*{suffix}"))


def test_convert_score_wraps_launch_oserror(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.musicxml"
    destination = tmp_path / "score.pdf"
    executable = tmp_path / "musescore"
    source.write_bytes(VALID_MUSICXML)
    destination.write_text("previous output")
    make_executable(executable)

    def deny_launch(command, *, check):
        raise PermissionError("launch denied")

    monkeypatch.setattr("music_score_toolkit.tools.subprocess.run", deny_launch)

    with pytest.raises(RuntimeError, match="Unable to run MuseScore.*launch denied"):
        convert_score(source, destination, musescore=executable)

    assert destination.read_text() == "previous output"
    assert not list(tmp_path.glob(".score.*.pdf"))


def test_cli_reports_unhandled_oserror_without_traceback(monkeypatch, capsys):
    def fail_conversion(*args, **kwargs):
        raise PermissionError("output denied")

    monkeypatch.setattr("music_score_toolkit.cli.convert_score", fail_conversion)

    result = main(["convert", "input.musicxml", "output.pdf"])

    assert result == 2
    assert "music-score: error: output denied" in capsys.readouterr().err


def test_newest_score_file_ignores_unrelated_files(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("ignore")
    first = tmp_path / "first.musicxml"
    second = tmp_path / "second.mxl"
    first.write_text("first")
    second.write_text("second")
    os.utime(first, (10, 10))
    os.utime(second, (20, 20))
    assert newest_score_file(tmp_path) == second


def test_wait_for_score_file_times_out(tmp_path: Path):
    with pytest.raises(TimeoutError):
        wait_for_score_file(tmp_path, timeout=0.01, poll_interval=0.001)
