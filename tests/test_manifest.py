"""Tests for the three "before M2" run-manifest obligations from the M0 closure
reviews (docs/BACKLOG.md): a ``RunMode`` enum instead of a free-form string,
schema versions (plural) plus data provenance, and working-tree state recorded
alongside ``code_revision``.

``run_manifest.v2`` is a breaking change; ``run_manifest.v1`` was never
persisted anywhere, so there is deliberately no compatibility shim and no
migration test here (see ``src/argos/config/manifest.py`` module docstring).
"""

from __future__ import annotations

import random
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from typer.testing import CliRunner

from argos import cli
from argos.cli import app
from argos.clock import ReplayClock
from argos.config import RunManifest, RunMode, Settings, WorkingTreeStatus, build_run_manifest
from argos.domain.provenance import SourceProvenanceV1

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
runner = CliRunner()


def _manifest(**overrides: Any) -> RunManifest:
    fields: dict[str, Any] = {
        "settings": Settings(),
        "clock": ReplayClock(START),
        "run_id": "run-1",
        "mode": RunMode.DISCOVER,
    }
    fields.update(overrides)
    return build_run_manifest(**fields)


def _provenance(**overrides: Any) -> SourceProvenanceV1:
    fields: dict[str, Any] = {
        "source": "gamma",
        "endpoint": "/markets",
        "http_status": 200,
        "retrieved_at": START,
        "raw_sha256": "0" * 64,
        "byte_length": 10,
    }
    fields.update(overrides)
    return SourceProvenanceV1(**fields)


