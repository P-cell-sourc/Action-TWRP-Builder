#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为 bootable/recovery/libtar 的 Android.mk 补上 libvold 在 AOSP 14 新增的运行库依赖。

背景
----
TWRP 14.1 的 libtar 通过
    LOCAL_STATIC_LIBRARIES += libvold
把整个 system/vold 静态库链进 libtar.so，以复用 fscrypt_policy / Decrypt 里的实现。
但 AOSP 14 的 vold 新增了对下面几个库的调用，而 libtar 的 Android.mk 还停留在
Android 12 的依赖列表上，于是链接阶段报：

    ld.lld: error: undefined symbol: _ZN7android3hal17BootControlClient14WaitForServiceEv
        >>> referenced by Checkpoint.cpp:141      → libboot_control_client
    ld.lld: error: undefined symbol: _ZN12NetlinkEvent4dumpEv
    ld.lld: error: undefined symbol: _ZN12NetlinkEvent9findParamEPKc
        >>> referenced by VolumeManager.cpp:211   → libsysutils
    ld.lld: error: undefined symbol: async_safe_format_log
        >>> referenced by VolumeManager.cpp:538   → libasync_safe（bionic 静态库）

这三个库在 system/vold/Android.bp 的 vold_default_libs 里都有声明
（libboot_control_client / libsysutils 为 shared，libasync_safe 为 static），
只是没有传递给 libtar。

做法
----
在 Android.mk 的 `ifeq ($(TW_INCLUDE_CRYPTO_FBE), true)` 分支里（也就是 libvold
真正被链接的那个分支）追加三行 +=，不修改任何原有内容：

    LOCAL_STATIC_LIBRARIES += libasync_safe
    LOCAL_SHARED_LIBRARIES += libboot_control_client libsysutils

幂等：带固定标记注释，重复执行直接跳过；只处理 BUILD_SHARED_LIBRARY 之前
的那一段（libtar_static 不链接 libvold，不需要）。
"""

import os
import re
import sys
from pathlib import Path

MARKER = "# [twrp-builder-fix] libvold 14.x 运行库依赖"

HEADER = [
    MARKER,
    "#   Checkpoint.cpp     -> android::hal::BootControlClient::WaitForService()",
    "#   VolumeManager.cpp  -> NetlinkEvent::dump() / findParam(), async_safe_format_log()",
    "#   与 system/vold/Android.bp 的 vold_default_libs 保持一致",
]

WANT_STATIC = ["libasync_safe"]
WANT_SHARED = ["libboot_control_client", "libsysutils"]


def has_module(text: str, name: str) -> bool:
    """按整词判断 makefile 里是否已经引用了某个模块。"""
    return re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", text) is not None


def main(root: str) -> int:
    root_path = Path(root)
    mk = root_path / "bootable" / "recovery" / "libtar" / "Android.mk"

    if not mk.is_file():
        print("libtar 链接依赖：未找到 %s，跳过" % mk)
        return 0

    text = mk.read_text(encoding="utf-8", errors="surrogateescape")

    if MARKER in text:
        print("libtar 链接依赖：已存在（%s），跳过" % MARKER)
        return 0

    lines = text.splitlines()

    # 只处理 shared library 那一段
    end = None
    for i, line in enumerate(lines):
        if line.strip() == "include $(BUILD_SHARED_LIBRARY)":
            end = i
            break
    if end is None:
        print("libtar 链接依赖：未找到 include $(BUILD_SHARED_LIBRARY)，跳过")
        return 0

    head_text = "\n".join(lines[:end])

    # 定位 libvold 的链接条件分支
    idx = None
    for i in range(end):
        s = lines[i].strip()
        if s.startswith("ifeq ($(TW_INCLUDE_CRYPTO_FBE)") or s.startswith(
            "ifeq ($(TW_INCLUDE_CRYPTO_FBE),"
        ):
            idx = i
            break
    if idx is None:
        print(
            "libtar 链接依赖：Android.mk 里没有 TW_INCLUDE_CRYPTO_FBE 分支"
            "（说明该版本不链接 libvold），跳过"
        )
        return 0

    if "libvold" not in head_text:
        print("libtar 链接依赖：该分支未链接 libvold，跳过")
        return 0

    add_static = [m for m in WANT_STATIC if not has_module(head_text, m)]
    add_shared = [m for m in WANT_SHARED if not has_module(head_text, m)]

    if not add_static and not add_shared:
        print("libtar 链接依赖：三个库都已在 Android.mk 中，无需修改")
        return 0

    block = ["    " + h for h in HEADER]
    if add_static:
        block.append("    LOCAL_STATIC_LIBRARIES += " + " ".join(add_static))
    if add_shared:
        block.append("    LOCAL_SHARED_LIBRARIES += " + " ".join(add_shared))

    new_lines = lines[: idx + 1] + block + lines[idx + 1 :]
    mk.write_text("\n".join(new_lines) + "\n", encoding="utf-8", errors="surrogateescape")

    print("libtar 链接依赖：已写入 (%s)" % mk)
    if add_static:
        print("  static += " + " ".join(add_static))
    if add_shared:
        print("  shared += " + " ".join(add_shared))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()))
