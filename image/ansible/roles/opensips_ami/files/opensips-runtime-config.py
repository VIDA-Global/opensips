#!/usr/bin/env python3
"""Render a validated OpenSIPS runtime bundle from AWS Secrets Manager."""

from __future__ import annotations

import ipaddress
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
CONFIG_TEMPLATE = Path(
    os.environ.get("OPENSIPS_CONFIG_TEMPLATE", "/etc/opensips/opensips.cfg.template")
)
SECRET_TAG = "OpenSIPSConfigSecretArn"
SECRET_VERSION_TAG = "OpenSIPSConfigSecretVersion"
MAX_SECRET_BYTES = 65536
REQUIRED_PLACEHOLDERS = {
    "@@NODE_ID@@",
    "@@CLUSTER_ID@@",
    "@@PRIVATE_IP@@",
    "@@ADVERTISED_IP@@",
    "@@STATE_OWNER@@",
    "@@DATABASE_URL@@",
    "@@CARRIER_UDP_REJECT@@",
    "@@CARRIER_TLS_REJECT@@",
    "@@RTPENGINE_NODES@@",
}
SECRET_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-us-gov|-cn)?):secretsmanager:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):secret:[A-Za-z0-9/_+=.@-]+$"
)


class ConfigurationError(RuntimeError):
    """A safe-to-report runtime configuration failure."""


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ConfigurationError("configuration secret contains duplicate object keys")
        value[key] = item
    return value


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
        secret = json.loads(secret_string, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("configuration secret is not valid JSON") from exc
    if not isinstance(secret, dict):
        raise ConfigurationError("configuration secret must be a JSON object")
    return secret


def validate_positive_int(value: Any, label: str, maximum: int = 2147483647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ConfigurationError(f"{label} must be an integer from 1 through {maximum}")
    return value


def validate_ipv4(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} must be an IPv4 address")
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise ConfigurationError(f"{label} must be an IPv4 address") from exc


def validate_ip_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"{label} must be a non-empty array")
    addresses = [validate_ipv4(item, label) for item in value]
    if len(addresses) != len(set(addresses)):
        raise ConfigurationError(f"{label} must not contain duplicates")
    return addresses


def validate_database_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("postgres://")
        or len(value) > 1024
        or any(char.isspace() or char in {'"', "\\", "\x00"} for char in value)
    ):
        raise ConfigurationError("deployment.database_url must be a safe postgres:// URL")
    return value


def validate_rtpengine_nodes(value: Any) -> str:
    if not isinstance(value, list) or not value:
        raise ConfigurationError("deployment.rtpengine_nodes must be a non-empty array")
    rendered = []
    seen = set()
    for node in value:
        if not isinstance(node, dict) or set(node) != {"url", "weight"}:
            raise ConfigurationError("each RTPengine node must contain only url and weight")
        url = node["url"]
        if not isinstance(url, str):
            raise ConfigurationError("RTPengine node URL must be text")
        match = re.fullmatch(r"udp:([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?):([0-9]{1,5})", url)
        if not match or not 1 <= int(match.group(2)) <= 65535:
            raise ConfigurationError("RTPengine node URL must use udp:host:port")
        if url in seen:
            raise ConfigurationError("RTPengine node URLs must be unique")
        seen.add(url)
        weight = validate_positive_int(node["weight"], "RTPengine node weight", 1000)
        rendered.append(f"{url}={weight}")
    return " ".join(rendered)


def render_config(deployment: Any, template: str) -> str:
    required = {
        "node_id",
        "cluster_id",
        "private_ip",
        "advertised_ip",
        "state_owner",
        "database_url",
        "carrier_udp_ips",
        "carrier_tls_ips",
        "rtpengine_nodes",
    }
    if not isinstance(deployment, dict) or set(deployment) != required:
        raise ConfigurationError("deployment must contain exactly the supported schema-v1 fields")
    missing_placeholders = REQUIRED_PLACEHOLDERS - {item for item in REQUIRED_PLACEHOLDERS if item in template}
    if missing_placeholders:
        raise ConfigurationError("OpenSIPS policy template is missing required placeholders")

    state_owner = deployment["state_owner"]
    if not isinstance(state_owner, str) or state_owner not in {"active", "backup"}:
        raise ConfigurationError("deployment.state_owner must be active or backup")
    udp_ips = validate_ip_list(deployment["carrier_udp_ips"], "deployment.carrier_udp_ips")
    tls_ips = validate_ip_list(deployment["carrier_tls_ips"], "deployment.carrier_tls_ips")
    replacements = {
        "@@NODE_ID@@": str(validate_positive_int(deployment["node_id"], "deployment.node_id")),
        "@@CLUSTER_ID@@": str(
            validate_positive_int(deployment["cluster_id"], "deployment.cluster_id")
        ),
        "@@PRIVATE_IP@@": validate_ipv4(deployment["private_ip"], "deployment.private_ip"),
        "@@ADVERTISED_IP@@": validate_ipv4(
            deployment["advertised_ip"], "deployment.advertised_ip"
        ),
        "@@STATE_OWNER@@": state_owner,
        "@@DATABASE_URL@@": validate_database_url(deployment["database_url"]),
        "@@CARRIER_UDP_REJECT@@": "(" + " && ".join(f"$si != {ip}" for ip in udp_ips) + ")",
        "@@CARRIER_TLS_REJECT@@": "(" + " && ".join(f"$si != {ip}" for ip in tls_ips) + ")",
        "@@RTPENGINE_NODES@@": validate_rtpengine_nodes(deployment["rtpengine_nodes"]),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    if re.search(r"@@[A-Z0-9_]+@@", rendered):
        raise ConfigurationError("OpenSIPS policy template contains an unresolved placeholder")
    return rendered


def read_template() -> str:
    try:
        template = CONFIG_TEMPLATE.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("OpenSIPS policy template is unavailable") from exc
    if not template.strip() or "\x00" in template:
        raise ConfigurationError("OpenSIPS policy template is invalid")
    return template


def validate_secret(secret: dict[str, Any], template: str | None = None) -> dict[str, str]:
    if (
        set(secret) != {"schema_version", "deployment", "tls"}
        or type(secret.get("schema_version")) is not int
        or secret["schema_version"] != 1
    ):
        raise ConfigurationError("configuration secret has an unsupported schema")
    files = {"opensips.cfg": render_config(secret["deployment"], template or read_template())}
    tls = secret.get("tls")
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


def openssl(*args: str) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/openssl", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigurationError("TLS material validation failed") from exc
    if result.returncode != 0:
        raise ConfigurationError("TLS material validation failed")
    return result.stdout


def validate_tls_material() -> None:
    tls_root = RUNTIME_ROOT / "tls"
    certificate = str(tls_root / "certificate.pem")
    private_key = str(tls_root / "private-key.pem")
    ca_bundle = str(tls_root / "ca-bundle.pem")
    openssl("crl2pkcs7", "-nocrl", "-certfile", certificate, "-outform", "DER")
    certificate_key = openssl("x509", "-in", certificate, "-pubkey", "-noout")
    private_public_key = openssl("pkey", "-in", private_key, "-pubout")
    if certificate_key != private_public_key:
        raise ConfigurationError("TLS certificate and private key do not match")
    openssl("crl2pkcs7", "-nocrl", "-certfile", ca_bundle, "-outform", "DER")


def main() -> int:
    try:
        recover_interrupted_update()
        secret_arn, version_id, region, account_id = instance_identity()
        files = validate_secret(get_secret(secret_arn, version_id, region, account_id))
        previous = write_bundle(files)
        try:
            validate_tls_material()
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
