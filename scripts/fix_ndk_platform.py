#!/usr/bin/env python3
"""把已被 AOSP 删除的 `-ndk_platform` 后缀改回现行的 `-ndk`。

AOSP 移除了 AIDL 的 ndk_platform 后端，build/make/target/product/gsi/Android.mk
里明确写着 "*-ndk_platform.so libraries no longer exist"。而
bootable/recovery 的 TW_INCLUDE_CRYPTO_FBE 分支仍在用旧名字
（android.security.apc-ndk_platform 等），链接 libtar 时报：

  ninja: 'out/.../android.security.apc-ndk_platform.so.toc', needed by
  '.../libtar.so', missing and no known rule to make it

用法：
  python3 fix_ndk_platform.py <源码根目录> [--dry-run]

只扫描我们自己的构建文件（bootable/recovery、vendor/twrp、device/），
不动 AOSP/soong 本体；幂等，可重复执行。
"""

import os
import re
import sys

# <模块名>-ndk_platform -> <模块名>-ndk
# 模块名允许字母数字和 . @ + - _ ，不吃掉前面的空白和续行符
PATTERN = re.compile(r"([A-Za-z0-9_.@+\-]+)-ndk_platform\b")
SCAN_DIRS = ("bootable/recovery", "vendor/twrp", "device")
EXTS = (".mk", ".bp")


def iter_targets(root):
    for d in SCAN_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in (".git", "out")]
            for fn in filenames:
                if fn.endswith(EXTS):
                    yield os.path.join(dirpath, fn)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv
    root = args[0] if args else "."

    if not os.path.isdir(root):
        print("::error::源码目录不存在：%s" % root)
        return 1

    changed_files = 0
    total = 0
    modules = set()

    for path in iter_targets(root):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            print("::warning::读取失败 %s: %s" % (path, e))
            continue
        text = raw.decode("utf-8", "surrogateescape")
        if "-ndk_platform" not in text:
            continue

        new_text, n = PATTERN.subn(r"\1-ndk", text)
        if not n:
            continue

        rel = os.path.relpath(path, root)
        for line in text.splitlines():
            if "-ndk_platform" in line:
                fixed = PATTERN.sub(r"\1-ndk", line)
                print("  %s: %s -> %s" % (rel, line.strip(), fixed.strip()))
                for m in PATTERN.finditer(line):
                    modules.add(m.group(1))

        if dry:
            print("  (dry-run，未写入 %s)" % rel)
        else:
            with open(path, "wb") as f:
                f.write(new_text.encode("utf-8", "surrogateescape"))
        changed_files += 1
        total += n

    if total:
        print("ndk_platform 后缀修补：%d 个文件，%d 处引用%s"
              % (changed_files, total, "（dry-run）" if dry else ""))
    else:
        print("未发现 -ndk_platform 引用（源码可能已更新，或此前已修补）")

    # 软校验：这些接口应当能在 system/security 下找到 AIDL 定义。
    # 注意引用名常带版本后缀（android.system.keystore2-V1-ndk），而
    # aidl_interface 的 name 不含 -V1，两种都要试。
    sec = os.path.join(root, "system", "security")
    if modules and os.path.isdir(sec):
        blobs = []
        for dirpath, dirnames, filenames in os.walk(sec):
            dirnames[:] = [x for x in dirnames if x != ".git"]
            for fn in filenames:
                if fn.endswith(".bp"):
                    try:
                        with open(os.path.join(dirpath, fn), "rb") as f:
                            blobs.append(f.read())
                    except OSError:
                        pass
        for mod in sorted(modules):
            base = re.sub(r"-V\d+$", "", mod)
            names = {mod, base}
            if any(b'name: "' + n.encode() + b'"' in b for b in blobs for n in names):
                print("  接口定义已确认：%s（system/security）" % base)
            else:
                print("::warning::system/security 下未找到 %s 的 AIDL 定义，"
                      "请确认清单是否同步了该仓库" % base)

    return 0


if __name__ == "__main__":
    sys.exit(main())