def _init_repo(path: Path) -> None:
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )
    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (path / "tracked.txt").write_text("original\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "initial")


# --- obligation 1: mode is an enum, not a free-form string ------------------------


def test_run_mode_membership_matches_actual_and_imminent_needs() -> None:
    """M1 has discover/audit, M2 adds capture, M3 adds replay; inspect is the
    standalone manifest CLI's own existing mode. Nothing beyond M3."""
    assert {member.value for member in RunMode} == {
        "discover",
        "audit",
        "capture",
        "replay",
        "inspect",
    }


def test_run_mode_serialized_values_are_stable() -> None:
    """Locks the wire values against an accidental rename; a persisted record's
    ``mode`` must never shift out from under it."""
    assert RunMode.DISCOVER.value == "discover"
    assert RunMode.AUDIT.value == "audit"
    assert RunMode.CAPTURE.value == "capture"
    assert RunMode.REPLAY.value == "replay"
    assert RunMode.INSPECT.value == "inspect"


def test_mode_is_rejected_when_it_is_not_a_declared_member() -> None:
    with pytest.raises(ValidationError):
        _manifest(mode="some-new-milestone-mode")


@given(mode=st.sampled_from(list(RunMode)))
def test_every_mode_survives_a_record_round_trip(mode: RunMode) -> None:
    manifest = _manifest(mode=mode)
    restored = RunManifest.from_record(manifest.to_record())
    assert restored.mode is mode


# --- obligation 2: schema versions (plural) and data provenance ------------------


def test_a_run_that_normalizes_and_compiles_records_both_schema_versions() -> None:
    manifest = _manifest(
        mode=RunMode.AUDIT,
        schema_versions=["market_definition.v1", "compiled_market_contract.v1"],
    )
    assert set(manifest.schema_versions) == {"market_definition.v1", "compiled_market_contract.v1"}


def test_input_provenance_reuses_source_provenance_v1_verbatim() -> None:
    """Invariant: reuse the existing provenance shape rather than a parallel one."""
    provenance = _provenance()
    manifest = _manifest(mode=RunMode.CAPTURE, input_provenance=[provenance])
    assert manifest.input_provenance[0] == provenance
    assert isinstance(manifest.input_provenance[0], SourceProvenanceV1)


@given(
    versions=st.lists(
        st.text(
            min_size=1, max_size=12, alphabet=st.characters(min_codepoint=97, max_codepoint=122)
        ),
        max_size=6,
    )
)
def test_schema_versions_are_canonical_regardless_of_input_order(versions: list[str]) -> None:
    shuffled = versions[:]
    random.shuffle(shuffled)
    a = _manifest(schema_versions=versions)
    b = _manifest(schema_versions=shuffled)
    assert a.schema_versions == b.schema_versions
    assert a.to_record() == b.to_record()


def test_input_provenance_order_does_not_change_the_canonical_record() -> None:
    first = _provenance(raw_sha256="1" * 64, endpoint="/markets")
    second = _provenance(raw_sha256="2" * 64, endpoint="/markets/2")
    forward = _manifest(input_provenance=[first, second]).to_record()
    backward = _manifest(input_provenance=[second, first]).to_record()
    assert forward == backward


def test_schema_versions_rejects_a_blank_entry() -> None:
    with pytest.raises(ValidationError):
        _manifest(schema_versions=[""])


def test_input_provenance_rejects_something_that_is_not_provenance() -> None:
    with pytest.raises(ValidationError):
        _manifest(input_provenance=[{"raw_sha256": "not-a-real-record"}])


def test_a_run_touching_no_source_payload_records_empty_provenance_not_missing() -> None:
    manifest = _manifest(mode=RunMode.INSPECT)
    assert manifest.input_provenance == ()
    assert "input_provenance" in manifest.to_record()


# --- obligation 3: working-tree state alongside code_revision --------------------


def test_working_tree_cannot_be_asserted_without_a_trusted_revision() -> None:
    with pytest.raises(ValidationError):
        _manifest(code_revision=None, working_tree=WorkingTreeStatus.CLEAN)
    with pytest.raises(ValidationError):
        _manifest(code_revision=None, working_tree=WorkingTreeStatus.DIRTY)


def test_unknown_working_tree_is_allowed_without_a_revision() -> None:
    manifest = _manifest(code_revision=None, working_tree=WorkingTreeStatus.UNKNOWN)
    assert manifest.working_tree is WorkingTreeStatus.UNKNOWN


def test_code_revision_reports_clean_for_a_pristine_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    revision, working_tree = cli._code_revision()
    assert revision is not None and len(revision) == 40
    assert working_tree is WorkingTreeStatus.CLEAN


def test_code_revision_reports_dirty_after_an_uncommitted_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    revision, working_tree = cli._code_revision()
    assert revision is not None
    assert working_tree is WorkingTreeStatus.DIRTY


def test_code_revision_reports_dirty_for_an_untracked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    (tmp_path / "untracked.txt").write_text("new\n", encoding="utf-8")
    revision, working_tree = cli._code_revision()
    assert revision is not None
    assert working_tree is WorkingTreeStatus.DIRTY


def test_code_revision_reports_clean_in_a_detached_head_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git rev-parse HEAD` resolves the same commit whether or not a branch is
    checked out; detaching HEAD must not be mistaken for "no repository" or
    corrupt the working-tree check that runs right after it."""
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "--detach", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    revision, working_tree = cli._code_revision()
    assert revision is not None and len(revision) == 40
    assert working_tree is WorkingTreeStatus.CLEAN


def test_code_revision_reports_dirty_in_a_detached_head_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "checkout", "-q", "--detach", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    revision, working_tree = cli._code_revision()
    assert revision is not None and len(revision) == 40
    assert working_tree is WorkingTreeStatus.DIRTY


def test_code_revision_reports_unknown_when_git_is_not_on_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first subprocess call (`rev-parse`) can fail structurally, not just
    return a nonzero exit: a container without a `git` binary raises `OSError`
    (`FileNotFoundError`), which must degrade to the same safe `UNKNOWN` outcome
    as any other "cannot prove this" case, never propagate as a crash."""

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git: command not found")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    revision, working_tree = cli._code_revision()
    assert revision is None
    assert working_tree is WorkingTreeStatus.UNKNOWN


def test_code_revision_falls_back_to_unknown_when_the_status_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The function now shells out twice; a revision proven by the first call must
    still be reported even if the second (`git status`) call itself fails, rather
    than the whole function collapsing to `(None, UNKNOWN)`."""
    _init_repo(tmp_path)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    real_run = subprocess.run

    def fake_run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "status" in command:
            return subprocess.CompletedProcess(command, returncode=128, stdout="", stderr="boom")
        return real_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    revision, working_tree = cli._code_revision()
    assert revision is not None and len(revision) == 40
    assert working_tree is WorkingTreeStatus.UNKNOWN


def test_code_revision_falls_back_to_unknown_when_the_status_check_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    real_run = subprocess.run

    def fake_run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "status" in command:
            raise TimeoutError("git status hung")
        return real_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    revision, working_tree = cli._code_revision()
    assert revision is not None and len(revision) == 40
    assert working_tree is WorkingTreeStatus.UNKNOWN


@pytest.mark.parametrize(
    "stdout",
    [
        "only-one-token\n",
        "{toplevel}\nnot-a-hex-revision\n",
        "{toplevel}\n" + "a" * 39 + "\n",
        "{toplevel}\n" + ("g" * 40) + "\n",
    ],
)
def test_code_revision_reports_unknown_for_malformed_rev_parse_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    """`rev-parse --show-toplevel HEAD` is trusted only after its shape is
    checked: two whitespace-separated tokens, and a revision that is exactly 40
    hex characters. A future git version or a corrupted repository answering
    something else must degrade to `UNKNOWN`, not raise or fabricate a revision."""
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)

    def fake_run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=stdout.format(toplevel=tmp_path), stderr=""
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    revision, working_tree = cli._code_revision()
    assert revision is None
    assert working_tree is WorkingTreeStatus.UNKNOWN


def test_code_revision_reports_unknown_working_tree_outside_any_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    revision, working_tree = cli._code_revision()
    assert revision is None
    assert working_tree is WorkingTreeStatus.UNKNOWN


def test_code_revision_reports_unknown_when_the_toplevel_is_a_foreign_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-existing security property (verify --show-toplevel) must survive
    the addition of the working-tree check: a foreign repo's dirt is not ours."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _init_repo(outer)
    inner = outer / "inner"
    inner.mkdir()
    monkeypatch.setattr(cli, "REPO_ROOT", inner)
    revision, working_tree = cli._code_revision()
    assert revision is None
    assert working_tree is WorkingTreeStatus.UNKNOWN


def test_manifest_built_with_a_trusted_revision_and_working_tree_round_trips() -> None:
    manifest = _manifest(code_revision="a" * 40, working_tree=WorkingTreeStatus.DIRTY)
    restored = RunManifest.from_record(manifest.to_record())
    assert restored.code_revision == "a" * 40
    assert restored.working_tree is WorkingTreeStatus.DIRTY


# --- CLI surface ------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(RunMode))
def test_manifest_command_accepts_every_declared_mode(mode: RunMode) -> None:
    result = runner.invoke(app, ["manifest", "--mode", mode.value])
    assert result.exit_code == 0
    payload = orjson.loads(result.stdout)
    assert payload["mode"] == mode.value
    assert payload["schema_version"] == "run_manifest.v3"


def test_manifest_command_rejects_a_mode_outside_the_enum() -> None:
    result = runner.invoke(app, ["manifest", "--mode", "bogus"])
    assert result.exit_code != 0


def test_manifest_command_reports_a_resolvable_working_tree_in_this_checkout() -> None:
    """This test runs inside the real ARGOS repository, so the revision must
    resolve and the working tree must not be reported as unknown."""
    result = runner.invoke(app, ["manifest"])
    assert result.exit_code == 0
    payload = orjson.loads(result.stdout)
    assert payload["code_revision"] is not None
    assert payload["working_tree"] in {"clean", "dirty"}


# --- security review: `CLEAN` is a positive claim and must never be a guess -------
#
# `working_tree=CLEAN` asserts that the code on disk is the code at `code_revision`.
# Three ways to hold a genuinely modified tree while `git status --porcelain`
# prints nothing were reproduced against `_code_revision` during the security
# review of this slice. Each must degrade to UNKNOWN, never to CLEAN.


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout


def test_a_genuinely_clean_tree_is_still_reported_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not be degenerate: if everything below reports UNKNOWN,
    the field would be useless rather than safe."""
    _init_repo(tmp_path)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    assert cli._working_tree_status() is WorkingTreeStatus.CLEAN


def test_assume_unchanged_cannot_report_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--assume-unchanged` makes git ignore edits to a tracked file outright, so
    the file executing differs from HEAD while `status` prints nothing."""
    _init_repo(tmp_path)
    _git(tmp_path, "update-index", "--assume-unchanged", "tracked.txt")
    (tmp_path / "tracked.txt").write_text("TAMPERED\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)

    assert _git(tmp_path, "status", "--porcelain").strip() == "", "precondition: git is blind"
    assert cli._working_tree_status() is WorkingTreeStatus.UNKNOWN


def test_skip_worktree_cannot_report_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "update-index", "--skip-worktree", "tracked.txt")
    (tmp_path / "tracked.txt").write_text("TAMPERED\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)

    assert cli._working_tree_status() is WorkingTreeStatus.UNKNOWN


def test_environment_injected_config_cannot_suppress_untracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status.showUntrackedFiles=no` is a real developer performance setting, so
    this is the likely accidental path, not only a hostile one. `-c` loses to
    `GIT_CONFIG_*`, which is why the environment is scrubbed rather than pinned."""
    _init_repo(tmp_path)
    (tmp_path / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "status.showUntrackedFiles")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "no")

    assert cli._working_tree_status() is WorkingTreeStatus.DIRTY


