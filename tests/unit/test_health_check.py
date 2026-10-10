"""Unit tests for scripts/health_check.py."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
import typing
from pathlib import Path

import pytest

from legal_corpus_ingester import cli
from legal_corpus_ingester.pipeline.state import CheckpointState, CheckpointStore

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import health_check  # noqa: E402
from health_check import (  # noqa: E402
    EXIT_CONFIG_MISSING,
    EXIT_SOURCE_PROBLEM,
    YamlDirError,
    _status_label,
    build_report,
    health_verdict,
    main,
)


class TestStatusLabel:
    def test_fresh(self):
        assert _status_label(10.0, stale_days=8) == "fresh"

    def test_stale(self):
        # 9 days = 216 hours > 8 * 24
        assert _status_label(216.0, stale_days=8) == "stale"

    def test_never_run(self):
        assert _status_label(None, stale_days=8) == "never-run"

    def test_exactly_stale(self):
        # lag == stale_days exactly → stale
        assert _status_label(192.0, stale_days=8) == "stale"


class TestBuildReport:
    @pytest.mark.parametrize("make_dir", [True, False], ids=["empty-dir", "missing-dir"])
    def test_no_sources_is_not_success(self, tmp_path, make_dir):
        # Zero sources, or no dir at all, must never be a clean report (terms-analysis#173, grumpy 4).
        config_dir = tmp_path / "config"
        if make_dir:
            config_dir.mkdir()
        # Grumpy r2-1: one contract only. build_report raises; it never returns a report.
        with pytest.raises(YamlDirError) as raised:
            build_report(
                config_dir=config_dir,
                state_dir=tmp_path / "state",
                out_dir=tmp_path / "out",
                stale_days=8,
                today="2026-07-04",
                refresh_wired=True,
            )
        if make_dir:
            assert str(raised.value) == health_check._empty_dir_problem(config_dir, "config dir")
        else:
            assert "does not exist" in str(raised.value)

    def test_all_fresh(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Write a checkpoint so the source is "fresh" (recent mtime)
        cp = state_dir / "eurlex.checkpoint.json"
        cp.write_text(json.dumps({"stage": "publish"}))
        # Patch _checkpoint_info to control lag — instead use real file with recent mtime
        report, verdict = build_report(
            config_dir=config_dir,
            state_dir=state_dir,
            out_dir=tmp_path / "out",
            stale_days=8,
            today="2026-07-04",
            refresh_wired=True,
        )
        # Checkpoint was just written — lag is seconds, far below 8 days
        assert "fresh" in report
        assert verdict == 0

    def test_one_never_run(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No checkpoint file → never-run
        report, verdict = build_report(
            config_dir=config_dir,
            state_dir=state_dir,
            out_dir=tmp_path / "out",
            stale_days=8,
            today="2026-07-04",
            refresh_wired=True,
        )
        assert "never-run" in report
        assert verdict == EXIT_SOURCE_PROBLEM


class TestMainExitCodes:
    """main() must not report success when it could not check anything (terms-analysis#90)."""

    def test_missing_config_dir_exits_config_missing(self, tmp_path, capsys):
        missing = tmp_path / "no-such-config"
        rc = main(["--config-dir", str(missing), "--state-dir", str(tmp_path / "state"),
                   "--out-dir", str(tmp_path / "out")])
        assert rc == EXIT_CONFIG_MISSING
        assert EXIT_CONFIG_MISSING not in (0, 1)
        captured = capsys.readouterr()
        assert "does not exist" in captured.err
        assert captured.out == ""

    def test_never_run_source_exits_one_once_refresh_is_wired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(health_check, "REFRESH_WIRED", True)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        rc = main(["--config-dir", str(config_dir), "--state-dir", str(tmp_path / "state"),
                   "--out-dir", str(tmp_path / "out")])
        assert rc == EXIT_SOURCE_PROBLEM == 1

    def test_never_run_while_unwired_exits_not_wired_and_says_why(self, tmp_path, capsys):
        # Today's real state: refresh is unwired, so no checkpoint can exist. That is the
        # known state (cli.EXIT_NOT_WIRED), not success and not a broken pipeline.
        assert health_check.REFRESH_WIRED is cli.REFRESH_WIRED is False
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        rc = main(["--config-dir", str(config_dir), "--state-dir", str(tmp_path / "state"),
                   "--out-dir", str(tmp_path / "out")])
        assert rc == cli.EXIT_NOT_WIRED
        out = capsys.readouterr().out
        assert "| eurlex | never | - | - | never-run |" in out
        assert "Refresh is not wired yet" in out
        written = next((tmp_path / "out" / "health").glob("*.md")).read_text()
        assert written == out

    def test_stale_source_while_unwired_still_exits_one(self, tmp_path):
        # Unwired only excuses never-run. A checkpoint that exists and is old is real staleness.
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        for name in ("eurlex", "cfpb"):
            (config_dir / f"{name}.yaml").write_text(f"name: {name}\n")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        cp = state_dir / "eurlex.checkpoint.json"
        cp.write_text(json.dumps({"stage": "done"}))
        old = time.time() - 9 * 86400
        os.utime(cp, (old, old))
        rc = main(["--config-dir", str(config_dir), "--state-dir", str(state_dir),
                   "--out-dir", str(tmp_path / "out")])
        assert rc == EXIT_SOURCE_PROBLEM


class TestHealthVerdict:
    """Truth table for the one function that decides the health exit code."""

    @pytest.mark.parametrize(
        ("statuses", "wired", "expected"),
        [
            ([], False, 0),
            ([], True, 0),
            (["fresh"], False, 0),
            (["fresh", "fresh"], True, 0),
            (["never-run"], False, cli.EXIT_NOT_WIRED),
            (["fresh", "never-run"], False, cli.EXIT_NOT_WIRED),
            (["never-run"], True, EXIT_SOURCE_PROBLEM),
            (["fresh", "never-run"], True, EXIT_SOURCE_PROBLEM),
            (["stale"], False, EXIT_SOURCE_PROBLEM),
            (["stale"], True, EXIT_SOURCE_PROBLEM),
            (["never-run", "stale"], False, EXIT_SOURCE_PROBLEM),
            # Fail closed: a status nobody planned for is a problem, never excused.
            (["unknown"], False, EXIT_SOURCE_PROBLEM),
            (["never-run", ""], False, EXIT_SOURCE_PROBLEM),
        ],
    )
    def test_truth_table(self, statuses, wired, expected):
        assert health_verdict(statuses, refresh_wired=wired) == expected

    def test_codes_are_distinct(self):
        # #17: an invalid checkpoint has its own documented code. It must not be 1, which is
        # also what an uncaught traceback exits with, so a crash and a handled bad checkpoint
        # stay apart. It must not be EXIT_NOT_WIRED either, which health.yml downgrades to a warning.
        codes = (0, EXIT_SOURCE_PROBLEM, EXIT_CONFIG_MISSING, cli.EXIT_NOT_WIRED, _checkpoint_invalid_code())
        assert len(set(codes)) == len(codes)
        # A plain exit status: not a shell "cannot execute"/"not found" (126/127) or signal (128+) code.
        assert all(0 <= code < 126 for code in codes)


# ---------------------------------------------------------------------------
# #17: a checkpoint that is not a CheckpointState object.
# The writer is pipeline.state.CheckpointStore.save; the schema is CheckpointState. The
# field names and types below are read from that dataclass, never restated (F10, F13).
# Contract pinned here:
#   * a checkpoint file that exists but is not a JSON object holding a str "stage", whose
#     every other CheckpointState field present has its declared type, and whose
#     source_name (if present) names the file's own source, is INVALID;
#   * an invalid checkpoint exits health_check.EXIT_CHECKPOINT_INVALID, whatever
#     REFRESH_WIRED says and whatever the other sources look like: never 0, never
#     EXIT_NOT_WIRED (health.yml's warning-only code), never a traceback;
#   * the output names the checkpoint file and never echoes its bytes raw (F2, F8);
#   * a MISSING checkpoint keeps today's never-run meaning (EXIT_NOT_WIRED / 1).
# Today: non-object JSON crashes (AttributeError, exit 1); unparseable JSON, wrong-typed
# fields, a directory at the path and a foreign source_name all exit 0 as "fresh";
# a dangling symlink reads as never-run.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
_SOURCE = "eurlex"
_CHECKPOINT_NAME = f"{_SOURCE}.checkpoint.json"
# Hostile text planted in bad checkpoints. None of it may reach stdout, stderr or the report.
_CANARY = "CANARY-17"
_RAW_FORBIDDEN = (_CANARY, "\x1b", "‮", " ", "\x00")
_HOSTILE_TEXT = f"{_CANARY}\x1b[2J‮ "
# Status labels that would wrongly describe a source whose checkpoint is unreadable.
_TRUSTED_STATUSES = ("fresh", "stale", "never-run")
# Bound for the child process: a FIFO or other blocking path must not hang the job.
_CHILD_TIMEOUT_S = 30.0

_CP_FIELDS = dataclasses.fields(CheckpointState)
_CP_TYPES = typing.get_type_hints(CheckpointState)
# health_check reads "stage"; it is the one field a checkpoint must carry. The other
# fields may be absent (existing fixtures write {"stage": ...} only) but never wrong-typed.
_REQUIRED_FIELD = "stage"
# One JSON value of every type that is not str, plus hostile text hidden inside containers.
_WRONG_TYPED_VALUES: dict[str, object] = {
    "null": None,
    "int": 7,
    "negative-int": -1,
    "float": 1.5,
    "bool": True,
    "list": [_HOSTILE_TEXT],
    "empty-list": [],
    "object": {"stage": _HOSTILE_TEXT},
    "deep-list": json.loads("[" * 5000 + "]" * 5000),
}


def _checkpoint_invalid_code() -> int:
    code = getattr(health_check, "EXIT_CHECKPOINT_INVALID", None)
    assert isinstance(code, int) and not isinstance(code, bool), (
        "scripts/health_check.py defines no EXIT_CHECKPOINT_INVALID (#17: documented exit code)"
    )
    return code


def _valid_checkpoint() -> dict[str, object]:
    """Exactly what the real writer produces for a finished run."""
    return dataclasses.asdict(CheckpointState(source_name=_SOURCE, stage="done", corpus_version="2026-10-10"))


def _field_vectors() -> list[tuple[str, dict[str, object], bool]]:
    """(id, checkpoint object, is_valid), generated from CheckpointState's fields."""
    vectors: list[tuple[str, dict[str, object], bool]] = [("full", _valid_checkpoint(), True)]
    for field in _CP_FIELDS:
        absent = {k: v for k, v in _valid_checkpoint().items() if k != field.name}
        vectors.append((f"{field.name}-absent", absent, field.name != _REQUIRED_FIELD))
        for label, value in _WRONG_TYPED_VALUES.items():
            vectors.append((f"{field.name}-{label}", {**_valid_checkpoint(), field.name: value}, False))
    # F7: the checkpoint must be this source's own, not another source's file copied in.
    vectors.append(("source_name-foreign", {**_valid_checkpoint(), "source_name": "cfpb"}, False))
    return vectors


_FIELD_VECTORS = _field_vectors()
_FIELD_VALID = [pytest.param(obj, id=vid) for vid, obj, ok in _FIELD_VECTORS if ok]
_FIELD_INVALID = [pytest.param(obj, id=vid) for vid, obj, ok in _FIELD_VECTORS if not ok]

# Valid JSON, wrong top-level shape (the card's case: a list, and every other non-object).
_NON_OBJECT_BYTES = {
    "empty-list": b"[]",
    "list": b"[1, 2]",
    "list-of-checkpoints": json.dumps([_valid_checkpoint()]).encode(),
    "list-hostile": json.dumps([_HOSTILE_TEXT]).encode(),
    "string": json.dumps("done").encode(),
    "string-hostile": json.dumps(_HOSTILE_TEXT).encode(),
    "zero": b"0",
    "number": b"42",
    "float": b"1.5",
    "nan": b"NaN",
    "true": b"true",
    "false": b"false",
    "null": b"null",
}
# Not a JSON document at all, or not UTF-8: structure forgery and encoding attacks.
_UNPARSEABLE_BYTES = {
    "empty-file": b"",
    "whitespace-only": b" \n\t\r\n",
    "open-brace": b"{",
    "truncated": json.dumps(_valid_checkpoint()).encode()[:-1],
    "trailing-garbage": json.dumps(_valid_checkpoint()).encode() + b"\n" + _CANARY.encode(),
    "utf8-bom": b"\xef\xbb\xbf" + json.dumps(_valid_checkpoint()).encode(),
    "raw-nul-in-string": b'{"stage": "do\x00ne"}',
    "raw-newline-in-string": b'{"stage": "do\nne"}',
    "invalid-utf8": b'{"stage": "\xff\xfe"}',
    "surrogate-as-utf8": b'{"stage": "\xed\xa0\x80"}',
    "utf16": json.dumps(_valid_checkpoint()).encode("utf-16"),
    "single-quotes": b"{'stage': 'done'}",
    "hostile-text": _HOSTILE_TEXT.encode(),
}


def _write_source_configs(config_dir: Path, names: tuple[str, ...] = (_SOURCE,)) -> None:
    config_dir.mkdir()
    for name in names:
        (config_dir / f"{name}.yaml").write_text(f"name: {name}\n")


def _run_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = main(["--config-dir", str(tmp_path / "config"), "--state-dir", str(tmp_path / "state"),
               "--out-dir", str(tmp_path / "out")])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _run_child(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the script as health.yml does, as its own process, bounded in time."""
    argv = [sys.executable, str(_SCRIPTS_DIR / "health_check.py"),
            "--config-dir", str(tmp_path / "config"), "--state-dir", str(tmp_path / "state"),
            "--out-dir", str(tmp_path / "out")]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=_CHILD_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail(f"health_check.py hung for {_CHILD_TIMEOUT_S}s on the checkpoint path (F6)")


def _source_rows(text: str, source: str = _SOURCE) -> list[list[str]]:
    rows = [line for line in text.splitlines() if line.startswith(f"| {source} |")]
    return [[cell.strip() for cell in row.strip("|").split(" | ")] for row in rows]


def _assert_checkpoint_invalid(rc: int, out: str, err: str, tmp_path: Path) -> None:
    """The one outcome for every bad checkpoint. Behaviour first, then the named code."""
    assert "Traceback" not in err, f"crashed instead of reporting the checkpoint: {err[-600:]!r}"
    assert rc not in (0, cli.EXIT_NOT_WIRED), (
        f"bad checkpoint read as success or as the warning-only unwired state: rc={rc}, out={out[-600:]!r}"
    )
    for cells in _source_rows(out):
        assert cells[-1] not in _TRUSTED_STATUSES, f"bad checkpoint reported as a trusted status: {cells!r}"
    assert _CHECKPOINT_NAME in out + err, "the message must name the checkpoint file it could not use (F8)"
    health_dir = tmp_path / "out" / "health"
    reports = list(health_dir.glob("*.md")) if health_dir.is_dir() else []
    for text in (out, err, *(r.read_text(encoding="utf-8") for r in reports)):
        for raw in _RAW_FORBIDDEN:
            assert raw not in text, f"checkpoint bytes echoed raw: {raw!r}"
    assert rc == _checkpoint_invalid_code()


def _state_with_checkpoint(tmp_path: Path, body: bytes) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (state / _CHECKPOINT_NAME).write_bytes(body)
    return state


@pytest.fixture(params=[False, True], ids=["unwired", "wired"])
def refresh_wired(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """An invalid checkpoint has one outcome whether or not refresh is wired."""
    monkeypatch.setattr(health_check, "REFRESH_WIRED", request.param)
    return request.param


class TestCheckpointShape:
    """#17: every malformed checkpoint gives one defined, non-zero outcome (red today)."""

    @pytest.mark.parametrize("body", list(_NON_OBJECT_BYTES.values()), ids=list(_NON_OBJECT_BYTES))
    def test_non_object_json_is_checkpoint_invalid(self, tmp_path, capsys, refresh_wired, body):
        _write_source_configs(tmp_path / "config")
        _state_with_checkpoint(tmp_path, body)
        rc, out, err = _run_main(tmp_path, capsys)
        _assert_checkpoint_invalid(rc, out, err, tmp_path)

    @pytest.mark.parametrize("body", list(_UNPARSEABLE_BYTES.values()), ids=list(_UNPARSEABLE_BYTES))
    def test_unparseable_checkpoint_is_invalid_not_fresh(self, tmp_path, capsys, refresh_wired, body):
        _write_source_configs(tmp_path / "config")
        _state_with_checkpoint(tmp_path, body)
        rc, out, err = _run_main(tmp_path, capsys)
        _assert_checkpoint_invalid(rc, out, err, tmp_path)

    @pytest.mark.parametrize("checkpoint", _FIELD_INVALID)
    def test_wrong_typed_or_missing_field_is_checkpoint_invalid(self, tmp_path, capsys, refresh_wired, checkpoint):
        _write_source_configs(tmp_path / "config")
        _state_with_checkpoint(tmp_path, json.dumps(checkpoint).encode())
        rc, out, err = _run_main(tmp_path, capsys)
        _assert_checkpoint_invalid(rc, out, err, tmp_path)

    @pytest.mark.parametrize("kind", ["directory", "dangling-symlink"])
    def test_checkpoint_path_that_is_not_a_regular_file_is_invalid(self, tmp_path, capsys, refresh_wired, kind):
        # Something is at the checkpoint path, so the source is not "never run"; it is unreadable.
        _write_source_configs(tmp_path / "config")
        state = tmp_path / "state"
        state.mkdir()
        if kind == "directory":
            (state / _CHECKPOINT_NAME).mkdir()
        else:
            (state / _CHECKPOINT_NAME).symlink_to(tmp_path / "nowhere.json")
        rc, out, err = _run_main(tmp_path, capsys)
        _assert_checkpoint_invalid(rc, out, err, tmp_path)

    def test_fifo_at_checkpoint_path_is_invalid_without_hanging(self, tmp_path):
        # F6: reading a FIFO blocks forever. The child must not open it as a file.
        _write_source_configs(tmp_path / "config")
        state = tmp_path / "state"
        state.mkdir()
        os.mkfifo(state / _CHECKPOINT_NAME)
        result = _run_child(tmp_path)
        _assert_checkpoint_invalid(result.returncode, result.stdout, result.stderr, tmp_path)

    @pytest.mark.parametrize(
        "body",
        [b"[]", b"[1, 2]", b"null", b"", b'{"stage": 5}'],
        ids=["empty-list", "list", "null", "empty-file", "stage-int"],
    )
    def test_script_process_reports_bad_checkpoint_without_traceback(self, tmp_path, body):
        # The card's own spec, at process level as health.yml runs it: today `[]` exits 1
        # with an AttributeError traceback, and an empty file or an int stage exits 0.
        _write_source_configs(tmp_path / "config")
        _state_with_checkpoint(tmp_path, body)
        result = _run_child(tmp_path)
        _assert_checkpoint_invalid(result.returncode, result.stdout, result.stderr, tmp_path)

    @pytest.mark.parametrize("other", ["fresh", "never-run", "stale"])
    def test_invalid_checkpoint_is_not_masked_by_other_sources(self, tmp_path, capsys, refresh_wired, other):
        # One bad checkpoint decides the exit code: a fresh neighbour must not turn it into 0,
        # a never-run neighbour must not downgrade it to the warning-only code, and a stale
        # neighbour must not hide it behind EXIT_SOURCE_PROBLEM.
        _write_source_configs(tmp_path / "config", (_SOURCE, "cfpb"))
        state = _state_with_checkpoint(tmp_path, b"[]")
        if other != "never-run":
            CheckpointStore(state).save(CheckpointState(source_name="cfpb", stage="done", corpus_version="v1"))
        if other == "stale":
            # The epoch is stale under any --stale-days, so the default is not restated here.
            os.utime(state / "cfpb.checkpoint.json", (0, 0))
        rc, out, err = _run_main(tmp_path, capsys)
        _assert_checkpoint_invalid(rc, out, err, tmp_path)


class TestCheckpointShapeControls:
    """Legitimate checkpoints and missing ones keep today's outcomes (green today)."""

    def test_checkpoint_from_the_real_writer_is_fresh(self, tmp_path, capsys, refresh_wired):
        _write_source_configs(tmp_path / "config")
        CheckpointStore(tmp_path / "state").save(
            CheckpointState(source_name=_SOURCE, stage="done", corpus_version="2026-10-10")
        )
        rc, out, err = _run_main(tmp_path, capsys)
        assert rc == 0, (out, err)
        assert err == ""
        ((name, _last_run, stage, _lag, status),) = _source_rows(out)
        assert (name, stage, status) == (_SOURCE, "done", "fresh")

    @pytest.mark.parametrize("checkpoint", _FIELD_VALID)
    def test_valid_field_variants_are_fresh(self, tmp_path, capsys, checkpoint):
        _write_source_configs(tmp_path / "config")
        _state_with_checkpoint(tmp_path, json.dumps(checkpoint).encode())
        rc, out, err = _run_main(tmp_path, capsys)
        assert rc == 0, (out, err)
        ((_name, _last_run, stage, _lag, status),) = _source_rows(out)
        assert (stage, status) == (checkpoint[_REQUIRED_FIELD], "fresh")

    def test_missing_checkpoint_stays_never_run(self, tmp_path, capsys, refresh_wired):
        # Missing and malformed stay apart: no file is "never run", with today's codes.
        _write_source_configs(tmp_path / "config")
        (tmp_path / "state").mkdir()
        rc, out, err = _run_main(tmp_path, capsys)
        assert rc == (EXIT_SOURCE_PROBLEM if refresh_wired else cli.EXIT_NOT_WIRED)
        ((_name, last_run, stage, lag, status),) = _source_rows(out)
        assert (last_run, stage, lag, status) == ("never", "-", "-", "never-run")
        assert "Traceback" not in err

    def test_field_vectors_cover_every_checkpoint_field(self):
        # Contract: the generated table tracks CheckpointState. Every field has invalid
        # vectors and appears in a valid one; the generator only knows how to break str fields.
        names = {f.name for f in _CP_FIELDS}
        assert _REQUIRED_FIELD in names
        assert all(_CP_TYPES[name] is str for name in names), "extend _WRONG_TYPED_VALUES for the new type"
        invalid_ids = {vid for vid, _obj, ok in _FIELD_VECTORS if not ok}
        for name in names:
            assert {f"{name}-{label}" for label in _WRONG_TYPED_VALUES} <= invalid_ids
        assert set(_valid_checkpoint()) == names
        assert _FIELD_VALID and _FIELD_INVALID
