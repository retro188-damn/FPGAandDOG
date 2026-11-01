# -*- coding: utf-8 -*-
"""组装提交用的源码包目录（不压缩，交由用户自行用 Bandizip 压缩）。

用法：python tools/make_submission.py [--name 名字]
输出：提交/<名字>（请压缩）/

只复制 L3 可写内容；L1/L2 资料盘、_scratch、构建中间产物、凭据文件一律不进包。
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "提交"

# 目录级：整目录复制，但按下述规则排除
TREES = [
    ("host", ["__pycache__", "*.pyc"]),
    ("tools", ["__pycache__", "*.pyc", "hosts.local.json"]),
    ("tests", ["__pycache__", "*.pyc", ".pytest_cache"]),
    ("docs", []),
    ("metrics", ["__pycache__"]),
    ("fpga/src", []),
    ("fpga/sim", []),
]

SINGLE_FILES = [
    "fpga/build/build.tcl",
    "fpga/build/build_all.tcl",
    "fpga/build/build_chain.tcl",
    "requirements.txt",
    "AGENTS.md",
]

BITSTREAM = [
    ("fpga/build/safety_gate/safety_gate.runs/impl_1/system_wrapper.bit",
     "fpga/deliverable/system.bit"),
    ("fpga/build/safety_gate/safety_gate.gen/sources_1/bd/system/hw_handoff/system.hwh",
     "fpga/deliverable/system.hwh"),
]


def ignore_factory(patterns):
    pats = list(patterns)

    def _ignore(directory, names):
        drop = set()
        for name in names:
            for pat in pats:
                if pat.startswith("*") and name.endswith(pat[1:]):
                    drop.add(name)
                elif name == pat:
                    drop.add(name)
            if name == "__pycache__":
                drop.add(name)
        return drop

    return _ignore


def copy_tree(src: Path, dst: Path, patterns):
    if not src.exists():
        print("  skip (missing):", src.relative_to(ROOT))
        return 0
    shutil.copytree(src, dst, ignore=ignore_factory(patterns), dirs_exist_ok=True)
    return sum(1 for _ in dst.rglob("*") if _.is_file())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="FPGAandDOG", help="作品名，用于文件夹名")
    args = ap.parse_args()

    target = OUT_ROOT / ("%s（请压缩）" % args.name)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    total = 0
    for rel, patterns in TREES:
        n = copy_tree(ROOT / rel, target / rel, patterns)
        print("  %-12s %d files" % (rel, n))
        total += n

    # 证据包：日志/CSV/自检输出，和代码一起交上去，评审可复跑
    ev = ROOT / "提交" / "证据"
    if ev.exists():
        n = copy_tree(ev, target / "证据", [])
        print("  %-12s %d files" % ("证据", n))
        total += n

    for rel in SINGLE_FILES:
        src = ROOT / rel
        if src.exists():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target / rel)
            total += 1

    for src_rel, dst_rel in BITSTREAM:
        src = ROOT / src_rel
        if src.exists():
            (target / dst_rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target / dst_rel)
            total += 1
            print("  bitstream:", dst_rel, "%.1f MB" % (src.stat().st_size / 1e6))
        else:
            print("  MISSING bitstream:", src_rel)

    readme_src = ROOT / "tools" / "submission_readme.md"
    if readme_src.exists():
        shutil.copy2(readme_src, target / "README.md")
        total += 1

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print("\noutput: %s" % target)
    print("files: %d, size: %.1f MB" % (total, size / 1e6))
    if size > 200e6:
        print("WARNING: 超过 200 MB 上限")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
