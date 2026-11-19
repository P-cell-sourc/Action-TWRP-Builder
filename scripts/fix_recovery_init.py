#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让 recovery 里的内核模块真的被加载，并让 TWRP 的报错进 kmsg。

背景（来自 pstore/console-ramoops 实测）：
  init: module bootprof can't be loaded; can't access /lib/modules/bootprof.ko
MTK 设备树把 .ko 放在 /lib/modules/<kver>/ 下，而 AOSP first stage init 只认
/lib/modules/<name>.ko 这种平铺路径，于是 recovery 里一个模块都没加载：
  * 没有 mediatek-drm.ko  → 没有 fb / DRM 节点，TWRP 初始化图形失败，直接 exit(1)
  * 没有 extcon-mtk-usb.ko → 没有 UDC，adb 也起不来（"couldn't find an available UDC"）
这份脚本做两件事，都只改 CI 克隆下来的源码，不动设备树仓库：

1. 往设备树 recovery/root/system/bin/ 里放一个 twrp-modules.sh，并在
   bootable/recovery/etc/init.rc 追加一条 `on fs` 的 exec —— 在 TWRP
   启动前把 modules.load.twrp（全量原清单，286 条）里的模块逐个 insmod。
   脚本自己会把统计结果 echo 出来，init 的 exec 继承 stdout(/dev/kmsg)，
   所以下次从 pstore 就能看到 "insmod ok: N" 和 fb0/udc 是否存在。

2. 给 recovery 服务加 stdio_to_kmsg（TWRP 上游 etc/init.recovery.service22.rc），
   这样 TWRP 的 stdout/stderr 不再丢进 /dev/null —— 它 exit(1) 的原因会直接
   出现在 console-ramoops 里，不用再猜。

用法：
    python3 fix_recovery_init.py <源码根目录> <设备树相对路径>
    python3 fix_recovery_init.py . device/vivo/PD2551
"""

import os
import re
import stat
import sys

MARK = "[twrp-builder-fix]"

INIT_RC_BLOCK = """
# @@MARK@@ 内核模块兜底加载
#   MTK 把模块放在 /lib/modules/<kver>/，first stage init 找不到，
#   这里在 TWRP 启动之前自己 insmod 一遍（fs 阶段早于 boot 触发）。
#   用 sh 显式调用，避免 recovery/root 里的文件丢了可执行位。
on fs
    exec - root root -- /system/bin/sh /system/bin/twrp-modules.sh
""".replace("@@MARK@@", MARK)

LOADER = r"""#!/system/bin/sh
# @@MARK@@ 由 twrp-builder 生成：加载 /lib/modules/<kver>/ 下的 vendor 内核模块
# 不加载它们的话：没有 mediatek-drm.ko → 无显示；没有 extcon-mtk-usb.ko → 无 adb。
#
# 为什么需要这个脚本：
#   MTK 把 .ko 放在 /lib/modules/6.6-gki/ 下，而 AOSP first stage init 只认
#   /lib/modules/<name>.ko 这种平铺路径，报 "module bootprof can't be loaded"，
#   结果是 recovery 里一个模块都没加载，TWRP 初始化图形失败后 exit(1)。
#   MTK 自己给 recovery 备了一份 286 条的清单，构建时被另存为
#   modules.load.twrp（原 modules.load.recovery 被精简成 3 条给 first stage 用），
#   这里就按那份全量清单加载。

# init 的 exec 子进程 stdout 通常已是 /dev/kmsg，再显式指一次，防止被丢进 /dev/null
[ -e /dev/kmsg ] && exec 1>/dev/kmsg 2>&1

log() { echo "[twrp-fix] $*"; }

