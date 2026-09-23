"""U1 downstream ABI compatibility, boundary, and secret-safety evidence."""

from __future__ import annotations

from dataclasses import replace
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi import AccessScope, Principal  # noqa: E402
from ephi.application import (  # noqa: E402
    ArtifactContentIdentity,
    ArtifactService,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    AuthorizationDeniedError,
    CurrentAuthorizationAuthority,
    ScopedArtifactReference,
    TargetContext,
)
from ephi.config import RuntimeSettings, downstream_entrypoint_from_environment  # noqa: E402
from ephi.downstream import (  # noqa: E402
    ABI_ID,
    ABI_VERSION,
    DownstreamFailure,
    DownstreamReasonCode,
    FamilyContextConfiguration,
    PolicyConfiguration,
    PolicySchemaMetadata,
    ProviderBinding,
    ProviderBundle,
    ProviderCategory,
    SourceProviderBinding,
    compose_downstream,
    load_provider_bundle,
    preflight,
    provider_contract,
    safe_manifest,
    safe_manifest_hash,
    validate_provider_bundle,
)
from ephi.downstream.boundary import check_synthetic_boundary  # noqa: E402
from ephi.infrastructure import FileArtifactBlobStore, SQLiteArtifactCatalog  # noqa: E402
from examples.synthetic_downstream.provider import SyntheticIdentityProvider, build_bundle  # noqa: E402


def _installed_module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class DownstreamManifestTests(unittest.TestCase):
    def test_manifest_hash_is_stable_across_bundle_rebuild_and_process_restart(self):
        first = safe_manifest_hash(build_bundle())
        second = safe_manifest_hash(build_bundle())
        self.assertEqual(first, second)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        code = "from examples.synthetic_downstream.provider import build_bundle; from ephi.downstream import safe_manifest_hash; print(safe_manifest_hash(build_bundle()))"
        one = subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, env=env, text=True).strip()
        two = subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, env=env, text=True).strip()
        self.assertEqual(first, one)
        self.assertEqual(one, two)

    def test_optional_additive_capability_is_ignored_but_hashed(self):
        bundle = build_bundle()
        binding = bundle.identity
        extended = replace(
            bundle,
            identity=ProviderBinding(
                replace(binding.contract, optional_capabilities=("optional.metrics.counter.v1",)),
                binding.implementation,
            ),
        )
        validate_provider_bundle(extended)
        self.assertNotEqual(safe_manifest_hash(bundle), safe_manifest_hash(extended))
        self.assertEqual(
            safe_manifest(extended)["providers"][0]["contract"]["optional_capabilities"],
            ["optional.metrics.counter.v1"],
        )

    def test_policy_configuration_version_is_public_metadata_and_part_of_hash(self):
        bundle = build_bundle()
        policy = bundle.policy
        evolved = replace(
            bundle,
            policy=ProviderBinding(
                policy.contract,
                policy.implementation,
                PolicySchemaMetadata("org.ephi.policy-configuration", "1.0.0", "1.1.0"),
            ),
        )
        validate_provider_bundle(evolved)
        self.assertNotEqual(safe_manifest_hash(bundle), safe_manifest_hash(evolved))
        with self.assertRaises(DownstreamFailure) as caught:
            validate_provider_bundle(
                replace(
                    bundle,
                    policy=ProviderBinding(
                        policy.contract,
                        policy.implementation,
                        PolicySchemaMetadata("org.ephi.policy-configuration", "1.0.0", "2.0.0"),
                    ),
                )
            )
        self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.POLICY_SCHEMA_UNSUPPORTED)

    def test_unknown_required_capability_is_incompatible(self):
        bundle = build_bundle()
        binding = bundle.identity
        altered = replace(
            bundle,
            identity=ProviderBinding(
                replace(binding.contract, required_capabilities=(*binding.contract.required_capabilities, "future.auth.override")),
                binding.implementation,
            ),
        )
        with self.assertRaises(DownstreamFailure) as caught:
            validate_provider_bundle(altered)
        self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.INCOMPATIBLE_PROVIDER_CONTRACT)
        self.assertEqual(caught.exception.categories, ("identity",))

    def test_safe_manifest_never_traverses_provider_private_fields(self):
        marker = "SYNTHETIC_SECRET_MARKER_DSN_TOKEN_COOKIE_PRIVATE_PATH"
        env = {
            "EPHI_TEST_POSTGRES_DSN": f"postgresql://user:{marker}@private.invalid/private_db",
            "EPHI_SYNTHETIC_ARTIFACT_ROOT": f"/private/{marker}",
        }
        with patch.dict(os.environ, env, clear=False):
            report = preflight("examples.synthetic_downstream.provider:build_bundle", compose=False)
        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn(marker, encoded)
        self.assertNotIn("private.invalid", encoded)
        self.assertNotIn("private_db", encoded)
        self.assertNotIn("private/", encoded)
        self.assertEqual(report["status_code"], "CONTRACT_PASS")
        self.assertEqual(report["safe_composition_smoke"]["status"], "NOT_RUN")

    def test_private_mapping_values_never_enter_the_manifest_or_preflight_json(self):
        marker = "PRIVATE_RAW_MAPPING_VALUE_MARKER"
        bundle = build_bundle()
        original = bundle.policy.implementation.configuration
        context = TargetContext("synthetic-target", marker, marker, marker)
        config = replace(
            original,
            family_contexts=(FamilyContextConfiguration("synthetic-u1-family", "1.0.0", (context,)),),
        )

        class PolicyAdapter:
            configuration = config

        altered = replace(
            bundle,
            policy=ProviderBinding(bundle.policy.contract, PolicyAdapter(), bundle.policy.public_metadata),
        )
        name = "u1_private_policy_fixture"
        module = _installed_module(name, build=lambda: altered)
        with patch.dict(sys.modules, {name: module}):
            report = preflight(f"{name}:build", compose=False)
        self.assertEqual(report["status_code"], "CONTRACT_PASS")
        self.assertNotIn(marker, json.dumps(report, ensure_ascii=False))

