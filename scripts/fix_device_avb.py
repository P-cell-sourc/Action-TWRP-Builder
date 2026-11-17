#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
补全设备树里 chained AVB 分区缺失的签名 key。

背景
----
AOSP 14 的 soong 在解析 Android.bp 阶段会跑 build/soong/fsgen，
为 BOARD_AVB_VBMETA_* 声明的每个 chained 分区准备签名参数。只声明了分区、
没给 KEY_PATH 时直接抛：

    error: build/soong/fsgen/Android.bp:32:1: module "soong_filesystem_creator":
      No key found for chained avb partition "vbmeta_system"
    04:37:10 soong bootstrap failed with: exit status 1

这类设备树通常只写了

    BOARD_AVB_ENABLE := true
    BOARD_AVB_VBMETA_SYSTEM := system
    BOARD_AVB_VENDOR_BOOT_KEY_PATH := external/avb/test/data/testkey_rsa2048.pem
    BOARD_AVB_VENDOR_BOOT_ALGORITHM := SHA256_RSA2048

漏掉的是 BOARD_AVB_VBMETA_SYSTEM_KEY_PATH —— 也就是 chained vbmeta 自己的 key。
因为报错发生在 soong bootstrap 阶段，整次编译只跑了 1 分钟就结束，
看起来像“环境问题”，实际是设备树配置不全。

做法
----
扫描设备树里所有 BoardConfig*.mk，找出 BOARD_AVB_VBMETA_<X> 的定义，
若对应的 BOARD_AVB_VBMETA_<X>_KEY_PATH 不存在，就按下面规则补上：

    key     = 设备树里已有的 BOARD_AVB_VENDOR_BOOT_KEY_PATH
              （没有就用 external/avb/test/data/testkey_rsa2048.pem）
    algorithm = 设备树里已有的 BOARD_AVB_VENDOR_BOOT_ALGORITHM
              （没有就用 SHA256_RSA2048）

只加 KEY_PATH 与 ALGORITHM 两个变量：fsgen 只要求这两项，
rollback index 之类保持缺省，避免影响设备原有的回滚语义。

幂等：带固定标记注释；找不到 BoardConfig 或没有 chained 分区时静默退出。
"""

import os
import re
import sys
from pathlib import Path

MARKER = "# [twrp-builder-fix] chained vbmeta 分区的签名 key"

DEFAULT_KEY = "external/avb/test/data/testkey_rsa2048.pem"
DEFAULT_ALGORITHM = "SHA256_RSA2048"

# BOARD_AVB_VBMETA_SYSTEM := system
CHAIN_RE = re.compile(r"^\s*(BOARD_AVB_(VBMETA_[A-Z0-9_]+))\s*[:+]?=\s*(.*)$")

# 这些是 chained 分区的属性变量，不是分区声明本身
SKIP_SUFFIX = (
    "_KEY_PATH",
    "_ALGORITHM",
    "_ROLLBACK_INDEX",
    "_ROLLBACK_INDEX_LOCATION",
    "_MIN_PARTITION_SIZE",
    "_INCLUDE_DESCRIPTOR",
    "_PARTITION_SIZE",
)


def var_defined(text: str, name: str) -> bool:
    """判断 makefile 里是否给某个变量赋过值（= := ?= += 均可）。"""
    return re.search(r"^\s*" + re.escape(name) + r"\s*[:+?]?=", text, re.M) is not None


def get_var(text: str, name: str):
    m = re.search(r"^\s*" + re.escape(name) + r"\s*[:+?]?=\s*(\S+)", text, re.M)
    return m.group(1) if m else None


def collect_board_configs(root: Path):
    """返回 (主 BoardConfig.mk, [所有 BoardConfig*.mk])"""
    files = sorted(root.rglob("BoardConfig*.mk"))
    # 只认设备树自身，不跟进 .git 里的内容
    files = [f for f in files if ".git/" not in str(f)]
    if not files:
        return None, []
    main = root / "BoardConfig.mk"
    if not main.is_file():
        main = files[0]
    return main, files


def process_tree(tree: Path) -> int:
    main, files = collect_board_configs(tree)
    if not main:
        print("设备树 AVB：%s 下没有 BoardConfig*.mk，跳过" % tree)
        return 0

    combined = "\n".join(f.read_text(encoding="utf-8", errors="surrogateescape") for f in files)

    if MARKER in combined:
        print("设备树 AVB：%s 已处理过，跳过" % tree)
        return 0

    chained = []
    for line in combined.splitlines():
        m = CHAIN_RE.match(line)
        if not m:
            continue
        name = m.group(2)
        if name.endswith(SKIP_SUFFIX):
            continue
        if name not in chained:
            chained.append(name)

    if not chained:
        print("设备树 AVB：%s 没有 BOARD_AVB_VBMETA_* 声明，跳过" % tree)
        return 0

    key = get_var(combined, "BOARD_AVB_VENDOR_BOOT_KEY_PATH") or DEFAULT_KEY
    algorithm = get_var(combined, "BOARD_AVB_VENDOR_BOOT_ALGORITHM") or DEFAULT_ALGORITHM

    blocks = []
    added = []
    for name in chained:
        full = "BOARD_AVB_" + name
        if var_defined(combined, full + "_KEY_PATH"):
            continue
        lines = ["", MARKER, "%s_KEY_PATH := %s" % (full, key)]
        if not var_defined(combined, full + "_ALGORITHM"):
            lines.append("%s_ALGORITHM := %s" % (full, algorithm))
        blocks.append("\n".join(lines))
        added.append(name)

    if not blocks:
        print("设备树 AVB：%s 的 chained 分区都已有 key，无需修改" % tree)
        return 0

    text = main.read_text(encoding="utf-8", errors="surrogateescape")
    if not text.endswith("\n"):
        text += "\n"
    text += "\n".join(blocks) + "\n"
    main.write_text(text, encoding="utf-8", errors="surrogateescape")

    print("设备树 AVB：已写入 %s" % main)
    for name in added:
        print("  BOARD_AVB_%s_KEY_PATH := %s" % (name, key))
    return 0


def main(argv):
    trees = argv[1:] or ["."]
    for t in trees:
        p = Path(t)
        if not p.is_dir():
            print("设备树 AVB：目录不存在，跳过：%s" % t)
            continue
        process_tree(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
