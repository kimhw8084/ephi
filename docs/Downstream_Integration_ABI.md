# EPHI downstream integration ABI v1

Status: **U1 architecture contract and synthetic conformance path**. A passing
ABI check proves that one declared provider bundle matches public contract
metadata and can be composed through the supported example. It does not pass
G02, G06, G10, G12, the Port Gate, a company deployment gate, or Production.

## One-way architecture rule

EPHI is the released generic product. An intranet deployment pulls a released
EPHI package and supplies private adapters, configuration, secrets and
infrastructure bindings at its downstream composition root. Company code,
data, logs, artifacts and patches do not flow upstream. Engineers do not patch
released `src/ephi` modules or migrations in the company environment. If a
private integration requires a generic-core change, treat that as an ABI
incompatibility and request a future sanitized upstream release.

The downstream package is trusted deployment code. It can translate private
identity and source formats into public EPHI types, but it does not become a
second authorization, workflow, source, artifact, read, receipt, queue or
scientific authority. Existing authorization, source-ingress, artifact,
handoff, planner/recovery, transport, operations and PostgreSQL contracts keep
their authority.

Before this provider boundary is invoked in a restricted installation, run
`ephi-release-preflight`, then `ephi-config-preflight`, then
`ephi-downstream-preflight`. The release preflight checks the installed
release, dependency/Base pins, migration identity, configuration-contract
identity and the ABI facts below. The configuration preflight checks generic
runtime settings and O8 policy without importing a provider or connecting to
a company system. `ephi-downstream-preflight` remains the authority for
provider discovery, compatibility, safe-manifest generation and composition.

## Public imports and ABI versioning

The supported integration API is exported by:

- `ephi.downstream` for ABI metadata, provider protocols, bundle construction,
  discovery, validation, composition and preflight.
- `ephi.application` for typed authorization, source, artifact, handoff,
  planner, workflow and storage-neutral service contracts.
- `ephi.infrastructure` for the existing generic PostgreSQL and reference
  adapter exports used by the current composition.
- `ephi` for existing top-level generic contracts such as `RecoveryPolicy`.

Downstream packages must not import `ephi.*` implementation submodules or
copy their implementations. The synthetic example is checked against this
boundary.

The ABI identity is `org.ephi.downstream`, version `1.0.0`, independent of the
EPHI distribution version. Versions use `major.minor.patch`. EPHI v1 accepts
ABI major 1 and provider-contract major 1. An unknown ABI or provider major
fails closed. A minor or patch update is compatible only when the existing
required contract remains intact. Every category's required capability set is
fixed in v1. Namespaced `optional.*` capabilities are additive declarations;
they are included in the safe manifest hash but ignored by EPHI v1. They must
not change existing behavior. Unknown required capabilities are incompatible.

The canonical manifest contains the ABI/schema identity, the required
category list, each category's public contract ID/version/required and
optional capabilities, the policy schema plus downstream configuration
version, and public runtime class/version metadata. Bump the public policy
configuration version when a curated downstream configuration changes; keep
its private values out of the manifest. Its SHA-256
is computed over EPHI canonical JSON. It does not include implementation
objects, credentials, DSNs, tokens, cookies, usernames, private endpoints,
source schemas or rows, mapping values, bucket/share paths, signed URLs, or
internal object paths.

## Provider bundle and ownership

Construct one frozen `ProviderBundle` and return it from one explicit
`module:factory` entrypoint. Each non-optional category has a
`ProviderBinding(ProviderContract, implementation)` with public category
metadata (`PolicySchemaMetadata` for policy and `RuntimeCapabilities` for
runtime). There is no mutable
registry or arbitrary installed-package scan. EPHI validates the ABI and all
provider metadata before calling a provider. Loading and factory failures
produce fixed typed reason codes; exception text and private configuration are
not returned.

