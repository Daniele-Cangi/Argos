"""Adversarial contracts for the committed proof-bearing V3 terminal evidence."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from shutil import copy2
from typing import Any, cast

import orjson
import pytest

from argos.config.manifest import RunMode
from argos.domain.provenance import sha256_hex
from argos.evaluation import prospective_terminal as terminal_module
from argos.evaluation.bundle import record_sha256
from argos.evaluation.claim_artifact import (
    ProspectiveClaimArtifactIndexV1,
    verify_published_claim_artifact,
)
from argos.evaluation.prospective_terminal import (
    CaptureRunSummaryV1,
    LifecycleContinuityStatus,
    ProspectiveExperimentBundleV4,
    ResolutionAdmissibilityStatus,
    TargetAccountingStatus,
    aggregate_prospective_terminal_experiment,
    build_capture_run_summary,
    build_lifecycle_poll_evidence,
    build_target_terminal_evidence,
)
from argos.resolution import ResolutionStatus
from argos.store import CompletionStatus

PROOF_DIR = (
    Path(__file__).resolve().parents[1] / "experiments" / "m4-prospective-20260820-v3" / "proof"
)


def _index(directory: Path = PROOF_DIR) -> ProspectiveClaimArtifactIndexV1:
    return ProspectiveClaimArtifactIndexV1.from_record(
        cast(dict[str, Any], orjson.loads((directory / "claim-index-v1.json").read_bytes()))
    )


def _record() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        orjson.loads((PROOF_DIR / "claim-bundle-v4.json").read_bytes()),
    )


def _targets(record: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], record["terminal_targets"])


def _polls(target: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], target["lifecycle_polls"])


def _redigest(record: dict[str, Any]) -> dict[str, Any]:
    """Recompute the global digest while deliberately leaving sibling proof intact."""

    record["evidence_digest"] = record_sha256(
        {
            "version": "prospective_terminal_evidence.v1",
            "protocol": record["protocol"],
            "protocol_receipt": record["protocol_receipt"],
            "terminal_targets": record["terminal_targets"],
            "terminal_target_receipts": record["terminal_target_receipts"],
            "report": record["report"],
        }
    )
    return record


def test_terminal_claims_are_three_distinct_types() -> None:
    """The old single observation_complete flag cannot express terminal V3."""

    assert TargetAccountingStatus.COMPLETE.value == "TARGET_ACCOUNTING_COMPLETE"
    assert LifecycleContinuityStatus.INCOMPLETE.value == "LIFECYCLE_CONTINUITY_INCOMPLETE"
    assert (
        ResolutionAdmissibilityStatus.NO_ADMISSIBLE_CUTOFF.value == "NO_ADMISSIBLE_CUTOFF_OBSERVED"
    )
    assert timedelta(seconds=300) * 2 < timedelta(hours=3, minutes=37)


def test_committed_v3_terminal_claim_is_portable_and_exact() -> None:
    index = _index()
    bundle = verify_published_claim_artifact(index, PROOF_DIR)

    assert isinstance(bundle, ProspectiveExperimentBundleV4)
    assert bundle.protocol.experiment_id == "m4-prospective-20260820-v3"
    assert bundle.protocol.lifecycle_deadline.isoformat() == "2026-08-21T06:00:00+00:00"
    assert bundle.evidence_digest == (
        "42443a664441b3fa77c10a920eaec587a4d50d13dd9573e5eceb755d8d13c92a"
    )
    raw = (PROOF_DIR / "claim-bundle-v4.json").read_bytes()
    assert len(raw) == index.bundle_byte_length == 1_230_831
    assert (
        sha256_hex(raw)
        == index.bundle_artifact_sha256
        == ("400f7b9619643900e41a0c38db6f62a13aba6112cef247209ab343268538c86d")
    )

    report = bundle.report
    assert report.target_accounting_status is TargetAccountingStatus.COMPLETE
    assert report.lifecycle_continuity_status is LifecycleContinuityStatus.INCOMPLETE
    assert (
        report.resolution_admissibility_status is ResolutionAdmissibilityStatus.NO_ADMISSIBLE_CUTOFF
    )
    assert report.admissible_cutoff_count == 0
    assert report.measurement_layer_verdict.value == "M4_BLOCKED"
    assert report.calibration_verdict.value == "CALIBRATION_NOT_EVALUABLE"

    expected = (
        (
            "3395619",
            "2026-08-21T02:23:08.236683+00:00",
            13_011_763_317,
            127,
            254,
        ),
        (
            "3608324",
            "2026-08-21T02:23:09.877160+00:00",
            13_010_122_840,
            37,
            74,
        ),
    )
    for terminal, facts in zip(bundle.terminal_targets, expected, strict=True):
        market_id, retrieved_at, final_gap, frame_count, accepted_count = facts
        assert terminal.target.market_id == market_id
        assert len(terminal.lifecycle_polls) == 98
        assert terminal.lifecycle_polls[-1].observation.ordinal == 98
        assert terminal.lifecycle_polls[-1].observation.retrieved_at.isoformat() == retrieved_at
        assert terminal.last_observed_finality.value == "proposed"
        assert terminal.admissible_cutoff_count == 0
        assert terminal.final_polling_gap_microseconds == final_gap
        assert terminal.continuity_status is LifecycleContinuityStatus.INCOMPLETE
        assert all(
            poll.observation.retrieved_at <= bundle.protocol.lifecycle_deadline
            for poll in terminal.lifecycle_polls
        )
        assert terminal.capture_summary.frame_count == frame_count
        assert terminal.capture_summary.accepted_count == accepted_count
        assert terminal.capture_summary.rejected_count == 0
        assert terminal.capture_summary.decode_failure_count == 0
        assert terminal.capture_summary.unknown_event_count == 0


def test_one_changed_v3_artifact_byte_is_rejected(tmp_path: Path) -> None:
    copy2(PROOF_DIR / "claim-index-v1.json", tmp_path / "claim-index-v1.json")
    copy2(PROOF_DIR / "claim-bundle-v4.json", tmp_path / "claim-bundle-v4.json")
    artifact = tmp_path / "claim-bundle-v4.json"
    raw = bytearray(artifact.read_bytes())
    raw[-1] = ord(" ")
    artifact.write_bytes(raw)

    with pytest.raises(ValueError, match="bytes disagree"):
        verify_published_claim_artifact(_index(tmp_path), tmp_path)


def _attack(record: dict[str, Any], name: str) -> None:
    target = _targets(record)[0]
    polls = _polls(target)
    last_observation = cast(dict[str, Any], polls[-1]["observation"])
    report = cast(dict[str, Any], record["report"])
    protocol = cast(dict[str, Any], record["protocol"])

    if name == "last_finality":
        last_observation["finality"] = "final"
        target["last_observed_finality"] = "final"
    elif name == "invent_cutoff":
        target["admissible_cutoff_count"] = 1
        report["admissible_cutoff_count"] = 1
        report["resolution_admissibility_status"] = "OBSERVED_ADMISSIBLE_CUTOFF"
    elif name == "move_deadline":
        protocol["lifecycle_deadline"] = "2026-08-21T07:00:00Z"
    elif name == "shorten_gap":
        target["final_polling_gap_microseconds"] = 300_000_000
        target["maximum_polling_gap_microseconds"] = 300_000_000
    elif name == "claim_continuity":
        target["continuity_status"] = "LIFECYCLE_CONTINUITY_COMPLETE"
        report["lifecycle_continuity_status"] = "LIFECYCLE_CONTINUITY_COMPLETE"
    elif name == "remove_observation":
        polls.pop(50)
    elif name == "remove_receipt":
        polls[50].pop("receipt")
    elif name == "reorder_observations":
        polls[49], polls[50] = polls[50], polls[49]
    elif name == "post_deadline_observation":
        last_observation["retrieved_at"] = "2026-08-21T06:00:01Z"
    elif name == "claim_observed_through_deadline":
        target["claims_observed_through_deadline"] = True
    elif name == "change_target_identity":
        cast(dict[str, Any], target["target"])["market_id"] = "other-market"
    elif name == "claim_resolved_without_cutoff":
        target["disposition"] = "TARGET_RESOLVED_BY_DEADLINE"
    else:  # pragma: no cover - the parametrization is the closed attack set
        raise AssertionError(f"unknown terminal attack: {name}")


@pytest.mark.parametrize(
    "attack_name",
    [
        "last_finality",
        "invent_cutoff",
        "move_deadline",
        "shorten_gap",
        "claim_continuity",
        "remove_observation",
        "remove_receipt",
        "reorder_observations",
        "post_deadline_observation",
        "claim_observed_through_deadline",
        "change_target_identity",
        "claim_resolved_without_cutoff",
    ],
)
def test_global_redigest_cannot_turn_false_terminal_evidence_into_a_claim(
    attack_name: str,
) -> None:
    record = _record()
    _attack(record, attack_name)

    with pytest.raises((KeyError, ValueError)):
        ProspectiveExperimentBundleV4.from_record(_redigest(record))


def _unchecked(model: Any, **updates: Any) -> Any:
    values = {name: getattr(model, name) for name in type(model).model_fields}
    values.update(updates)
    return type(model).model_construct(**values)


def _receipt_for_record(receipt: Any, record: Any) -> Any:
    raw = orjson.dumps(record.to_record(), option=orjson.OPT_SORT_KEYS)
    return _unchecked(
        receipt,
        artifact_sha256=sha256_hex(raw),
        artifact_byte_length=len(raw),
    )


@pytest.fixture(scope="module")
def real_bundle() -> ProspectiveExperimentBundleV4:
    bundle = verify_published_claim_artifact(_index(), PROOF_DIR)
    assert isinstance(bundle, ProspectiveExperimentBundleV4)
    return bundle


def test_real_terminal_builders_round_trip_the_committed_evidence(
    real_bundle: ProspectiveExperimentBundleV4,
) -> None:
    terminal = real_bundle.terminal_targets[0]
    summary = terminal.capture_summary
    rebuilt_summary = build_capture_run_summary(
        experiment_id=summary.experiment_id,
        target_id=summary.target_id,
        manifest=summary.manifest,
        started_at=summary.started_at,
        ended_at=summary.ended_at,
        completion_status=summary.completion_status,
        frame_count=summary.frame_count,
        accepted_count=summary.accepted_count,
        duplicate_count=summary.duplicate_count,
        rejected_count=summary.rejected_count,
        decode_failure_count=summary.decode_failure_count,
        unknown_event_count=summary.unknown_event_count,
        observation_schema_counts=summary.observation_schema_counts,
        rejection_reason_counts=summary.rejection_reason_counts,
        ledger_sha256=summary.ledger_sha256,
        ledger_byte_length=summary.ledger_byte_length,
        delivery_collection_digest=summary.delivery_collection_digest,
        rejection_collection_digest=summary.rejection_collection_digest,
    )
    assert rebuilt_summary == summary

    poll = terminal.lifecycle_polls[0]
    rebuilt_poll = build_lifecycle_poll_evidence(
        observation=poll.observation,
        receipt=poll.receipt,
        raw_payload=poll.raw_payload_utf8.encode(),
    )
    assert rebuilt_poll == poll

    rebuilt_terminal = build_target_terminal_evidence(
        protocol=real_bundle.protocol,
        protocol_receipt=real_bundle.protocol_receipt,
        target=terminal.target,
        target_receipt=terminal.target_receipt,
        capture_summary=summary,
        lifecycle_polls=terminal.lifecycle_polls,
        closed_at=terminal.closed_at,
    )
    assert rebuilt_terminal == terminal

    rebuilt_bundle = aggregate_prospective_terminal_experiment(
        protocol=real_bundle.protocol,
        protocol_receipt=real_bundle.protocol_receipt,
        terminal_targets=real_bundle.terminal_targets,
        terminal_target_receipts=real_bundle.terminal_target_receipts,
        created_at=real_bundle.report.created_at,
    )
    assert rebuilt_bundle == real_bundle

    with pytest.raises(ValueError, match="not UTF-8"):
        build_lifecycle_poll_evidence(
            observation=poll.observation,
            receipt=poll.receipt,
            raw_payload=b"\xff",
        )
    with pytest.raises(ValueError, match="at least one lifecycle poll"):
        build_target_terminal_evidence(
            protocol=real_bundle.protocol,
            protocol_receipt=real_bundle.protocol_receipt,
            target=terminal.target,
            target_receipt=terminal.target_receipt,
            capture_summary=summary,
            lifecycle_polls=(),
            closed_at=terminal.closed_at,
        )


def test_capture_summary_validator_rejects_every_false_completion_shape(
    real_bundle: ProspectiveExperimentBundleV4,
) -> None:
    summary = real_bundle.terminal_targets[0].capture_summary

    invalid_hex = summary.to_record()
    invalid_hex["ledger_sha256"] = "z" * 64
    with pytest.raises(ValueError, match="digests must be hexadecimal"):
        CaptureRunSummaryV1.from_record(invalid_hex)

    invalid_counts = summary.to_record()
    invalid_counts["observation_schema_counts"] = {"": 1}
    with pytest.raises(ValueError, match="named and non-negative"):
        CaptureRunSummaryV1.from_record(invalid_counts)

    cases = (
        (
            _unchecked(summary, completion_status=CompletionStatus.FAILED),
            "completed run",
        ),
        (
            _unchecked(summary, ended_at=summary.started_at - timedelta(microseconds=1)),
            "predates capture start",
        ),
        (
            _unchecked(summary, manifest=_unchecked(summary.manifest, mode=RunMode.REPLAY)),
            "does not name one capture run",
        ),
        (
            _unchecked(summary, observation_schema_counts={}),
            "schema counts disagree",
        ),
        (
            _unchecked(summary, rejected_count=1, rejection_reason_counts={}),
            "rejection reasons disagree",
        ),
        (
            _unchecked(
                summary,
                rejected_count=0,
                decode_failure_count=1,
                unknown_event_count=1,
            ),
            "rejection subsets exceed",
        ),
        (
            _unchecked(summary, capture_summary_id="capture-summary-attacked"),
            "capture_summary_id disagrees",
        ),
    )
    for attacked, message in cases:
        with pytest.raises(ValueError, match=message):
            attacked._summary_is_recomputable()


def test_poll_and_raw_validators_reject_forged_source_evidence(
    real_bundle: ProspectiveExperimentBundleV4,
) -> None:
    poll = real_bundle.terminal_targets[0].lifecycle_polls[0]
    observation = poll.observation
    raw = poll.raw_payload_utf8.encode()

    wrong_location_observation = _unchecked(
        observation,
        raw_payload_location="source/00/attacked.raw.json",
    )
    wrong_location_poll = _unchecked(
        poll,
        observation=wrong_location_observation,
        receipt=_receipt_for_record(poll.receipt, wrong_location_observation),
    )
    cases = (
        (
            _unchecked(
                poll,
                receipt=_unchecked(poll.receipt, experiment_id="other-experiment"),
            ),
            "different observation",
        ),
        (
            _unchecked(
                poll,
                receipt=_unchecked(
                    poll.receipt,
                    persisted_at=observation.retrieved_at - timedelta(microseconds=1),
                ),
            ),
            "predates source retrieval",
        ),
        (_unchecked(poll, raw_payload_utf8="{}"), "source bytes disagree"),
        (wrong_location_poll, "source location disagrees"),
        (_unchecked(poll, poll_evidence_id="poll-attacked"), "poll_evidence_id disagrees"),
    )
    for attacked, message in cases:
        with pytest.raises(ValueError, match=message):
            attacked._poll_is_semantically_bound()

    with pytest.raises(ValueError, match="not JSON"):
        terminal_module._verify_observation_against_raw(observation, b"{")
    with pytest.raises(ValueError, match="not a JSON object"):
        terminal_module._verify_observation_against_raw(observation, b"[]")
    attacked_finality = (
        ResolutionStatus.DISPUTED
        if observation.finality is not ResolutionStatus.DISPUTED
        else ResolutionStatus.PROPOSED
    )
    with pytest.raises(ValueError, match="normalized source bytes"):
        terminal_module._verify_observation_against_raw(
            _unchecked(observation, finality=attacked_finality),
            raw,
        )

    invalid_time = cast(dict[str, Any], orjson.loads(raw))
    invalid_time["updatedAt"] = "not-an-iso-time"
    with pytest.raises(ValueError, match="not an ISO timestamp"):
        terminal_module._verify_observation_against_raw(
            observation,
            orjson.dumps(invalid_time),
        )

    different_time = cast(dict[str, Any], orjson.loads(raw))
    different_time["updatedAt"] = "2030-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="source time disagrees"):
        terminal_module._verify_observation_against_raw(
            observation,
            orjson.dumps(different_time),
        )

    assert (
        terminal_module._nonfinal_status({"umaResolutionStatuses": "["}) is ResolutionStatus.UNKNOWN
    )
    assert terminal_module._nonfinal_status({}) is ResolutionStatus.UNKNOWN
    assert (
        terminal_module._nonfinal_status({"umaResolutionStatuses": ["disputed"]})
        is ResolutionStatus.DISPUTED
    )


def test_terminal_target_validator_rejects_false_chain_semantics(
    real_bundle: ProspectiveExperimentBundleV4,
) -> None:
    terminal = real_bundle.terminal_targets[0]
    first = terminal.lifecycle_polls[0]
    last = terminal.lifecycle_polls[-1]
    final_poll = _unchecked(
        last,
        observation=_unchecked(last.observation, finality=ResolutionStatus.FINAL),
    )

    cases = (
        (_unchecked(terminal, experiment_id="other-experiment"), "different selected target"),
        (
            _unchecked(
                terminal,
                capture_summary=_unchecked(terminal.capture_summary, target_id="target-other"),
            ),
            "capture summary names",
        ),
        (_unchecked(terminal, lifecycle_polls=()), "requires a lifecycle chain"),
        (
            _unchecked(
                terminal,
                lifecycle_polls=(
                    _unchecked(
                        first,
                        observation=_unchecked(first.observation, target_id="target-other"),
                    ),
                    *terminal.lifecycle_polls[1:],
                ),
            ),
            "lifecycle chain names",
        ),
        (
            _unchecked(terminal, last_admissible_observation_id="lifecycle-other"),
            "terminal summary disagrees",
        ),
        (
            _unchecked(
                terminal,
                lifecycle_polls=(*terminal.lifecycle_polls[:-1], final_poll),
                last_observed_finality=ResolutionStatus.FINAL,
            ),
            "requires cutoff evidence",
        ),
        (_unchecked(terminal, disposition="wrong"), "wrong target disposition"),
        (
            _unchecked(terminal, replacement_target_id="target-replacement"),
            "cannot be replaced or rescued",
        ),
    )
    for attacked, message in cases:
        with pytest.raises(ValueError, match=message):
            attacked._terminal_chain_is_recomputable()


def _summary_with_parameters(summary: Any, **updates: Any) -> Any:
    parameters = dict(summary.manifest.run_parameters)
    parameters.update(updates)
    manifest = _unchecked(summary.manifest, run_parameters=parameters)
    return _unchecked(summary, manifest=manifest)


def test_terminal_target_protocol_guards_cover_runtime_window_and_cadence(
    real_bundle: ProspectiveExperimentBundleV4,
) -> None:
    protocol = real_bundle.protocol
    terminal = real_bundle.terminal_targets[0]
    last = terminal.lifecycle_polls[-1]
    late_poll = _unchecked(
        last,
        observation=_unchecked(
            last.observation,
            retrieved_at=protocol.lifecycle_deadline + timedelta(microseconds=1),
        ),
    )
    cases = (
        (_unchecked(terminal, experiment_id="other-experiment"), "frozen protocol"),
        (
            _unchecked(
                terminal,
                closed_at=protocol.lifecycle_deadline - timedelta(microseconds=1),
            ),
            "closes before",
        ),
        (
            _unchecked(
                terminal,
                lifecycle_polls=(*terminal.lifecycle_polls[:-1], late_poll),
            ),
            "post-deadline",
        ),
        (
            _unchecked(
                terminal,
                capture_summary=_unchecked(
                    terminal.capture_summary,
                    manifest=_unchecked(
                        terminal.capture_summary.manifest,
                        code_revision="revision-attacked",
                    ),
                ),
            ),
            "protocol runtime",
        ),
        (
            _unchecked(
                terminal,
                capture_summary=_summary_with_parameters(
                    terminal.capture_summary,
                    subscribed_token_ids="not-a-token-sequence",
                ),
            ),
            "subscriptions disagree",
        ),
        (
            _unchecked(
                terminal,
                capture_summary=_summary_with_parameters(
                    terminal.capture_summary,
                    max_seconds="not-a-number",
                ),
            ),
            "not a numeric frozen bound",
        ),
        (
            _unchecked(
                terminal,
                capture_summary=_summary_with_parameters(
                    terminal.capture_summary,
                    max_frames=999,
                ),
            ),
            "frozen capture bounds",
        ),
        (
            _unchecked(
                terminal,
                capture_summary=_unchecked(
                    terminal.capture_summary,
                    started_at=protocol.observation_window_start - timedelta(microseconds=1),
                ),
            ),
            "outside the observation window",
        ),
        (
            _unchecked(
                terminal,
                capture_summary=_unchecked(terminal.capture_summary, rejected_count=1),
            ),
            "rejected or unknown input",
        ),
        (
            _unchecked(terminal, final_polling_gap_microseconds=0),
            "polling gap disagrees",
        ),
        (
            _unchecked(
                terminal,
                continuity_status=LifecycleContinuityStatus.COMPLETE,
            ),
            "continuity status disagrees",
        ),
    )
    for attacked, message in cases:
        with pytest.raises(ValueError, match=message):
            attacked.validate_against_protocol(protocol)


def test_terminal_aggregate_guards_and_builder_refusals(
    real_bundle: ProspectiveExperimentBundleV4,
) -> None:
    protocol = real_bundle.protocol
    first, second = real_bundle.terminal_targets
    first_receipt, second_receipt = real_bundle.terminal_target_receipts

    with pytest.raises(ValueError, match="duplicate terminal target receipt"):
        aggregate_prospective_terminal_experiment(
            protocol=protocol,
            protocol_receipt=real_bundle.protocol_receipt,
            terminal_targets=(first,),
            terminal_target_receipts=(first_receipt, first_receipt),
            created_at=real_bundle.report.created_at,
        )
    with pytest.raises(ValueError, match="missing its persistence receipt"):
        aggregate_prospective_terminal_experiment(
            protocol=protocol,
            protocol_receipt=real_bundle.protocol_receipt,
            terminal_targets=(first,),
            terminal_target_receipts=(),
            created_at=real_bundle.report.created_at,
        )
    with pytest.raises(ValueError, match="unrelated artifact"):
        aggregate_prospective_terminal_experiment(
            protocol=protocol,
            protocol_receipt=real_bundle.protocol_receipt,
            terminal_targets=(first,),
            terminal_target_receipts=(first_receipt, second_receipt),
            created_at=real_bundle.report.created_at,
        )

    cases = (
        (
            _unchecked(
                real_bundle,
                protocol_receipt=_unchecked(
                    real_bundle.protocol_receipt,
                    experiment_id="other-experiment",
                ),
            ),
            "protocol receipt names different",
        ),
        (
            _unchecked(real_bundle, terminal_target_receipts=(first_receipt,)),
            "receipt counts disagree",
        ),
        (
            _unchecked(
                real_bundle,
                terminal_targets=(
                    _unchecked(first, protocol_receipt_id="receipt-other"),
                    second,
                ),
            ),
            "different protocol receipt",
        ),
        (
            _unchecked(
                real_bundle,
                terminal_target_receipts=(
                    _unchecked(first_receipt, artifact_id="terminal-other"),
                    second_receipt,
                ),
            ),
            "receipt names different",
        ),
        (
            _unchecked(
                real_bundle,
                terminal_targets=(first, first),
                terminal_target_receipts=(first_receipt, first_receipt),
            ),
            "unique target and capture runs",
        ),
        (
            _unchecked(
                real_bundle,
                terminal_targets=(second, first),
                terminal_target_receipts=(second_receipt, first_receipt),
            ),
            "ordered by frozen selection rank",
        ),
        (
            _unchecked(
                real_bundle,
                report=_unchecked(
                    real_bundle.report,
                    limitations=("an attacked limitation",),
                ),
            ),
            "report disagrees",
        ),
        (_unchecked(real_bundle, evidence_digest="0" * 64), "evidence_digest disagrees"),
    )
    for attacked, message in cases:
        with pytest.raises(ValueError, match=message):
            attacked._aggregate_is_recomputable()


def test_terminal_report_gap_and_limit_branches(
    real_bundle: ProspectiveExperimentBundleV4,
) -> None:
    protocol = real_bundle.protocol
    first, second = real_bundle.terminal_targets

    report_record = real_bundle.report.to_record()
    report_record["limitations"] = []
    with pytest.raises(ValueError, match="must state its limitations"):
        type(real_bundle.report).from_record(report_record)

    incomplete = terminal_module._terminal_report(
        protocol=protocol,
        terminal_targets=(),
        created_at=protocol.declared_at,
    )
    assert incomplete.target_accounting_status is TargetAccountingStatus.INCOMPLETE
    assert not incomplete.experiment_closed_by_frozen_deadline

    complete_cutoff_targets = (
        _unchecked(
            first,
            continuity_status=LifecycleContinuityStatus.COMPLETE,
            admissible_cutoff_count=1,
        ),
        _unchecked(
            second,
            continuity_status=LifecycleContinuityStatus.COMPLETE,
            admissible_cutoff_count=1,
        ),
    )
    complete = terminal_module._terminal_report(
        protocol=protocol,
        terminal_targets=complete_cutoff_targets,
        created_at=protocol.lifecycle_deadline,
    )
    assert complete.lifecycle_continuity_status is LifecycleContinuityStatus.COMPLETE
    assert (
        complete.resolution_admissibility_status
        is ResolutionAdmissibilityStatus.OBSERVED_ADMISSIBLE_CUTOFF
    )

    observations = tuple(poll.observation for poll in first.lifecycle_polls)
    with pytest.raises(ValueError, match="requires observations"):
        terminal_module._lifecycle_gaps(protocol, ())
    with pytest.raises(ValueError, match="not monotonic"):
        terminal_module._lifecycle_gaps(protocol, (observations[1], observations[0]))
    with pytest.raises(ValueError, match="outside the frozen polling window"):
        terminal_module._lifecycle_gaps(
            protocol,
            (
                _unchecked(
                    observations[0],
                    retrieved_at=protocol.declared_at - timedelta(microseconds=1),
                ),
            ),
        )

    near_deadline = _unchecked(
        observations[0],
        retrieved_at=protocol.lifecycle_deadline - timedelta(seconds=300),
    )
    final_gap, maximum_gap, continuity = terminal_module._lifecycle_gaps(
        protocol,
        (near_deadline,),
    )
    assert final_gap == maximum_gap == 300_000_000
    assert continuity is LifecycleContinuityStatus.COMPLETE
