#!/usr/bin/env python3
"""把 GOMEMLIMIT / GOGC 透传给 soong_build。

背景：build/soong/ui/build/soong.go 里构造 soong_build 的 invocationEnv 时，
默认只在 debug 模式放 GODEBUG；bootstrap 生成的命令是

    env -i <soong_build> ...

`env -i` 会把父进程环境清空，所以在 shell 里 export 的 GOMEMLIMIT / GOGC 对
soong_build 完全无效。结果就是：内存受限的机器上，soong_build 解析 Android.bp
时堆一路涨到被内核 OOM 杀掉，而日志恰好停在
`cp out/host/linux-x86/bin/soong_build` 之后（接下来那一大段是静默的）。

这个补丁在 primaryBuilderInvocation 里加一段，把这两个变量放进 invocationEnv，
soong_build 就能按 limit 提前 GC，而不是被系统杀掉。

用法：patch_soong_memlimit.py [源码根目录]（默认当前目录）
重复执行安全。
"""

import os
import sys

REL = os.path.join("build", "soong", "ui", "build", "soong.go")

ANCHOR = "allArgs = append(allArgs, environmentArgs(pb.config, pb.name)...)"

BLOCK = """\t// [auto-patch] soong_build 由 env -i 启动，父进程环境会被清空，
\t// 必须显式传递内存相关变量，否则内存受限的机器上会在解析 Android.bp
\t// 阶段被内核 OOM 杀掉（日志停在 cp soong_build 之后）。
\tfor _, memEnv := range []string{"GOMEMLIMIT", "GOGC"} {
\t\tif v := os.Getenv(memEnv); v != "" {
\t\t\tinvocationEnv[memEnv] = v
\t\t}
\t}
"""


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(root, REL)

    if not os.path.isfile(path):
        print("::warning::找不到 %s，跳过（目录结构可能不同）" % path)
        return 0

    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    if "GOMEMLIMIT" in src:
        print("%s 已包含 GOMEMLIMIT 传递逻辑，跳过" % REL)
        return 0

    lines = src.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if ANCHOR in line:
            lines.insert(i + 1, "\n" + BLOCK)
            break
    else:
        print("::warning::%s 里找不到锚点，跳过（soong 版本可能不同）" % REL)
        return 0

    if '"os"' not in src:
        print("::warning::%s 没有导入 os 包，跳过以免编译失败" % REL)
        return 0

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))

    print("已给 %s 加上 GOMEMLIMIT / GOGC 透传" % REL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
