from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/promote-ami.py"
SPEC = importlib.util.spec_from_file_location("promote_ami", MODULE_PATH)
assert SPEC and SPEC.loader
PROMOTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROMOTION)


class PromotionManifestTests(unittest.TestCase):
    def write_manifest(self, value: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "promotion.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_valid_manifest(self) -> None:
        manifest = PROMOTION.load_manifest(
            self.write_manifest(
                {
                    "source_region": "us-east-2",
                    "source_ami_id": "ami-0123456789abcdef0",
                    "expected_account_id": "123456789012",
                    "build_id": "123-1",
                    "destinations": {"us-west-2": "alias/opensips-ami"},
                }
            )
        )
        self.assertEqual(manifest["destinations"]["us-west-2"], "alias/opensips-ami")

    def test_source_region_cannot_be_destination(self) -> None:
        with self.assertRaises(PROMOTION.PromotionError):
            PROMOTION.load_manifest(
                self.write_manifest(
                    {
                        "source_region": "us-east-2",
                        "source_ami_id": "ami-0123456789abcdef0",
                        "expected_account_id": "123456789012",
                        "build_id": "123-1",
                        "destinations": {"us-east-2": "alias/opensips-ami"},
                    }
                )
            )

    def test_non_string_source_fields_are_rejected_cleanly(self) -> None:
        with self.assertRaises(PROMOTION.PromotionError):
            PROMOTION.load_manifest(
                self.write_manifest(
                    {
                        "source_region": None,
                        "source_ami_id": 123,
                        "expected_account_id": [],
                        "build_id": {},
                        "destinations": {"us-west-2": "alias/opensips-ami"},
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