class ProviderValidationTests(unittest.TestCase):
    def test_unknown_abi_major_fails_before_provider_use(self):
        class CountingIdentity(SyntheticIdentityProvider):
            def __init__(self):
                self.calls = 0

            def resolve_principal(self):
                self.calls += 1
                return super().resolve_principal()

        bundle = build_bundle()
        identity = CountingIdentity()
        invalid = replace(
            bundle,
            abi_version="2.0.0",
            identity=ProviderBinding(bundle.identity.contract, identity),
        )
        with self.assertRaises(DownstreamFailure) as caught:
            validate_provider_bundle(invalid)
        self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.INCOMPATIBLE_ABI)
        self.assertEqual(identity.calls, 0)

    def test_each_required_category_fails_individually_with_a_bounded_reason(self):
        bundle = build_bundle()
        for category in ProviderCategory:
            with self.subTest(category=category.value):
                invalid = replace(bundle, **{category.value: None})
                with self.assertRaises(DownstreamFailure) as caught:
                    validate_provider_bundle(invalid)
                self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.MISSING_REQUIRED_PROVIDER)
                self.assertEqual(caught.exception.categories, (category.value,))

    def test_multiple_missing_categories_are_sorted_and_do_not_disclose_bundle_data(self):
        marker = "PRIVATE_CONFIG_MARKER_NOT_PUBLIC"
        bundle = build_bundle()
        invalid = replace(bundle, identity=None, policy=None, runtime=None)
        name = "u1_incomplete_provider_fixture"
        module = _installed_module(name, build=lambda: invalid)
        with patch.dict(sys.modules, {name: module}):
            report = preflight(f"{name}:build", compose=False)
        self.assertEqual(report["status_code"], "MISSING_REQUIRED_PROVIDER")
        self.assertEqual(report["compatibility"]["categories"], ["identity", "policy", "runtime"])
        self.assertNotIn(marker, json.dumps(report))
        self.assertEqual(report["providers"][0]["status"], "MISSING")

    def test_wrong_bundle_return_and_wrong_provider_object_fail_typed(self):
        name = "u1_wrong_bundle_fixture"
        module = _installed_module(name, build=lambda: {"identity": "not-a-bundle"})
        with patch.dict(sys.modules, {name: module}):
            with self.assertRaises(DownstreamFailure) as caught:
                load_provider_bundle(f"{name}:build")
        self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.PROVIDER_LOAD_ERROR)

        bundle = build_bundle()
        incompatible = replace(bundle, identity=ProviderBinding(bundle.identity.contract, object()))
        with self.assertRaises(DownstreamFailure) as caught:
            validate_provider_bundle(incompatible)
        self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.INCOMPATIBLE_PROVIDER_CONTRACT)
        self.assertEqual(caught.exception.categories, ("identity",))

    def test_import_and_factory_errors_are_structurally_sanitized(self):
        marker = "SYNTHETIC_SECRET_MARKER_PASSWORD_COOKIE_DSN"
        factory_name = "u1_factory_failure_fixture"
        factory_module = _installed_module(factory_name, build=lambda: (_ for _ in ()).throw(RuntimeError(marker)))
        with patch.dict(sys.modules, {factory_name: factory_module}):
            report = preflight(f"{factory_name}:build", compose=False)
        self.assertEqual(report["status_code"], "PROVIDER_LOAD_ERROR")
        self.assertNotIn(marker, json.dumps(report))

        import_name = "u1_import_failure_fixture"

        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                raise RuntimeError(marker)

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == import_name:
                    return importlib.machinery.ModuleSpec(fullname, Loader())
                return None

        finder = Finder()
        sys.meta_path.insert(0, finder)
        try:
            report = preflight(f"{import_name}:build", compose=False)
        finally:
            sys.meta_path.remove(finder)
            sys.modules.pop(import_name, None)
        self.assertEqual(report["status_code"], "PROVIDER_LOAD_ERROR")
        self.assertNotIn(marker, json.dumps(report))

    def test_provider_execution_errors_are_bounded_and_source_errors_fail_before_runtime_open(self):
        marker = "SYNTHETIC_SECRET_MARKER_PRIVATE_RUNTIME_DSN"
        bundle = build_bundle()

        class BrokenRuntime:
            capabilities = bundle.runtime.public_metadata

            def open_postgresql(self):
                raise RuntimeError(marker)

        runtime_bundle = replace(
            bundle,
            runtime=ProviderBinding(bundle.runtime.contract, BrokenRuntime(), bundle.runtime.public_metadata),
        )
        runtime_name = "u1_runtime_failure_fixture"
        runtime_module = _installed_module(runtime_name, build=lambda: runtime_bundle)
        with patch.dict(sys.modules, {runtime_name: runtime_module}):
            runtime_report = preflight(f"{runtime_name}:build")
        self.assertEqual(runtime_report["status_code"], "COMPOSITION_FAIL_CLOSED")
        self.assertNotIn(marker, json.dumps(runtime_report))

        class BrokenObserver:
            def describe(self):
                raise RuntimeError(marker)

            def read_partition(self, **_kwargs):
                return ()

        source = SourceProviderBinding(bundle.source.implementation.expected_binding, BrokenObserver())
        source_bundle = replace(bundle, source=ProviderBinding(bundle.source.contract, source))
        source_name = "u1_source_failure_fixture"
        source_module = _installed_module(source_name, build=lambda: source_bundle)
        with patch.dict(sys.modules, {source_name: source_module}):
            source_report = preflight(f"{source_name}:build")
        self.assertEqual(source_report["status_code"], "SOURCE_BINDING_MISMATCH")
        self.assertEqual(source_report["safe_composition_smoke"]["status"], "FAIL")
        self.assertNotIn(marker, json.dumps(source_report))

    def test_explicit_entrypoint_grammar_rejects_import_scanning_and_expressions(self):
        for entrypoint in ("foo", "foo:bar:baz", "foo.bar:factory()", "../private:factory", "foo :factory"):
            with self.subTest(entrypoint=entrypoint):
                with self.assertRaises(DownstreamFailure) as caught:
                    load_provider_bundle(entrypoint)
                self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.INVALID_ENTRYPOINT)

    def test_missing_non_development_bundle_does_not_use_fixture_fallback(self):
        with patch.dict(os.environ, {"EPHI_ENV": "production"}, clear=False):
            os.environ.pop("EPHI_DOWNSTREAM_ENTRYPOINT", None)
            with self.assertRaisesRegex(RuntimeError, "explicit downstream provider bundle"):
                downstream_entrypoint_from_environment()

    def test_explicit_non_development_bundle_is_returned_for_composition(self):
        self.assertEqual(
            downstream_entrypoint_from_environment(
                {"EPHI_ENV": "production", "EPHI_DOWNSTREAM_ENTRYPOINT": " package.factory:build "}
            ),
            "package.factory:build",
        )

    def test_development_and_test_may_use_the_retained_fallback(self):
        for environment in ("development", "test"):
            with self.subTest(environment=environment):
                self.assertEqual(downstream_entrypoint_from_environment({"EPHI_ENV": environment}), "")


