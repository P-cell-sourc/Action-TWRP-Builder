#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 first stage 的内核模块清单缩到「真正需要的那几个」，并平铺到 /lib/modules/。

问题
----
MTK 设备树把 vendor 模块放在 /lib/modules/<kver>/ 下（如 6.6-gki），
而 AOSP init 加载模块时拼的是**平铺**路径：

    /lib/modules/<name>.ko

`modules.load` 里第一行（MTK 是 bootprof.ko）在平铺路径下不存在，
`LoadModulesFromFile()` 直接返回失败，后面 266 条一条都不再尝试：

    init: module bootprof can't be loaded; can't access /lib/modules/bootprof.ko

后果分两种情况：
* 若 first stage 确实需要某些模块（ext4/f2fs/erofs 是模块而非 builtin），
  挂载会失败 —— 这是硬故障；
* 若这些文件系统都是 builtin（本机 PD2551 就是），first stage 不需要模块，
  失败只丢 MTK 的崩溃转储/性能跟踪数据 —— 软故障，但日志很脏。

处理
----
1. 读 `modules.dep` 建依赖图；
2. 保留集 = 清单里的关键文件系统模块（f2fs/ext4/erofs/squashfs）+ MTK 崩溃
   转储/性能跟踪（bootprof/mrdump/aee_*）+ 它们的依赖闭包；
   文件系统是 builtin 时保留集就只剩后面那几个（约 300 KB）；
3. 重写 `modules.load`（按原顺序，保证依赖在前）；
4. 把保留的 .ko **平铺复制**到 `lib/modules/` 根下，供 init 按
   /lib/modules/<name>.ko 找到。

注意：first stage 只要有一条加载失败就整体失败、init exit(1)、内核 panic。
所以写清单前会按文件存在性再过滤一遍。

为什么不直接「全平铺」：303 个 .ko 共 61 MB，vendor_boot 分区 128 MB，
现有产物已 87 MB，全平铺会直接超限（add_hash_footer: image too large）。

`modules.load.recovery` 必须一起处理 —— recovery 模式（cmdline 带 recoverymode=1）
下 init 读的是它，不是 `modules.load`。原 286 条清单另存为 `modules.load.twrp`，
交给 second stage 的 system/bin/twrp-modules.sh 按 6.6-gki 子目录全量加载。

用法
----
    python3 fix_first_stage_modules.py <源码根目录> <设备树相对路径>
    python3 fix_first_stage_modules.py . device/vivo/PD2551