| Category | Public responsibility | EPHI authority that remains in control |
|---|---|---|
| `identity` | Resolve an operation-time `Principal` and `AccessScope`; resolve the current `Principal` for the presented subject. | `CurrentAuthorizationAuthority` compares exact subject, session and security revisions, presented grants, current grants and required capabilities. A newer identity never upgrades an old presented principal. |
| `source` | Implement `BoundedMetrologyObserver.describe()` and bounded `read_partition()` using canonical observations with explicit event and availability times. `SourceProviderBinding.expected_binding` must equal `describe()` exactly. | O4 `MetrologySourceBinding`, row/unit/identifier validation, immutable snapshots, capability state and temporal rules. The v1 source contract exposes bounded observation reads and no manufacturing command capability. |
| `artifacts` | Supply an `ArtifactBlobStore` and a catalog bound to the existing PostgreSQL adapter. | `ArtifactService`, current authorization, exact SHA-256 plus byte size, scope isolation and corruption checks. Possession of an ID/hash/URL is not permission. |
| `notifications` | Supply the existing `RecipientResolver` and `DeliveryChannelAdapter` ports. | O5 committed outbox projection, deduplication/material-change identity, current recipient authorization, worker receipts, retry and UNKNOWN/reconciliation. External messaging is at least once; exactly once is not promised. |
| `policy` | Supply schema `org.ephi.policy-configuration` v1 with existing `CheckTemplateCatalog`, `PlannerPolicy`, `RecoveryPolicy`, and typed family/context identities. | EPHI's generic validation, authorization, source qualification, temporal integrity, planner eligibility, evidence, recovery and workflow CAS. No policy callback or private detector is accepted. |
| `runtime` | State public target environment class, PostgreSQL major and contract version; open the existing PostgreSQL reference adapter using deployment-owned configuration. | `RuntimeSettings`, O8 transport/security, and the existing PostgreSQL command/read/source/artifact/workflow authorities. Metadata is preflight information, not readiness evidence. |

The U1 v1 `read_partition()` contract remains unchanged. Providers may
additionally implement the optional public
`RevisionPinnedBoundedMetrologyObserver.read_partition_revision()` protocol
and return a `RevisionPinnedObservationBatch` carrying the exact O4 binding,
source partition and source revision with its bounded canonical rows. This
capability is not added to the required source-provider capabilities or the
safe manifest. Asset 360 requires the optional method for historical
measurements and fails closed with a typed limitation when it is absent; it
never falls back to the ordinary live read.

The provider bundle may hold secrets and private paths in implementation-owned
objects. Those values must never be copied into `ProviderContract`,
`RuntimeCapabilities`, errors, or application logs. Contract fields accept
only bounded public identifiers and version/capability tokens. The manifest
serializer reads those typed fields only; it never serializes an implementation
or policy object.

## Composition and discovery

`compose_downstream(bundle, runtime_settings=...)` validates the complete ABI
before provider use, checks the current identity and exact source binding,
opens the declared PostgreSQL authority, checks its major version, then builds
the existing Attention, Episode, workflow, decision-loop, planner, handoff,
artifact and source-ingress services. The returned frozen composition exposes
those services and their authorities. It does not make a second server,
authorization layer or state store.

Set `EPHI_DOWNSTREAM_ENTRYPOINT=downstream_package.providers:build_bundle` to
use the same bundle in the application UI. The import grammar is one Python
module name, a colon, and one attribute name. The factory must be callable and
return exactly one `ProviderBundle`. The runtime does not search entry-point
groups, installed packages, current directories or public registries, and it
does not evaluate configuration text. Package installation and imports can be
performed offline beside the pulled EPHI release.

The existing explicit environment-backed identity/source path remains only for
development and test. QA and production fail closed without an explicit
downstream bundle. The synthetic example is never an automatic production
fallback.

## Implementing the public seams

Use `provider_contract(ProviderCategory.X)` to obtain the published required
descriptor and `ProviderBinding` to pair it with the implementation. The
bundle's ABI ID/version must be explicit. Use existing immutable application
types: do not introduce generic dictionaries for Principal grants, source
rows, artifacts, policy rules, or workflow mutations.

Identity resolution is operation-time. Browser submitted actor names, grants
and capabilities are untrusted. `resolve_current_principal(subject)` must
return the current server-resolved identity. EPHI retains the exact presented
Principal and denies revision or grant changes.

