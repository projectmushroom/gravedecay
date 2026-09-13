#!/usr/bin/env python3
"""Seal and verify local backup inventories. Checksums detect damage, not forgery."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


def regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"not a regular file: {path.name}")


def inventory(root):
    files = {}

    def walk_error(error):
        raise error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("symlink in backup inventory")
        for name in names:
            path = Path(directory) / name
            regular(path)
            relative = path.relative_to(root).as_posix()
            if relative != "manifest.json":
                files[relative] = path
    if "configs/grave-platform.tar.gz" not in files:
        raise ValueError("platform archive missing")
    return files


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def run(action, root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("backup directory missing or symlinked")
    manifest_path = root / "manifest.json"
    regular(manifest_path)
    if manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("backup manifest too large")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
        raise ValueError("invalid backup manifest")
    if manifest["version"] == 1 and action == "preflight":
        print("WARNING: legacy backup has no checksums; integrity cannot be established", file=sys.stderr)
        return
    if action == "seal":
        if manifest["version"] != 1:
            raise ValueError("only a newly created backup can be sealed")
        files = inventory(root)
        manifest.update(version=2, artifacts={
            name: {"size": path.stat().st_size, "sha256": digest(path)}
            for name, path in sorted(files.items())
        })
        # Staging is private and unpublished until this write completes.
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    else:
        if manifest["version"] != 2:
            raise ValueError("backup has no supported checksum inventory; create a new backup")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            raise ValueError("empty or invalid checksum inventory")
        for name, expected in artifacts.items():
            parts = PurePosixPath(name).parts
            if (not parts or parts[0] not in {"repos", "configs", "volumes"}
                    or ".." in parts or str(PurePosixPath(name)) != name
                    or not isinstance(expected, dict)
                    or type(expected.get("size")) is not int or expected["size"] < 0
                    or not isinstance(expected.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])):
                raise ValueError("invalid artifact record")
        files = inventory(root)
        if set(files) != set(artifacts):
            raise ValueError("backup inventory differs: missing or unexpected artifacts")
        for name, path in sorted(files.items()):
            expected = artifacts[name]
            if path.stat().st_size != expected["size"]:
                raise ValueError(f"size mismatch: {name}")
            if action != "check" and digest(path) != expected["sha256"]:
                raise ValueError(f"checksum mismatch: {name}")
    label = {"check": "inventory checked (sizes only)", "seal": "checksums recorded"}.get(action, "checksums verified")
    print(f"backup {label}: {len(files)} artifact(s)")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {"seal", "verify", "check", "preflight"}:
        print("usage: backup-integrity.py seal|verify|check|preflight <directory>", file=sys.stderr)
        return 2
    try:
        run(sys.argv[1], Path(sys.argv[2]))
    except (OSError, ValueError) as error:
        print(f"backup integrity failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
