"""Reject ambiguous controller manifests before SDK credential discovery."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ownership_controller import load_configuration, run


class ConfigurationTests(unittest.TestCase):
    def document(self) -> dict[str, object]:
        return {
            "namespace": "telephony", "database_url": "postgresql://controller@db.example.test/opensips",
            "database_ca_bundle": None, "region": "us-east-2", "account": "123456789012",
            "instances": ["i-" + "1" * 17, "i-" + "2" * 17],
        }

    def test_manifest_retains_an_exact_instance_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.json"
            path.write_text(json.dumps(self.document()))
            config = load_configuration(path)
        self.assertEqual(config.namespace, "telephony")
        self.assertEqual(config.instances, frozenset({"i-" + "1" * 17, "i-" + "2" * 17}))

    def test_invalid_inputs_fail_before_sdk_import_or_discovery(self) -> None:
        changes = (
            ("account", "wrong"), ("region", "https://other.invalid"),
            ("namespace", "../other"), ("database_url", "http://db.example.test"),
            ("database_ca_bundle", "relative.pem"), ("instances", []),
            ("instances", ["i-" + "1" * 17] * 2), ("instances", [1]),
            ("instances", ["i-" + "1" * 17] * 65), ("extra", True),
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"boto3": None}):
            path = Path(directory) / "controller.json"
            for key, value in changes:
                with self.subTest(key=key, value=value):
                    document = self.document()
                    document[key] = value
                    path.write_text(json.dumps(document))
                    with self.assertRaises(ValueError):
                        asyncio.run(run(path, "i-" + "2" * 17, 0, uuid4()))

    def test_duplicate_keys_and_out_of_scope_candidates_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"boto3": None}):
            path = Path(directory) / "controller.json"
            path.write_text('{"namespace":"first","namespace":"second"}')
            with self.assertRaises(ValueError):
                load_configuration(path)
            path.write_text(json.dumps(self.document()))
            with self.assertRaises(ValueError):
                asyncio.run(run(path, "i-" + "3" * 17, 0, uuid4()))


if __name__ == "__main__":
    unittest.main()
