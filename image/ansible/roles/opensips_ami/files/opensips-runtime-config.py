#!/usr/bin/env python3
"""Render a validated OpenSIPS runtime bundle from AWS Secrets Manager."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import boto3

IMDS_BASE = os.environ.get("OPENSIPS_IMDS_URL", "http://169.254.169.254/latest")
RUNTIME_ROOT = Path(os.environ.get("OPENSIPS_RUNTIME_ROOT", "/run/opensips-secure/config"))
SECRET_TAG = "OpenSIPSConfigSecretArn"
SECRET_VERSION_TAG = "OpenSIPSConfigSecretVersion"
MAX_SECRET_BYTES = 65536
SECRET_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-us-gov|-cn)?):secretsmanager:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):secret:[A-Za-z0-9/_+=.@-]+$"
)


class ConfigurationError(RuntimeError):
    """A safe-to-report runtime configuration failure."""


def imds_request(path: str, token: str | None = None, method: str = "GET") -> bytes:
    headers = {}
    if token:
        headers["X-aws-ec2-metadata-token"] = token
    if method == "PUT":
        headers["X-aws-ec2-metadata-token-ttl-seconds"] = "60"
    request = urllib.request.Request(f"{IMDS_BASE}/{path}", headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.read(MAX_SECRET_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConfigurationError("instance metadata is unavailable") from exc


def instance_identity() -> tuple[str, str, str, str]:
    token = imds_request("api/token", method="PUT").decode("ascii")
    if not token or len(token) > 256:
        raise ConfigurationError("instance metadata returned an invalid token")
    tag_value = imds_request(f"meta-data/tags/instance/{SECRET_TAG}", token).decode("utf-8")
    version_value = imds_request(f"meta-data/tags/instance/{SECRET_VERSION_TAG}", token).decode("utf-8")
    document_bytes = imds_request("dynamic/instance-identity/document", token)
    try:
        document = json.loads(document_bytes)
        region = document["region"]
        account_id = document["accountId"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ConfigurationError("instance identity document is invalid") from exc
    if not isinstance(region, str) or not re.fullmatch(r"[a-z0-9-]+", region):
        raise ConfigurationError("instance identity region is invalid")
    if not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{12}", account_id):
        raise ConfigurationError("instance identity account is invalid")
    if not re.fullmatch(r"[A-Za-z0-9-]{32,64}", version_value):
        raise ConfigurationError("configuration secret version is invalid")
    return tag_value, version_value, region, account_id


def get_secret(secret_arn: str, version_id: str, region: str, account_id: str) -> dict[str, Any]:
    match = SECRET_ARN_RE.fullmatch(secret_arn)
    if not match or match.group("region") != region or match.group("account") != account_id:
        raise ConfigurationError("configuration secret ARN is invalid or belongs to another account or region")
    endpoint_url = os.environ.get("OPENSIPS_SECRETS_ENDPOINT")
    client = boto3.client("secretsmanager", region_name=region, endpoint_url=endpoint_url)
    try:
        response = client.get_secret_value(SecretId=secret_arn, VersionId=version_id)
    except Exception as exc:  # botocore exceptions vary by failure mode
        raise ConfigurationError("configuration secret retrieval failed") from exc
    secret_string = response.get("SecretString")
    if response.get("VersionId") != version_id:
        raise ConfigurationError("configuration secret returned an unexpected version")
    if not isinstance(secret_string, str) or len(secret_string.encode("utf-8")) > MAX_SECRET_BYTES:
        raise ConfigurationError("configuration secret is absent or too large")
    try:
        secret = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("configuration secret is not valid JSON") from exc
    if not isinstance(secret, dict):
        raise ConfigurationError("configuration secret must be a JSON object")
    return secret


def validate_secret(secret: dict[str, Any]) -> dict[str, str]:
    allowed = {"schema_version", "opensips_config", "tls"}
    if set(secret) - allowed or secret.get("schema_version") != 1:
        raise ConfigurationError("configuration secret has an unsupported schema")
    config = secret.get("opensips_config")
    if not isinstance(config, str) or not config.strip() or "\x00" in config:
        raise ConfigurationError("opensips_config must be a non-empty text value")
    files = {"opensips.cfg": config}
    tls = secret.get("tls")
    if tls is not None:
        if not isinstance(tls, dict) or set(tls) != {"certificate", "private_key", "ca_bundle"}:
            raise ConfigurationError("tls must contain certificate, private_key, and ca_bundle")
        for source_key, filename in (
            ("certificate", "tls/certificate.pem"),
            ("private_key", "tls/private-key.pem"),
            ("ca_bundle", "tls/ca-bundle.pem"),
        ):
            value = tls[source_key]
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ConfigurationError("TLS material must be non-empty text")
            files[filename] = value
    return files


def prepare_parent() -> Path:
    parent = RUNTIME_ROOT.parent
    parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chmod(parent, 0o750)
    return parent


def recover_interrupted_update() -> None:
    parent = prepare_parent()
    previous = parent / ".config-previous"
    if not previous.exists():
        return
    if previous.is_symlink() or not previous.is_dir():
        raise ConfigurationError("runtime configuration backup path is unsafe")
    if RUNTIME_ROOT.exists():
        remove_bundle(RUNTIME_ROOT)
    previous.rename(RUNTIME_ROOT)


def write_bundle(files: dict[str, str]) -> Path | None:
    parent = prepare_parent()
    staging = Path(tempfile.mkdtemp(prefix=".config-", dir=parent))
    previous = parent / ".config-previous"
    try:
        os.chmod(staging, 0o750)
        for relative, content in files.items():
            target = staging / relative
            target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                if not content.endswith("\n"):
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(target, 0o640)
        if RUNTIME_ROOT.exists() and RUNTIME_ROOT.is_symlink():
            raise ConfigurationError("runtime configuration destination is a symlink")
        if previous.exists():
            raise ConfigurationError("an interrupted runtime configuration update was not recovered")
        if RUNTIME_ROOT.exists():
            RUNTIME_ROOT.rename(previous)
        try:
            staging.rename(RUNTIME_ROOT)
        except Exception:
            if previous.exists() and not RUNTIME_ROOT.exists():
                previous.rename(RUNTIME_ROOT)
            raise
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return previous if previous.exists() else None
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def remove_bundle(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ConfigurationError("runtime configuration path is unsafe")
    shutil.rmtree(path)


def restore_bundle(previous: Path | None) -> None:
    if RUNTIME_ROOT.exists():
        remove_bundle(RUNTIME_ROOT)
    if previous is not None:
        previous.rename(RUNTIME_ROOT)


def validate_opensips() -> None:
    try:
        result = subprocess.run(
            ["/usr/sbin/runuser", "-u", "opensips", "--", "/usr/sbin/opensips", "-C", "-f", str(RUNTIME_ROOT / "opensips.cfg")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigurationError("OpenSIPS configuration validation timed out") from exc
    if result.returncode != 0:
        raise ConfigurationError("OpenSIPS rejected the rendered configuration")


def main() -> int:
    try:
        recover_interrupted_update()
        secret_arn, version_id, region, account_id = instance_identity()
        files = validate_secret(get_secret(secret_arn, version_id, region, account_id))
        previous = write_bundle(files)
        try:
            validate_opensips()
        except ConfigurationError:
            restore_bundle(previous)
            raise
        if previous is not None:
            remove_bundle(previous)
    except ConfigurationError as exc:
        print(f"opensips-runtime-config: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("opensips-runtime-config: unexpected configuration failure", file=sys.stderr)
        return 1
    print("opensips-runtime-config: configuration validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
