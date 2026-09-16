# Selected audited EPHI source excerpts

Source: original uploaded archive, unchanged. Line numbers refer to the archive source root. These excerpts support the associated findings; surrounding behavior and scope are discussed in `02_Source_Audit.md`.

## F01 — `company_port/src/ephi_company/services.py:26–34`

```text
  26 | def build_services(config: EphiConfig) -> CompanyServices:
  27 |     """Compose company-backed materialized read services.
  28 | 
  29 |     The generic API must not query raw manufacturing history per request. Build these
  30 |     services from approved shared state/materializations and immutable artifacts.
  31 |     """
  32 |     raise CompanyPortNotConfiguredError(
  33 |         "wire company-backed Advisory/History/Value/Operations services"
  34 |     )
```

## F02 — `src/ephi/advisory/service.py:63–78`

```text
  63 |     def upsert(self, source: AdvisoryEpisodeSource) -> None:
  64 |         self._items[source.episode.episode_id] = source
  65 | 
  66 | 
  67 | @dataclass(slots=True)
  68 | class AdvisoryService:
  69 |     """Read-model + workflow facade over authoritative EPHI domain objects."""
  70 | 
  71 |     sources: AdvisorySourceRepository = field(default_factory=InMemoryAdvisorySourceRepository)
  72 |     workflow: WorkflowService = field(default_factory=WorkflowService)
  73 |     presentation_policy: PresentationPolicy = field(default_factory=PresentationPolicy)
  74 |     _versions: dict[str, int] = field(default_factory=dict)
  75 |     _missed_events: list[MissedEventReport] = field(default_factory=list)
  76 |     _missed_counter: int = 0
  77 |     _view_cache: dict[tuple[str, int, str], EpisodeView] = field(default_factory=dict)
  78 |     _analytical_revisions: dict[str, list[AnalyticalRevision]] = field(default_factory=dict)
```

## F02 — `src/ephi/advisory/service.py:106–118`

```text
 106 |             self._view_cache.pop(key, None)
 107 | 
 108 |     def get_view(self, episode_id: str) -> EpisodeView:
 109 |         source = self.sources.get(episode_id)
 110 |         if source is None:
 111 |             raise KeyError(f"unknown episode: {episode_id}")
 112 |         version = self._versions[episode_id]
 113 |         key = (episode_id, version, self.presentation_policy.version)
 114 |         cached = self._view_cache.get(key)
 115 |         if cached is not None:
 116 |             return cached
 117 |         view = build_episode_view(
 118 |             source.episode,
```

## F03/F07 — `src/ephi/advisory/service.py:181–231`

```text
 181 |     def list_attention(self) -> tuple[AttentionQueueItem, ...]:
 182 |         items: list[AttentionQueueItem] = []
 183 |         for episode_id in self.sources.list_episode_ids():
 184 |             view = self.get_view(episode_id)
 185 |             if view.header.state == "RESOLVED" or view.workflow.state.value in {
 186 |                 "RESOLVED",
 187 |                 "BENIGN",
 188 |                 "DATA_ISSUE",
 189 |                 "DUPLICATE",
 190 |             }:
 191 |                 continue
 192 |             items.append(
 193 |                 AttentionQueueItem(
 194 |                     episode_id=episode_id,
 195 |                     asset_id=view.header.asset_id,
 196 |                     short_title=view.short_title,
 197 |                     technical_severity=view.header.technical_severity,
 198 |                     operational_priority=view.header.operational_priority,
 199 |                     confidence=view.header.confidence,
 200 |                     leading_hypothesis=view.header.leading_hypothesis,
 201 |                     opened_at=view.header.opened_at,
 202 |                     last_updated_at=view.header.last_updated_at,
 203 |                     trend=view.header.trend,
 204 |                     workflow_state=view.workflow.state,
 205 |                     future_preventable=view.exposure.future_preventable,
 206 |                 )
 207 |             )
 208 |         priority_rank = {
 209 |             OperationalPriority.P1: 0,
 210 |             OperationalPriority.P2: 1,
 211 |             OperationalPriority.P3: 2,
 212 |             OperationalPriority.P4: 3,
 213 |         }
 214 |         return tuple(
 215 |             sorted(
 216 |                 items,
 217 |                 key=lambda x: (
 218 |                     priority_rank.get(x.operational_priority, 9),
 219 |                     -{
 220 |                         TechnicalSeverity.UNKNOWN: 0,
 221 |                         TechnicalSeverity.NORMAL: 1,
 222 |                         TechnicalSeverity.OBSERVE: 2,
 223 |                         TechnicalSeverity.MONITOR: 3,
 224 |                         TechnicalSeverity.INVESTIGATE: 4,
 225 |                         TechnicalSeverity.URGENT: 5,
 226 |                     }[x.technical_severity],
 227 |                     -x.confidence,
 228 |                     x.opened_at,
 229 |                 ),
 230 |             )
 231 |         )
```

