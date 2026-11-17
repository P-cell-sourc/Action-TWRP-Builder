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
按 build/make/core/Makefile 里 add-vbmeta-image 的四项非空校验逐个补齐：

    BOARD_AVB_<X>_KEY_PATH              <- BOARD_AVB_VENDOR_BOOT_KEY_PATH
                                           （没有就用 external/avb/test/data/testkey_rsa2048.pem）
    BOARD_AVB_<X>_ALGORITHM             <- BOARD_AVB_VENDOR_BOOT_ALGORITHM
                                           （没有就用 SHA256_RSA2048）
    BOARD_AVB_<X>_ROLLBACK_INDEX        <- 0
    BOARD_AVB_<X>_ROLLBACK_INDEX_LOCATION <- 取一个未被占用的编号（从 1 开始）

四个变量缺任意一个，make 阶段都会直接 $(error)：

    build/make/core/Makefile:4815: error:
      BOARD_AVB_VBMETA_SYSTEM_ROLLBACK_INDEX is not defined.

幂等：缺什么补什么，全部齐了就不动文件；找不到 BoardConfig 或没有
chained 分区时静默退出。
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

    # rollback index location 必须在整机范围内唯一，先收集已被占用的编号
    used_locations = set()
    for m in re.finditer(r"^\s*BOARD_AVB_[A-Z0-9_]+_ROLLBACK_INDEX_LOCATION\s*[:+]?=\s*(\d+)", combined, re.M):
        used_locations.add(int(m.group(1)))

    def next_location():
        n = 1
        while n in used_locations:
            n += 1
        used_locations.add(n)
        return n

    # build/make/core/Makefile 的 add-vbmeta-image 对四个变量都做了非空校验，
    # 缺任何一个都会 $(error)，所以逐个检查、缺什么补什么。
    blocks = []
    added = []
    for name in chained:
        full = "BOARD_AVB_" + name
        lines = []
        if not var_defined(combined, full + "_KEY_PATH"):
            lines.append("%s_KEY_PATH := %s" % (full, key))
        if not var_defined(combined, full + "_ALGORITHM"):
            lines.append("%s_ALGORITHM := %s" % (full, algorithm))
        if not var_defined(combined, full + "_ROLLBACK_INDEX"):
            lines.append("%s_ROLLBACK_INDEX := 0" % full)
        if not var_defined(combined, full + "_ROLLBACK_INDEX_LOCATION"):
            lines.append("%s_ROLLBACK_INDEX_LOCATION := %d" % (full, next_location()))
        if not lines:
            continue
        head = MARKER if MARKER not in combined else MARKER + "（补充）"
        blocks.append("\n".join([""] + [head] + lines))
        added.append(name)

    if not blocks:
        print("设备树 AVB：%s 的 chained 分区配置完整，无需修改" % tree)
        return 0

    text = main.read_text(encoding="utf-8", errors="surrogateescape")
    if not text.endswith("\n"):
        text += "\n"
    text += "\n".join(blocks) + "\n"
    main.write_text(text, encoding="utf-8", errors="surrogateescape")

    print("设备树 AVB：已写入 %s" % main)
    for line in "\n".join(blocks).splitlines():
        if line.strip().startswith("BOARD_AVB_"):
            print("  " + line.strip())
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
