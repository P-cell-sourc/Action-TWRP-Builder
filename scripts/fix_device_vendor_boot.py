#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给设备树 BoardConfig 补齐 vendor_boot 构建所需的变量（幂等）。

核心问题
--------
AOSP 判断是否构建 vendor_boot.img 的变量是

    BOARD_USES_VENDOR_BOOTIMAGE     （BOOTIMAGE，不是 BOOT）

写错成 BOARD_USES_VENDOR_BOOT 时，build/make/core/Makefile 里
`ifeq ($(BOARD_USES_VENDOR_BOOTIMAGE),true)` 整个分支被跳过，
`vendorbootimage` 于是成为一个「没有依赖、也没有配方」的空目标：

    $ make vendorbootimage
    make: Nothing to be done for 'vendorbootimage'.
    $ echo $?
    0

退出码 0，但 out/target/product/<device>/ 下不会出现 vendor_boot.img。
这就是「编译成功、却没有镜像」的真正原因。

顺带补齐
--------
1. BOARD_MOVE_RECOVERY_RESOURCES_TO_VENDOR_BOOT := true
   把 recovery（TWRP）的 root/ 与 ramdisk 挪进 vendor_boot；
2. BOARD_AVB_VENDOR_BOOT_KEY_PATH / _ALGORITHM / _ROLLBACK_INDEX /
   _ROLLBACK_INDEX_LOCATION
   缺这四项时 vendor_boot.img 不带 AVB hash footer。有些机器不允许刷
   vbmeta 去关校验，镜像自身带 footer 才有机会过；
   回滚槽位自动取 1..15 里最小的未占用编号（BOOT 通常已占 3）。

用法
----
    python3 fix_device_vendor_boot.py <源码根目录> [BoardConfig 相对路径]
