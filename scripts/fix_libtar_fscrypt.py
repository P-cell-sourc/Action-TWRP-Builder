#!/usr/bin/env python3
"""给 TWRP 14.1 的 libtar 补回 Android 12 时代的 fscrypt_policy API。

背景（TWRP 上游自己的两个仓库不同步，不是设备树的问题）：

  * system/vold（TeamWin/android_system_vold @android-14.1）的
    fscrypt_policy.h 已经改成重载形式：

        bool fscrypt_policy_get_struct(const char *, struct fscrypt_policy_v1 *);
        bool fscrypt_policy_get_struct(const char *, struct fscrypt_policy_v2 *);

    并且删掉了旧版的
        union fscrypt_policy / get_policy / get_policy_size /
        get_policy_descriptor / fscrypt_policy_size / get_policy_content。

  * bootable/recovery（TeamWin/android_bootable_recovery @android-14.1）
    的 libtar 仍在用旧 API，直接 `#include "fscrypt_policy.h"`。

  * 雪上加霜：bionic 的 <linux/fscrypt.h> 里有
        #define fscrypt_policy fscrypt_policy_v1
    这个兼容宏会把 libtar 里的类型名整个替换掉，于是 C 里
    `fscrypt_policy *fep;` 变成 `fscrypt_policy_v1 *fep;`（缺 struct 关键字）。

典型报错：

    libtar.h:74:2: error: must use 'struct' tag to refer to type 'fscrypt_policy_v1'
    append.c:152:43: error: incompatible pointer types passing
        'struct fscrypt_policy_v1 *' to parameter of type 'struct fscrypt_policy_v2 *'
        [-Werror,-Wincompatible-pointer-types]
    block.c:365:13: error: call to undeclared function 'get_policy'

修法：在 libtar 目录里生成一个自包含的兼容头 fscrypt_compat.h，
把 union 类型和 5 个纯计算的 helper 以 static inline 补回来，
再把 fscrypt_policy_get_struct / _set_struct 的参数 cast 到 vold 期望的
版本结构体（由 USE_FSCRYPT_POLICY_V1/V2 决定）。
libtar 源码只改 include 那一行，其余不动。

用法：
  python3 fix_libtar_fscrypt.py <源码根目录> [--dry-run]

幂等；目录不存在时安静退出（exit 0），不影响非 TWRP 场景。
"""

import os
import sys

MARK = "LIBTAR_FSCRYPT_COMPAT_H"
OLD_INCLUDE = '#include "fscrypt_policy.h"'
NEW_INCLUDE = '#include "fscrypt_compat.h"'