class ExistingAuthorityPreservationTests(unittest.TestCase):
    def test_identity_resolves_current_principal_and_keeps_scope_capability_rules(self):
        identity = SyntheticIdentityProvider()
        current = identity.resolve_principal()
        authority = CurrentAuthorizationAuthority(identity.resolve_current_principal)
        scope = identity.resolve_scope()
        authority.authorize(current, scope, "ephi.attention.read")
        with self.assertRaises(AuthorizationDeniedError):
            authority.authorize(current, AccessScope("other-scope"), "ephi.attention.read")
        with self.assertRaises(AuthorizationDeniedError):
            authority.authorize(current, scope, "ungranted.capability")

    def test_revocation_revision_change_and_stale_presented_grants_fail_closed(self):
        identity = SyntheticIdentityProvider()
        presented = identity.resolve_principal()
        scope = identity.resolve_scope()
        authority = CurrentAuthorizationAuthority(identity.resolve_current_principal)
        with patch.dict(
            os.environ,
            {
                "EPHI_SYNTHETIC_REVOKED_CAPABILITIES": "ephi.attention.read",
                "EPHI_SYNTHETIC_SECURITY_REVISION": "2",
            },
            clear=False,
        ):
            with self.assertRaises(AuthorizationDeniedError):
                authority.authorize(presented, scope, "ephi.attention.read")
        for changed_identity in (
            {"EPHI_SYNTHETIC_SUBJECT": "different-subject"},
            {"EPHI_SYNTHETIC_AUTH_SESSION_REVISION": "2"},
        ):
            with self.subTest(changed_identity=changed_identity), patch.dict(os.environ, changed_identity, clear=False):
                with self.assertRaises(AuthorizationDeniedError):
                    authority.authorize(presented, scope, "ephi.episode.read")
        elevated_presented = Principal(
            presented.subject,
            (*presented.capabilities, "newly-added.capability"),
            (scope,),
            presented.auth_session_revision,
            presented.security_revision,
        )
        authority.authorize(elevated_presented, scope, "ephi.attention.read")
        with self.assertRaises(AuthorizationDeniedError):
            authority.authorize(elevated_presented, scope, "newly-added.capability")

        class CurrentProviderWithMoreGrants(SyntheticIdentityProvider):
            def resolve_current_principal(self, subject):
                current = super().resolve_current_principal(subject)
                return Principal(
                    current.subject,
                    (*current.capabilities, "newly-added.capability"),
                    current.scope_grants,
                    current.auth_session_revision,
                    current.security_revision,
                )

        stale_presented = identity.resolve_principal()
        upgraded_current = CurrentAuthorizationAuthority(CurrentProviderWithMoreGrants().resolve_current_principal)
        with self.assertRaises(AuthorizationDeniedError):
            upgraded_current.authorize(stale_presented, scope, "newly-added.capability")

    def test_short_common_and_non_ascii_generic_identities_remain_valid(self):
        scope = AccessScope("x", family_id="家庭")
        principal = Principal("é", ("read",), (scope,), "r1", "r1")
        self.assertTrue(principal.grants_scope(scope))
        config = PolicyConfiguration(
            "org.ephi.policy-configuration",
            "1.0.0",
            build_bundle().policy.implementation.configuration.check_catalog,
            build_bundle().policy.implementation.configuration.planner_policy,
            build_bundle().policy.implementation.configuration.recovery_policy,
            (FamilyContextConfiguration("家庭", "1.0.0", build_bundle().policy.implementation.configuration.family_contexts[0].contexts),),
        )
        self.assertEqual(config.family_contexts[0].family_id, "家庭")


