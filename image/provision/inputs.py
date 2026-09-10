"""Validate non-secret Packer input and final installed module inventory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate image input")
        result[key] = value
    return result


def validate(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"version", "commit", "sha256", "modules"}:
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
    return value


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    value = validate(json.loads((root / "input.json").read_text(), object_pairs_hook=unique_object))
    operation = sys.argv[1]
    if operation == "check":
        if hashlib.sha256((root / "source.tar.gz").read_bytes()).hexdigest() != value["sha256"]:
            raise ValueError("source archive checksum mismatch")
    elif operation == "modules":
        print("\n".join(value["modules"]))
    elif operation == "verify":
        directory = Path("/usr/lib/aarch64-linux-gnu/opensips/modules")
        if sorted(path.stem for path in directory.glob("*.so")) != sorted(value["modules"]):
            raise ValueError("installed module set does not match image input")
        destination = Path("/usr/share/opensips-ami")
        destination.mkdir(parents=True, exist_ok=True)
        manifest = {"architecture": "arm64", "opensips_version": value["version"],
                    "opensips_source_commit": value["commit"],
                    "opensips_source_sha256": value["sha256"], "modules": value["modules"]}
        (destination / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        (destination / "modules.txt").write_text("\n".join(sorted(value["modules"])) + "\n")
    else:
        raise ValueError("unknown provisioning operation")


if __name__ == "__main__":
    main()
