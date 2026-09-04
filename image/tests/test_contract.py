from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


IMAGE_ROOT = Path(__file__).resolve().parents[1]


class ImageContractTests(unittest.TestCase):
    EXPECTED_MODULES = {
        "b2b_entities", "b2b_logic", "clusterer", "db_postgres", "dialog", "freeswitch",
        "load_balancer", "maxfwd", "proto_bin", "proto_hep", "proto_tls", "rr", "rtpengine",
        "sipmsgops", "sl", "textops", "tls_mgm", "tls_openssl", "tm", "topology_hiding",
        "tracer", "uac_auth",
    }

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

    def test_module_allowlists_and_production_example_are_consistent(self) -> None:
        variables = (IMAGE_ROOT / "packer/variables.pkr.hcl").read_text(encoding="utf-8")
        defaults = (IMAGE_ROOT / "ansible/roles/opensips_ami/defaults/main.yml").read_text(
            encoding="utf-8"
        )
        config = (
            IMAGE_ROOT / "ansible/roles/opensips_ami/files/opensips.cfg.template"
        ).read_text(encoding="utf-8")
        hcl_block = variables.split('variable "opensips_modules"', 1)[1].split("}\n", 1)[0]
        hcl_modules = set(re.findall(r'^\s+"([a-z0-9_]+)",?$', hcl_block, re.MULTILINE))
        yaml_block = defaults.split("opensips_ami_modules:\n", 1)[1].split(
            "opensips_ami_build_packages:", 1
        )[0]
        yaml_modules = set(re.findall(r"^  - ([a-z0-9_]+)$", yaml_block, re.MULTILINE))
        loaded_modules = set(re.findall(r'^loadmodule "([a-z0-9_]+)\.so"$', config, re.MULTILINE))
        core_protocols = {"proto_tcp", "proto_udp"}
        self.assertEqual(hcl_modules, self.EXPECTED_MODULES)
        self.assertEqual(yaml_modules, self.EXPECTED_MODULES)
        self.assertLessEqual(loaded_modules, self.EXPECTED_MODULES | core_protocols)
        self.assertIn("freeswitch", loaded_modules)
        self.assertIn("load_balancer", loaded_modules)

    def test_production_example_replaces_spoofed_sage_headers(self) -> None:
        config = (
            IMAGE_ROOT / "ansible/roles/opensips_ami/files/opensips.cfg.template"
        ).read_text(encoding="utf-8")
        self.assertIn('remove_hf_glob("X-SAGE-*")', config)
        self.assertIn('$avp(sage_header_name) = "X-SAGE-Source-IP"', config)
        self.assertIn("$avp(sage_header_body) = $si", config)
        self.assertNotIn("$hdr(X-SAGE-Source-IP)", config)
        self.assertNotIn('modparam("b2b_logic", "custom_headers', config)
        self.assertEqual(config.count('$avp(sage_header_name) = "X-SAGE-Source-IP"'), 1)

    def test_default_policy_template_is_installed(self) -> None:
        tasks = (IMAGE_ROOT / "ansible/roles/opensips_ami/tasks/runtime.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("src: opensips.cfg.template", tasks)
        self.assertIn("dest: /etc/opensips/opensips.cfg.template", tasks)
        self.assertIn("mode: '0640'", tasks)

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
