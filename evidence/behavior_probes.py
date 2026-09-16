"""Reproduce four product behaviors using only EPHI's shipped synthetic fixture.

From the extracted EPHI source root:
    PYTHONPATH=src:company_port/src python /path/to/behavior_probes.py \
        --output /path/to/behavior_probes.json

This observational harness does not modify EPHI source or contact company systems.
An exit code of zero means the probes ran, NOT that the product defects are fixed.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ephi.advisory.demo import build_demo_advisory_service
from ephi.advisory.models import AdvisoryWorkflowState, WorkflowMutation
from ephi.advisory.service import AdvisoryService
from ephi.domain.episode import EpisodeState
from ephi.domain.evidence import Hypothesis
from ephi.domain.health import HealthSeverity
from ephi.domain.value import (
    ValueCategory, ValueEvidenceState, ValueLedgerEntry, ValueMaturity,
)
from ephi.episodes.service import EpisodeService
from ephi.value.ledger import InMemoryValueLedgerRepository
from ephi.value.service import ValueLedgerService


def run_probes() -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    service = build_demo_advisory_service()
    episode_id = service.list_attention()[0].episode_id
    service.mutate_workflow(
        episode_id,
        WorkflowMutation(actor="audit-fixture", state=AdvisoryWorkflowState.INVESTIGATING),
    )
    source = service.sources.get(episode_id)
    service.upsert_source(
        replace(source, episode=replace(source.episode, state=EpisodeState.RESOLVED))
    )
    observations.append({
        "id": "F03",
        "scenario": "Technical episode resolves while engineering investigation remains open",
        "workflow": service.get_view(episode_id).workflow.state.value,
        "visible_in_attention": any(
            item.episode_id == episode_id for item in service.list_attention()
        ),
        "required_product_behavior": "Remain discoverable as open engineering work.",
    })

    # Deliberately restore sources ONLY, not the separate service checkpoint.
    try:
        recomposed = AdvisoryService(sources=service.sources)
        recomposed.get_view(episode_id)
        outcome = "success"
    except KeyError:
        outcome = "KeyError"
    observations.append({
        "id": "F02",
        "scenario": "Recompose advisory around populated source repository without restoring its separate checkpoint",
        "outcome": outcome,
        "interpretation": "Persisting source rows alone is insufficient; version/workflow state must be restored or repository backed.",
    })

    known_original = datetime(2026, 1, 1, tzinfo=timezone.utc)
    known_correction = known_original + timedelta(days=2)
    ledger = InMemoryValueLedgerRepository()
    original_entry = ValueLedgerEntry(
        ledger_entry_id="audit-original", episode_id="audit-episode",
        category=ValueCategory.INVESTIGATION_COST, quantity=1, unit="hour",
        monetary_value=10, currency="USD", method="fixture",
        evidence_state=ValueEvidenceState.OBSERVED,
        maturity=ValueMaturity.ACTION_RECORDED, computed_at=known_original,
    )
    ledger.append_entry(original_entry)
    ledger.append_entry(original_entry.model_copy(update={
        "ledger_entry_id": "audit-revision", "supersedes_entry_id": "audit-original",
        "computed_at": known_correction, "monetary_value": 20,
    }))
    summary = ValueLedgerService(repository=ledger).episode_summary(
        "audit-episode", as_of=known_original + timedelta(days=1)
    )
    observations.append({
        "id": "F04",
        "scenario": "Value summary as_of before a later superseding correction",
        "expected_operating_cost": 10,
        "actual_operating_cost": summary.observed_operating_cost,
        "interpretation": "active_entries removes the old row before as_of filtering; historical summaries require as-of-aware supersession.",
    })

    baseline = build_demo_advisory_service()
    assessment = baseline.sources.get(baseline.list_attention()[0].episode_id).assessment
    engine = EpisodeService()
    last = engine.process(assessment)
    for index in range(engine.config.recovery_required):
        last = engine.process(replace(
            assessment, severity=HealthSeverity.OBSERVE, confidence=0.0,
            leading_hypothesis=Hypothesis.DATA_PIPELINE_OR_SCHEMA_CHANGE,
            event_time=assessment.event_time + timedelta(minutes=index + 1),
        ))
    if last is None:
        raise RuntimeError("Fixture no longer yields an episode; inspect the changed baseline.")
    observations.append({
        "id": "F05",
        "scenario": "Low-confidence OBSERVE assessments attributed to data pipeline change after active episode",
        "assessment_count": engine.config.recovery_required,
        "actual_episode_state": last.state.value,
        "required_product_behavior": "Do not count data-unreliable assessments as affirmative recovery evidence.",
    })
    return observations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    arguments = parser.parse_args()
    rendered = json.dumps(run_probes(), indent=2, ensure_ascii=False) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