class SourceBoundaryTests(unittest.TestCase):
    def test_synthetic_source_is_exact_read_only_observer(self):
        bundle = build_bundle()
        source = bundle.source.implementation
        self.assertEqual(source.observer.describe(), source.expected_binding)
        safe_source = safe_manifest(bundle)["providers"][1]["contract"]
        capabilities = safe_source["required_capabilities"]
        self.assertTrue(any("observation" in item for item in capabilities))
        self.assertFalse(any(any(word in item for word in ("command", "control", "actuator", "manufactur")) for item in capabilities))
        self.assertFalse(callable(getattr(source.observer, "command", None)))
        self.assertFalse(callable(getattr(source.observer, "write", None)))

    def test_each_public_source_binding_dimension_must_match_exactly(self):
        bundle = build_bundle()
        source = bundle.source.implementation
        original = source.expected_binding
        variants = (
            replace(original, source_id="synthetic-source-other"),
            replace(original, family_id="synthetic-family-other"),
            replace(original, scope=AccessScope("other-scope")),
            replace(original, schema_id="synthetic-schema-other"),
            replace(original, mapping_version="2.0.0"),
            replace(original, mapping_hash="1" * 64),
            replace(original, unit="mm"),
            replace(original, adapter_id="synthetic-adapter-other"),
        )
        for binding in variants:
            with self.subTest(binding=binding):
                altered = replace(bundle, source=ProviderBinding(bundle.source.contract, SourceProviderBinding(binding, source.observer)))
                with self.assertRaises(DownstreamFailure) as caught:
                    compose_downstream(altered, runtime_settings=RuntimeSettings(environment="development"))
                self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.SOURCE_BINDING_MISMATCH)
                self.assertEqual(caught.exception.categories, ("source",))


