#!/usr/bin/env python3
"""Copy a validated source AMI to explicitly configured AWS regions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

AMI_RE = re.compile(r"^ami-[0-9a-f]+$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]+$")


class PromotionError(RuntimeError):
    """An AMI promotion contract or AWS operation failed."""


def aws(region: str, *arguments: str) -> dict[str, Any]:
    command = ["aws", "--region", region, "--output", "json", *arguments]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PromotionError(f"AWS command failed in {region}: {' '.join(arguments[:2])}")
    if not result.stdout.strip():
        return {}
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError("AWS CLI returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise PromotionError("AWS CLI returned an unexpected response")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionError("promotion manifest is unreadable or invalid") from exc
    required = {"source_region", "source_ami_id", "expected_account_id", "build_id", "destinations"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise PromotionError("promotion manifest has an unsupported schema")
    if not isinstance(manifest["source_region"], str) or not REGION_RE.fullmatch(manifest["source_region"]):
        raise PromotionError("source_region is invalid")
    if not isinstance(manifest["source_ami_id"], str) or not AMI_RE.fullmatch(manifest["source_ami_id"]):
        raise PromotionError("source_ami_id is invalid")
    if not isinstance(manifest["expected_account_id"], str) or not re.fullmatch(
        r"[0-9]{12}", manifest["expected_account_id"]
    ):
        raise PromotionError("expected_account_id is invalid")
    if not isinstance(manifest["build_id"], str) or not re.fullmatch(r"[A-Za-z0-9._-]+", manifest["build_id"]):
        raise PromotionError("build_id is invalid")
    destinations = manifest["destinations"]
    if not isinstance(destinations, dict) or not destinations:
        raise PromotionError("destinations must be a non-empty object")
    for region, kms_key in destinations.items():
        if not REGION_RE.fullmatch(region) or region == manifest["source_region"]:
            raise PromotionError("a destination region is invalid or duplicates the source")
        if not isinstance(kms_key, str) or not kms_key:
            raise PromotionError("every destination requires a KMS key")
    return manifest


def image_from_response(response: dict[str, Any]) -> dict[str, Any]:
    images = response.get("Images")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise PromotionError("expected exactly one AMI")
    return images[0]


def verify_source(manifest: dict[str, Any]) -> dict[str, Any]:
    response = aws(
        manifest["source_region"],
        "ec2",
        "describe-images",
        "--image-ids",
        manifest["source_ami_id"],
        "--owners",
        manifest["expected_account_id"],
    )
    image = image_from_response(response)
    tags = {tag["Key"]: tag["Value"] for tag in image.get("Tags", [])}
    if image.get("State") != "available" or image.get("Architecture") != "arm64":
        raise PromotionError("source AMI is not an available ARM64 image")
    if tags.get("BuildId") != manifest["build_id"] or tags.get("Application") != "opensips":
        raise PromotionError("source AMI identity tags do not match the manifest")
    mappings = image.get("BlockDeviceMappings", [])
    if not mappings or any(not mapping.get("Ebs", {}).get("Encrypted") for mapping in mappings):
        raise PromotionError("source AMI contains an unencrypted EBS mapping")
    return image


def existing_copy(region: str, source_ami_id: str, build_id: str) -> dict[str, Any] | None:
    response = aws(
        region,
        "ec2",
        "describe-images",
        "--owners",
        "self",
        "--filters",
        f"Name=tag:PromotionSourceAmi,Values={source_ami_id}",
        f"Name=tag:BuildId,Values={build_id}",
    )
    images = response.get("Images", [])
    if len(images) > 1:
        raise PromotionError(f"multiple promoted AMIs already exist in {region}")
    return images[0] if images else None


def wait_for_image(region: str, image_id: str, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        image = image_from_response(aws(region, "ec2", "describe-images", "--image-ids", image_id, "--owners", "self"))
        state = image.get("State")
        if state == "available":
            return image
        if state in {"failed", "deregistered", "error"}:
            raise PromotionError(f"promoted AMI entered {state} state in {region}")
        time.sleep(15)
    raise PromotionError(f"timed out waiting for promoted AMI in {region}")


def promote(manifest: dict[str, Any], source_image: dict[str, Any]) -> dict[str, str]:
    promoted: dict[str, str] = {}
    source_region = manifest["source_region"]
    source_ami_id = manifest["source_ami_id"]
    build_id = manifest["build_id"]
    deadline = time.monotonic() + 4500
    for region, kms_key in sorted(manifest["destinations"].items()):
        kms_response = aws(region, "kms", "describe-key", "--key-id", kms_key)
        expected_kms_arn = kms_response.get("KeyMetadata", {}).get("Arn")
        if not isinstance(expected_kms_arn, str):
            raise PromotionError(f"destination KMS key is invalid in {region}")
        image = existing_copy(region, source_ami_id, build_id)
        if image is None:
            token = hashlib.sha256(f"{source_ami_id}:{build_id}:{region}".encode()).hexdigest()
            tag_specifications = json.dumps(
                [
                    {
                        "ResourceType": "image",
                        "Tags": [
                            {"Key": "Application", "Value": "opensips"},
                            {"Key": "Architecture", "Value": "arm64"},
                            {"Key": "BuildId", "Value": build_id},
                            {"Key": "ManagedBy", "Value": "packer-promotion"},
                            {"Key": "PromotionSourceAmi", "Value": source_ami_id},
                        ],
                    },
                    {
                        "ResourceType": "snapshot",
                        "Tags": [
                            {"Key": "Application", "Value": "opensips"},
                            {"Key": "Architecture", "Value": "arm64"},
                            {"Key": "BuildId", "Value": build_id},
                            {"Key": "ManagedBy", "Value": "packer-promotion"},
                            {"Key": "PromotionSourceAmi", "Value": source_ami_id},
                        ],
                    },
                ],
                separators=(",", ":"),
            )
            response = aws(
                region,
                "ec2",
                "copy-image",
                "--source-region",
                source_region,
                "--source-image-id",
                source_ami_id,
                "--name",
                source_image["Name"],
                "--description",
                source_image.get("Description", "Promoted OpenSIPS ARM64 AMI"),
                "--encrypted",
                "--kms-key-id",
                kms_key,
                "--client-token",
                token,
                "--copy-image-tags",
                "--tag-specifications",
                tag_specifications,
            )
            image_id = response.get("ImageId")
            if not isinstance(image_id, str) or not AMI_RE.fullmatch(image_id):
                raise PromotionError(f"copy-image did not return a valid AMI ID in {region}")
        else:
            image_id = image.get("ImageId")
            if not isinstance(image_id, str) or not AMI_RE.fullmatch(image_id):
                raise PromotionError(f"existing promoted AMI is invalid in {region}")
        verified = wait_for_image(region, image_id, deadline)
        if verified.get("Architecture") != "arm64":
            raise PromotionError(f"promoted AMI architecture is invalid in {region}")
        if any(not mapping.get("Ebs", {}).get("Encrypted") for mapping in verified.get("BlockDeviceMappings", [])):
            raise PromotionError(f"promoted AMI encryption is invalid in {region}")
        snapshot_ids = [mapping.get("Ebs", {}).get("SnapshotId") for mapping in verified.get("BlockDeviceMappings", [])]
        if not snapshot_ids or any(not snapshot_id for snapshot_id in snapshot_ids):
            raise PromotionError(f"promoted AMI snapshot inventory is invalid in {region}")
        snapshots = aws(region, "ec2", "describe-snapshots", "--snapshot-ids", *snapshot_ids).get("Snapshots", [])
        if len(snapshots) != len(snapshot_ids) or any(
            not snapshot.get("Encrypted") or snapshot.get("KmsKeyId") != expected_kms_arn for snapshot in snapshots
        ):
            raise PromotionError(f"promoted AMI KMS encryption is invalid in {region}")
        promoted[region] = image_id
    return promoted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("build/promoted-amis.json"))
    arguments = parser.parse_args()
    try:
        manifest = load_manifest(arguments.manifest)
        source_image = verify_source(manifest)
        promoted = promote(manifest, source_image)
        output = {
            "build_id": manifest["build_id"],
            "source": {manifest["source_region"]: manifest["source_ami_id"]},
            "promoted": promoted,
        }
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except PromotionError as exc:
        print(f"promote-ami: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
