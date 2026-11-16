#!/usr/bin/env python3
"""补齐设备树漏掉的 TARGET_COPY_OUT_<partition> 定义。

build/make/core/board_config.mk 里的 check_image_config 宏规定：
只要设备树声明了 BOARD_<P>IMAGE_FILE_SYSTEM_TYPE 或 BOARD_PREBUILT_<P>IMAGE，
就必须把 <P> 作为独立分区输出，即 TARGET_COPY_OUT_<P> := <P>；
否则该分区会被当作 system（或 vendor）下的子目录，两者矛盾，直接 $(error)：

    build/make/core/board_config.mk:758: error:
    TARGET_COPY_OUT_SYSTEM_EXT must be set to 'system_ext' to use a system_ext image.

twrpdtgen 生成的 BoardConfig 常只补了 vendor / product / odm / vendor_dlkm，
漏掉 system_ext，于是 lunch 直接失败。

用法：
    fix_copy_out.py <device_tree_dir> [common_tree_dir]

重复执行安全（已存在的定义会跳过）。
"""

import os
import re
import sys

PARTS = ["system_ext", "product", "odm", "vendor_dlkm", "odm_dlkm", "system_dlkm"]

MARKER = "# [auto-fix]"


def board_configs(tree):
    """收集树里所有 BoardConfig*.mk（只看文件名，避免扫全树太慢）。"""
    found = []
    for root, dirs, files in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in (".git", "out")]
        for name in files:
            if name.startswith("BoardConfig") and name.endswith(".mk"):
                found.append(os.path.join(root, name))
    return sorted(found)


def assigned(name):
    """匹配 NAME := / NAME = / NAME += / NAME ?= 形式的赋值（忽略注释行）。"""
    return re.compile(r"^[ \t]*%s[ \t]*[:+?]?=" % re.escape(name), re.MULTILINE)


def main():
    if len(sys.argv) < 2:
        print("用法: fix_copy_out.py <device_tree> [common_tree]")
        return 1

    dev = sys.argv[1]
    common = sys.argv[2] if len(sys.argv) > 2 else ""
    trees = [t for t in (dev, common) if t and os.path.isdir(t)]

    if not trees:
        print("未找到设备树目录，跳过 COPY_OUT 检查")
        return 0

    texts = {}
    for tree in trees:
        for path in board_configs(tree):
            try:
                with open(path, errors="ignore") as fh:
                    texts[path] = fh.read()
            except OSError:
                pass

    if not texts:
        print("未找到 BoardConfig*.mk，跳过 COPY_OUT 检查")
        return 0

    print("扫描 %d 个 BoardConfig 文件：%s" % (len(texts), ", ".join(texts)))

    for part in PARTS:
        up = part.upper()
        fs_type = "BOARD_%sIMAGE_FILE_SYSTEM_TYPE" % up
        prebuilt = "BOARD_PREBUILT_%sIMAGE" % up
        copy_out = "TARGET_COPY_OUT_%s" % up

        if not any(assigned(fs_type).search(v) or assigned(prebuilt).search(v) for v in texts.values()):
            continue

        if any(assigned(copy_out).search(v) for v in texts.values()):
            print("%-45s 已定义，跳过" % copy_out)
            continue

        # 优先写到设备树顶层 BoardConfig.mk（它在 include 之后，能覆盖 common 树）
        target = os.path.join(dev, "BoardConfig.mk") if dev else ""
        if not target or not os.path.isfile(target):
            cands = [f for f in texts if os.path.basename(f) == "BoardConfig.mk"]
            if not cands:
                print("::warning::%s 声明了 %s，但找不到可写的 BoardConfig.mk" % (fs_type, part))
                continue
            target = cands[0]

        with open(target, "a") as fh:
            fh.write(
                "\n%s 声明了 %s，%s 必须作为独立分区输出，\n"
                "# 否则 board_config.mk 的 check_image_config 会直接 $(error)\n"
                "%s := %s\n" % (MARKER, fs_type, part, copy_out, part)
            )
        print("已追加到 %s：%s := %s" % (target, copy_out, part))
        texts[target] = texts.get(target, "") + "\n%s := %s\n" % (copy_out, part)

    return 0


if __name__ == "__main__":
    sys.exit(main())