"""

import os
import re
import shutil
import sys

MARK = "  # [twrp-builder-fix]"

# (变量名, 变量名匹配用的正则)
ASSIGN_RE = r"^\s*{name}\s*:?="

# 需要保证存在的键值对
AVB_KEYS = {
    "KEY_PATH": "external/avb/test/data/testkey_rsa2048.pem",
    "ALGORITHM": "SHA256_RSA2048",
}


def find_boardconfig(root, hint=None):
    if hint:
        p = hint if os.path.isabs(hint) else os.path.join(root, hint)
        for name in ("BoardConfig.mk", "BoardConfigCommon.mk"):
            if os.path.isfile(os.path.join(p, name)):
                return os.path.join(p, name)
        if os.path.isfile(p):
            return p
    # 常规位置：<DEVICE_PATH>/BoardConfig.mk
    best = None
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if re.match(r"^BoardConfig.*\.mk$", fn):
                cand = os.path.join(dirpath, fn)
                if best is None or len(cand) < len(best):
                    best = cand
    return best


def has_var(lines, name):
    pat = re.compile(ASSIGN_RE.format(name=re.escape(name)))
    return any(pat.match(l) for l in lines)


def get_value(lines, name):
    pat = re.compile(r"^\s*" + re.escape(name) + r"\s*:?=\s*(.*?)\s*(?:#.*)?$")
    for l in reversed(lines):
        m = pat.match(l)
        if m:
            return m.group(1).strip()
    return None


def set_var(lines, name, value, anchor=None, comment=None):
    """把 name 设为 value：已存在则原地替换，否则追加到文件末尾。"""
    pat = re.compile(r"^(\s*)" + re.escape(name) + r"\s*:?=\s*(.*?)\s*(?:#.*)?$")
    for i, l in enumerate(lines):
        m = pat.match(l)
        if m:
            if m.group(2).strip() == str(value):
                return False
            lines[i] = "%s%s := %s%s" % (m.group(1), name, value, MARK)
            return True
    block = []
    if comment:
        block.append("# " + comment + "\n")
    block.append("%s := %s%s\n" % (name, value, MARK))
    if anchor:
        apat = re.compile(r"^\s*" + re.escape(anchor) + r"\s*:?=")
        for i, l in enumerate(lines):
            if apat.match(l):
                lines[i + 1:i + 1] = block
                return True
    if lines and lines[-1].strip():
        block = ["\n"] + block
    lines.extend(block)
    return True


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    hint = sys.argv[2] if len(sys.argv) > 2 else None
    path = find_boardconfig(root, hint)
    if not path:
        print("未找到 BoardConfig，跳过")
        return 0

    rel = os.path.relpath(path, root)
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        text = f.read()
    lines = text.splitlines(True)
    changed = []

    # ── 1. 变量名纠错：BOARD_USES_VENDOR_BOOT -> BOARD_USES_VENDOR_BOOTIMAGE
    alias = re.compile(r"^(\s*)BOARD_USES_VENDOR_BOOT\s*:?=\s*(true|1)\s*$", re.I)
    fixed_alias = False
    for i, l in enumerate(lines):
        if alias.match(l):
            indent = alias.match(l).group(1)
            lines[i] = ("%sBOARD_USES_VENDOR_BOOTIMAGE := true"
                        "   # [twrp-builder-fix] 原 BOARD_USES_VENDOR_BOOT 不是 AOSP 变量\n" % indent)
            fixed_alias = True
            changed.append("BOARD_USES_VENDOR_BOOT -> BOARD_USES_VENDOR_BOOTIMAGE（变量名纠错）")

    if has_var(lines, "BOARD_USES_VENDOR_BOOTIMAGE"):
        pass
    else:
        # 没有这个变量就直接补上（拼写正确的那份）
        if set_var(lines, "BOARD_USES_VENDOR_BOOTIMAGE", "true"):
            changed.append("BOARD_USES_VENDOR_BOOTIMAGE := true")

    # ── 2. 让 recovery 资源进 vendor_boot
    if set_var(lines, "BOARD_MOVE_RECOVERY_RESOURCES_TO_VENDOR_BOOT", "true",
               comment="TWRP：把 recovery root/ramdisk 挪进 vendor_boot"):
        changed.append("BOARD_MOVE_RECOVERY_RESOURCES_TO_VENDOR_BOOT := true")

    # ── 3. header version：vendor_boot 只能是 3 或 4
    hv = get_value(lines, "BOARD_BOOT_HEADER_VERSION")
    if hv in (None, ""):
        set_var(lines, "BOARD_BOOT_HEADER_VERSION", "4")
        changed.append("BOARD_BOOT_HEADER_VERSION := 4")
    elif hv not in ("3", "4"):
        set_var(lines, "BOARD_BOOT_HEADER_VERSION", "4")
        changed.append("BOARD_BOOT_HEADER_VERSION : %s -> 4" % hv)

    # ── 4. AVB：vendor_boot 也要 hash footer
    avb_on = (get_value(lines, "BOARD_AVB_ENABLE") or "").lower() in ("true", "1")
    if avb_on:
        if set_var(lines, "BOARD_AVB_VENDOR_BOOT_KEY_PATH", AVB_KEYS["KEY_PATH"],
                   anchor="BOARD_AVB_BOOT_KEY_PATH"):
            changed.append("BOARD_AVB_VENDOR_BOOT_KEY_PATH")
        if set_var(lines, "BOARD_AVB_VENDOR_BOOT_ALGORITHM", AVB_KEYS["ALGORITHM"],
                   anchor="BOARD_AVB_VENDOR_BOOT_KEY_PATH"):
            changed.append("BOARD_AVB_VENDOR_BOOT_ALGORITHM := %s" % AVB_KEYS["ALGORITHM"])

        # 回滚索引沿用 boot 的写法，保持一致
        boot_rb = get_value(lines, "BOARD_AVB_BOOT_ROLLBACK_INDEX")
        if not has_var(lines, "BOARD_AVB_VENDOR_BOOT_ROLLBACK_INDEX"):
            set_var(lines, "BOARD_AVB_VENDOR_BOOT_ROLLBACK_INDEX",
                    boot_rb if boot_rb else "0",
                    anchor="BOARD_AVB_BOOT_ROLLBACK_INDEX")
            changed.append("BOARD_AVB_VENDOR_BOOT_ROLLBACK_INDEX := %s" % (boot_rb or "0"))

        # 回滚槽位：取 1..15 中未被占用的最小号
        if not has_var(lines, "BOARD_AVB_VENDOR_BOOT_ROLLBACK_INDEX_LOCATION"):
            used = set()
            for k in ("BOOT", "VBMETA_SYSTEM", "VBMETA_VENDOR", "VBMETA", "INIT_BOOT",
                      "SYSTEM", "VENDOR", "PRODUCT", "SYSTEM_EXT", "DTBO"):
                v = get_value(lines, "BOARD_AVB_%s_ROLLBACK_INDEX_LOCATION" % k)
                if v and v.isdigit():
                    used.add(int(v))
            loc = next((n for n in range(1, 16) if n not in used), 4)
            set_var(lines, "BOARD_AVB_VENDOR_BOOT_ROLLBACK_INDEX_LOCATION", str(loc),
                    anchor="BOARD_AVB_VENDOR_BOOT_ROLLBACK_INDEX")
            changed.append("BOARD_AVB_VENDOR_BOOT_ROLLBACK_INDEX_LOCATION := %d" % loc)

    if not changed:
        print("%s：vendor_boot 配置已完整，无需修改" % rel)
        return 0

    shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("".join(lines))

    print("已修补 %s：" % rel)
    for c in changed:
        print("  + " + c)
    print("当前 BOARD_USES_VENDOR_BOOTIMAGE = %s" % (get_value(lines, "BOARD_USES_VENDOR_BOOTIMAGE") or "<未定义>"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
