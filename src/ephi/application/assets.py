"""Bounded Asset 360 reads over existing Episode, O5, O4 and U1 authorities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from typing import Any

from .context import AccessScope, CurrentAuthorizationAuthority, Principal
from .episodes import EPISODE_READ_CAPABILITY
from .errors import (
    AggregateNotFoundError,
    AuthorizationDeniedError,
    QueryCursorValidationError,
    QueryIdentityMismatchError,
    QueryTooBroadError,
    SourceBindingUnavailableError,
    SourceQuarantineError,
    StorageFailureError,
    ValidationFailureError,
)
from .hashing import canonical_json
from .investigation import INVESTIGATION_PROFILE_KEY, InvestigationProfile
from .read import (
    DEFAULT_SNAPSHOT_TTL_SECONDS,
    MAX_PAGE_SIZE,
    PageResult,
    ReadSnapshotStore,
    RetainedSnapshotRow,
    VersionedReadRow,
)
from .source_ingress import (
    SOURCE_READ_CAPABILITY,
    MetrologyObservation,
    MetrologySourceBinding,
    RevisionPinnedObservationBatch,
    SourceSnapshotRecord,
    SourceSnapshotStatus,
)


ASSET_READ_CAPABILITY = EPISODE_READ_CAPABILITY
MAX_ASSET_EPISODES = 1000
MAX_ASSET_HISTORY_REVISIONS = 200
MAX_ASSET_WORKFLOW_RECEIPTS = 5000
MAX_ASSET_OBSERVATION_POINTS = 500
_FILTER_FIELDS = frozenset({"asset", "family", "site", "context", "characteristic"})
_SORT_FIELDS = frozenset({"asset_id", "family_identity", "context_identity", "characteristic_identity", "source_state", "open_work_count", "latest_known_at"})


def _identity(value: object, field: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > maximum:
        raise ValidationFailureError(f"{field} must be a bounded non-empty canonical identity")
    return value


def _aware(value: object, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationFailureError(f"{field} must be a timezone-aware timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailureError(f"{field} must be a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


def _digest(value: object) -> str:
    def exact_numbers(item: object) -> object:
        if isinstance(item, float):
            return Decimal(str(item))
        if isinstance(item, Mapping):
            return {key: exact_numbers(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [exact_numbers(child) for child in item]
        return item

    return hashlib.sha256(canonical_json(exact_numbers(value)).encode("utf-8")).hexdigest()


def _profile(revision: Mapping[str, Any]) -> InvestigationProfile | None:
    payload = revision.get("payload")
    if not isinstance(payload, Mapping):
        raise StorageFailureError("Episode read revision payload is invalid")
    raw = payload.get(INVESTIGATION_PROFILE_KEY)
    if raw is None:
        return None
    known_at = revision.get("known_at")
    if not isinstance(known_at, datetime):
        raise StorageFailureError("Episode read revision known_at is invalid")
    try:
        return InvestigationProfile.from_payload(raw, revision_known_at=known_at)
    except ValidationFailureError:
        return None


def _normalize_filters(filters: Mapping[str, object] | None, scope: AccessScope) -> dict[str, str]:
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        raise ValidationFailureError("Asset filters must be an object")
    result: dict[str, str] = {}
    for field, raw in filters.items():
        field = _identity(field, "Asset filter field")
        if field not in _FILTER_FIELDS:
            raise ValidationFailureError(f"unsupported Asset filter: {field}")
        value = _identity(raw, f"Asset filter {field}")
        if field == "site" and value != scope.site_id:
            # Site is the server-derived scope identity, so a different site
            # is a truthful empty match rather than a cross-scope query.
            result[field] = value
        else:
            result[field] = value
    return dict(sorted(result.items()))


def _normalize_order(order: Sequence[object] | None) -> tuple[dict[str, str], ...]:
    raw = order or ({"field": "asset_id", "direction": "asc"},)
    if not isinstance(raw, (tuple, list)) or not raw:
        raise ValidationFailureError("Asset order must contain at least one sort")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, Mapping) and set(item) == {"field", "direction"}:
            field, direction = item["field"], item["direction"]
        elif isinstance(item, str) and ":" in item:
            field, direction = item.split(":", 1)
        else:
            raise ValidationFailureError("Asset sort is invalid")
        field = _identity(field, "Asset sort field")
        direction = _identity(direction, "Asset sort direction").lower()
        if field not in _SORT_FIELDS or direction not in {"asc", "desc"} or field in seen:
            raise ValidationFailureError("Asset sort is unsupported or repeated")
        seen.add(field)
        normalized.append({"field": field, "direction": direction})
    if "asset_id" not in seen:
        normalized.append({"field": "asset_id", "direction": "asc"})
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class AssetPage:
    rows: tuple[dict[str, Any], ...]
    total_count: int
    snapshot_id: str
    next_cursor: str | None
    query_identity: dict[str, Any]
    result_identity: str
    filters: dict[str, str]
    order: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class Asset360Result:
    asset_id: str
    query_identity: str
    result_identity: str
    knowledge_cutoff: datetime
    asset: dict[str, Any]
    episodes: tuple[dict[str, Any], ...]
    changes: tuple[dict[str, Any], ...]
    source: dict[str, Any]
    measurement: dict[str, Any]
    limitations: tuple[str, ...]
    compare: dict[str, Any] | None = None


class Asset360QueryService:
    """Compose one exact, bounded authorized view without adding a store."""

    def __init__(
        self,
        row_source: object,
        read_store: ReadSnapshotStore,
        current_authorization: CurrentAuthorizationAuthority,
        source_observer: object,
        source_binding: MetrologySourceBinding,
        source_store: object,
    ) -> None:
        if not hasattr(row_source, "fetch_asset_episode_heads") or not hasattr(row_source, "fetch_asset_episode_history"):
            raise TypeError("Asset 360 requires the existing scoped Episode read source")
        if not isinstance(read_store, ReadSnapshotStore):
            raise TypeError("Asset 360 requires the existing retained read snapshot authority")
        if not isinstance(current_authorization, CurrentAuthorizationAuthority):
            raise TypeError("Asset 360 requires current O8 authorization")
        if not isinstance(source_binding, MetrologySourceBinding):
            raise TypeError("Asset 360 requires one exact O4 source binding")
        if not hasattr(source_observer, "describe") or not hasattr(source_observer, "read_partition"):
            raise TypeError("Asset 360 requires the bounded U1 observation contract")
        if not hasattr(source_store, "get_capability") or not hasattr(source_store, "get_snapshot"):
            raise TypeError("Asset 360 requires the existing O4 capability and snapshot authority")
        self.row_source = row_source
        self.read_store = read_store
        self.current_authorization = current_authorization
        self.source_observer = source_observer
        self.source_binding = source_binding
        self.source_store = source_store

    def _authorize(self, principal: Principal, scope: AccessScope) -> None:
        self.current_authorization.authorize(principal, scope, ASSET_READ_CAPABILITY)

    @staticmethod
    def _asset_membership(rows: Sequence[Mapping[str, Any]], asset_id: str) -> tuple[Mapping[str, Any], ...]:
        matched = []
        for row in rows:
            profile = _profile(row)
            if profile is not None and profile.target.asset_identity == asset_id:
                matched.append(row)
        return tuple(matched)

    def list_assets(
        self,
        principal: Principal,
        scope: AccessScope,
        *,
        filters: Mapping[str, object] | None = None,
        order: Sequence[object] | None = None,
        page_size: int = 25,
        snapshot_id: str | None = None,
        cursor: str | None = None,
        facts_identity: str | None = None,
        snapshot_ttl_seconds: int = DEFAULT_SNAPSHOT_TTL_SECONDS,
    ) -> AssetPage:
        normalized_filters = _normalize_filters(filters, scope)
        normalized_order = _normalize_order(order)
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise QueryTooBroadError("Asset page size exceeds the bounded read contract", limit=MAX_PAGE_SIZE)
        # O8 is deliberately before head, count, retained-snapshot, or cursor lookups.
        self._authorize(principal, scope)
        source_facts: tuple[Mapping[str, Any], ...] | None = None
        rows: tuple[VersionedReadRow, ...] | None = None
        if snapshot_id is None:
            if cursor is not None or facts_identity is not None:
                raise QueryCursorValidationError("Asset cursor identity requires an existing retained snapshot")
            source_facts = tuple(self.row_source.fetch_asset_episode_heads(principal, scope))
            if len(source_facts) > MAX_ASSET_EPISODES:
                raise QueryTooBroadError("Qualified Episode heads exceed the bounded Asset projection", limit=MAX_ASSET_EPISODES)
            rows = self._list_rows_from_facts(source_facts, principal, scope, normalized_filters, normalized_order)
            if len(rows) > MAX_ASSET_EPISODES:
                raise QueryTooBroadError("Asset result exceeds the retained page bound", limit=MAX_ASSET_EPISODES)
            facts_identity = _digest([(row.row_id, row.row_version, row.payload) for row in rows])
        query_identity = {
            "query": "asset-list.v1",
            "scope": scope.as_dict(),
            "filters": normalized_filters,
            "sort": list(normalized_order),
            "facts_identity": _identity(facts_identity, "Asset facts identity"),
            "page_snapshot_identity": _digest({
                "scope": scope.as_dict(),
                "filters": normalized_filters,
                "sort": list(normalized_order),
                "facts_identity": facts_identity,
            }),
        }
        self._authorize(principal, scope)
        if snapshot_id is None:
            assert rows is not None
            snapshot = self.read_store.create_query_snapshot(
                principal,
                scope,
                query_identity,
                ASSET_READ_CAPABILITY,
                rows,
                ttl_seconds=snapshot_ttl_seconds,
            )
            snapshot_id = snapshot.snapshot_id
        elif cursor is None:
            # A retained page may be reopened after a browser or application
            # restart; the exact fact identity comes back with the URL state.
            if not facts_identity:
                raise QueryCursorValidationError("retained Asset page requires its original facts identity")
        self._authorize(principal, scope)
        page: PageResult = self.read_store.read_query_snapshot_page(
            principal, scope, snapshot_id, query_identity, ASSET_READ_CAPABILITY,
            page_size=page_size, cursor=cursor,
        )
        rows = tuple(dict(row.payload) for row in page.rows)
        result_identity = _digest({
            "query_identity": query_identity,
            "first_ordinal": page.rows[0].ordinal if page.rows else 1,
            "rows": [(row.row_id, row.row_version) for row in page.rows],
        })
        return AssetPage(rows, page.total_row_count, page.snapshot_id, page.next_cursor, query_identity, result_identity, normalized_filters, normalized_order)

    def _list_rows_from_facts(
        self,
        source_facts: Sequence[Mapping[str, Any]],
        principal: Principal,
        scope: AccessScope,
        filters: Mapping[str, str],
        order: Sequence[Mapping[str, str]],
    ) -> tuple[VersionedReadRow, ...]:
        # The calculation is intentionally repeated from the same source facts
        # so snapshot payloads and their query identity are byte-for-byte bound.
        grouped: dict[str, list[tuple[Mapping[str, Any], InvestigationProfile]]] = {}
        for row in source_facts:
            profile = _profile(row)
            if profile is None:
                continue
            target = profile.target
            if scope.family_id is not None and target.family_identity != scope.family_id:
                continue
            if scope.site_id is not None and filters.get("site", scope.site_id) != scope.site_id:
                continue
            if scope.site_id is None and filters.get("site") is not None:
                continue
            grouped.setdefault(target.asset_identity, []).append((row, profile))
        assets: list[dict[str, Any]] = []
        source = self._source_summary(principal, scope, datetime.now(timezone.utc), authorize=False)
        for asset_id, members in grouped.items():
            members.sort(key=lambda pair: (pair[0]["known_at"], pair[0]["published_at"], pair[0]["entity_id"], pair[0]["revision_id"]))
            latest_row, latest_profile = members[-1]
            workflow_states = [item[0].get("workflow_state", {}) for item in members]
            states = [str(state.get("work_state", "UNKNOWN")) for state in workflow_states]
            assets.append({
                "asset_id": asset_id,
                "family_identity": latest_profile.target.family_identity,
                "context_identity": latest_profile.target.context_identity,
                "characteristic_identity": latest_profile.target.characteristic_identity,
                "unit_identity": latest_profile.target.unit_identity,
                "open_work_count": sum(state in {"OPEN", "CLAIMED", "ACKNOWLEDGED"} for state in states),
                "episode_count": len(members),
                "latest_episode_id": latest_row["entity_id"],
                "latest_revision_id": latest_row["revision_id"],
                "latest_known_at": latest_row["known_at"],
                "latest_published_at": latest_row["published_at"],
                "latest_episode_headline": latest_profile.change.headline,
                "latest_work_state": states[-1],
                "latest_owner": workflow_states[-1].get("owner"),
                "family_context": f"{latest_profile.target.family_identity} · {latest_profile.target.context_identity}",
                "characteristic_unit": f"{latest_profile.target.characteristic_identity} · {latest_profile.target.unit_identity}",
                "latest_episode_work": f"{latest_row['entity_id']} · {states[-1]} · owner {workflow_states[-1].get('owner') or 'unassigned'}",
                "source_status_age": "UNAVAILABLE · unknown",
                "source_age_display": "unknown",
                "source_state": "UNAVAILABLE",
                "source_age_seconds": None,
                "source_snapshot_id": None,
                "source_capability_state": "UNAVAILABLE",
            })
        assets = [
            row for row in assets
            if all(filters.get(field) in (None, value) for field, value in (
                ("family", row["family_identity"]),
                ("asset", row["asset_id"]),
                ("context", row["context_identity"]),
                ("characteristic", row["characteristic_identity"]),
            ))
        ]
        for row in assets:
            if row["family_identity"] == self.source_binding.family_id:
                row.update({
                    "source_state": source["state"],
                    "source_age_seconds": source["age_seconds"],
                    "source_snapshot_id": source["snapshot_id"],
                    "source_capability_state": source["state"],
                    "source_age_display": f"{source['age_seconds']} sec" if source.get("age_seconds") is not None else "unknown",
                    "source_status_age": f"{source['state']} · {source['age_seconds']} sec" if source.get("age_seconds") is not None else f"{source['state']} · age unknown",
                })
        return tuple(
            VersionedReadRow(
                item["asset_id"],
                _digest(item),
                {**item, "site_identity": scope.site_id, "source_binding_identity": self._binding_identity()},
            )
            for item in self._sort_assets(assets, order)
        )

    @staticmethod
    def _sort_assets(rows: Sequence[dict[str, Any]], order: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
        result = list(rows)
        for item in reversed(order):
            field, direction = item["field"], item["direction"]
            result.sort(key=lambda row: (row.get(field) is None, row.get(field) or ""), reverse=direction == "desc")
        if not any(item["field"] == "asset_id" for item in order):
            result.sort(key=lambda row: row["asset_id"])
        return result

    def get_asset_360(
        self,
        principal: Principal,
        scope: AccessScope,
        asset_id: str,
        *,
        knowledge_cutoff: datetime,
        window_start: datetime,
        window_end: datetime,
        characteristic_identity: str | None = None,
        unit_identity: str | None = None,
        peer_asset_id: str | None = None,
    ) -> Asset360Result:
        asset_id = _identity(asset_id, "asset_id")
        cutoff = _aware(knowledge_cutoff, "knowledge_cutoff")
        start, end = _aware(window_start, "window_start"), _aware(window_end, "window_end")
        if start > end or end > cutoff:
            raise ValidationFailureError("Asset measurement window must be bounded and no later than the knowledge cutoff")
        characteristic_identity = _identity(characteristic_identity, "characteristic_identity") if characteristic_identity is not None else None
        unit_identity = _identity(unit_identity, "unit_identity") if unit_identity is not None else None
        peer_asset_id = _identity(peer_asset_id, "peer_asset_id") if peer_asset_id is not None else None
        self._authorize(principal, scope)
        current = tuple(self.row_source.fetch_asset_episode_heads(principal, scope))
        if len(current) > MAX_ASSET_EPISODES:
            raise QueryTooBroadError("Qualified Episode heads exceed the bounded Asset projection", limit=MAX_ASSET_EPISODES)
        history = tuple(self.row_source.fetch_asset_episode_history(principal, scope, asset_id, cutoff, limit=MAX_ASSET_HISTORY_REVISIONS + 1))
        if len(history) > MAX_ASSET_HISTORY_REVISIONS:
            raise QueryTooBroadError("Asset Episode history exceeds the bounded timeline", limit=MAX_ASSET_HISTORY_REVISIONS)
        asset_history = [row for row in history if (profile := _profile(row)) is not None and profile.target.asset_identity == asset_id]
        if not asset_history:
            raise AggregateNotFoundError("Asset is not present in qualified Episode truth at the requested cutoff")
        profiles = [(row, _profile(row)) for row in asset_history]
        profiles = [(row, profile) for row, profile in profiles if profile is not None]
        latest_row, latest_profile = max(profiles, key=lambda pair: (pair[0]["known_at"], pair[0]["published_at"], pair[0]["entity_id"], pair[0]["revision_id"]))
        selected_characteristic = characteristic_identity or latest_profile.target.characteristic_identity
        selected_unit = unit_identity or latest_profile.target.unit_identity
        compatible_target_rows = [
            (row, profile) for row, profile in profiles
            if profile is not None and profile.target.characteristic_identity == selected_characteristic and profile.target.unit_identity == selected_unit
        ]
        if not compatible_target_rows:
            raise ValidationFailureError("selected characteristic and unit are not present in qualified Episode truth for this asset")
        latest_row, latest_profile = max(compatible_target_rows, key=lambda pair: (pair[0]["known_at"], pair[0]["published_at"], pair[0]["entity_id"], pair[0]["revision_id"]))
        source_summary, source_snapshot = self._source_view(principal, scope, cutoff, authorize=True)
        workflow_history = tuple(self.row_source.fetch_asset_workflow_versions(
            principal, scope, tuple(sorted({row["entity_id"] for row, _ in profiles})), cutoff,
            limit=MAX_ASSET_WORKFLOW_RECEIPTS + 1,
        ))
        if len(workflow_history) > MAX_ASSET_WORKFLOW_RECEIPTS:
            raise QueryTooBroadError("O5 Episode workflow history exceeds the bounded Asset view", limit=MAX_ASSET_WORKFLOW_RECEIPTS)
        workflows = self._workflow_as_of(asset_history, workflow_history, cutoff)
        episodes = self._timeline(profiles, workflows, cutoff)
        latest_by_episode: dict[str, dict[str, Any]] = {}
        for episode in episodes:
            prior = latest_by_episode.get(episode["episode_id"])
            if prior is None or (episode["known_at"], episode["published_at"], episode["revision_id"]) > (prior["known_at"], prior["published_at"], prior["revision_id"]):
                latest_by_episode[episode["episode_id"]] = episode
        open_work_count = sum(item["workflow_state"] in {"OPEN", "CLAIMED", "ACKNOWLEDGED"} for item in workflows.values())
        latest_episode = max(latest_by_episode.values(), key=lambda item: (item["known_at"], item["published_at"], item["episode_id"], item["revision_id"]))
        target = latest_profile.target.as_dict()
        source_summary["binding_identity"] = self._binding_identity()
        asset = {
            "asset_id": asset_id,
            "site_identity": scope.site_id,
            "family_identity": target["family_identity"],
            "context_identity": target["context_identity"],
            "characteristic_identity": selected_characteristic,
            "unit_identity": selected_unit,
            "open_work_count": open_work_count,
            "latest_episode_id": latest_episode["episode_id"],
            "latest_episode_revision_id": latest_episode["revision_id"],
            "latest_episode_known_at": latest_episode["known_at"],
            "latest_episode_published_at": latest_episode["published_at"],
            "latest_work_state": latest_episode["workflow_state"],
            "latest_owner": latest_episode["owner"],
            "knowledge_cutoff": cutoff,
        }
        measurement = self._measure(
            principal, scope, asset_id, target["family_identity"], target["context_identity"],
            selected_characteristic, selected_unit, start, end, cutoff, source_snapshot,
        )
        changes = self._changes(episodes)
        limitations = {
            "QUALIFIED_EPISODE_SCOPE_ONLY",
            "NO_COMPANY_ASSET_MASTER_COMPLETENESS_CLAIM",
            "NO_CAUSAL_OR_PREDICTIVE_INTERPRETATION",
            "MATERIAL_CONTEXT_UNAVAILABLE_WITHOUT_QUALIFIED_SOURCE",
        }
        if source_summary["state"] != "READY":
            limitations.add("SOURCE_CAPABILITY_NOT_READY")
        limitations.update(source_summary.get("limitations", ()))
        if measurement["limitations"]:
            limitations.update(measurement["limitations"])
        compare = None
        if peer_asset_id is not None:
            compare = self._compare(
                principal, scope, asset_id, peer_asset_id, cutoff, start, end,
                target["family_identity"], target["context_identity"], selected_characteristic,
                selected_unit, measurement, source_summary, source_snapshot,
            )
            if compare["state"] != "READY":
                limitations.add("PEER_COMPARISON_UNAVAILABLE")
        identity_facts = {
            "scope": scope.as_dict(),
            "asset": asset,
            "knowledge_cutoff": cutoff,
            "window": {"start": start, "end": end},
            "episodes": [
                {"episode_id": row["episode_id"], "revision_id": row["revision_id"], "known_at": row["known_at"], "published_at": row["published_at"], "workflow_version": row["workflow_version"], "workflow_state": row["workflow_state"]}
                for row in episodes
            ],
            "workflow_versions": {episode_id: value["workflow_version"] for episode_id, value in sorted(workflows.items())},
            "source_binding": self._binding_identity(),
            "source": source_summary,
            "measurement": measurement["identity_facts"],
            "selected_characteristic": selected_characteristic,
            "selected_unit": selected_unit,
            "compare": compare,
        }
        query_identity = _digest(identity_facts)
        result_identity = _digest({"query_identity": query_identity, "episodes": episodes, "changes": changes, "measurement": measurement, "compare": compare})
        return Asset360Result(
            asset_id, query_identity, result_identity, cutoff, asset, tuple(episodes), tuple(changes),
            source_summary, measurement, tuple(sorted(limitations)), compare,
        )

    def _source_summary(self, principal: Principal, scope: AccessScope, cutoff: datetime, *, authorize: bool) -> dict[str, Any]:
        return self._source_view(principal, scope, cutoff, authorize=authorize)[0]

    def _source_view(
        self,
        principal: Principal,
        scope: AccessScope,
        cutoff: datetime,
        *,
        authorize: bool,
    ) -> tuple[dict[str, Any], SourceSnapshotRecord | None]:
        unavailable = {
            "state": "UNAVAILABLE",
            "o4_state": "UNAVAILABLE",
            "age_seconds": None,
            "snapshot_id": None,
            "source_partition": None,
            "source_revision": None,
            "latest_event_at": None,
            "latest_available_at": None,
            "binding_identity": self._binding_identity(),
            "knowledge_cutoff": cutoff,
        }
        if self.source_binding.scope != scope:
            return ({**unavailable, "reason": "SOURCE_BINDING_SCOPE_MISMATCH", "limitations": ["SOURCE_BINDING_SCOPE_MISMATCH"]}, None)
        try:
            self.current_authorization.authorize(principal, scope, SOURCE_READ_CAPABILITY)
            lookup = getattr(self.source_store, "get_latest_snapshot_as_of", None)
            if not callable(lookup):
                return ({**unavailable, "reason": "O4_AS_OF_SNAPSHOT_READ_UNSUPPORTED", "limitations": ["O4_AS_OF_SNAPSHOT_READ_UNSUPPORTED"]}, None)
            snapshot = lookup(principal, self.source_binding, cutoff)
            if snapshot is None:
                reason = "O4_NO_ELIGIBLE_SOURCE_SNAPSHOT"
                return ({**unavailable, "reason": reason, "limitations": [reason]}, None)

            if snapshot.binding != self.source_binding or snapshot.published_at > cutoff or snapshot.available_cutoff > cutoff:
                return ({**unavailable, "reason": "O4_AS_OF_SNAPSHOT_IDENTITY_INVALID", "limitations": ["O4_AS_OF_SNAPSHOT_IDENTITY_INVALID"]}, None)

            freshness_limit = snapshot.freshness_age_seconds
            age = max(0, int((cutoff - snapshot.available_cutoff).total_seconds()))
            reason = ""
            limitations: list[str] = []
            if snapshot.status is SourceSnapshotStatus.PARTIAL and snapshot.row_count > 0:
                state, o4_state, reason = "PARTIAL", "PARTIAL", "PARTIAL_SNAPSHOT"
            elif snapshot.status in {SourceSnapshotStatus.INSUFFICIENT, SourceSnapshotStatus.QUARANTINED} or snapshot.row_count <= 0:
                state = "UNAVAILABLE"
                o4_state = "INSUFFICIENT" if snapshot.status is SourceSnapshotStatus.INSUFFICIENT or snapshot.row_count <= 0 else "UNAVAILABLE"
                reason = "NO_SUFFICIENT_SOURCE_ROWS" if o4_state == "INSUFFICIENT" else "SOURCE_QUARANTINED"
            elif freshness_limit is None:
                state, o4_state, reason = "UNAVAILABLE", "UNAVAILABLE", "O4_FRESHNESS_POLICY_NOT_RECONSTRUCTABLE"
                limitations.append("O4_FRESHNESS_POLICY_NOT_RECONSTRUCTABLE")
            else:
                state = "STALE" if age > freshness_limit else "READY"
                o4_state = state
                reason = "SOURCE_AVAILABILITY_EXCEEDS_FRESHNESS_LIMIT" if state == "STALE" else "FRESH_PUBLISHED_SNAPSHOT"
            if not limitations and state != "READY":
                limitations.append(reason or "O4_CAPABILITY_NOT_READY")
            return ({
                "state": state,
                "o4_state": o4_state,
                "age_seconds": age,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_manifest_hash": snapshot.manifest_hash,
                "snapshot_status": snapshot.status.value,
                "snapshot_row_count": snapshot.row_count,
                "source_partition": snapshot.source_partition,
                "source_revision": snapshot.source_revision,
                "binding_identity": self._binding_identity(),
                "source_id": snapshot.binding.source_id,
                "provider_id": snapshot.binding.provider_id,
                "family_id": snapshot.binding.family_id,
                "capability_id": snapshot.binding.capability_id,
                "adapter_id": snapshot.binding.adapter_id,
                "schema_id": snapshot.binding.schema_id,
                "mapping_version": snapshot.binding.mapping_version,
                "mapping_hash": snapshot.binding.mapping_hash,
                "reference_population_id": snapshot.binding.reference_population_id,
                "comparable_population_id": snapshot.binding.comparable_population_id,
                "latest_event_at": snapshot.event_end,
                "latest_available_at": snapshot.available_cutoff,
                "published_at": snapshot.published_at,
                "checked_at": None,
                "freshness_limit_seconds": freshness_limit,
                "reason": reason,
                "limitations": limitations,
                "knowledge_cutoff": cutoff,
            }, snapshot)
        except AuthorizationDeniedError:
            return ({**unavailable, "reason": "SOURCE_READ_PERMISSION_REQUIRED", "limitations": ["SOURCE_READ_PERMISSION_REQUIRED"]}, None)
        except Exception:
            if authorize:
                raise
            return ({**unavailable, "reason": "SOURCE_CAPABILITY_UNAVAILABLE", "limitations": ["SOURCE_CAPABILITY_UNAVAILABLE"]}, None)

    def _binding_identity(self) -> str:
        return _digest(self.source_binding.as_dict())

    def _measure(
        self, principal: Principal, scope: AccessScope, asset_id: str, family_id: str, context_id: str,
        characteristic_id: str, unit: str, start: datetime, end: datetime, cutoff: datetime,
        snapshot: SourceSnapshotRecord | None,
    ) -> dict[str, Any]:
        binding = self.source_binding
        if binding != self.source_observer.describe():
            raise SourceBindingUnavailableError("configured U1 observer does not exactly match the O4 source binding")
        snapshot_facts = {
            "snapshot_id": snapshot.snapshot_id,
            "source_partition": snapshot.source_partition,
            "source_revision": snapshot.source_revision,
            "manifest_hash": snapshot.manifest_hash,
            "binding_identity": self._binding_identity(),
        } if snapshot is not None else {
            "snapshot_id": None,
            "source_partition": None,
            "source_revision": None,
            "manifest_hash": None,
            "binding_identity": self._binding_identity(),
        }

        def unavailable(reason: str) -> dict[str, Any]:
            return {
                "state": "UNAVAILABLE",
                "points": [],
                **snapshot_facts,
                "binding_identity": self._binding_identity(),
                "limitations": [reason, "SOURCE_ROWS_NOT_PERSISTED_BY_EPHI"],
                "identity_facts": {
                    **snapshot_facts,
                    "observation_identity_set": [],
                    "window_start": start,
                    "window_end": end,
                    "knowledge_cutoff": cutoff,
                    "reason": reason,
                },
            }

        if binding.scope != scope or binding.family_id != family_id or binding.unit != unit:
            return unavailable("EXACT_SOURCE_BINDING_MISMATCH")
        try:
            self.current_authorization.authorize(principal, scope, SOURCE_READ_CAPABILITY)
        except AuthorizationDeniedError:
            return unavailable("SOURCE_READ_PERMISSION_REQUIRED")
        if snapshot is None:
            return unavailable("O4_NO_ELIGIBLE_SOURCE_REVISION")
        if snapshot.status is SourceSnapshotStatus.QUARANTINED:
            return unavailable("O4_SOURCE_SNAPSHOT_QUARANTINED")
        if snapshot.status is SourceSnapshotStatus.INSUFFICIENT or snapshot.row_count <= 0:
            return unavailable("O4_SOURCE_SNAPSHOT_INSUFFICIENT")
        reader = getattr(self.source_observer, "read_partition_revision", None)
        if not callable(reader):
            return unavailable("REVISION_PINNED_READ_UNSUPPORTED")
        try:
            batch = reader(
                source_partition=snapshot.source_partition,
                source_revision=snapshot.source_revision,
                start_at=start,
                end_at=end,
                limit=MAX_ASSET_OBSERVATION_POINTS + 1,
            )
        except Exception:
            return unavailable("REVISION_PINNED_READ_FAILED")
        if not isinstance(batch, RevisionPinnedObservationBatch):
            return unavailable("REVISION_PINNED_READ_UNPROVEN")
        if batch.source_partition != snapshot.source_partition or batch.source_revision != snapshot.source_revision:
            return unavailable("REVISION_PINNED_SOURCE_REVISION_MISMATCH")
        if batch.binding != binding:
            return unavailable("REVISION_PINNED_SOURCE_BINDING_MISMATCH")
        observations = batch.observations
        if len(observations) > MAX_ASSET_OBSERVATION_POINTS:
            raise QueryTooBroadError("bounded source observation result exceeds its point limit", limit=MAX_ASSET_OBSERVATION_POINTS)
        accepted: list[MetrologyObservation] = []
        for item in observations:
            if not isinstance(item, MetrologyObservation):
                return unavailable("REVISION_PINNED_OBSERVATION_INVALID")
            try:
                binding.validate_observation(item)
            except SourceQuarantineError:
                if item.unit != binding.unit:
                    continue
                return unavailable("REVISION_PINNED_OBSERVATION_BINDING_INVALID")
            if (item.asset_id, item.context_id, item.characteristic_id, item.unit) != (asset_id, context_id, characteristic_id, unit):
                continue
            if start <= item.event_at <= end and item.event_at <= cutoff and item.source_available_at <= cutoff:
                accepted.append(item)
        accepted.sort(key=lambda item: (item.event_at, item.source_available_at, item.source_row_id))
        point_ids = [_digest({
            "source_row_id": item.source_row_id,
            "asset_id": item.asset_id,
            "context_id": item.context_id,
            "characteristic_id": item.characteristic_id,
            "unit": item.unit,
            "value": item.value,
            "event_at": item.event_at,
            "source_available_at": item.source_available_at,
            "reference_population_id": item.reference_population_id,
            "comparable_population_id": item.comparable_population_id,
        }) for item in accepted]
        return {
            "state": "READY" if accepted else "PARTIAL",
            "points": [
                {
                    "value": item.value,
                    "event_at": item.event_at,
                    "source_available_at": item.source_available_at,
                    "source_row_id": item.source_row_id,
                    "unit": item.unit,
                    "reference_population_id": item.reference_population_id,
                    "comparable_population_id": item.comparable_population_id,
                }
                for item in accepted
            ],
            "binding_identity": self._binding_identity(),
            **snapshot_facts,
            "source_id": binding.source_id,
            "provider_id": binding.provider_id,
            "schema_id": binding.schema_id,
            "mapping_version": binding.mapping_version,
            "mapping_hash": binding.mapping_hash,
            "limitations": ["NO_INTERPOLATION_OR_SMOOTHING", "SOURCE_ROWS_NOT_PERSISTED_BY_EPHI"],
            "identity_facts": {
                **snapshot_facts,
                "observation_identity_set": point_ids,
                "window_start": start,
                "window_end": end,
                "knowledge_cutoff": cutoff,
            },
        }

    @staticmethod
    def _workflow_as_of(
        revisions: Sequence[Mapping[str, Any]], receipts: Sequence[Mapping[str, Any]], cutoff: datetime,
    ) -> dict[str, dict[str, Any]]:
        seed: dict[str, dict[str, Any]] = {}
        for row in revisions:
            episode_id = row["entity_id"]
            prior = seed.get(episode_id)
            if prior is None or (row["known_at"], row["published_at"], row["revision_id"]) > (prior["known_at"], prior["published_at"], prior["revision_id"]):
                state = row.get("workflow_state", {})
                seed[episode_id] = {
                    "state": dict(state), "workflow_version": int(row["workflow_version"]),
                    "known_at": row["known_at"], "published_at": row["published_at"],
                    "revision_id": row["revision_id"], "recorded_at": row["published_at"],
                }
        for row in receipts:
            episode_id = row["episode_id"]
            recorded_at = row["committed_at"]
            state = row.get("state")
            if not isinstance(state, Mapping) or not isinstance(recorded_at, datetime) or recorded_at > cutoff:
                continue
            candidate = {"state": dict(state), "workflow_version": int(row["workflow_version"]), "recorded_at": recorded_at}
            prior = seed.get(episode_id)
            if prior is None or int(candidate["workflow_version"]) >= int(prior["workflow_version"]):
                seed[episode_id] = candidate
        result: dict[str, dict[str, Any]] = {}
        for episode_id, item in seed.items():
            state = item["state"]
            loop = state.get("decision_loop", {})
            cycles = loop.get("cycles", []) if isinstance(loop, Mapping) else []
            actions, recovery, checks = [], [], []
            for cycle in cycles if isinstance(cycles, list) else []:
                if not isinstance(cycle, Mapping):
                    continue
                for action in (cycle.get("actions", {}) or {}).values() if isinstance(cycle.get("actions", {}), Mapping) else ():
                    if not isinstance(action, Mapping):
                        continue
                    recorded_at = _timestamp_from(action.get("recorded_at"))
                    if recorded_at is not None and recorded_at > cutoff:
                        continue
                    actions.append(dict(action))
                for plan in (cycle.get("recovery_plans", {}) or {}).values() if isinstance(cycle.get("recovery_plans", {}), Mapping) else ():
                    if not isinstance(plan, Mapping):
                        continue
                    recorded_at = _timestamp_from(plan.get("recorded_at", plan.get("locked_at")))
                    if recorded_at is not None and recorded_at > cutoff:
                        continue
                    recovery.append(dict(plan))
                for check in (cycle.get("checks", {}) or {}).values() if isinstance(cycle.get("checks", {}), Mapping) else ():
                    if isinstance(check, Mapping):
                        checks.append(dict(check))
            result[episode_id] = {
                "workflow_state": state.get("work_state", "UNKNOWN"),
                "owner": state.get("owner"),
                "workflow_version": item["workflow_version"],
                "recorded_at": item.get("recorded_at"),
                "actions": sorted(actions, key=lambda action: (action.get("recorded_at", ""), action.get("action_id", ""))),
                "recovery_plans": sorted(recovery, key=lambda plan: (plan.get("recorded_at", plan.get("locked_at", "")), plan.get("recovery_plan_id", ""))),
                "checks": sorted(checks, key=lambda check: (check.get("requested_at", ""), check.get("check_id", ""))),
                "reopen_history": list(loop.get("reopen_history", ())) if isinstance(loop, Mapping) else [],
                "closures": [closure for cycle in cycles if isinstance(cycle, Mapping) for closure in cycle.get("closures", ()) if isinstance(closure, Mapping)],
            }
        return result

    @staticmethod
    def _timeline(
        profile_rows: Sequence[tuple[Mapping[str, Any], InvestigationProfile | None]],
        workflows: Mapping[str, Mapping[str, Any]], cutoff: datetime,
    ) -> list[dict[str, Any]]:
        latest_revision: dict[str, str] = {}
        for row, profile in profile_rows:
            if profile is None:
                continue
            current = latest_revision.get(row["entity_id"])
            if current is None:
                latest_revision[row["entity_id"]] = row["revision_id"]
            else:
                match = next(item for item, _ in profile_rows if item["revision_id"] == current)
                if (row["known_at"], row["published_at"], row["revision_id"]) > (match["known_at"], match["published_at"], match["revision_id"]):
                    latest_revision[row["entity_id"]] = row["revision_id"]
        result: list[dict[str, Any]] = []
        for row, profile in profile_rows:
            if profile is None or row["known_at"] > cutoff or row["published_at"] > cutoff:
                continue
            episode_id = row["entity_id"]
            stored_workflow = dict(row.get("workflow_state", {}))
            latest = latest_revision.get(episode_id) == row["revision_id"]
            workflow = workflows.get(episode_id, {}) if latest else {}
            result.append({
                "episode_id": episode_id,
                "revision_id": row["revision_id"],
                "known_at": row["known_at"],
                "published_at": row["published_at"],
                "historical_revision": not latest,
                "workflow_label": "O5 workflow as known at cutoff" if latest else "Historical workflow snapshot at Episode publication",
                "workflow_state": workflow.get("workflow_state", stored_workflow.get("work_state", "UNKNOWN")),
                "owner": workflow.get("owner", stored_workflow.get("owner")),
                "workflow_version": workflow.get("workflow_version", row["workflow_version"]),
                "change": {"headline": profile.change.headline, "description": profile.change.description, "magnitude": profile.change.magnitude, "onset_at": profile.change.onset_at},
                "target": profile.target.as_dict(),
                "actions": list(workflow.get("actions", ())) if latest else [],
                "checks": list(workflow.get("checks", ())) if latest else [],
                "recovery_plans": list(workflow.get("recovery_plans", ())) if latest else [],
                "closures": list(workflow.get("closures", ())) if latest else [],
                "reopen_history": list(workflow.get("reopen_history", ())) if latest else [],
            })
        result.sort(key=lambda item: (item["known_at"], item["published_at"], item["episode_id"], item["revision_id"]))
        return result

    @staticmethod
    def _changes(episodes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for episode in episodes:
            for action in episode.get("actions", ()):
                changes.append({"kind": "ACTION", "episode_id": episode["episode_id"], **dict(action)})
            for plan in episode.get("recovery_plans", ()):
                changes.append({"kind": "RECOVERY", "episode_id": episode["episode_id"], **dict(plan)})
                for assessment in plan.get("assessments", ()):
                    if isinstance(assessment, Mapping):
                        observation = assessment.get("observation")
                        changes.append({
                            "kind": "RECOVERY_OBSERVATION", "episode_id": episode["episode_id"],
                            "recovery_plan_id": plan.get("recovery_plan_id"),
                            "recorded_at": assessment.get("recorded_at"),
                            "evaluated_at": assessment.get("evaluated_at"),
                            "eligibility": assessment.get("eligibility"),
                            "observation_identity": observation.get("observation_id") if isinstance(observation, Mapping) else None,
                        })
            for check in episode.get("checks", ()):
                changes.append({"kind": "CHECK", "episode_id": episode["episode_id"], **dict(check)})
            for closure in episode.get("closures", ()):
                changes.append({"kind": "WORK_CLOSURE", "episode_id": episode["episode_id"], **dict(closure)})
            for reopen in episode.get("reopen_history", ()):
                changes.append({"kind": "WORK_REOPEN", "episode_id": episode["episode_id"], **dict(reopen)})
        return sorted(changes, key=lambda item: (item.get("recorded_at", item.get("requested_at", item.get("closed_at", item.get("reopened_at", "")))), item["episode_id"], item.get("kind", "")))

    def _compare(
        self, principal: Principal, scope: AccessScope, primary_id: str, peer_id: str,
        cutoff: datetime, start: datetime, end: datetime, family_id: str, context_id: str,
        characteristic_id: str, unit: str, primary_measurement: Mapping[str, Any],
        source_summary: Mapping[str, Any], source_snapshot: SourceSnapshotRecord | None,
    ) -> dict[str, Any]:
        if peer_id == primary_id:
            return {"state": "BLOCKED", "reason": "SAME_ASSET_SELECTED", "primary_asset_id": primary_id, "peer_asset_id": peer_id}
        if source_summary["state"] != "READY":
            return {"state": "BLOCKED", "reason": "SOURCE_CAPABILITY_NOT_READY", "primary_asset_id": primary_id, "peer_asset_id": peer_id}
        if primary_measurement["state"] == "UNAVAILABLE":
            reason = next(iter(primary_measurement.get("limitations", ())), "PRIMARY_SOURCE_OBSERVATIONS_UNAVAILABLE")
            return {"state": "BLOCKED", "reason": reason, "primary_asset_id": primary_id, "peer_asset_id": peer_id}
        peer_history = tuple(self.row_source.fetch_asset_episode_history(principal, scope, peer_id, cutoff, limit=MAX_ASSET_HISTORY_REVISIONS + 1))
        if len(peer_history) > MAX_ASSET_HISTORY_REVISIONS:
            raise QueryTooBroadError("peer Episode history exceeds the bounded comparison read", limit=MAX_ASSET_HISTORY_REVISIONS)
        peers = [(_profile(row), row) for row in peer_history]
        peers = [(profile, row) for profile, row in peers if profile is not None and profile.target.asset_identity == peer_id]
        if not peers:
            return {"state": "BLOCKED", "reason": "PEER_NOT_IN_AUTHORIZED_QUALIFIED_EPISODE_SCOPE", "primary_asset_id": primary_id, "peer_asset_id": peer_id}
        latest_profile, _ = max(peers, key=lambda pair: (pair[1]["known_at"], pair[1]["published_at"], pair[1]["revision_id"]))
        target = latest_profile.target
        expected = (family_id, context_id, characteristic_id, unit)
        actual = (target.family_identity, target.context_identity, target.characteristic_identity, target.unit_identity)
        if actual != expected:
            return {"state": "BLOCKED", "reason": "FAMILY_CONTEXT_CHARACTERISTIC_OR_UNIT_MISMATCH", "primary_asset_id": primary_id, "peer_asset_id": peer_id, "expected": list(expected), "actual": list(actual)}
        population = self.source_binding.comparable_population_id or self.source_binding.reference_population_id
        if not population:
            return {"state": "BLOCKED", "reason": "QUALIFIED_REFERENCE_OR_COMPARABLE_IDENTITY_UNAVAILABLE", "primary_asset_id": primary_id, "peer_asset_id": peer_id}
        peer_measurement = self._measure(
            principal, scope, peer_id, family_id, context_id, characteristic_id, unit,
            start, end, cutoff, source_snapshot,
        )
        if peer_measurement["state"] == "UNAVAILABLE":
            reason = next(iter(peer_measurement.get("limitations", ())), "PEER_SOURCE_OBSERVATIONS_UNAVAILABLE")
            return {"state": "BLOCKED", "reason": reason, "primary_asset_id": primary_id, "peer_asset_id": peer_id}
        # Both asset series must actually carry the exact configured peer or
        # reference identity. No population broadening or conversion occurs.
        def qualified(points: Sequence[Mapping[str, Any]]) -> bool:
            return any(
                item.get("comparable_population_id") == population or item.get("reference_population_id") == population
                for item in points
            )
        if not qualified(primary_measurement.get("points", ())) or not qualified(peer_measurement.get("points", ())):
            return {"state": "BLOCKED", "reason": "OBSERVATIONS_DO_NOT_PROVE_EXACT_QUALIFIED_POPULATION", "primary_asset_id": primary_id, "peer_asset_id": peer_id, "population_identity": population}
        return {
            "state": "READY", "reason": None, "primary_asset_id": primary_id, "peer_asset_id": peer_id,
            "family_identity": family_id, "context_identity": context_id,
            "characteristic_identity": characteristic_id, "unit_identity": unit,
            "population_identity": population,
            "source_snapshot_identity": {
                key: primary_measurement["identity_facts"][key]
                for key in ("snapshot_id", "source_partition", "source_revision", "manifest_hash", "binding_identity")
            },
            "primary_observation_identity_set": primary_measurement["identity_facts"]["observation_identity_set"],
            "peer_observation_identity_set": peer_measurement["identity_facts"]["observation_identity_set"],
            "primary_points": primary_measurement["points"], "peer_points": peer_measurement["points"],
            "interpretation": "Descriptive paired asset observations only; no causal or predictive inference.",
        }


def _timestamp_from(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return _aware(value, "workflow timestamp")
    except ValidationFailureError:
        return None


__all__ = ["ASSET_READ_CAPABILITY", "Asset360QueryService", "Asset360Result", "AssetPage"]