class ArtifactAndPolicyBoundaryTests(unittest.TestCase):
    def test_downstream_artifact_backend_keeps_hash_size_scope_and_integrity_rules(self):
        identity = SyntheticIdentityProvider()
        principal = identity.resolve_principal()
        scope = identity.resolve_scope()
        current = CurrentAuthorizationAuthority(identity.resolve_current_principal)
        with tempfile.TemporaryDirectory() as directory:
            blob_store = FileArtifactBlobStore(Path(directory) / "private-object-root")
            catalog = SQLiteArtifactCatalog(Path(directory) / "catalog.sqlite3")
            try:
                service = ArtifactService(blob_store, catalog, current)
                content = b"downstream exact bytes"
                result = service.write_and_register(
                    principal,
                    scope,
                    content,
                    media_type="application/octet-stream",
                    logical_purpose="synthetic-test",
                    required_write_capability="synthetic.artifact.write",
                )
                self.assertEqual(result.metadata.reference.content, ArtifactContentIdentity.from_bytes(content))
                self.assertEqual(service.retrieve(principal, result.metadata.reference, "synthetic.artifact.read").content, content)
                other = Principal(principal.subject, principal.capabilities, (AccessScope("other-scope"),), 1, 1)
                with self.assertRaises(AuthorizationDeniedError):
                    service.retrieve(other, result.metadata.reference, "synthetic.artifact.read")
                absent = ScopedArtifactReference(scope, ArtifactContentIdentity.from_bytes(b"missing"))
                with self.assertRaises(ArtifactNotFoundError):
                    service.retrieve(principal, absent, "synthetic.artifact.read")
                stored = result.metadata.reference
                object_path = blob_store.root / stored.sha256[:2] / stored.sha256[2:]
                object_path.write_bytes(b"corrupt")
                with self.assertRaises(ArtifactIntegrityError):
                    service.retrieve(principal, stored, "synthetic.artifact.read")
            finally:
                catalog.close()

    def test_unknown_policy_schema_and_malformed_typed_values_fail_closed(self):
        bundle = build_bundle()
        policy_binding = bundle.policy
        policy = policy_binding.implementation.configuration
        unsupported = replace(policy, version="2.0.0")

        class PolicyAdapter:
            configuration = unsupported

        changed = replace(
            bundle,
            policy=ProviderBinding(
                policy_binding.contract,
                PolicyAdapter(),
                PolicySchemaMetadata(unsupported.schema_id, unsupported.version),
            ),
        )
        with self.assertRaises(DownstreamFailure) as caught:
            validate_provider_bundle(changed)
        self.assertEqual(caught.exception.reason_code, DownstreamReasonCode.POLICY_SCHEMA_UNSUPPORTED)

        supported = build_bundle().policy.implementation.configuration
        self.assertEqual(supported.check_catalog.templates[0].template_id, "synthetic-check")
        self.assertEqual(supported.planner_policy.policy_id, "synthetic-planner")
        self.assertEqual(supported.recovery_policy.policy_id, "W0_DETERMINISTIC_REGRESSION")

        for kwargs in (
            {"check_catalog": object()},
            {"planner_policy": object()},
            {"recovery_policy": object()},
        ):
            with self.subTest(kwargs=tuple(kwargs)):
                values = {
                    "schema_id": policy.schema_id,
                    "version": policy.version,
                    "check_catalog": policy.check_catalog,
                    "planner_policy": policy.planner_policy,
                    "recovery_policy": policy.recovery_policy,
                    "family_contexts": policy.family_contexts,
                }
                values.update(kwargs)
                with self.assertRaises((TypeError, ValueError)):
                    PolicyConfiguration(**values)