The source observer returns bounded `MetrologyObservation` values. It may
translate a private schema behind the adapter; public binding identity,
mapping version/hash and unit must match its exact description. It has no
manufacturing-control, setpoint or command role. A source read alone does not
qualify G02/G06.

Artifact implementations plug into `ArtifactBlobStore` and
`ArtifactCatalog`. Bind catalog metadata to the existing PostgreSQL adapter.
Do not put transport URLs or physical paths into artifact metadata.

Notification adapters receive deliveries only through the existing O5
handoff service. They do not receive a workflow mutation API. Preserve
recipient re-resolution and UNKNOWN/reconciliation behavior for ambiguous
external outcomes.

Policy configuration carries typed current EPHI values. Unknown policy schema
or major fails before composition. Keep family-specific qualification and
scientific policy out of U1; malformed values must be rejected by the generic
types and services.

Runtime providers keep secrets in deployment-owned configuration and return
only `RuntimeCapabilities` for public preflight. O8 origin, session, cookie,
proxy and TLS requirements remain in the existing runtime-security boundary.
Do not infer production readiness from a configured capability.

## Conformance and status meanings

After installing EPHI and the explicitly named downstream package, run:

```bash
python -m ephi.downstream --entrypoint downstream_package.providers:build_bundle --json
```

The CLI emits deterministic secret-safe JSON. It performs the composition
smoke by default and returns a successful process status only after compatible
providers compose against the declared PostgreSQL major. Use
`--contracts-only` to check metadata and loading without opening PostgreSQL;
that mode is not the U1 composition exit.

| Reason code | Meaning |
|---|---|
| `CONTRACT_PASS` | ABI and required provider contract metadata are compatible. A full CLI run also reports a passing composition smoke. |
| `MISSING_REQUIRED_PROVIDER` | One or more required categories are absent; categories are sorted and listed. |
| `INCOMPATIBLE_ABI` | ABI ID or major is unsupported. No provider method is used. |
| `INCOMPATIBLE_PROVIDER_CONTRACT` | A category, major, required capability set or typed provider interface is incompatible. |
| `POLICY_SCHEMA_UNSUPPORTED` | Curated policy configuration does not use the supported schema major. |
| `INVALID_ENTRYPOINT` | Discovery input is not one bounded `module:factory` name. |
| `PROVIDER_LOAD_ERROR` | The explicit import, factory, or factory return type failed. Raw details are suppressed. |
| `SOURCE_BINDING_MISMATCH` | The observer's canonical described binding does not equal its declared binding. |
| `COMPOSITION_FAIL_CLOSED` | Runtime, identity or service composition did not meet the declared contract. Raw details are suppressed. |

The report labels real family/source/science G02/G06, company identity/TLS,
production-like capacity G10 and G12/Port Gate/Production as NOT_RUN or
NOT_CLAIMED. `CONTRACT_PASS` does not assert deployment readiness.

## Synthetic private-style example

`examples/synthetic_downstream/provider.py` is deliberately outside
`src/ephi`. It implements all six categories with synthetic identity, one
bounded observer, the existing immutable artifact ports, a deterministic
recipient/channel, typed generic policy and a PostgreSQL 18 reference runtime.
It is discovered through the same `module:factory` path and uses no private
composition shortcut. `check_synthetic_boundary()` checks its public imports
and location. This deterministic check proves that the supported example
needs no forbidden core edit; it is not a cryptographic review of arbitrary
private package code.

## Private data and upstream issue process

Keep private schemas, mappings, credentials, source rows, user/group facts,
logs, artifact bytes/paths and endpoint details in the intranet package or its
secret/configuration system. Never add them to upstream manifests, preflight
output, logs, issue attachments or evidence.

If the generic contract cannot support a private integration, prepare a
sanitized upstream issue containing the public ABI/version, the generic
contract that was violated, a synthetic reproduction and the expected bounded
behavior. Do not export company code, data, logs, artifacts, patches, schemas,
identity facts or endpoint details. A necessary generic change is released by
EPHI upstream and then pulled downstream; it is not carried as a private core
patch.
