"""Shared renderer/service scope validation is credential-safe and fail-closed."""

import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("placement_configuration", ROOT / "assets/placement_config.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PlacementConfigurationTests(unittest.TestCase):
    def config(self):
        return json.loads((ROOT / "config/deployment.json.example").read_text())["placement"]

    def test_complete_config_and_sip_destinations(self):
        config = MODULE.validate_placement(self.config())
        MODULE.validate_sip_destinations(("sip:10.0.1.2:5070;transport=udp", "sip:10.0.1.2:5072;transport=udp"), config)
        for target in ("sip:10.1.1.2:5070;transport=udp", "sip:10.0.1.2:8021;transport=udp",
                       "sip:10.0.1.2:5070;transport=tls", "sip:10.0.1.2:5070;transport=tcp"):
            with self.assertRaises(ValueError):
                MODULE.validate_sip_destinations((target,), config)
        MODULE.validate_instance_scope(config, "us-east-2", "123456789012")
        with self.assertRaises(ValueError):
            MODULE.validate_instance_scope(config, "us-east-1", "123456789012")
        with self.assertRaises(ValueError):
            MODULE.validate_instance_scope(config, "us-east-2", "999999999999")

    def test_bad_secret_scopes_and_transports_are_safe_errors(self):
        changes = (
            {"service_token": "secret\r\n"}, {"sage_token": "a"*64}, {"sage_origin": "http://sage.internal"},
            {"database_url": "postgres://missing-password@db/database"}, {"gateway_networks": ["0.0.0.0/0"]},
            {"gateway_networks": ["169.254.0.0/16"]}, {"gateway_ports": [True]}, {"sip_ports": [5070,5070]},
            {"gateway_networks": ["::ffff:0.0.0.0/96"]},
            {"ca_bundle": "relative.pem"}, {"load_secret_prefix": "arn:aws:secretsmanager:region:account:secret:*"},
            {"inventory_networks": []}, {"inventory_ports": [8443]}, {"unexpected": "secret"},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError) as raised:
                MODULE.validate_placement({**self.config(), **change})
            self.assertEqual(str(raised.exception), "invalid placement configuration")
