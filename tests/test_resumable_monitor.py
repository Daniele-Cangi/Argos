import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from argos.monitoring.resumable import (
    ExclusiveFileLease,
    PollCommit,
    ResumableMonitor,
    ResumableMonitorCheckpointV1,
    checkpoint_after_failure,
    checkpoint_after_poll,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _hold_exclusive_lease(lock_path: str, ready: object) -> None:
    with ExclusiveFileLease(Path(lock_path)):
        ready.set()  # type: ignore[attr-defined]
        while True:
            ready.wait()  # type: ignore[attr-defined]


def _checkpoint() -> ResumableMonitorCheckpointV1:
    return ResumableMonitorCheckpointV1(
        campaign_id="campaign-v1",
        configuration_sha256="a" * 64,
        next_ordinal=0,
        updated_at=NOW,
    )


def _commit(checkpoint: ResumableMonitorCheckpointV1) -> PollCommit:
    return PollCommit(
        ordinal=checkpoint.next_ordinal,
        receipt_id=f"receipt-{checkpoint.next_ordinal}",
        record_sha256="b" * 64,
        persisted_at=NOW + timedelta(minutes=checkpoint.next_ordinal + 1),
        previous_receipt_id=checkpoint.last_receipt_id,
    )


def test_checkpoint_advances_one_ordinal_and_preserves_chain_head() -> None:
    first = checkpoint_after_poll(_checkpoint(), _commit(_checkpoint()))
    second = checkpoint_after_poll(first, _commit(first))
    assert second.next_ordinal == 2
    assert second.last_receipt_id == "receipt-1"


@pytest.mark.parametrize("wrong", [-1, 1, 7])
def test_checkpoint_refuses_wrong_ordinal(wrong: int) -> None:
    checkpoint = _checkpoint()
    commit = _commit(checkpoint)
    with pytest.raises(ValueError, match="ordinal"):
        checkpoint_after_poll(
            checkpoint,
            PollCommit(
                ordinal=wrong,
                receipt_id=commit.receipt_id,
                record_sha256=commit.record_sha256,
                persisted_at=commit.persisted_at,
                previous_receipt_id=commit.previous_receipt_id,
            ),
        )


def test_checkpoint_refuses_a_broken_receipt_predecessor() -> None:
    checkpoint = checkpoint_after_poll(_checkpoint(), _commit(_checkpoint()))
    commit = _commit(checkpoint)
    with pytest.raises(ValueError, match="predecessor"):
        checkpoint_after_poll(
            checkpoint,
            PollCommit(
                ordinal=commit.ordinal,
                receipt_id=commit.receipt_id,
                record_sha256=commit.record_sha256,
                persisted_at=commit.persisted_at,
                previous_receipt_id="other",
            ),
        )


def test_poll_cannot_regress_behind_a_failure_checkpoint() -> None:
    checkpoint = checkpoint_after_failure(
        _checkpoint(),
        started_at=NOW,
        ended_at=NOW + timedelta(minutes=2),
        reason="TimeoutError: injected",
    )
    with pytest.raises(ValueError, match="regresses behind the checkpoint"):
        checkpoint_after_poll(checkpoint, _commit(checkpoint))


def test_failure_is_explicit_and_does_not_consume_an_ordinal() -> None:
    checkpoint = _checkpoint()
    failed = checkpoint_after_failure(
        checkpoint,
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=3),
        reason="TimeoutError: injected network loss",
    )
    assert failed.next_ordinal == 0
    assert failed.gaps[0].attempted_ordinal == 0


def test_failure_gap_cannot_regress_behind_checkpoint() -> None:
    checkpoint = checkpoint_after_poll(_checkpoint(), _commit(_checkpoint()))
    with pytest.raises(ValueError, match="failure gap regresses"):
        checkpoint_after_failure(
            checkpoint,
            started_at=NOW,
            ended_at=NOW + timedelta(seconds=30),
            reason="injected",
        )


