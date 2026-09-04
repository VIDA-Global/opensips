from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/package-runtime-secret.py"
SPEC = importlib.util.spec_from_file_location("package_runtime_secret", MODULE_PATH)
assert SPEC and SPEC.loader
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)


class RuntimeSecretPackagerTests(unittest.TestCase):
    def test_packages_schema_v1_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = {
                "node_id": 1,
                "cluster_id": 10,
                "private_ip": "10.0.1.10",
                "advertised_ip": "198.51.100.10",
                "state_owner": "active",
                "database_url": "postgres://opensips:password@db.example/opensips",
                "carrier_udp_ips": ["192.0.2.10"],
                "carrier_tls_ips": ["192.0.2.11"],
                "rtpengine_nodes": [{"url": "udp:10.0.2.10:2223", "weight": 10}],
            }
            paths = []
            for name, value in (
                ("deployment.json", json.dumps(deployment)),
                ("certificate.pem", "certificate\n"),
                ("private-key.pem", "private key\n"),
                ("ca-bundle.pem", "ca bundle\n"),
            ):
                path = root / name
                path.write_text(value, encoding="utf-8")
                paths.append(path)

            encoded = PACKAGER.package_secret(*paths)
            payload = json.loads(encoded)
            self.assertLessEqual(len(encoded) + 1, PACKAGER.MAX_SECRET_BYTES)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["deployment"], deployment)
            self.assertEqual(payload["tls"]["private_key"], "private key\n")

    def test_rejects_empty_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty"
            path.write_text("", encoding="utf-8")
            with self.assertRaises(ValueError):
                PACKAGER.package_secret(path, path, path, path)

    def test_rejects_oversized_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / "deployment.json"
            deployment.write_text("x" * (PACKAGER.MAX_SECRET_BYTES + 1), encoding="utf-8")
            tls = root / "tls.pem"
            tls.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "maximum"):
                PACKAGER.package_secret(deployment, tls, tls, tls)

    def test_rejects_non_regular_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "regular file"):
                PACKAGER.package_secret(root, root, root, root)

    def test_rejects_incomplete_deployment_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / "deployment.json"
            deployment.write_text('{"node_id":1}', encoding="utf-8")
            tls = root / "tls.pem"
            tls.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly"):
                PACKAGER.package_secret(deployment, tls, tls, tls)

    def test_rejects_semantically_invalid_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / "deployment.json"
            invalid = {
                "node_id": True,
                "cluster_id": 10,
                "private_ip": "10.0.1.10",
                "advertised_ip": "198.51.100.10",
                "state_owner": "active",
                "database_url": "postgres://db/opensips",
                "carrier_udp_ips": ["192.0.2.10"],
                "carrier_tls_ips": ["192.0.2.11"],
                "rtpengine_nodes": [{"url": "udp:10.0.2.10:2223", "weight": 10}],
            }
            deployment.write_text(json.dumps(invalid), encoding="utf-8")
            tls = root / "tls.pem"
            tls.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "node_id"):
                PACKAGER.package_secret(deployment, tls, tls, tls)

    def test_rejects_symbolic_link_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("x", encoding="utf-8")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "cannot read"):
                PACKAGER.package_secret(link, link, link, link)

    def test_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = root / "deployment.json"
            deployment.write_text('{"node_id":1,"node_id":2}', encoding="utf-8")
            tls = root / "tls.pem"
            tls.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                PACKAGER.package_secret(deployment, tls, tls, tls)


if __name__ == "__main__":
    unittest.main()
