#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 bootable/recovery 补上 AOSP 14 已删除的 FDE（全盘加密）API 占位。

背景
----
`bootable/recovery` 的 android-14.1 分支里，partition.cpp / partitionmanager.cpp
仍在 `#ifdef TW_INCLUDE_CRYPTO` 内调用一组 FDE 时代的函数：

    set_partition_data()
    cryptfs_check_footer()
    cryptfs_get_password_type()
    delete_crypto_blk_dev()
    cryptfs_check_passwd()

而 `system/vold` 的 android-14.1 分支已经把这批 API 连同 `union fscrypt_policy`
一起删干净了，cryptfs.h 里只剩 cryptfs_setup_ext_volume / cryptfs_get_keygen。
两个仓库同一分支对不上，编译到 86% 时报一串：

    bootable/recovery/partition.cpp:711:5: error:
        use of undeclared identifier 'set_partition_data'
    bootable/recovery/partition.cpp:712:9: error:
        use of undeclared identifier 'cryptfs_check_footer'
    ...

这属于 TWRP 14.1 自身未完成 AOSP 14 适配，不是设备树问题。

做法
----
不删代码、不改调用点、不打补丁替换语句（那会破坏 if/else 结构），
而是在 `bootable/recovery/cryptfs_fde_compat.h` 里给出语义正确的
static inline 占位，然后在调用方原本 `#include "cryptfs.h"` 的下一行
插入一行 include。

这样 vold 的 cryptfs.h 依旧生效（CRYPT_TYPE_* 等常量、KeyBuffer 类型
都还能拿到），只在它缺函数时由我们补位。

语义为什么是安全的
------------------
Android 13 起 vold 已完全不支持 FDE，所有设备都是 FBE。FBE 解密走
Decrypt_DE() / Decrypt_CE() + keymaster/gatekeeper，不经过 crypto footer。
这几个函数的调用点全部位于「FBE 建立失败，回退尝试 FDE」的兜底分支，
在真实设备上不会被走到；即便走到，占位返回的也是“没有 FDE footer /
密码不对”，和实际硬件状态一致，不会误判成功。