"""

import os
import re
import shutil
import sys

MARK = "[twrp-builder-fix]"

# 清单里出现这些，说明对应文件系统是模块（而非 builtin），first stage 必须加载
FS_CRITICAL = ("f2fs.ko", "ext4.ko", "erofs.ko", "squashfs.ko")

# 即使文件系统 builtin，也值得保留：MTK 崩溃转储 + 开机性能跟踪
# 注意不要加 aee_rs.ko：它在 modules.dep 里依赖整条显示栈（mediatek-drm /
# vivo_display / mtk_sync 等 46 个模块），会把 51 条、十几 MB 一起拖进来。
KEEP_ALWAYS = ("bootprof.ko", "mrdump.ko", "aee_aed.ko")


def read_lines(p):
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def parse_dep(path):
    """modules.dep -> {name.ko: [dep.ko, ...]}，统一用 basename。"""
    deps = {}
    if not os.path.isfile(path):
        return deps
    for line in read_lines(path):
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        name = left.strip().rsplit("/", 1)[-1]
        if not name:
            continue
        deps[name] = [x.strip().rsplit("/", 1)[-1]
                      for x in right.split() if x.strip()]
    return deps


def closure(roots, deps):
    """依赖闭包，依赖排在前面（可直接作为加载顺序）。"""
    out, seen = [], set()

    def walk(m):
        if m in seen:
            return
        seen.add(m)
        for d in deps.get(m, []):
            walk(d)
        out.append(m)

    for r in roots:
        walk(r)
    return out


def find_module_dir(root, device_path):
    base = os.path.join(root, device_path, "recovery", "root", "lib", "modules")
    if not os.path.isdir(base):
        return None, base
    cands = []
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "modules.load")):
            cands.append(d)
    return (cands[0] if cands else None), base


def build_list(order, deps):
    """返回 (保留列表, 是否因 fs builtin 而精简)。"""
    fs_in_list = [m for m in order if m in FS_CRITICAL]
    roots = fs_in_list + [m for m in KEEP_ALWAYS if m in order]
    if not roots:
        return [], True
    wanted = set(closure(roots, deps))
    # 按原清单顺序输出，天然满足依赖先后
    kept = [m for m in order if m in wanted]
    # 依赖闭包里不在原清单的，追加到末尾
    extra = [m for m in wanted if m not in kept]
    return kept + extra, not fs_in_list


def render(kept, fs_builtin, src_dir):
    lines = [
        "# %s 由 twrp-builder 生成：first stage 只加载下列模块。" % MARK,
        "#",
        "# 背景：AOSP init 拼的是 /lib/modules/<name>.ko 这种平铺路径，不认",
        "#   /lib/modules/<kver>/ 子目录；清单第一行不存在就整体中断，后面全部不加载。",
        "#   完整清单（266~286 条）已精简，保留项与它们的依赖闭包如下。",
        "#",
        "#   drm / panel / touch / usb 由 second stage 的 system/bin/twrp-modules.sh",
        "#   按同目录 modules.load.twrp（全量原清单）加载，不在本清单内。",
        "#",
        "# 原始目录：%s" % os.path.basename(src_dir),
    ]
    if fs_builtin:
        lines += [
            "#",
            "# 本机 ext4 / erofs / f2fs 均为 builtin（不在 modules.dep 里），",
            "# first stage 挂载 logical 分区不需要额外模块；",
            "# 保留项只剩 MTK 崩溃转储与性能跟踪（约 300 KB）。",
        ]
    lines.append("")
    lines += kept
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    root = os.path.abspath(sys.argv[1])
    device_path = sys.argv[2].strip("/")

    print("== first stage 模块清单 ==")
    src_dir, base = find_module_dir(root, device_path)
    if not src_dir:
        print("  ! %s 下没有含 modules.load 的子目录，跳过" % base)
        return 0

    load = os.path.join(src_dir, "modules.load")
    order = [l.strip() for l in read_lines(load)
             if l.strip() and not l.strip().startswith("#")]
    deps = parse_dep(os.path.join(src_dir, "modules.dep"))
    kept, fs_builtin = build_list(order, deps)

    # 防御性过滤：清单里若混进目录中根本不存在的 .ko，init 会整体失败并 panic
    # （LoadModulesFromFile 是全有或全无，一条失败就 exit(1) → Attempted to kill init）。
    # 这次真正踩到的是 modules.load.recovery 那条路径（见 1b），这层检查留着，
    # 免得换机型再中一次。
    absent = [m for m in kept if not os.path.isfile(os.path.join(src_dir, m))]
    if absent:
        kept = [m for m in kept if m not in absent]
        print("  ! 目录中不存在，从清单剔除: %s" % " ".join(absent))

    print("  模块目录 : %s" % os.path.relpath(src_dir, root))
    print("  原清单   : %d 条，dep 图 %d 条" % (len(order), len(deps)))
    print("  保留     : %d 条 %s" % (len(kept), " ".join(kept[:8]) if kept else "(空)"))

    # ── 1. 重写 modules.load
    want = render(kept, fs_builtin, src_dir)
    if os.path.isfile(load):
        with open(load, "r", encoding="utf-8", errors="replace") as f:
            cur = f.read()
    else:
        cur = None
    if cur == want:
        print("  modules.load 已是最新")
    else:
        with open(load, "w", encoding="utf-8") as f:
            f.write(want)
        print("  ✓ 重写 modules.load（%d -> %d 条）" % (len(order), len(kept)))

    # ── 1b. recovery 模式读的不是 modules.load，而是 modules.load.recovery
    #   system/core/init/first_stage_init.cpp:
    #       case BootMode::RECOVERY_MODE: module_load_file = "modules.load.recovery";
    #       ... 只有该文件不存在，才 fall back 到 modules.load
    #   本机 cmdline 带 recoverymode=1，所以只精简 modules.load 等于没做 ——
    #   init 仍然按 286 条那份加载，第 4 条 vivo_bsp_engine 找不到就 exit(1)。
    #   全量清单另存为 modules.load.twrp（给 second stage 的 twrp-modules.sh），
    #   modules.load.recovery 自己写精简版，内容全是已平铺的 .ko。
    rload = os.path.join(src_dir, "modules.load.recovery")
    full = os.path.join(src_dir, "modules.load.twrp")
    if os.path.isfile(rload):
        if not os.path.isfile(full):
            rn = len([l for l in read_lines(rload)
                      if l.strip() and not l.strip().startswith("#")])
            shutil.copy2(rload, full)
            print("  ✓ 全量清单另存为 modules.load.twrp（%d 条，供 second stage）" % rn)
        with open(rload, "r", encoding="utf-8", errors="replace") as f:
            rcur = f.read()
        if rcur != want:
            with open(rload, "w", encoding="utf-8") as f:
                f.write(want)
            print("  ✓ 重写 modules.load.recovery（recovery 模式也只用这 %d 条）" % len(kept))
    else:
        print("  ! 没有 modules.load.recovery：recovery 模式会回退 modules.load")

    # ── 2. 平铺复制到 lib/modules/ 根下
    copied, missing, size = 0, [], 0
    for m in kept:
        s = os.path.join(src_dir, m)
        if not os.path.isfile(s):
            missing.append(m)
            continue
        d = os.path.join(base, m)
        need = True
        if os.path.isfile(d) and os.path.getsize(d) == os.path.getsize(s):
            need = False
        if need:
            shutil.copy2(s, d)
            copied += 1
        size += os.path.getsize(s)
    print("  ✓ 平铺 %d 个（新复制 %d，合计 %.1f MB）" % (len(kept) - len(missing), copied, size / 1048576.0))
    if missing:
        print("  ! 清单里有但目录中不存在的: %s" % " ".join(missing[:8]))
    if fs_builtin:
        print("  说明: ext4/erofs/f2fs 均为 builtin，first stage 挂载不依赖模块")
    return 0


if __name__ == "__main__":
    sys.exit(main())
