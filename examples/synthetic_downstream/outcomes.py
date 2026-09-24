"""Generic synthetic U2/O7 Outcomes facts for integration and browser evidence.

Every value, identity, timestamp, reference and policy label below is fictional.
The fixture contains no company monetary data or production ROI claim.
"""

from __future__ import annotations

from datetime import datetime, timezone


UTC = timezone.utc
SYNTHETIC_OUTCOME_FACTS = (
    {
        "economic_event_key": "synthetic-pending-opportunity",
        "category": "estimated_opportunity",
        "amount": "8.25",
        "currency": "USD",
        "maturity": "PENDING",
        "evidence_ids": ("synthetic-evidence-pending",),
        "episode_ids": (),
    },
    {
        "economic_event_key": "synthetic-observed-outcome",
        "category": "observed_outcome",
        "amount": "3.50",
        "currency": "USD",
        "maturity": "OBSERVED",
        "evidence_ids": ("synthetic-evidence-observed",),
        "episode_ids": ("synthetic-episode-observed",),
    },
    {
        "economic_event_key": "synthetic-reviewed-benefit",
        "category": "benefit",
        "amount": "12.00",
        "currency": "USD",
        "maturity": "OBSERVED",
        "evidence_ids": ("synthetic-evidence-reviewed",),
        "episode_ids": ("synthetic-episode-reviewed",),
    },
    {
        "economic_event_key": "synthetic-zero-outcome",
        "category": "benefit",
        "amount": "0.00",
        "currency": "USD",
        "maturity": "OBSERVED",
        "evidence_ids": ("synthetic-evidence-zero",),
        "episode_ids": (),
    },
    {
        "economic_event_key": "synthetic-negative-outcome",
        "category": "benefit",
        "amount": "-1.75",
        "currency": "USD",
        "maturity": "OBSERVED",
        "evidence_ids": ("synthetic-evidence-negative",),
        "episode_ids": (),
    },
    {
        "economic_event_key": "synthetic-shared-event-once",
        "category": "estimated_opportunity",
        "amount": "5.00",
        "currency": "USD",
        "maturity": "PENDING",
        "evidence_ids": ("synthetic-evidence-shared",),
        "episode_ids": ("synthetic-episode-a", "synthetic-episode-a", "synthetic-episode-b"),
        "decision_ids": ("synthetic-decision-a", "synthetic-decision-a"),
        "action_ids": ("synthetic-action-a", "synthetic-action-a"),
        "contributor_ids": ("synthetic-contributor-a", "synthetic-contributor-a"),
    },
    {
        "economic_event_key": "synthetic-separate-eur-event",
        "category": "benefit",
        "amount": "4.00",
        "currency": "EUR",
        "maturity": "OBSERVED",
        "evidence_ids": ("synthetic-evidence-eur",),
        "episode_ids": (),
    },
)

SYNTHETIC_PERIOD_MOVE = {
    "economic_event_key": "synthetic-period-moving-correction",
    "original_amount": "10.00",
    "original_event_at": datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
    "corrected_amount": "20.00",
    "corrected_event_at": datetime(2025, 1, 17, 12, 0, tzinfo=UTC),
    "explanation": "A later-known correction moves the one active value to a later event period.",
}

SYNTHETIC_LATE_REVIEW = {
    "decision": "APPROVED",
    "explanation": "The independent approval is known only after the earlier AS_KNOWN cutoff.",
}
