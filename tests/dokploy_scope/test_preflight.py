"""Offline evidence regressions, not successful live permission tests."""

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.dokploy_scope.preflight import (
    EVIDENCE,
    assess,
    main,
    permission_collisions,
    verify_sources,
)


class ScopePreflight(unittest.TestCase):
    def setUp(self):
        self.evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    def test_provisioning_403_is_not_a_scoped_identity_denial(self):
        report = assess(self.evidence)
        self.assertTrue(report["custom_role_provisioning_denied"])
        self.assertFalse(report["direct_scope_proven"])
        self.assertEqual(report["direct_scope_cases_run"], 0)
        self.assertFalse(report["production_admission"])

    def test_cleanup_was_observed_and_no_credential_was_issued(self):
        report = assess(self.evidence)
        self.assertTrue(report["custom_role_absent_after_attempt"])
        self.assertEqual(report["outstanding_resource_count"], 0)
        self.assertEqual(self.evidence["cleanup"]["created_resources"], [])
        self.assertEqual(self.evidence["cleanup"]["revoked_credentials"], [])

    def test_version_labels_do_not_identify_installed_source(self):
        report = assess(self.evidence)
        self.assertFalse(report["version_endpoints_agree"])
        self.assertFalse(report["installed_build_verified"])
        changed = copy.deepcopy(self.evidence)
        changed["observations"]["release_tag"] = changed["observations"]["version"]
        self.assertFalse(assess(changed)["pin_matches_installed_build"])

    def test_source_model_cannot_grant_image_change_without_generic_update(self):
        routes = self.evidence["source_model"]["routes"]
        self.assertEqual(permission_collisions(routes), ["service:create"])
        self.assertTrue(
            self.evidence["source_model"]["ordinary_read_returns_broad_application"]
        )
        self.assertIn(
            "envVars:write",
            self.evidence["source_model"]["member_base_grants_forbidden"],
        )

    def test_separate_manager_permissions_would_remove_only_the_source_collision(self):
        routes = copy.deepcopy(self.evidence["source_model"]["routes"])
        routes["application.saveDockerProvider"] = [
            "workshopControl:deploy-approved-image"
        ]
        self.assertEqual(permission_collisions(routes), [])
        self.assertFalse(assess(self.evidence)["production_admission"])

    def test_unrecognized_schema_cannot_be_evaluated_as_proof(self):
        with self.assertRaises(ValueError):
            assess({"schema": 2})

    def test_successful_provisioning_alone_never_admits_the_controller(self):
        changed = copy.deepcopy(self.evidence)
        changed["observations"]["custom_role_result"] = {"success": True}
        report = assess(changed)
        self.assertFalse(report["custom_role_provisioning_denied"])
        self.assertFalse(report["production_admission"])
        self.assertFalse(report["direct_scope_proven"])
        self.assertGreater(len(report["pending"]), 10)

    def test_failed_cleanup_remains_visible(self):
        changed = copy.deepcopy(self.evidence)
        name = changed["observations"]["custom_role_request"]["roleName"]
        changed["observations"]["custom_roles_after"] = [{"role": name}]
        changed["cleanup"]["outstanding_resources"] = [name]
        report = assess(changed)
        self.assertFalse(report["custom_role_absent_after_attempt"])
        self.assertEqual(report["outstanding_resource_count"], 1)

    def test_missing_or_tampered_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(OSError):
                verify_sources(root, self.evidence)
            (root / "application.ts").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify_sources(root, self.evidence)

    def test_cli_distinguishes_blocked_from_success_without_echoing_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            sentinel = "dummy-sensitive-response-never-emit"
            changed = copy.deepcopy(self.evidence)
            changed["observations"]["custom_role_result"]["details"] += sentinel
            path.write_text(json.dumps(changed), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--evidence", str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["state"], "BLOCKED")
            self.assertNotIn(sentinel, output.getvalue())

    def test_malformed_input_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            sentinel = "dummy-sensitive-parse-error-never-emit"
            path.write_text(sentinel, encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--evidence", str(path)])
            self.assertEqual(code, 1)
            self.assertNotIn(sentinel, output.getvalue())
            self.assertFalse(json.loads(output.getvalue())["production_admission"])
