#!/usr/bin/env python3
"""确保 external/guava 已同步（soong 的 `guava` 模块）。

设了 ALLOW_MISSING_DEPENDENCIES=true 时，soong 不会在解析期报“模块不存在”，
而是生成一条构建期必然失败的占位规则：

  FAILED: .../unsupportedappusage-annotation-processor-lib.jar
  module unsupportedappusage-annotation-processor-lib missing dependencies: guava

`unsupportedappusage-annotation-processor`（frameworks/base 的 javac 插件）
static_libs 里就有 guava，所以只要 guava 模块缺失，任何 java 目标都会挂在这里。

本脚本在编译前：
  1. 检查 external/guava/Android.bp 是否定义了 `name: "guava"`；
  2. 缺失时打印诊断（目录在不在、清单里谁 remove 了它）；
  3. 按清单里的 remote / revision 写一个 local manifest 再 repo sync；
  4. 仍然失败就用 git clone 兜底；
  5. 全过程只告警不中断，最终结果打印清楚，便于从日志直接定位。

用法：python3 ensure_guava.py <源码根目录>
"""

import os
import re
import shutil
import subprocess
import sys

MODULE = "guava"
PROJ_PATH = "external/guava"
PROJ_NAME = "platform/external/guava"
MAIN_MANIFEST = ".repo/manifests/default.xml"
LOCAL_MANIFEST = ".repo/local_manifests/zz-ensure-guava.xml"


def has_module(root):
    bp = os.path.join(root, PROJ_PATH, "Android.bp")
    if not os.path.isfile(bp):
        return False
    try:
        with open(bp, "rb") as f:
            return b'name: "' + MODULE.encode() + b'"' in f.read()
    except OSError:
        return False


def attrs(tag):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', tag))


def manifest_info(root):
    """从主清单里取 guava 的 remote / revision，取不到就退回 <default>。"""
    path = os.path.join(root, MAIN_MANIFEST)
    remote, revision, name = None, None, PROJ_NAME
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return name, remote, revision, ""

    m = re.search(r"<default[^>]*>", text)
    if m:
        d = attrs(m.group(0))
        remote, revision = d.get("remote"), d.get("revision")

    p = re.search(r'<project[^>]*path="%s"[^>]*>' % re.escape(PROJ_PATH), text)
    if p:
        a = attrs(p.group(0))
        name = a.get("name", name)
        remote = a.get("remote", remote)
        revision = a.get("revision", revision)

    # 谁把它 remove 了（twrp 的 remove-minimal.xml 就有这一条）
    removed_by = ""
    mdir = os.path.join(root, ".repo", "manifests")
    if os.path.isdir(mdir):
        for fn in os.listdir(mdir):
            if not fn.endswith(".xml"):
                continue
            try:
                with open(os.path.join(mdir, fn), encoding="utf-8", errors="ignore") as f:
                    body = f.read()
            except OSError:
                continue
            if re.search(r'remove-project[^>]*(path|name)="[^"]*%s[^"]*"' % re.escape(PROJ_PATH), body):
                removed_by = fn
                break
    return name, remote, revision, removed_by


def write_local_manifest(root, name, remote, revision):
    d = os.path.join(root, os.path.dirname(LOCAL_MANIFEST))
    os.makedirs(d, exist_ok=True)
    attrs_str = 'name="%s"' % name
    if remote:
        attrs_str += ' remote="%s"' % remote
    if revision:
        attrs_str += ' revision="%s"' % revision
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<manifest>\n"
        "  <!-- 由 scripts/ensure_guava.py 自动生成：补回被精简清单移除的 guava -->\n"
        '  <project path="%s" %s />\n'
        "</manifest>\n" % (PROJ_PATH, attrs_str)
    )
    with open(os.path.join(root, LOCAL_MANIFEST), "w", encoding="utf-8") as f:
        f.write(body)
    return attrs_str


def run(cmd, cwd, timeout=600):
    print("  $ %s" % " ".join(cmd))
    try:
        r = subprocess.run(cmd, cwd=cwd, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode("utf-8", "replace").strip().splitlines()
        for line in out[-12:]:
            print("    " + line)
        return r.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        print("    (%s)" % e)
        return 1


def branch_of(revision):
    if not revision:
        return None
    for p in ("refs/tags/", "refs/heads/"):
        if revision.startswith(p):
            return revision[len(p):]
    return revision


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."

    if has_module(root):
        print("external/guava 已就绪：%s/Android.bp 定义了 guava 模块" % PROJ_PATH)
        return 0

    print("::warning::external/guava 缺失或未定义 guava 模块，开始排查")

    d = os.path.join(root, PROJ_PATH)
    if os.path.isdir(d):
        n = len(os.listdir(d))
        print("  目录存在，但 Android.bp %s（目录内 %d 项）"
              % ("缺少 guava 定义" if os.path.isfile(os.path.join(d, "Android.bp")) else "不存在", n))
    else:
        print("  目录不存在：%s" % PROJ_PATH)

    name, remote, revision, removed_by = manifest_info(root)
    print("  清单条目：name=%s remote=%s revision=%s" % (name, remote, revision))
    if removed_by:
        print("  ::notice::%s 里有 remove-project 删掉了它（TWRP 的 remove-minimal.xml 就会）"
              % removed_by)

    if shutil.which("repo") and os.path.isdir(os.path.join(root, ".repo")):
        attrs_str = write_local_manifest(root, name, remote, revision)
        print("  已写入 .repo/local_manifests/zz-ensure-guava.xml：%s" % attrs_str)
        run(["repo", "sync", "-c", "--force-sync", "--no-clone-bundle",
             "-j4", PROJ_PATH], cwd=root)
        if has_module(root):
            print("::notice::repo sync 已补回 external/guava")
            return 0

    # 兜底：直接 git clone（repo 不可用 / sync 失败时）
    branch = branch_of(revision)
    if remote and branch:
        path = os.path.join(root, MAIN_MANIFEST)
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
            m = re.search(r'<remote[^>]*name="%s"[^>]*fetch="([^"]+)"' % re.escape(remote), text)
            fetch = m.group(1) if m else None
        except OSError:
            fetch = None
        if fetch:
            url = fetch.rstrip("/") + "/" + name
            if not url.startswith(("http://", "https://", "git@")):
                url = "https://" + url
            print("  兜底 git clone：%s (%s)" % (url, branch))
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            run(["git", "clone", "--depth=1", "--single-branch",
                 "-b", branch, url, PROJ_PATH], cwd=root, timeout=900)
            if has_module(root):
                print("::notice::git clone 已补回 external/guava")
                return 0

    print("::warning::external/guava 仍未就绪；若编译仍报 "
          "'missing dependencies: guava'，请检查上面的清单诊断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