class DiscoveryAndPreflightTests(unittest.TestCase):
    def test_synthetic_package_uses_public_imports_and_stays_outside_product(self):
        result = check_synthetic_boundary(ROOT)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["public_imports"], "PASS")
        self.assertEqual(result["core_edit_boundary"], "PASS")
        self.assertTrue(all(path.startswith("examples/synthetic_downstream/") for path in result["fixture_files"]))

    def test_empty_cli_and_incompatible_bundle_have_deterministic_typed_states(self):
        missing = preflight(None, compose=False)
        self.assertEqual(missing["status_code"], "MISSING_REQUIRED_PROVIDER")
        self.assertEqual(missing["compatibility"]["categories"], ["artifacts", "identity", "notifications", "policy", "runtime", "source"])
        incompatible_name = "u1_incompatible_abi_fixture"
        incompatible_bundle = replace(build_bundle(), abi_version="9.0.0")
        module = _installed_module(incompatible_name, build=lambda: incompatible_bundle)
        with patch.dict(sys.modules, {incompatible_name: module}):
            incompatible = preflight(f"{incompatible_name}:build", compose=False)
        self.assertEqual(incompatible["status_code"], "INCOMPATIBLE_ABI")
        self.assertEqual(incompatible["compatibility"]["verdict"], "INCOMPATIBLE_ABI")
        for report in (missing, incompatible):
            self.assertEqual(report["target_qualification"]["real_family_source_science_g02_g06"], "NOT_RUN")
            self.assertEqual(report["target_qualification"]["g12_port_gate_production"], "NOT_CLAIMED")

    def test_manifest_safe_runtime_and_source_facts_are_only_public_metadata(self):
        bundle = build_bundle()
        report = preflight("examples.synthetic_downstream.provider:build_bundle", compose=False)
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        for forbidden in ("password", "token", "cookie", "postgresql://", "object-root", "synthetic-row-1", "private.invalid"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(report["downstream_abi"]["id"], ABI_ID)
        self.assertEqual(report["downstream_abi"]["version"], ABI_VERSION)
        self.assertEqual(report["downstream_abi"]["safe_manifest_hash"], safe_manifest_hash(bundle))
        self.assertEqual(len(report["providers"]), 6)


if __name__ == "__main__":
    unittest.main()