def test_git_dir_cannot_redirect_the_revision_to_a_foreign_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `GIT_DIR` set and no `GIT_WORK_TREE`, git reports cwd as
    `--show-toplevel` while `HEAD` comes from the foreign repository — so the M0
    toplevel check passes and the manifest is stamped with someone else's code."""
    foreign = tmp_path / "foreign"
    local = tmp_path / "local"
    foreign.mkdir()
    local.mkdir()
    _init_repo(foreign)
    _init_repo(local)
    # Identical content, author and timestamp yield identical commit hashes, so
    # the two repositories have to be told apart deliberately.
    (foreign / "divergent.txt").write_text("foreign\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=foreign, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "foreign"], cwd=foreign, check=True, capture_output=True
    )
    foreign_head = _git(foreign, "rev-parse", "HEAD").strip()
    local_head = _git(local, "rev-parse", "HEAD").strip()
    assert foreign_head != local_head

    monkeypatch.setattr(cli, "REPO_ROOT", local)
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))

    revision, _ = cli._code_revision()
    assert revision == local_head, "GIT_DIR must not redirect provenance to another repository"


def test_a_probe_does_not_write_to_the_git_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance collection is a read: `--no-optional-locks` keeps `git status`
    from refreshing `.git/index` as a side effect."""
    _init_repo(tmp_path)
    index = tmp_path / ".git" / "index"
    before = index.stat().st_mtime_ns
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)

    cli._working_tree_status()

    assert index.stat().st_mtime_ns == before