def test_checkpoint_record_cannot_hide_a_future_gap() -> None:
    gap = checkpoint_after_failure(
        _checkpoint(),
        started_at=NOW,
        ended_at=NOW + timedelta(seconds=5),
        reason="injected",
    ).gaps[0]
    with pytest.raises(ValidationError, match="cannot precede a recorded gap"):
        ResumableMonitorCheckpointV1(
            campaign_id="campaign-v1",
            configuration_sha256="a" * 64,
            next_ordinal=0,
            updated_at=NOW,
            gaps=(gap,),
        )


def test_corrupt_partial_chain_head_is_rejected() -> None:
    with pytest.raises(ValidationError, match="complete receipt-chain head"):
        ResumableMonitorCheckpointV1(
            campaign_id="campaign-v1",
            configuration_sha256="a" * 64,
            next_ordinal=1,
            last_receipt_id="receipt-0",
            updated_at=NOW,
        )


def test_os_lease_refuses_a_concurrent_owner(tmp_path: Path) -> None:
    path = tmp_path / "monitor.lock"
    with (
        ExclusiveFileLease(path),
        pytest.raises(RuntimeError, match="exclusive owner"),
        ExclusiveFileLease(path),
    ):
        pytest.fail("second owner acquired the lease")
    with ExclusiveFileLease(path):
        pass


def test_monitor_resumes_from_durable_checkpoint(tmp_path: Path) -> None:
    monitor = ResumableMonitor(tmp_path / "checkpoint.json", tmp_path / "monitor.lock")
    monitor.save(_checkpoint())
    first = monitor.run_once(poll=_commit, failure_time=lambda: NOW)
    reloaded = ResumableMonitor(tmp_path / "checkpoint.json", tmp_path / "monitor.lock")
    second = reloaded.run_once(poll=_commit, failure_time=lambda: NOW)
    assert first.next_ordinal == 1
    assert second.next_ordinal == 2
    assert reloaded.load() == second


def test_monitor_persists_gap_before_propagating_poll_failure(tmp_path: Path) -> None:
    monitor = ResumableMonitor(tmp_path / "checkpoint.json", tmp_path / "monitor.lock")
    monitor.save(_checkpoint())
    times = iter((NOW, NOW + timedelta(seconds=5)))

    def fail(_: ResumableMonitorCheckpointV1) -> PollCommit:
        raise TimeoutError("injected")

    with pytest.raises(TimeoutError, match="injected"):
        monitor.run_once(poll=fail, failure_time=lambda: next(times))
    persisted = monitor.load()
    assert persisted.next_ordinal == 0
    assert persisted.gaps[0].reason == "TimeoutError: injected"


def test_existing_partial_checkpoint_blocks_overwrite(tmp_path: Path) -> None:
    monitor = ResumableMonitor(tmp_path / "checkpoint.json", tmp_path / "monitor.lock")
    partial = tmp_path / "checkpoint.json.partial"
    partial.write_bytes(b"corrupt interrupted write")
    with pytest.raises(FileExistsError):
        monitor.save(_checkpoint())


def test_corrupt_checkpoint_json_has_a_boundary_error(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_bytes(b"{not-json")
    monitor = ResumableMonitor(checkpoint_path, tmp_path / "monitor.lock")
    with pytest.raises(ValueError, match="checkpoint is not valid JSON"):
        monitor.load()


def test_kernel_releases_lease_after_real_process_termination(tmp_path: Path) -> None:
    lock_path = tmp_path / "monitor.lock"
    checkpoint_path = tmp_path / "checkpoint.json"
    monitor = ResumableMonitor(checkpoint_path, lock_path)
    monitor.save(_checkpoint())
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_hold_exclusive_lease, args=(str(lock_path), ready))
    process.start()
    assert ready.wait(timeout=10)
    process.terminate()
    process.join(timeout=10)
    assert not process.is_alive()
    assert process.exitcode != 0
    process.close()
    with ExclusiveFileLease(lock_path):
        pass
    resumed = monitor.run_once(
        poll=_commit,
        failure_time=lambda: NOW,
    )
    assert resumed.next_ordinal == 1
    assert resumed.last_receipt_id == "receipt-0"