## F04 — `src/ephi/value/ledger.py:99–104`

```text
  99 | 
 100 |     def active_entries(self, episode_id: str | None = None) -> tuple[ValueLedgerEntry, ...]:
 101 |         return tuple(
 102 |             item for item in self.entries(episode_id)
 103 |             if item.ledger_entry_id not in self._superseded
 104 |         )
```

## F04 — `src/ephi/value/service.py:437–440`

```text
 437 | 
 438 |     def episode_summary(self, episode_id: str, *, as_of: datetime | None = None) -> EpisodeValueSummary:
 439 |         as_of = as_of or datetime.now(timezone.utc)
 440 |         entries = tuple(item for item in self.repository.active_entries(episode_id) if item.computed_at <= as_of)
```

## F05 — `src/ephi/episodes/service.py:67–91`

```text
  67 |         # Recovery requires sustained NORMAL/OBSERVE evidence. MONITOR does not count as healthy.
  68 |         if assessment.severity <= HealthSeverity.OBSERVE:
  69 |             recovery_count = current.consecutive_recovery + 1
  70 |             if recovery_count >= self.config.recovery_required:
  71 |                 closed = replace(
  72 |                     current,
  73 |                     last_updated_at=assessment.event_time,
  74 |                     state=EpisodeState.RESOLVED,
  75 |                     consecutive_recovery=recovery_count,
  76 |                     closed_at=assessment.event_time,
  77 |                 )
  78 |                 self._history.append(closed)
  79 |                 del self._active[key]
  80 |                 return closed
  81 |             recovering = replace(
  82 |                 current,
  83 |                 last_updated_at=assessment.event_time,
  84 |                 state=EpisodeState.RECOVERING,
  85 |                 consecutive_recovery=recovery_count,
  86 |             )
  87 |             self._active[key] = recovering
  88 |             return recovering
  89 | 
  90 |         return current
  91 | 
```

## F08 — `src/ephi/advisory/read_models.py:220–244`

```text
 220 | def _recommendations(hypothesis: Hypothesis) -> tuple[VerificationRecommendation, ...]:
 221 |     recipes: dict[Hypothesis, tuple[tuple[str, str, tuple[Hypothesis, ...]], ...]] = {
 222 |         Hypothesis.METROLOGY_SYSTEM_SHIFT: (
 223 |             (
 224 |                 "Run or review a reference-standard measurement on a matched healthy head/tool.",
 225 |                 "Separates head-local measurement bias from a real production-material shift.",
 226 |                 (Hypothesis.METROLOGY_SYSTEM_SHIFT, Hypothesis.COMMON_MODE_PROCESS_CHANGE),
 227 |             ),
 228 |             (
 229 |                 "Compare the same material/characteristic on a compatible metrology peer.",
 230 |                 "Cross-tool reproducibility is strongly discriminating for measurement-system attribution.",
 231 |                 (Hypothesis.METROLOGY_SYSTEM_SHIFT, Hypothesis.LOCAL_ASSET_DEGRADATION),
 232 |             ),
 233 |         ),
 234 |         Hypothesis.LOCAL_ASSET_DEGRADATION: (
 235 |             (
 236 |                 "Inspect the dominant physical feature/subsystem against the healthy reference and matched peers.",
 237 |                 "Confirms that the deviation is local rather than context or fleet common mode.",
 238 |                 (Hypothesis.LOCAL_ASSET_DEGRADATION, Hypothesis.COMMON_MODE_PROCESS_CHANGE),
 239 |             ),
 240 |             (
 241 |                 "Review maintenance/calibration/component events around the estimated onset.",
 242 |                 "Tests whether an intervention or wear transition explains the state change.",
 243 |                 (Hypothesis.LOCAL_ASSET_DEGRADATION, Hypothesis.MAINTENANCE_TRANSITION),
 244 |             ),
```
