#!/usr/bin/env python3
"""Unit tests for the os-keyring launch-dir shim (zero model spend).

ZCode app 3.18.2 encrypts ~/.zcode/v2/credentials.json with an OS-keyring
secret; the standalone headless CLI can only decrypt it through
@napi-rs/keyring, which the /opt bundle cannot import (the package lives in
the Electron app's asar). scripts/zcode_common.py answers with a private
launch copy of the bundle that carries the app's keyring packages on its
module resolution path. These tests exercise that machinery against a fake
app layout: asar parsing, extraction/symlinking, freshness reuse, rebuild on
app update, and the no-op gates. Run: python3 tests/test_zcode_keyring_launch.py
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import zcode_common as zc  # noqa: E402

failures = []


def check(name: str, cond, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def add_tree_file(tree: dict, rel: str, *, size=None, offset=None, unpacked=False) -> None:
    parts = rel.split("/")
    node = tree
    for p in parts[:-1]:
        node = node.setdefault("files", {}).setdefault(p, {"files": {}})
    entry = {}
    if size is not None:
        entry["size"] = size
    if offset is not None:
        entry["offset"] = str(offset)
    if unpacked:
        entry["unpacked"] = True
    node["files"][parts[-1]] = entry


def build_asar(path: Path, packed: dict, unpacked: list) -> None:
    """Write a minimal but format-true app.asar: 16-byte pickle header + JSON
    directory (4-byte aligned) + concatenated file payloads. Directory offsets
    are relative to the end of the header (as in real asar files), so they do
    not depend on the JSON length."""
    tree = {"files": {}}
    for rel in unpacked:
        add_tree_file(tree, rel, unpacked=True)
    cur = 0
    for rel, blob in packed.items():
        add_tree_file(tree, rel, size=len(blob), offset=cur)
        cur += len(blob)
    j = json.dumps(tree).encode("utf-8")
    header = struct.pack("<IIII", 4, len(j) + 9, len(j) + 4, len(j)) + j
    header += b"\0" * ((-len(j)) % 4)
    with path.open("wb") as f:
        f.write(header)
        for blob in packed.values():
            f.write(blob)


def fake_app(root: Path) -> Path:
    """A fake ZCode app: resources/{glm/zcode.cjs, config/provider, asar}."""
    res = root / "resources"
    (res / "glm").mkdir(parents=True)
    (res / "glm" / "zcode.cjs").write_text("// fake cli bundle v1\n" + "x" * 64, encoding="utf-8")
    (res / "config" / "provider").mkdir(parents=True)
    (res / "config" / "provider" / "zcode-builtin.json").write_text('{"schemaVersion":1}', encoding="utf-8")
    packed = {
        "node_modules/@napi-rs/keyring/index.js": b"// keyring wrapper js\n",
        "node_modules/@napi-rs/keyring/package.json": b'{"name":"@napi-rs/keyring","main":"index.js"}',
        "node_modules/@napi-rs/keyring-linux-x64-gnu/package.json": b'{"name":"@napi-rs/keyring-linux-x64-gnu"}',
    }
    unpacked = ["node_modules/@napi-rs/keyring-linux-x64-gnu/keyring.linux-x64-gnu.node"]
    build_asar(res / "app.asar", packed, unpacked)
    up = res / "app.asar.unpacked" / "node_modules" / "@napi-rs" / "keyring-linux-x64-gnu"
    up.mkdir(parents=True)
    (up / "keyring.linux-x64-gnu.node").write_bytes(b"\x7fELF-fake-native-binding")
    return res / "glm" / "zcode.cjs"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="zc-keyring-test-") as td:
        tmp = Path(td)
        cjs = fake_app(tmp / "app")
        launch_root = tmp / "launch-root"
        zc.LAUNCH_ROOT = launch_root

        print("scenario 1: gates keep the original bundle when they do not apply")
        real_creds, real_resolvable = zc._credentials_use_os_keyring, zc._keyring_resolvable
        zc._credentials_use_os_keyring = lambda: False
        check("no shim when credentials are not os-encrypted",
              zc._keyring_aware_cjs(cjs, dry_run=False) == cjs)
        zc._credentials_use_os_keyring = lambda: True
        zc._keyring_resolvable = lambda c: True
        check("no shim when keyring already resolvable",
              zc._keyring_aware_cjs(cjs, dry_run=False) == cjs)
        zc._keyring_resolvable = real_resolvable
        zc._credentials_use_os_keyring = real_creds
        check("dry run never builds", zc._keyring_aware_cjs(cjs, dry_run=True) == cjs)

        print("scenario 2: launch dir builds with the right layout")
        zc.log = lambda msg: None  # keep test output clean
        built = zc._build_keyring_launch_dir(cjs)
        check("returns a launch cjs under the keyed root",
              built is not None and launch_root in built.parents and built.name == "zcode.cjs", str(built))
        assert built
        check("bundle copied byte-identical",
              built.read_bytes() == cjs.read_bytes())
        # node_modules sits at the launch root: node's walk-up from glm/zcode.cjs
        # checks glm/node_modules (absent) then the root's node_modules
        nm = built.parent.parent / "node_modules" / "@napi-rs"
        check("keyring wrapper extracted from asar",
              (nm / "keyring" / "index.js").read_bytes() == b"// keyring wrapper js\n")
        check("platform package.json extracted",
              (nm / "keyring-linux-x64-gnu" / "package.json").is_file())
        bind = nm / "keyring-linux-x64-gnu" / "keyring.linux-x64-gnu.node"
        check("unpacked native binding linked to the app file",
              bind.is_symlink() and bind.resolve()
              == (cjs.parent.parent / "app.asar.unpacked" / "node_modules" / "@napi-rs"
                  / "keyring-linux-x64-gnu" / "keyring.linux-x64-gnu.node").resolve())
        cfg = built.parent.parent / "config" / "provider" / "zcode-builtin.json"
        check("builtin catalog reachable through sibling config/",
              (cfg.is_file() or cfg.is_symlink())
              and cfg.read_text(encoding="utf-8") == '{"schemaVersion":1}')
        check(".ready marker written", (built.parent.parent / ".ready").is_file())
        check("keyring now resolvable from the launch copy",
              zc._keyring_resolvable(built))

        print("scenario 3: freshness reuse and rebuild on app update")
        first = zc._build_keyring_launch_dir(cjs)
        check("second build reuses the same dir", first == built)
        old_dirs = list(launch_root.iterdir())
        time.sleep(0.02)
        with cjs.open("a") as f:  # app update: bundle changes size+mtime
            f.write("v2")
        second = zc._build_keyring_launch_dir(cjs)
        check("changed bundle gets a NEW launch dir",
              second is not None and second != built, f"{second} vs {built}")
        check("old launch dir left in place (no live clobbering)",
              sorted(p.name for p in old_dirs)[0] in {p.name for p in launch_root.iterdir()})

        print("scenario 4: broken layouts degrade to None (caller falls back)")
        bare = tmp / "bare" / "resources" / "glm" / "zcode.cjs"
        bare.parent.mkdir(parents=True)
        shutil.copy2(cjs, bare)
        check("no asar -> None", zc._build_keyring_launch_dir(bare) is None)
        noscript = tmp / "noscript" / "resources"
        noscript.mkdir(parents=True)
        (noscript / "app.asar.unpacked").mkdir()
        (noscript / "app.asar").write_bytes(b"not an asar")
        bare2 = noscript / "glm" / "zcode.cjs"
        bare2.parent.mkdir(parents=True)
        shutil.copy2(cjs, bare2)
        check("unparseable asar -> None", zc._build_keyring_launch_dir(bare2) is None)

        print("scenario 5: the 3.18.2 error shape gets the keyring hint")
        hint = zc._zcode_failure_hint(
            "Error: Select a model before sending a message (traceId: t)")
        check("new wording matched",
              "os-keyring-encrypted" in hint and "zcode login" in hint, hint[:120])
        old_hint = zc._zcode_failure_hint(
            "Error: Select a model before continuing (traceId: t)")
        check("0.16.9 wording still matched", "zcode login" in old_hint)
        check("unrelated stderr passes through",
              zc._zcode_failure_hint("some other failure") == "some other failure")

        print("scenario 6: explicit --zcode-cli .cjs paths get the same shim")
        if shutil.which("node"):
            zc._credentials_use_os_keyring = lambda: True
            zc._keyring_resolvable = real_resolvable
            cmd = zc._zcode_explicit_cmd(str(cjs))
            zc._credentials_use_os_keyring = real_creds
            check("explicit bundle routed through the launch copy",
                  len(cmd) == 2 and cmd[0] == shutil.which("node")
                  and launch_root in Path(cmd[1]).parents, str(cmd))
        else:
            print("SKIP explicit-cmd routing (no node on PATH)")

    print(failures and f"\n{len(failures)} FAILURE(S): {failures}" or "\nALL SCENARIOS PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
