"""CHG-295/U3.7 deterministic installed release-slot control metadata tests."""

from __future__ import annotations

import json
import io
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ephi.release_slots import (  # noqa: E402
    ReleaseSlotFailure,
    _finalize_state,
    _slot_environment,
    _slot_location_identity,
    _state_body,
    initialize,
    main,
    read_selection,
    register,
    rollback,
    select,
    verify,
)


IDENTITY_A = "a" * 64
IDENTITY_B = "b" * 64
INPUTS = "c" * 64
COMMIT = "d" * 40
TREE = "e" * 40


def _preflight(identity: str = IDENTITY_A, inputs: str = INPUTS) -> dict[str, str]:
    return {
        "release_identity_sha256": identity,
        "install_inputs_sha256": inputs,
        "candidate_commit": COMMIT,
        "candidate_tree": TREE,
    }


class ReleaseSlotStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "selection.json"
        self.slot_a = self.root / "slot-a"
        self.slot_b = self.root / "slot-b"
        self.slot_a.mkdir()
        self.slot_b.mkdir()
        (self.root / "slot-c").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _initialize(self):
        with patch("ephi.release_slots._run_preflight", return_value=_preflight()):
            return initialize(self.state, "slot-a", self.slot_a, self.root, label="A", change_id="CHG-295")

    def _register_b(self, identity: str = IDENTITY_A):
        with patch("ephi.release_slots._run_preflight", return_value=_preflight(identity)):
            return register(self.state, 0, "slot-b", self.slot_b, self.root, label="B", change_id="U3.7")

    def test_initialize_register_select_and_rollback_increment_once(self):
        initial = self._initialize()
        self.assertEqual((initial["generation"], initial["current_slot_id"], initial["previous_slot_id"]), (0, "slot-a", None))
        self.assertRegex(initial["slots"]["slot-a"]["slot_location_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(initial["slots"]["slot-a"]["slot_location_id"], _slot_location_identity(self.slot_a))
        registered = self._register_b()
        self.assertEqual(registered["generation"], 1)
        self.assertEqual(registered["slots"]["slot-b"]["slot_location_id"], _slot_location_identity(self.slot_b))
        self.assertNotEqual(
            registered["slots"]["slot-a"]["slot_location_id"],
            registered["slots"]["slot-b"]["slot_location_id"],
        )
        with patch("ephi.release_slots._run_preflight", return_value=_preflight()):
            selected = select(self.state, 1, "slot-b", self.slot_b, self.root)
        self.assertEqual((selected["generation"], selected["current_slot_id"], selected["previous_slot_id"]), (2, "slot-b", "slot-a"))
        self.assertEqual(selected["last_transition_slot_id"], "slot-b")
        self.assertEqual(selected["last_transition_slot_location_id"], registered["slots"]["slot-b"]["slot_location_id"])
        wrong_rollback_state = self.state.read_bytes()
        with patch("ephi.release_slots._run_preflight") as preflight:
            with self.assertRaisesRegex(ReleaseSlotFailure, "SLOT_LOCATION_MISMATCH"):
                rollback(self.state, 2, self.slot_b, self.root)
        preflight.assert_not_called()
        self.assertEqual(self.state.read_bytes(), wrong_rollback_state)
        with patch("ephi.release_slots._run_preflight", return_value=_preflight()):
            rolled_back = rollback(self.state, 2, self.slot_a, self.root)
        self.assertEqual((rolled_back["generation"], rolled_back["current_slot_id"], rolled_back["previous_slot_id"]), (3, "slot-a", "slot-b"))
        self.assertEqual(rolled_back["slots"]["slot-a"]["release_identity_sha256"], IDENTITY_A)
        self.assertEqual(rolled_back["slots"]["slot-b"]["release_identity_sha256"], IDENTITY_A)
        self.assertEqual(rolled_back["last_transition_type"], "ROLLBACK")
        self.assertEqual(rolled_back["last_transition_slot_id"], "slot-a")
        self.assertEqual(rolled_back["last_transition_slot_location_id"], initial["slots"]["slot-a"]["slot_location_id"])
        self.assertRegex(rolled_back["last_transition_identity"], r"^[0-9a-f]{64}$")
        self.assertEqual(read_selection(self.state), rolled_back)
        serialized = self.state.read_text(encoding="utf-8")
        self.assertNotIn(str(self.slot_a), serialized)
        self.assertNotIn(str(self.slot_b), serialized)
        self.assertNotIn("postgresql://", serialized)
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(main(["read", "--state-file", str(self.state)]), 0)
        cli_report = json.loads(output.getvalue())
        self.assertNotIn(str(self.slot_a), json.dumps(cli_report))
        self.assertNotIn(str(self.slot_b), json.dumps(cli_report))
        self.assertEqual(
            cli_report["slots"]["slot-a"]["slot_location_id"],
            initial["slots"]["slot-a"]["slot_location_id"],
        )

    def test_competing_writers_with_one_generation_commit_only_one_transition(self):
        self._initialize()
        barrier = threading.Barrier(2)

        def attempt(slot_id: str, root: Path) -> str:
            barrier.wait()
            try:
                register(self.state, 0, slot_id, root, self.root)
            except ReleaseSlotFailure as exc:
                return exc.reason_code
            return "COMMITTED"

        with patch("ephi.release_slots._run_preflight", return_value=_preflight()):
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = {
                    executor.submit(attempt, "slot-b", self.slot_b),
                    executor.submit(attempt, "slot-c", self.root / "slot-c"),
                }
                results = [future.result(timeout=5) for future in outcomes]
        self.assertCountEqual(results, ["COMMITTED", "EXPECTED_GENERATION_MISMATCH"])
        current = read_selection(self.state)
        self.assertEqual(current["generation"], 1)
        self.assertEqual(len(current["slots"]), 2)

    def test_stale_generation_does_not_verify_or_change_prior_state(self):
        self._initialize()
        before = self.state.read_bytes()
        with patch("ephi.release_slots._run_preflight") as preflight:
            with self.assertRaisesRegex(ReleaseSlotFailure, "EXPECTED_GENERATION_MISMATCH"):
                register(self.state, 8, "slot-b", self.slot_b, self.root)
        preflight.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)

    def test_preflight_failure_keeps_prior_valid_state_untouched(self):
        self._initialize()
        before = self.state.read_bytes()
        with patch("ephi.release_slots._run_preflight", side_effect=ReleaseSlotFailure("SLOT_PREFLIGHT_FAILED")):
            with self.assertRaisesRegex(ReleaseSlotFailure, "SLOT_PREFLIGHT_FAILED"):
                verify(self.state, 0, "slot-a", self.slot_a, self.root)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(read_selection(self.state)["generation"], 0)

    def test_correct_registered_root_verifies_and_preserves_location_binding(self):
        self._initialize()
        self._register_b()
        with patch("ephi.release_slots._run_preflight", return_value=_preflight()):
            verified = verify(self.state, 1, "slot-b", self.slot_b, self.root)
        self.assertEqual(verified["generation"], 2)
        self.assertEqual(verified["last_transition_slot_id"], "slot-b")
        self.assertEqual(verified["last_transition_slot_location_id"], _slot_location_identity(self.slot_b))

    def test_duplicate_location_registration_fails_without_mutation(self):
        self._initialize()
        before = self.state.read_bytes()
        with patch("ephi.release_slots._run_preflight") as preflight:
            with self.assertRaisesRegex(ReleaseSlotFailure, "SLOT_LOCATION_ALREADY_REGISTERED"):
                register(self.state, 0, "slot-b", self.slot_a, self.root)
        preflight.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)

    def test_verify_and_select_swapped_roots_fail_without_mutation(self):
        self._initialize()
        self._register_b()
        before = self.state.read_bytes()
        for operation in (verify, select):
            with self.subTest(operation=operation.__name__):
                with patch("ephi.release_slots._run_preflight") as preflight:
                    with self.assertRaisesRegex(ReleaseSlotFailure, "SLOT_LOCATION_MISMATCH"):
                        operation(self.state, 1, "slot-b", self.slot_a, self.root)
                preflight.assert_not_called()
                self.assertEqual(self.state.read_bytes(), before)

    def test_malformed_and_tampered_location_identities_fail_state_validation(self):
        self._initialize()
        valid = self.state.read_bytes()
        malformed = read_selection(self.state)
        malformed["slots"]["slot-a"]["slot_location_id"] = "not-a-sha256"
        body = {key: value for key, value in malformed.items() if key != "state_sha256"}
        self.state.write_text(
            json.dumps(_finalize_state(body), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ReleaseSlotFailure, "STATE_MALFORMED_OR_TAMPERED"):
            read_selection(self.state)
        self.state.write_bytes(valid)
        tampered = read_selection(self.state)
        tampered["slots"]["slot-a"]["slot_location_id"] = "f" * 64
        tampered["last_transition_slot_location_id"] = "f" * 64
        body = {key: value for key, value in tampered.items() if key != "state_sha256"}
        self.state.write_text(
            json.dumps(_finalize_state(body), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ReleaseSlotFailure, "STATE_MALFORMED_OR_TAMPERED"):
            read_selection(self.state)

    def test_malformed_and_tampered_state_fail_closed(self):
        self._initialize()
        valid = self.state.read_bytes()
        self.state.write_bytes(b"{")
        with self.assertRaisesRegex(ReleaseSlotFailure, "STATE_MALFORMED_OR_TAMPERED"):
            read_selection(self.state)
        self.state.write_bytes(valid.replace(b'"generation":0', b'"generation":9'))
        with self.assertRaisesRegex(ReleaseSlotFailure, "STATE_MALFORMED_OR_TAMPERED"):
            read_selection(self.state)

    def test_missing_slot_and_failed_identity_check_leave_state_unchanged(self):
        self._initialize()
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ReleaseSlotFailure, "SLOT_NOT_REGISTERED"):
            select(self.state, 0, "slot-b", self.slot_b, self.root)
        self.assertEqual(self.state.read_bytes(), before)
        self._register_b()
        before = self.state.read_bytes()
        with patch("ephi.release_slots._run_preflight", return_value=_preflight("f" * 64)):
            with self.assertRaisesRegex(ReleaseSlotFailure, "SLOT_IDENTITY_MISMATCH"):
                verify(self.state, 1, "slot-b", self.slot_b, self.root)
        self.assertEqual(self.state.read_bytes(), before)

    def test_cross_release_identity_is_explicitly_not_qualified(self):
        self._initialize()
        self._register_b(IDENTITY_B)
        before = self.state.read_bytes()
        with patch("ephi.release_slots._run_preflight") as preflight:
            with self.assertRaisesRegex(ReleaseSlotFailure, "CROSS_RELEASE_COMPATIBILITY_NOT_QUALIFIED"):
                select(self.state, 1, "slot-b", self.slot_b, self.root)
        preflight.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)

    def test_atomic_fault_before_replace_keeps_one_complete_old_state(self):
        self._initialize()
        before = self.state.read_bytes()

        def fail_before_replace():
            raise ReleaseSlotFailure("ATOMIC_REPLACEMENT_INJECTED")

        with patch("ephi.release_slots._run_preflight", return_value=_preflight()):
            with self.assertRaisesRegex(ReleaseSlotFailure, "ATOMIC_REPLACEMENT_INJECTED"):
                register(
                    self.state,
                    0,
                    "slot-b",
                    self.slot_b,
                    self.root,
                    before_replace=fail_before_replace,
                )
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(read_selection(self.state)["generation"], 0)
        self.assertEqual(sorted(path.name for path in self.root.glob("selection.json*")), ["selection.json", "selection.json.lock"])

    def test_successful_replacement_exposes_one_canonical_complete_state(self):
        self._initialize()
        with patch("ephi.release_slots._run_preflight", return_value=_preflight()):
            state = register(self.state, 0, "slot-b", self.slot_b, self.root)
        raw = self.state.read_bytes()
        self.assertEqual(raw, json.dumps(state, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        self.assertEqual(json.loads(raw)["generation"], 1)
        self.assertEqual(sorted(path.name for path in self.root.glob("selection.json*")), ["selection.json", "selection.json.lock"])

    def test_slot_root_traversal_and_symlink_escape_are_rejected(self):
        self._initialize()
        traversal = str(self.root / "slot-a" / ".." / "slot-b")
        with self.assertRaisesRegex(ReleaseSlotFailure, "SLOT_ROOT_INVALID"):
            verify(self.state, 0, "slot-a", traversal, self.root)
        link = self.root / "slot-link"
        link.symlink_to(self.slot_a, target_is_directory=True)
        with self.assertRaisesRegex(ReleaseSlotFailure, "SLOT_ROOT_INVALID"):
            verify(self.state, 0, "slot-a", link, self.root)

    def test_venv_interpreter_symlink_keeps_slot_local_preflight_entrypoint(self):
        environment = self.root / "slot-c"
        binary = environment / "bin"
        binary.mkdir()
        python = binary / "python"
        python.symlink_to(sys.executable)
        preflight = binary / "ephi-release-preflight"
        preflight.write_text("#!/bin/sh\n", encoding="utf-8")
        root, actual_python, actual_preflight = _slot_environment(environment)
        self.assertEqual((root, actual_python, actual_preflight), (environment, python, preflight))

    def test_cli_failures_emit_fixed_secret_safe_reason_only(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            status = main(["read", "--state-file", str(self.root / "secret-dsn-user-password-host")])
        self.assertEqual(status, 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report, {
            "schema": "org.ephi.release-slot-report.v1",
            "status": "FAIL",
            "reason_code": "STATE_MISSING",
        })


if __name__ == "__main__":
    unittest.main()
