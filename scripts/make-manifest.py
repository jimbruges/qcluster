#!/usr/bin/env python3
"""Writes runtime/manifest.json so the provisioner can tell when a node's copy is stale."""
import hashlib
import json
import pathlib
import sys

SKIP = {"manifest.json"}


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "runtime").resolve()
    files = {}
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name in SKIP:
            continue
        files[path.name] = {"sha256": sha256(path), "size": path.stat().st_size}
    manifest = {"version": 1, "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"manifest: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
