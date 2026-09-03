from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "ansible/roles/opensips_ami/files/opensips-runtime-config.py"
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *args, **kwargs: None))
SPEC = importlib.util.spec_from_file_location("opensips_runtime_config", MODULE_PATH)
assert SPEC and SPEC.loader
RUNTIME = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME)


class RuntimeConfigurationTests(unittest.TestCase):
    def test_minimal_secret(self) -> None:
        files = RUNTIME.validate_secret({"schema_version": 1, "opensips_config": "log_stderror=yes"})
        self.assertEqual(files, {"opensips.cfg": "log_stderror=yes"})

    def test_tls_bundle(self) -> None:
        files = RUNTIME.validate_secret(
            {
                "schema_version": 1,
                "opensips_config": "log_stderror=yes",
                "tls": {"certificate": "cert", "private_key": "key", "ca_bundle": "ca"},
            }
        )
        self.assertEqual(files["tls/private-key.pem"], "key")

    def test_unknown_keys_are_rejected(self) -> None:
        with self.assertRaises(RUNTIME.ConfigurationError):
            RUNTIME.validate_secret({"schema_version": 1, "opensips_config": "x", "unexpected": True})

    def test_incomplete_tls_is_rejected(self) -> None:
        with self.assertRaises(RUNTIME.ConfigurationError):
            RUNTIME.validate_secret(
                {"schema_version": 1, "opensips_config": "x", "tls": {"private_key": "secret"}}
            )

    def test_identity_requires_pinned_secret_version(self) -> None:
        responses = {
            "api/token": b"token",
            "meta-data/tags/instance/OpenSIPSConfigSecretArn": (
                b"arn:aws:secretsmanager:us-east-2:123456789012:secret:opensips-test"
            ),
            "meta-data/tags/instance/OpenSIPSConfigSecretVersion": b"12345678-1234-1234-1234-123456789012",
            "dynamic/instance-identity/document": b'{"region":"us-east-2","accountId":"123456789012"}',
        }
        original = RUNTIME.imds_request
        RUNTIME.imds_request = lambda path, token=None, method="GET": responses[path]
        self.addCleanup(setattr, RUNTIME, "imds_request", original)
        identity = RUNTIME.instance_identity()
        self.assertEqual(identity[1], "12345678-1234-1234-1234-123456789012")

    def test_secret_version_must_match_response(self) -> None:
        class Client:
            def get_secret_value(self, **kwargs):
                return {"VersionId": "different-version-id-000000000000", "SecretString": "{}"}

        original = RUNTIME.boto3.client
        RUNTIME.boto3.client = lambda *args, **kwargs: Client()
        self.addCleanup(setattr, RUNTIME.boto3, "client", original)
        with self.assertRaises(RUNTIME.ConfigurationError):
            RUNTIME.get_secret(
                "arn:aws:secretsmanager:us-east-2:123456789012:secret:opensips-test",
                "12345678-1234-1234-1234-123456789012",
                "us-east-2",
                "123456789012",
            )

    def test_interrupted_update_restores_previous_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = RUNTIME.RUNTIME_ROOT
            RUNTIME.RUNTIME_ROOT = Path(directory) / "config"
            self.addCleanup(setattr, RUNTIME, "RUNTIME_ROOT", original)
            RUNTIME.RUNTIME_ROOT.mkdir()
            (RUNTIME.RUNTIME_ROOT / "opensips.cfg").write_text("new", encoding="utf-8")
            previous = Path(directory) / ".config-previous"
            previous.mkdir()
            (previous / "opensips.cfg").write_text("old", encoding="utf-8")
            RUNTIME.recover_interrupted_update()
            self.assertEqual((RUNTIME.RUNTIME_ROOT / "opensips.cfg").read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
