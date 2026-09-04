from __future__ import annotations

import importlib.util
import json
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
TEMPLATE = (MODULE_PATH.parent / "opensips.cfg.template").read_text(encoding="utf-8")


def valid_secret() -> dict:
    return {
        "schema_version": 1,
        "deployment": {
            "node_id": 1,
            "cluster_id": 10,
            "private_ip": "10.0.1.10",
            "advertised_ip": "198.51.100.10",
            "state_owner": "active",
            "database_url": "postgres://opensips:password@db.example:5432/opensips",
            "carrier_udp_ips": ["192.0.2.10", "192.0.2.12"],
            "carrier_tls_ips": ["192.0.2.11"],
            "rtpengine_nodes": [
                {"url": "udp:10.0.2.10:2223", "weight": 10},
                {"url": "udp:rtp-b.internal.example:2223", "weight": 5},
            ],
        },
        "tls": {"certificate": "cert", "private_key": "key", "ca_bundle": "ca"},
    }


class RuntimeConfigurationTests(unittest.TestCase):
    def test_structured_secret_renders_installed_policy(self) -> None:
        files = RUNTIME.validate_secret(valid_secret(), TEMPLATE)
        config = files["opensips.cfg"]
        self.assertIn("socket=udp:10.0.1.10:5060 as 198.51.100.10:5060", config)
        self.assertIn('modparam("clusterer", "my_node_id", 1)', config)
        self.assertIn("$si != 192.0.2.10 && $si != 192.0.2.12", config)
        self.assertIn("udp:10.0.2.10:2223=10 udp:rtp-b.internal.example:2223=5", config)
        self.assertNotIn("@@", config)

    def test_tls_bundle(self) -> None:
        files = RUNTIME.validate_secret(valid_secret(), TEMPLATE)
        self.assertEqual(files["tls/private-key.pem"], "key")

    def test_checked_in_schema_v1_example_renders(self) -> None:
        example = MODULE_PATH.parents[4] / "config/runtime-secret.json.example"
        secret = json.loads(example.read_text(encoding="utf-8"))
        files = RUNTIME.validate_secret(secret, TEMPLATE)
        self.assertNotIn("@@", files["opensips.cfg"])

    def test_unknown_keys_are_rejected(self) -> None:
        secret = valid_secret()
        secret["unexpected"] = True
        with self.assertRaises(RUNTIME.ConfigurationError):
            RUNTIME.validate_secret(secret, TEMPLATE)

    def test_only_schema_version_one_is_accepted(self) -> None:
        for value in (2, True, 1.0, "1"):
            secret = valid_secret()
            secret["schema_version"] = value
            with self.subTest(value=value), self.assertRaises(RUNTIME.ConfigurationError):
                RUNTIME.validate_secret(secret, TEMPLATE)

    def test_legacy_arbitrary_config_is_rejected(self) -> None:
        with self.assertRaises(RUNTIME.ConfigurationError):
            RUNTIME.validate_secret(
                {
                    "schema_version": 1,
                    "opensips_config": "log_level=2",
                    "tls": {"certificate": "cert", "private_key": "key", "ca_bundle": "ca"},
                },
                TEMPLATE,
            )

    def test_incomplete_tls_is_rejected(self) -> None:
        secret = valid_secret()
        secret["tls"] = {"private_key": "secret"}
        with self.assertRaises(RUNTIME.ConfigurationError):
            RUNTIME.validate_secret(secret, TEMPLATE)

    def test_invalid_deployment_values_are_rejected(self) -> None:
        invalid_values = {
            "node_id": 0,
            "private_ip": "not-an-ip",
            "state_owner": "primary",
            "database_url": 'postgres://db/opensips"\nloadmodule "evil.so',
            "carrier_udp_ips": [],
            "rtpengine_nodes": [{"url": "tcp:rtp.example:2223", "weight": 10}],
        }
        for field, value in invalid_values.items():
            secret = valid_secret()
            secret["deployment"][field] = value
            with self.subTest(field=field), self.assertRaises(RUNTIME.ConfigurationError):
                RUNTIME.validate_secret(secret, TEMPLATE)

    def test_duplicate_addresses_and_rtpengine_nodes_are_rejected(self) -> None:
        secret = valid_secret()
        secret["deployment"]["carrier_udp_ips"] = ["192.0.2.10", "192.0.2.10"]
        with self.assertRaises(RUNTIME.ConfigurationError):
            RUNTIME.validate_secret(secret, TEMPLATE)
        secret = valid_secret()
        secret["deployment"]["rtpengine_nodes"] = [
            {"url": "udp:10.0.2.10:2223", "weight": 10},
            {"url": "udp:10.0.2.10:2223", "weight": 5},
        ]
        with self.assertRaises(RUNTIME.ConfigurationError):
            RUNTIME.validate_secret(secret, TEMPLATE)

    def test_unresolved_template_placeholder_is_rejected(self) -> None:
        with self.assertRaisesRegex(RUNTIME.ConfigurationError, "unresolved"):
            RUNTIME.validate_secret(valid_secret(), TEMPLATE + "\n@@UNKNOWN@@\n")

    def test_missing_required_template_placeholder_is_rejected(self) -> None:
        with self.assertRaisesRegex(RUNTIME.ConfigurationError, "missing required"):
            RUNTIME.validate_secret(valid_secret(), TEMPLATE.replace("@@CARRIER_UDP_REJECT@@", ""))

    def test_tls_material_must_match(self) -> None:
        original = RUNTIME.openssl
        RUNTIME.openssl = lambda *args: b"same-public-key"
        self.addCleanup(setattr, RUNTIME, "openssl", original)
        RUNTIME.validate_tls_material()

        RUNTIME.openssl = lambda *args: b"certificate-key" if args[0] == "x509" else b"private-key"
        with self.assertRaisesRegex(RUNTIME.ConfigurationError, "do not match"):
            RUNTIME.validate_tls_material()

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

    def test_secret_json_rejects_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(RUNTIME.ConfigurationError, "duplicate"):
            json.loads('{"schema_version":2,"schema_version":1}', object_pairs_hook=RUNTIME.reject_duplicate_keys)

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
