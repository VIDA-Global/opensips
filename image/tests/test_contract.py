from __future__ import annotations

import json
import unittest
from pathlib import Path


IMAGE_ROOT = Path(__file__).resolve().parents[1]


class ImageContractTests(unittest.TestCase):
    def test_requested_defaults_and_private_build_contract(self) -> None:
        variables = (IMAGE_ROOT / "packer/variables.pkr.hcl").read_text(encoding="utf-8")
        template = (IMAGE_ROOT / "packer/opensips-arm64.pkr.hcl").read_text(encoding="utf-8")
        self.assertIn('default     = "m9g.large"', variables)
        self.assertIn('default     = "us-east-2"', variables)
        self.assertIn('default     = "3.6.8"', variables)
        self.assertIn('ssh_interface             = "private_ip"', template)
        self.assertIn('associate_public_ip_address = false', template)
        self.assertNotIn("ami_regions", template)
        self.assertIn("common_tags = merge(var.additional_tags, {", template)

    def test_source_identity_is_consistent(self) -> None:
        variables = (IMAGE_ROOT / "packer/variables.pkr.hcl").read_text(encoding="utf-8")
        fetch = (IMAGE_ROOT / "scripts/fetch-source.sh").read_text(encoding="utf-8")
        digest = "b3e1ab4d82dce763bbd51c99a1733f133465fda8fe2591f86aec9c3eefababf0"
        commit = "f9f85260e5def73e3f854f5e22d148d2d977e85f"
        self.assertIn(digest, variables)
        self.assertIn(digest, fetch)
        self.assertIn(commit, variables)
        self.assertIn(commit, fetch)

    def test_source_gate_runs_every_maintained_unit_suite(self) -> None:
        runner = (IMAGE_ROOT / "scripts/run-source-tests.sh").read_text(encoding="utf-8")
        for module in ("core", "acc", "cfgutils", "registrar"):
            self.assertIn(module, runner)
        for fuzzer in ("fuzz_msg_parser", "fuzz_uri_parser", "fuzz_csv_parser", "fuzz_core_funcs"):
            self.assertIn(fuzzer, runner)

    def test_iam_templates_render_as_json(self) -> None:
        replacements = {
            "${AWS_ACCOUNT_ID}": "123456789012",
            "${AWS_REGION}": "us-east-2",
            "${GITHUB_ORG}": "example",
            "${GITHUB_REPOSITORY}": "opensips",
            "${TRUSTED_REF}": "refs/heads/main",
            "${BUILD_KMS_KEY_ARN}": "arn:aws:kms:us-east-2:123456789012:key/example",
            "${BUILD_INSTANCE_ROLE_ARN}": "arn:aws:iam::123456789012:role/packer-instance",
            "${VALIDATION_INSTANCE_ROLE_ARN}": "arn:aws:iam::123456789012:role/validator-instance",
            "${ALL_PROMOTION_KMS_KEY_ARNS_JSON}": '["arn:aws:kms:us-west-2:123456789012:key/example"]',
            "${OPENSIPS_CONFIG_SECRET_ARN}": "arn:aws:secretsmanager:us-east-2:123456789012:secret:opensips",
            "${SECRETS_KMS_KEY_ARN}": "arn:aws:kms:us-east-2:123456789012:key/secrets",
        }
        for template_path in (IMAGE_ROOT / "iam").glob("*.json.tmpl"):
            rendered = template_path.read_text(encoding="utf-8")
            for placeholder, value in replacements.items():
                rendered = rendered.replace(placeholder, value)
            with self.subTest(template=template_path.name):
                self.assertNotIn("${", rendered)
                json.loads(rendered)


if __name__ == "__main__":
    unittest.main()