# ── 1. 找模块目录与清单
#   顺序很重要：first stage 会把 modules.load.recovery 精简成 3 条（否则 init
#   加载失败直接 panic），全量 286 条被另存为 modules.load.twrp，所以必须先找它。
D=""
L=""
for d in /lib/modules/*/ ; do
    [ -d "$d" ] || continue
    for f in modules.load.twrp modules.load.recovery modules.load; do
        if [ -f "$d$f" ]; then
            D="$d"; L="$d$f"; break 2
        fi
    done
done
# 兜底：清单直接平铺在 /lib/modules/ 下
if [ -z "$L" ]; then
    for f in modules.load.twrp modules.load.recovery modules.load; do
        if [ -f "/lib/modules/$f" ]; then
            L="/lib/modules/$f"; D=/lib/modules/; break
        fi
    done
fi

if [ -z "$L" ]; then
    log "no modules.load under /lib/modules — 跳过"
    exit 0
fi

# ── 2. 逐个 insmod
t0=$(date +%s)
total=0; ok=0; skip=0; fail=0; failed=""
while IFS= read -r m || [ -n "$m" ]; do
    m=$(echo "$m" | tr -d '\r')
    case "$m" in ''|'#'*) continue ;; esac
    total=$((total+1))
    err=$(insmod "$D$m" 2>&1); rc=$?
    if [ $rc -eq 0 ]; then
        ok=$((ok+1))
    else
        case "$err" in
            # 已内建或已被依赖链加载过，不算失败
            *"File exists"*|*"already"*) skip=$((skip+1)) ;;
            *)
                fail=$((fail+1))
                [ $fail -le 8 ] && failed="$failed $m"
                ;;
        esac
    fi
done < "$L"

t1=$(date +%s)
log "modules from $L: total=$total ok=$ok skip=$skip fail=$fail ($((t1-t0))s)"
[ -n "$failed" ] && log "insmod failed:$failed"

# ── 3. 关键节点自检：这几个是"能不能显示 / 能不能 adb"的判据
sleep 1
if [ -e /dev/graphics/fb0 ]; then
    log "/dev/graphics/fb0 present"
else
    log "/dev/graphics/fb0 MISSING"
fi
if [ -e /dev/dri/card0 ]; then
    log "/dev/dri/card0 present"
else
    log "/dev/dri/card0 MISSING"
fi
u=$(ls /sys/class/udc 2>/dev/null | tr '\n' ' ')
if [ -n "$u" ]; then
    log "udc: $u"
else
    log "udc: NONE"
fi
exit 0
""".replace("@@MARK@@", MARK)

STDIO_OPT = "    stdio_to_kmsg"


def read(p):
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write(p, s, mode=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)
    if mode is not None:
        os.chmod(p, mode)


def patch_loader(source_root, device_path):
    """把加载脚本写进设备树 recovery/root/system/bin/"""
    rec_root = os.path.join(source_root, device_path, "recovery", "root")
    dst = os.path.join(rec_root, "system", "bin", "twrp-modules.sh")
    old = read(dst) if os.path.isfile(dst) else None
    if old == LOADER:
        print("  %s 已是最新" % dst)
    else:
        write(dst, LOADER, 0o755)
        print("  ✓ 写入 %s" % dst)
    os.chmod(dst, 0o755)
    return os.path.isdir(rec_root)


def patch_init_rc(source_root):
    """bootable/recovery/etc/init.rc 追加 on fs 的 exec"""
    p = os.path.join(source_root, "bootable", "recovery", "etc", "init.rc")
    if not os.path.isfile(p):
        print("  ! 找不到 %s" % p)
        return False
    s = read(p)
    if MARK in s and "twrp-modules.sh" in s:
        print("  %s 已含模块加载段" % p)
        return True
    if not s.endswith("\n"):
        s += "\n"
    s += INIT_RC_BLOCK
    write(p, s)
    print("  ✓ 追加 on fs → twrp-modules.sh (%s)" % p)
    return True


def patch_service_rc(source_root):
    """recovery 服务加 stdio_to_kmsg，让 TWRP 的输出进 kmsg"""
    cand = [
        os.path.join(source_root, "bootable", "recovery", "etc",
                     "init.recovery.service22.rc"),
        os.path.join(source_root, "bootable", "recovery", "etc",
                     "init.recovery.service.rc"),
    ]
    p = next((c for c in cand if os.path.isfile(c)), None)
    if not p:
        print("  ! 没有找到 init.recovery.service*.rc，跳过 stdio_to_kmsg")
        return False
    s = read(p)
    if "stdio_to_kmsg" in s:
        print("  %s 已含 stdio_to_kmsg" % p)
        return True
    m = re.search(r"^service\s+recovery\b[^\n]*\n(?:(?:[ \t]+[^\n]*)\n)*", s,
                  re.M)
    if not m:
        print("  ! %s 里没有 service recovery 段" % p)
        return False
    block = m.group(0)
    insert_at = m.start() + len(block)
    new = s[:insert_at] + STDIO_OPT + "\n" + s[insert_at:]
    write(p, new)
    print("  ✓ %s 的 recovery 服务加上 stdio_to_kmsg" % p)
    return True


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    source_root = os.path.abspath(sys.argv[1])
    device_path = sys.argv[2].strip("/")

    print("== 内核模块加载 + TWRP 输出重定向 ==")
    print("源码根目录 : %s" % source_root)
    print("设备树     : %s" % device_path)

    ok_rec = patch_loader(source_root, device_path)
    if not ok_rec:
        print("  ! 设备树里没有 recovery/root，脚本仍会写入，但可能不生效")
    patch_init_rc(source_root)
    patch_service_rc(source_root)
    print("== 完成 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
