"""Validate non-secret Packer input and final installed module inventory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

UA_BASELINE = {
    "b2b_entities.c": "d2d6e9b036f1304d5fa867038fe40bbca7bae2504f370a387e0e23eac0d168bb",
    "ua_api.c": "62719004d2a4e1b1d735d26140b712fcf11d7dd57c45b7489731b64a978a8beb",
    "ua_api.h": "47c199036c1ad783adaae435375098cde698f1b4a51b1693df55639766c41eb0",
}
UA_FILES = {*UA_BASELINE, "ua_storage.c"}
PLACEMENT_FILES = {"placement/" + name + ".py" for name in (
    "gateway_load_polling", "gateway_load_selection", "placement_store", "placement_observer", "placement_service", "placement_secret"
)} | {"assets/" + name for name in ("placement_config.py", "placement-schema.sql", "opensips.cfg.template", "opensips-placement.service")}


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate image input")
        result[key] = value
    return result


def validate(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"version", "commit", "sha256", "modules", "ua_sources", "placement_sources"}:
        raise ValueError("invalid image input fields")
    for key, pattern in (("version", r"[0-9]+\.[0-9]+\.[0-9]+"),
                         ("commit", r"[a-f0-9]{40}"), ("sha256", r"[a-f0-9]{64}")):
        if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
            raise ValueError("invalid source identity")
    modules = value["modules"]
    if not isinstance(modules, list) or not 1 <= len(modules) <= 256:
        raise ValueError("invalid module list")
    if any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
           for name in modules) or len(set(modules)) != len(modules):
        raise ValueError("invalid or duplicate module")
    sources = value["ua_sources"]
    if not isinstance(sources, dict) or set(sources) != UA_FILES or any(
        not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
        for digest in sources.values()
    ):
        raise ValueError("invalid UA source provenance")
    sources = value["placement_sources"]
    if not isinstance(sources, dict) or set(sources) != PLACEMENT_FILES or any(
        not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in sources.values()
    ):
        raise ValueError("invalid placement source provenance")
    return value


def apply_ua_sources(source: Path, overrides: Path, expected: dict[str, str]) -> None:
    """Apply exactly the reviewed UA files only to their checksum-matched upstream baseline."""
    if set(expected) != UA_FILES:
        raise ValueError("invalid UA source set")
    module = source / "modules/b2b_entities"
    for name, digest in UA_BASELINE.items():
        if hashlib.sha256((module / name).read_bytes()).hexdigest() != digest:
            raise ValueError("UA upstream baseline changed; source review required")
    contents = {name: (overrides / name).read_bytes() for name in expected}
    if any(hashlib.sha256(data).hexdigest() != expected[name] for name, data in contents.items()):
        raise ValueError("UA source checksum mismatch")
    for name, data in contents.items():
        (module / name).write_bytes(data)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    value = validate(json.loads((root / "input.json").read_text(), object_pairs_hook=unique_object))
    operation = sys.argv[1]
    if operation == "check":
        if hashlib.sha256((root / "source.tar.gz").read_bytes()).hexdigest() != value["sha256"]:
            raise ValueError("source archive checksum mismatch")
        for name, digest in value["placement_sources"].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise ValueError("placement source checksum mismatch")
    elif operation == "modules":
        print("\n".join(value["modules"]))
    elif operation == "apply-ua":
        apply_ua_sources(Path("/usr/local/src/opensips"), root / "ua-overrides", value["ua_sources"])
    elif operation == "verify":
        directory = Path("/usr/lib/aarch64-linux-gnu/opensips/modules")
        if sorted(path.stem for path in directory.glob("*.so")) != sorted(value["modules"]):
            raise ValueError("installed module set does not match image input")
        destination = Path("/usr/share/opensips-ami")
        destination.mkdir(parents=True, exist_ok=True)
        manifest = {"architecture": "arm64", "opensips_version": value["version"],
                    "opensips_source_commit": value["commit"],
                    "opensips_source_sha256": value["sha256"], "modules": value["modules"],
                    "ua_sources": value["ua_sources"], "placement_sources": value["placement_sources"]}
        (destination / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        (destination / "modules.txt").write_text("\n".join(sorted(value["modules"])) + "\n")
    else:
        raise ValueError("unknown provisioning operation")


if __name__ == "__main__":
    main()