COMPAT_HEADER = r'''/*
 * 由 scripts/fix_libtar_fscrypt.py 生成，请勿手改。
 *
 * TWRP 14.1 的 system/vold 把 fscrypt_policy.h 换成了 v1/v2 重载版本，
 * 而 libtar 仍在用 Android 12 时代的旧接口。这里把旧接口补回来，
 * 只在 libtar 内部可见，不污染 vold。
 */
#ifndef LIBTAR_FSCRYPT_COMPAT_H
#define LIBTAR_FSCRYPT_COMPAT_H

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <linux/fscrypt.h>

/* vold 的新头：提供 fscrypt_policy_get_struct / set_struct / bytes_to_hex */
#include "fscrypt_policy.h"

/* bionic uapi 用宏把 fscrypt_policy 指到 fscrypt_policy_v1，
 * 会把下面的 typedef 和 libtar.h 里的类型名一起改写，先解除。 */
#ifdef fscrypt_policy
#undef fscrypt_policy
#endif

typedef union fscrypt_policy {
    uint8_t version;
    struct fscrypt_policy_v1 v1;
    struct fscrypt_policy_v2 v2;
} fscrypt_policy;

/* 新头里这两个宏的名字和旧版不一样（..._HEX_SIZE / ..._SIZE_HEX），补一个 */
#ifndef FS_KEY_DESCRIPTOR_SIZE_HEX
#define FS_KEY_DESCRIPTOR_SIZE_HEX (2 * FSCRYPT_KEY_DESCRIPTOR_SIZE + 1)
#endif
#ifndef FSCRYPT_KEY_IDENTIFIER_SIZE_HEX
#define FSCRYPT_KEY_IDENTIFIER_SIZE_HEX (2 * FSCRYPT_KEY_IDENTIFIER_SIZE + 1)
#endif

/* libtar 用 union 指针，vold 的 get/set_struct 按版本重载，这里按编译开关 cast */
#ifdef USE_FSCRYPT_POLICY_V1
#define LIBTAR_FSCRYPT_STRUCT struct fscrypt_policy_v1
#define LIBTAR_FSCRYPT_CONST_STRUCT const struct fscrypt_policy_v1
#else
#define LIBTAR_FSCRYPT_STRUCT struct fscrypt_policy_v2
#define LIBTAR_FSCRYPT_CONST_STRUCT const struct fscrypt_policy_v2
#endif

/* 同名宏只展开一次，不会递归；加括号可避免展开自身 */
#define fscrypt_policy_get_struct(dir, fep) \
    fscrypt_policy_get_struct((dir), (LIBTAR_FSCRYPT_STRUCT *)(void *)(fep))
#define fscrypt_policy_set_struct(dir, fep) \
    fscrypt_policy_set_struct((dir), (LIBTAR_FSCRYPT_CONST_STRUCT *)(const void *)(fep))

static inline uint8_t* get_policy_descriptor(fscrypt_policy* fep) {
    if (!fep) return NULL;
    switch (fep->version) {
        case FSCRYPT_POLICY_V1:
            return fep->v1.master_key_descriptor;
        case FSCRYPT_POLICY_V2:
            return fep->v2.master_key_identifier;
        default:
            return NULL;
    }
}

static inline uint8_t get_policy_size(fscrypt_policy* fep, bool hex) {
    if (!fep) return 0;
    switch (fep->version) {
        case FSCRYPT_POLICY_V1:
            return hex ? FS_KEY_DESCRIPTOR_SIZE_HEX : FSCRYPT_KEY_DESCRIPTOR_SIZE;
        case FSCRYPT_POLICY_V2:
            return hex ? FSCRYPT_KEY_IDENTIFIER_SIZE_HEX : FSCRYPT_KEY_IDENTIFIER_SIZE;
        default:
            return 0;
    }
}

static inline void* get_policy(const fscrypt_policy* fep) {
    if (!fep) return NULL;
    switch (fep->version) {
        case FSCRYPT_POLICY_V1:
            return (void*)&(fep->v1);
        case FSCRYPT_POLICY_V2:
            return (void*)&(fep->v2);
        default:
            return NULL;
    }
}

static inline int fscrypt_policy_size(const fscrypt_policy* fep) {
    if (!fep) return 0;
    switch (fep->version) {
        case FSCRYPT_POLICY_V1:
            return (int)sizeof(fep->v1);
        case FSCRYPT_POLICY_V2:
            return (int)sizeof(fep->v2);
        default:
            return 0;
    }
}

static inline void get_policy_content(fscrypt_policy* fep, char* content) {
    if (!fep || !content) return;
    switch (fep->version) {
        case FSCRYPT_POLICY_V1:
            sprintf(content, "%i %i %i %i %s", (int)fep->v1.version,
                    (int)fep->v1.contents_encryption_mode,
                    (int)fep->v1.filenames_encryption_mode,
                    (int)fep->v1.flags, fep->v1.master_key_descriptor);
            break;
        case FSCRYPT_POLICY_V2:
            sprintf(content, "%i %i %i %i %s", (int)fep->v2.version,
                    (int)fep->v2.contents_encryption_mode,
                    (int)fep->v2.filenames_encryption_mode,
                    (int)fep->v2.flags, fep->v2.master_key_identifier);
            break;
        default:
            break;
    }
}

/* vold 14.1 只保留了 lookup_ref_key 的声明（实现被删），
 * 且签名是 v2*。libtar 只关心“有没有可用的 ref key”，
 * 返回 false 表示不写 key-ref 段，备份本身不受影响。 */
static inline bool libtar_lookup_ref_key(fscrypt_policy* fep, uint8_t* policy_type) {
    (void)fep;
    (void)policy_type;
    return false;
}
#define lookup_ref_key(fep, policy_type) libtar_lookup_ref_key((fep), (policy_type))

#endif /* LIBTAR_FSCRYPT_COMPAT_H */
'''


def find_libtar(root):
    p = os.path.join(root, "bootable", "recovery", "libtar")
    return p if os.path.isdir(p) else None


def write_compat(libtar, dry_run):
    path = os.path.join(libtar, "fscrypt_compat.h")
    if os.path.isfile(path):
        with open(path, "r", errors="replace") as f:
            if MARK in f.read():
                return "已存在，跳过"
    if dry_run:
        return "将写入"
    with open(path, "w") as f:
        f.write(COMPAT_HEADER)
    return "已写入"


def patch_sources(libtar, dry_run):
    changed = []
    for name in sorted(os.listdir(libtar)):
        if not name.endswith((".c", ".h")):
            continue
        if name == "fscrypt_compat.h":
            continue
        path = os.path.join(libtar, name)
        with open(path, "r", errors="replace") as f:
            text = f.read()
        if OLD_INCLUDE not in text:
            continue
        if dry_run:
            changed.append(name)
            continue
        with open(path, "w") as f:
            f.write(text.replace(OLD_INCLUDE, NEW_INCLUDE))
        changed.append(name)
    return changed


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    root = args[0] if args else "."
    libtar = find_libtar(root)
    if not libtar:
        print("没有 bootable/recovery/libtar，跳过 libtar fscrypt 兼容处理")
        return 0

    action = write_compat(libtar, dry_run)
    changed = patch_sources(libtar, dry_run)
    print("libtar fscrypt 兼容层：%s（%s）" % (action, "dry-run" if dry_run else "已生效"))
    if changed:
        print("  include 改写：%s" % ", ".join(changed))
    else:
        print("  include 已是兼容头，无需改写")

    # 顺手给出诊断：vold 的 fscrypt_policy.h 属于哪一代
    vold_h = os.path.join(root, "system", "vold", "fscrypt_policy.h")
    if os.path.isfile(vold_h):
        with open(vold_h, "r", errors="replace") as f:
            head = f.read()
        if "get_policy_descriptor" in head:
            print("  检测到 vold 侧仍是旧接口（自带 helper），兼容层等价于空操作")
        else:
            print("  检测到 vold 侧是 14.1 新接口，已由兼容层补齐旧 helper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
