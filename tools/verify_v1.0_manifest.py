"""v1.0 manifest 完整性校验（git 感知，Windows CRLF 无关）。

用法:
    .venv/Scripts/python tools/verify_v1.0_manifest.py [--manifest PATH]

策略（与 manifest.hash_policy 一致）:
  - git 跟踪文件: sha256(git show v1.0:<path>) —— 锚 v1.0 tag blob，
    行尾(CRLF/LF)无关, tag 不可变 → 永久可校验
  - data/ 本地产物(不入 git): sha256(open(path,'rb')) —— 本机防漂移

退出码: 0 = 全部 OK; 1 = 存在 MISMATCH/MISSING
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = "v1.0"


def git_show(rel):
    r = subprocess.run(
        ["git", "show", f"{TAG}:{rel}"],
        capture_output=True,
        cwd=BASE,
    )
    return r.stdout if r.returncode == 0 else None


def tracked(rel):
    r = subprocess.run(
        ["git", "ls-files", "--", rel], capture_output=True, text=True, cwd=BASE
    )
    return bool(r.stdout.strip())


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(
        BASE, "data/exports/releases/v1.0_manifest.json"))
    args = ap.parse_args()

    m = json.load(open(args.manifest, encoding="utf-8"))
    files = m["files"]
    ok, mismatch, missing, skipped = [], [], [], []

    for f in files:
        rel = f["path"]
        want = f["sha256"]
        if tracked(rel):
            b = git_show(rel)
            if b is None:
                # tag 中不存在（理论上不出现：manifest 只含 v1.0 交付文件）
                missing.append((rel, "not in tag %s" % TAG))
                continue
            got = sha256(b)
            label = "git-blob"
        else:
            p = os.path.join(BASE, rel.replace("/", os.sep))
            if not os.path.exists(p):
                missing.append((rel, "local file absent"))
                continue
            got = sha256(open(p, "rb").read())
            label = "local-file"

        if got == want:
            ok.append((rel, label))
        else:
            mismatch.append((rel, label, want[:12], got[:12]))

    print(f"[v1.0 manifest] {len(files)} files | tag={TAG}")
    print(f"  OK       : {len(ok)}")
    if ok and "--verbose" in sys.argv:
        for rel, label in ok:
            print(f"    OK  {label:10s} {rel}")
    for rel, why in missing:
        print(f"  MISSING  : {rel} ({why})")
    for rel, label, want, got in mismatch:
        print(f"  MISMATCH : {rel} [{label}] want={want} got={got}")

    clean = not mismatch and not missing
    print("\nRESULT:", "ALL OK" if clean else f"{len(mismatch)} MISMATCH + {len(missing)} MISSING")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
