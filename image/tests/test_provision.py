from __future__ import annotations

import importlib.util
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("image_inputs", ROOT / "provision/inputs.py")
assert SPEC and SPEC.loader
INPUTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INPUTS)


class ProvisionTests(unittest.TestCase):
    def test_non_secret_inputs_reject_malformed_and_duplicate_values(self) -> None:
        good = {"version": "3.6.8", "commit": "a" * 40, "sha256": "b" * 64,
                "modules": ["tm", "sl"], "ua_sources": {name: "c" * 64 for name in INPUTS.UA_FILES},
                "placement_sources": {name: "d" * 64 for name in INPUTS.PLACEMENT_FILES}}
        self.assertEqual(INPUTS.validate(good), good)
        for bad in ([], {**good, "token": "unexpected"}, {**good, "commit": "main"},
                    {**good, "modules": []}, {**good, "modules": ["tm", "tm"]},
                    {**good, "modules": ["../escape"]}, {**good, "modules": [None]},
                    {**good, "ua_sources": {}},
                    {**good, "placement_sources": {}},
                    {**good, "ua_sources": {name: "bad" for name in INPUTS.UA_FILES}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                INPUTS.validate(bad)
        with self.assertRaises(ValueError):
            json.loads('{"version":1,"version":2}', object_pairs_hook=INPUTS.unique_object)

    def test_source_overrides_validate_every_input_before_replacing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "modules/b2b_entities"
            module.mkdir(parents=True)
            overrides = root / "overrides"
            overrides.mkdir()
            baseline = {name: hashlib.sha256(b"upstream").hexdigest() for name in INPUTS.UA_BASELINE}
            expected = {name: hashlib.sha256(b"reviewed").hexdigest() for name in INPUTS.UA_FILES}
            for name in baseline:
                (module / name).write_bytes(b"upstream")
            for name in expected:
                (overrides / name).write_bytes(b"reviewed")
            with patch.object(INPUTS, "UA_BASELINE", baseline):
                with self.assertRaisesRegex(ValueError, "source set"):
                    INPUTS.apply_ua_sources(root, overrides, {"../escape": "a" * 64})
                (overrides / "ua_storage.c").write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    INPUTS.apply_ua_sources(root, overrides, expected)
                self.assertEqual((module / "ua_api.c").read_bytes(), b"upstream")
                (overrides / "ua_storage.c").write_bytes(b"reviewed")
                (module / "ua_api.c").write_bytes(b"different release")
                with self.assertRaisesRegex(ValueError, "baseline"):
                    INPUTS.apply_ua_sources(root, overrides, expected)
                (module / "ua_api.c").write_bytes(b"upstream")
                INPUTS.apply_ua_sources(root, overrides, expected)
                self.assertTrue(all((module / name).read_bytes() == b"reviewed" for name in expected))

    def test_shell_phase_order_and_no_framework_dependency(self) -> None:
        template = (ROOT / "packer/opensips-arm64.pkr.hcl").read_text()
        self.assertNotIn('provisioner "ansible"', template)
        self.assertNotIn("hashicorp/ansible", (ROOT / "packer/plugins.pkr.hcl").read_text())
        phases = [template.index("provision.sh " + phase) for phase in
                  ("preflight", "dependencies", "build", "configure", "cleanup", "verify", "sanitize")]
        self.assertEqual(phases, sorted(phases))
        unit = (ROOT / "assets/opensips.service").read_text()
        self.assertNotIn("ExecReload=", unit)
        self.assertNotIn(" -FE ", unit)
        self.assertIn("opensips -F -f", unit)


if __name__ == "__main__":
    unittest.main()
