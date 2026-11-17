#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 recovery 可执行文件补上 libvold 在 AOSP 14 新增的 libsysutils 依赖。

背景
----
`bootable/recovery/Android.mk` 用

    LOCAL_STATIC_LIBRARIES += libguitwrp libvold

把整个 vold 静态库链进 recovery。`libvold.a` 里的 VolumeManager.cpp 在
AOSP 14 会调用：

    NetlinkEvent::dump()
    NetlinkEvent::findParam()

这两个符号由 `libsysutils` 提供，而 recovery 的 LOCAL_SHARED_LIBRARIES
里没有它（依赖表停在 Android 12），于是链接到 96% 时报：

    ld.lld: error: undefined symbol: _ZN12NetlinkEvent4dumpEv
    >>> referenced by VolumeManager.cpp:208
    ld.lld: error: undefined symbol: _ZN12NetlinkEvent9findParamEPKc
    >>> referenced by VolumeManager.cpp:211

同一批符号在 libtar.so 上也出现过，由 fix_libtar_link.py 处理；
recovery 是另一处独立链接，需要单独补。

做法
----
在 `LOCAL_MODULE := recovery` 模块块内（到 `include $(BUILD_` 为止）
找到第一条 `LOCAL_SHARED_LIBRARIES` 行，在行尾追加 `libsysutils`。
不动 libtar、不动其他模块、不动条件分支（条件分支都排在这行之后）。

幂等：模块块内已出现 libsysutils 就跳过；Android.mk 不存在时静默退出。
"""

import os
import re
import sys

MODULE = "recovery"
LIB = "libsysutils"

MODULE_RE = re.compile(r"^\s*LOCAL_MODULE\s*:=\s*%s\s*$" % re.escape(MODULE))
BUILD_RE = re.compile(r"^\s*include\s+\$\(BUILD_")
SHARED_RE = re.compile(r"^(?P<head>\s*LOCAL_SHARED_LIBRARIES\s*(?:\+=|:=))(?P<rest>.*)$")


def patch_mk(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        lines = f.read().splitlines()

    start = None
    for i, line in enumerate(lines):
        if MODULE_RE.match(line):
            start = i
            break
    if start is None:
        print("recovery 链接依赖：%s 中没有 `LOCAL_MODULE := recovery`，跳过" % path)
        return False

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if BUILD_RE.match(lines[i]):
            end = i
            break

    if any(LIB in lines[i] for i in range(start, end) if "LOCAL_SHARED_LIBRARIES" in lines[i]):
        print("recovery 链接依赖：recovery 模块已含 %s，无需修改" % LIB)
        return False

    for i in range(start, end):
        m = SHARED_RE.match(lines[i])
        if not m:
            continue
        rest = m.group("rest").strip()
        tail = ""
        if rest.endswith("\\"):
            rest = rest[:-1].rstrip()
            tail = " \\"
        lines[i] = "%s %s %s%s" % (m.group("head"), rest, LIB, tail)
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\n".join(lines) + "\n")
        print("recovery 链接依赖：已写入 %s" % path)
        print("  %s %s %s" % (m.group("head").strip(), LIB, tail.strip()))
        return True

    print("recovery 链接依赖：recovery 模块内没有 LOCAL_SHARED_LIBRARIES 行，跳过")
    return False


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    mk = os.path.join(root, "bootable", "recovery", "Android.mk")
    if not os.path.isfile(mk):
        print("recovery 链接依赖：未找到 %s，跳过" % mk)
        return 0
    patch_mk(mk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