幂等：头文件带固定 guard；.cpp 里已有 include 标记就不再插入；
找不到 bootable/recovery 时静默退出（exit 0）。
"""

import os
import sys

HEADER_NAME = "cryptfs_fde_compat.h"
MARKER = "cryptfs_fde_compat"

# 只处理真正调用了这些函数的翻译单元
FUNCS = (
    "set_partition_data",
    "cryptfs_check_footer",
    "cryptfs_get_password_type",
    "delete_crypto_blk_dev",
    "cryptfs_check_passwd",
)

HEADER = r'''/*
 * cryptfs_fde_compat.h —— AOSP 14 (vold) 已删除的 FDE API 占位
 *
 * 由 Action-TWRP-Builder 的 scripts/fix_recovery_cryptfs.py 生成，请勿手改。
 *
 * Android 13 起 vold 彻底移除 FDE（全盘加密），cryptfs.h 里只保留
 * cryptfs_setup_ext_volume / cryptfs_get_keygen；但 bootable/recovery
 * 的 android-14.1 分支仍在调用 set_partition_data / cryptfs_check_footer /
 * cryptfs_get_password_type / delete_crypto_blk_dev / cryptfs_check_passwd，
 * 导致编译期 undeclared identifier。
 *
 * 这些调用都位于「FBE 失败 → 回退 FDE」的兜底分支，现代设备不会走到；
 * 占位返回“没有 FDE footer / 密码错误”，与真实的硬件状态一致。
 * 真正的 FBE 解密路径（Decrypt_DE / Decrypt_CE）不受任何影响。
 */

#ifndef TWRP_CRYPTFS_FDE_COMPAT_H
#define TWRP_CRYPTFS_FDE_COMPAT_H

#if defined(__GNUC__) || defined(__clang__)
#define TWRP_FDE_MAYBE_UNUSED __attribute__((unused))
#else
#define TWRP_FDE_MAYBE_UNUSED
#endif

/* vold 的 cryptfs.h 现在不再引入 fscrypt-common.h，
 * 但 partitionmanager.cpp 仍用这个常量做比较，这里兜底定义。 */
#ifndef CRYPT_TYPE_DEFAULT
#define CRYPT_TYPE_DEFAULT 1
#endif

/* 记录 block device 与 key location，供 cryptfs_check_footer 使用。
 * FDE 不存在，无需记录。 */
static inline TWRP_FDE_MAYBE_UNUSED
void set_partition_data(const char* path, const char* key_loc) {
    (void)path;
    (void)key_loc;
}

/* 非 0 表示“没有可用的 crypto footer”，调用方会走提示分支。 */
static inline TWRP_FDE_MAYBE_UNUSED
int cryptfs_check_footer(void) {
    return -1;
}

/* 返回 -1 而不是 CRYPT_TYPE_DEFAULT，避免把 FBE 设备误判成
 * “用默认密码加密”，进而触发一次必然失败的解密尝试。 */
static inline TWRP_FDE_MAYBE_UNUSED
int cryptfs_get_password_type(void) {
    return -1;
}

/* FDE 不存在 → 没有 dm-crypt 设备可删，视为成功，不刷错误日志。 */
static inline TWRP_FDE_MAYBE_UNUSED
int delete_crypto_blk_dev(char* blkdev) {
    (void)blkdev;
    return 0;
}

/* FDE 密码校验必然失败，与真实状态一致。 */
static inline TWRP_FDE_MAYBE_UNUSED
int cryptfs_check_passwd(char* passwd) {
    (void)passwd;
    return -1;
}

#endif /* TWRP_CRYPTFS_FDE_COMPAT_H */
'''


def read(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def find_insert_index(lines):
    """返回插入位置：优先跟在本地的 #include "cryptfs.h" 之后。"""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#include") and "cryptfs.h" in stripped:
            return i + 1, line[: len(line) - len(line.lstrip())]
    # 退路：放在第一处 #include 之后
    for i, line in enumerate(lines):
        if line.strip().startswith("#include"):
            return i + 1, line[: len(line) - len(line.lstrip())]
    return 0, ""


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    rec = os.path.join(root, "bootable", "recovery")
    if not os.path.isdir(rec):
        print("recovery FDE 兼容层：未找到 %s，跳过" % rec)
        return 0

    targets = []
    for name in sorted(os.listdir(rec)):
        if not name.endswith((".cpp", ".cc", ".c")):
            continue
        path = os.path.join(rec, name)
        if not os.path.isfile(path):
            continue
        try:
            text = read(path)
        except OSError:
            continue
        if not any(fn + "(" in text for fn in FUNCS):
            continue
        targets.append((path, text))

    if not targets:
        print("recovery FDE 兼容层：没有文件引用已删除的 FDE API，无需处理")
        return 0

    header_path = os.path.join(rec, HEADER_NAME)
    changed_header = True
    if os.path.isfile(header_path):
        changed_header = read(header_path) != HEADER
    if changed_header:
        write(header_path, HEADER)

    touched = []
    for path, text in targets:
        if MARKER in text:
            continue
        lines = text.splitlines(keepends=True)
        idx, indent = find_insert_index(lines)
        if idx == 0 and lines:
            idx = 1
        lines.insert(idx, '%s#include "%s"\n' % (indent, HEADER_NAME))
        write(path, "".join(lines))
        touched.append(os.path.basename(path))

    print("recovery FDE 兼容层：已写入 %s" % header_path)
    if touched:
        print("  include 插入：%s" % ", ".join(touched))
    else:
        print("  include 已存在，未重复插入")
    print("  说明：FDE 相关调用改为占位返回，FBE 解密路径不受影响")
    return 0


if __name__ == "__main__":
    sys.exit(main())
