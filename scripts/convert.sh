#!/bin/bash
# 把设备树里的 *.dependencies（roomservice 风格的 XML 片段）转换成 repo 的 local_manifest。
#
# 用法:
#   bash convert.sh <path-to-dependencies-file> [<path-to-local-manifest>]
#
# 行为:
#   - 未指定输出路径时，自动写入 .repo/local_manifests/roomservice.xml（需在源码根目录执行）
#   - 输出文件已存在时做「合并」而不是覆盖（已有的 project 保留，新条目追加）
#
# 与原版的区别:
#   - 原版 `grep remote | cut -d'"' -f4` 会把 <remote fetch="..."> 的 fetch 值当成 remote 名，
#     生成的 remote="https://github.com/" 是非法属性，repo sync 会直接报错；
#   - 原版固定循环 6 次，超过 6 个依赖项目会被静默丢弃；
#   - 原版用 sed 拼接字符串，路径/分支里有特殊字符时会产生非法 XML。
#   现在改用 python3 解析 XML 节点，按属性生成，并对 remote/revision 做归一化。

set -uo pipefail

if [ -z "${1:-}" ] || [ ! -f "${1:-}" ]; then
	echo " ** Input File : ${1:-<empty>} does not exist"
	echo " ** Please specify the correct dependencies file"
	echo " ** Usage : bash <path-to-script> <path-to-dependencies-file> [<path-to-local-manifest>]"
	exit 1
fi

file="$1"

if [ -n "${2:-}" ]; then
	manifest_path="$2"
elif [ -d .repo ]; then
	mkdir -p .repo/local_manifests
	manifest_path=".repo/local_manifests/roomservice.xml"
else
	echo " ** Manifest file to create not specified."
	echo " ** And .repo folder does not exist in $PWD"
	echo " ** Either run the script from root of your source or specify a custom path+filename"
	echo " ** Usage : bash <path-to-script> <path-to-dependencies-file> [<path-to-local-manifest>]"
	exit 1
fi

mkdir -p "$(dirname "$manifest_path")"

python3 - "$file" "$manifest_path" <<'PY'
import os
import re
import sys
import xml.etree.ElementTree as ET

src, dst = sys.argv[1], sys.argv[2]

try:
    raw = open(src, 'r', encoding='utf-8', errors='replace').read()
except OSError as e:
    print(f" ** 无法读取 {src}: {e}", file=sys.stderr)
    sys.exit(1)


def parse_fragment(text):
    """解析 .dependencies：可能带 <manifest> 根节点，也可能只有若干 <project> 行。"""
    text = text.strip()
    if not text:
        return None
    body = re.sub(r'<\?xml[^>]*\?>', '', text).strip()
    if not body.startswith('<manifest'):
        body = '<manifest>\n' + body + '\n</manifest>'
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        print(f" ** XML 解析失败 ({src}): {e}", file=sys.stderr)
        return None


def tag_of(el):
    return el.tag.split('}')[-1]


def collect(root):
    projects, remotes = [], {}
    for el in root.iter():
        t = tag_of(el)
        if t == 'remote' and el.get('name'):
            remotes[el.get('name')] = el.get('fetch', '')
        elif t == 'project' and el.get('name') and el.get('path'):
            projects.append(dict(el.attrib))
    return projects, remotes


root = parse_fragment(raw)
if root is None:
    if not raw.strip():
        print(f" ** 依赖文件为空：{src}", file=sys.stderr)
    sys.exit(1)
new_projects, dep_remotes = collect(root)

if not new_projects:
    print(f" ** {src} 中没有找到任何 <project> 条目（检查文件内容/分支是否正确）", file=sys.stderr)
    sys.exit(1)

# 读取已有 manifest，做合并
existing = []
existing_remotes = {}
if os.path.exists(dst):
    try:
        old_root = ET.parse(dst).getroot()
        existing, existing_remotes = collect(old_root)
    except ET.ParseError:
        print(f" ** 已有 manifest {dst} 无法解析，将重新生成", file=sys.stderr)

seen = {(p.get('path'), p.get('name')) for p in existing}
names = list(existing)

# 归一化：remote 若是 URL，生成一个同名派生 remote；revision 支持 branch 别名
remotes = dict(existing_remotes)
remotes.update(dep_remotes)
for idx, p in enumerate(new_projects):
    remote = (p.get('remote') or '').strip()
    if remote and ('://' in remote or remote.startswith('git@')):
        rname = f"dep{idx}"
        remotes[rname] = remote
        p['remote'] = rname
    if not p.get('revision') and p.get('branch'):
        p['revision'] = p['branch']
    p.pop('branch', None)
    if (p.get('path'), p.get('name')) not in seen:
        seen.add((p.get('path'), p.get('name')))
        names.append(p)

used_remotes = {p.get('remote') for p in names if p.get('remote')}

def attr_escape(v):
    return (v.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))

lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<manifest>']
for rname in sorted(used_remotes):
    fetch = remotes.get(rname)
    if fetch:
        lines.append(f'  <remote name="{attr_escape(rname)}" fetch="{attr_escape(fetch)}" />')

order = ('path', 'name', 'remote', 'revision', 'groups', 'clone-depth', 'upstream', 'sync-c')
for p in names:
    parts = []
    for k in order:
        if p.get(k) not in (None, ''):
            parts.append(f'{k}="{attr_escape(str(p[k]))}"')
    for k, v in p.items():
        if k not in order and v not in (None, ''):
            parts.append(f'{k}="{attr_escape(str(v))}"')
    lines.append('  <project ' + ' '.join(parts) + ' />')
lines.append('</manifest>')

tmp = dst + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\n')
os.replace(tmp, dst)

print(f" ** 已写入 {dst}：共 {len(names)} 个 project（本次新增 {len(names) - len(existing)} 个）")
PY

exit $?
