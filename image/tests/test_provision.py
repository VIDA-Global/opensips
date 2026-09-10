from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("image_inputs", ROOT / "provision/inputs.py")
assert SPEC and SPEC.loader
INPUTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INPUTS)


class ProvisionTests(unittest.TestCase):
    def test_non_secret_inputs_reject_malformed_and_duplicate_values(self) -> None:
        good = {"version": "3.6.8", "commit": "a" * 40, "sha256": "b" * 64,
                "modules": ["tm", "sl"]}
        self.assertEqual(INPUTS.validate(good), good)
        for bad in ([], {**good, "token": "unexpected"}, {**good, "commit": "main"},
                    {**good, "modules": []}, {**good, "modules": ["tm", "tm"]},
                    {**good, "modules": ["../escape"]}, {**good, "modules": [None]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                INPUTS.validate(bad)
        with self.assertRaises(ValueError):
            json.loads('{"version":1,"version":2}', object_pairs_hook=INPUTS.unique_object)

    def test_shell_phase_order_and_no_framework_dependency(self) -> None:
        template = (ROOT / "packer/opensips-arm64.pkr.hcl").read_text()
        self.assertNotIn('provisioner "ansible"', template)
        self.assertNotIn("hashicorp/ansible", (ROOT / "packer/plugins.pkr.hcl").read_text())
        phases = [template.index("provision.sh " + phase) for phase in
                  ("preflight", "dependencies", "build", "configure", "cleanup", "verify", "sanitize")]
        self.assertEqual(phases, sorted(phases))
        unit = (ROOT / "assets/opensips.service").read_text()
        self.assertNotIn("ExecReload=", unit)


if __name__ == "__main__":
    unittest.main()
