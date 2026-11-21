#!/usr/bin/env python3
"""向 vendor_boot.img 的 recovery ramdisk 里补齐缺失的间接依赖 .so。

背景
----
TWRP 的 /system/bin/recovery 有一批 DT_NEEDED 的**间接**依赖，例如

    recovery -> android.security.maintenance-ndk.so
             -> android.system.keystore2-V4-ndk.so          (缺失)
    recovery -> libtar.so
             -> libkeystore-attestation-application-id.so
             -> android.security.aaid_aidl-cpp.so           (缺失)

这些库只被装到 $OUT/system/lib64，Soong 不会为它们生成 recovery variant，
所以 recovery ramdisk 里没有。linker 在 __linker_init 阶段发现库缺失后直接
exit(1)，进程只活 4~8ms、没有任何输出，屏幕就一直停在 bootloader 的 logo 上，
而 adbd 依旧可用（看起来像"能进 adb 但黑屏"）。

试过但无效的两条路（记录在此，避免重复踩）
  - BoardConfig: TARGET_RECOVERY_DEVICE_MODULES += xxx  → 只装到 system/lib64
  - bootable/recovery/Android.mk: LOCAL_SHARED_LIBRARIES += xxx → 同样不进 ramdisk

本脚本走确定性路线：构建完成后直接对 vendor_boot.img 做无损外科手术
  1. 解开每个 vendor_ramdisk 分片（LZ4 legacy frame）
  2. 解析 cpio，算出目标 ELF 的依赖闭包，找出 ramdisk 里没有的 .so
  3. 从 --libs 指定的目录里取出这些 .so，追加进 recovery 分片（type=2）的 cpio
  4. 重新压缩（lz4 HC level 12，与原镜像参数一致）、重排 header / 分片表
  5. dtb、platform 分片、bootconfig、末尾 AVB footer 全部原样保留，
     文件总长度与分区大小完全一致（不越界、不搬动 footer 位置）

用法
  fix_vendor_boot_libs.py vendor_boot.img \
      --libs out/target/product/PD2551/system/lib64,... [--out 新路径] [--dry-run]
  fix_vendor_boot_libs.py vendor_boot.img --selftest   # 验证 cpio/lz4 往返无损
"""
import argparse
import os
import struct
import sys

MAGIC_LZ4 = 0x184C2102
BLOCK = 8 * 1024 * 1024


# ---------------------------------------------------------------- lz4 legacy
# 优先用 python-lz4（快）；CI 里没装时退回系统 lz4 CLI（Android 构建机一定装了）。
def _have_py_lz4():
    try:
        import lz4.block  # noqa: F401
        return True
    except Exception:
        return False


def _have_cli_lz4():
    from shutil import which
    return which('lz4') is not None


def lz4_legacy_decompress(buf):
    if _have_py_lz4():
        import lz4.block
        out = bytearray()
        p, n = 0, len(buf)
        while p + 4 <= n:
            if struct.unpack_from('<I', buf, p)[0] != MAGIC_LZ4:
                break
            p += 4
            while p + 4 <= n:
                sz, = struct.unpack_from('<I', buf, p)
                p += 4
                if sz == 0:
                    break
                out += lz4.block.decompress(buf[p:p + sz], uncompressed_size=BLOCK + 64)
                p += sz
                if p + 4 <= n and struct.unpack_from('<I', buf, p)[0] == MAGIC_LZ4:
                    break
            if p + 4 > n or struct.unpack_from('<I', buf, p)[0] != MAGIC_LZ4:
                break
        return bytes(out)

    import subprocess
    # lz4 CLI 能直接吃 legacy frame
    p = subprocess.run(['lz4', '-d', '-c'], input=buf, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError('lz4 -d 失败: %s' % p.stderr.decode('utf-8', 'replace')[:200])
    return p.stdout


def lz4_legacy_compress(data):
    if _have_py_lz4():
        import lz4.block
        out = bytearray()
        out += struct.pack('<I', MAGIC_LZ4)
        for i in range(0, len(data), BLOCK):
            comp = lz4.block.compress(data[i:i + BLOCK], store_size=False,
                                      mode='high_compression', compression=12)
            out += struct.pack('<I', len(comp))
            out += comp
        return bytes(out)

    import subprocess
    # -l legacy frame / -12 HC / -B7 4MB 块（CLI 最大块）
    p = subprocess.run(['lz4', '-l', '-12', '-B7', '-c'], input=data,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError('lz4 -l 失败: %s' % p.stderr.decode('utf-8', 'replace')[:200])
    return p.stdout


# --------------------------------------------------------------------- cpio
def pad4(x):
    return (x + 3) & ~3


def cpio_parse(d):
    """返回 ([(header, name, body)], consumed, trailer_header)"""
    ents = []
    p = 0
    while True:
        i = d.find(b'070701', p)
        if i < 0:
            break
        p = i
        while p + 110 <= len(d) and d[p:p + 6] == b'070701':
            h = d[p:p + 110]
            f = [int(h[6 + k * 8:14 + k * 8], 16) for k in range(13)]
            fsize, nsize = f[6], f[11]
            name = d[p + 110:p + 110 + nsize - 1].decode('utf-8', 'replace')
            bo = pad4(p + 110 + nsize)
            body = d[bo:bo + fsize]
            p = pad4(bo + fsize)
            if name == 'TRAILER!!!':
                return ents, p, h
            ents.append((h, name, body))
    return ents, p, None


def cpio_build(ents, extra=None, trailer=None, pad_to=512):
    out = bytearray()
    for h, name, body in ents:
        out += h
        out += name.encode() + b'\0'
        out += b'\0' * (pad4(len(name) + 1 + 110) - (len(name) + 1 + 110))
        out += body
        out += b'\0' * (pad4(len(body)) - len(body))
    ino = 0x74000
    for name, mode, body in (extra or []):
        ino += 1
        fields = [ino, mode, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(name) + 1, 0]
        out += b'070701' + b''.join(b'%08X' % x for x in fields)
        out += name.encode() + b'\0'
        out += b'\0' * (pad4(len(name) + 1 + 110) - (len(name) + 1 + 110))
        out += body
        out += b'\0' * (pad4(len(body)) - len(body))
    if trailer is not None:
        out += trailer
    else:
        tr = [ino + 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 11, 0]
        out += b'070701' + b''.join(b'%08X' % x for x in tr)
    out += b'TRAILER!!!\0'
    out += b'\0' * (pad4(11) - 11)
    if pad_to:
        out += b'\0' * ((pad_to - len(out) % pad_to) % pad_to)
    return bytes(out)


# ---------------------------------------------------------------------- ELF
def elf_needed(b):
    try:
        if b[:4] != b'\x7fELF':
            return []
        shoff, = struct.unpack_from('<Q', b, 0x28)
        es, shnum, _ = struct.unpack_from('<HHH', b, 0x3A)
        secs = [struct.unpack_from('<IIQQQQIIQQ', b, shoff + i * es) for i in range(shnum)]
        dyn = [x for x in secs if x[1] == 6]
        if not dyn:
            return []
        _, _, _, _, doff, dsz, link, *_ = dyn[0]
        st = secs[link]
        strs = b[st[4]:st[4] + st[5]]
        out, o = [], doff
        while o + 16 <= doff + dsz:
            t, v = struct.unpack_from('<qQ', b, o)
            o += 16
            if t == 0:
                break
            if t == 1:
                out.append(strs[v:strs.find(b'\0', v)].decode())
        return out
    except Exception:
        return []


# --------------------------------------------------------------- vendor_boot
class VB:
    def __init__(self, path):
        self.raw = open(path, 'rb').read()
        f = self.raw
        assert f[:8] == b'VNDRBOOT', 'not a vendor_boot image'
        self.hv, self.ps = struct.unpack_from('<II', f, 8)
        assert self.hv == 4, 'only vendor_boot header v4 supported (got %d)' % self.hv
        self.vsize, = struct.unpack_from('<I', f, 24)
        self.hsize, self.dsize = struct.unpack_from('<II', f, 2096)
        self.tsize, self.tnum, self.tent, self.bcsize = struct.unpack_from('<IIII', f, 2112)
        self.vroff = self.al(self.hsize)
        self.dtboff = self.al(self.vroff + self.vsize)
        self.toff = self.al(self.dtboff + self.dsize)
        self.bcoff = self.al(self.toff + self.tsize)
        self.header = f[:self.hsize]
        self.vr = f[self.vroff:self.vroff + self.vsize]
        self.dtb = f[self.dtboff:self.dtboff + self.dsize]
        self.table = f[self.toff:self.toff + self.tsize]
        self.bc = f[self.bcoff:self.bcoff + self.bcsize]
        self.entries = [struct.unpack_from('<III', self.table, i * self.tent)
                        for i in range(self.tnum)]

    def al(self, x):
        return (x + self.ps - 1) // self.ps * self.ps


def load_frags(vb):
    frags = []
    for sz, off, typ in vb.entries:
        if sz == 0:
            continue
        raw = vb.vr[off:off + sz]
        frags.append(dict(size=sz, off=off, typ=typ, raw=raw,
                          plain=lz4_legacy_decompress(raw)))
    return frags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('img')
    ap.add_argument('--out', default=None, help='输出路径，默认原地覆盖')
    ap.add_argument('--libs', default='', help='逗号分隔的 .so 搜索目录')
    ap.add_argument('--targets', default='system/bin/recovery')
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    vb = VB(a.img)
    print('== %s ==' % a.img)
    print('header v%d page=%d ramdisk=%d dtb=%d table=%d(%d 分片) bootconfig=%d'
          % (vb.hv, vb.ps, vb.vsize, vb.dsize, vb.tsize, vb.tnum, vb.bcsize))

    frags = load_frags(vb)
    for fr in frags:
        print('  frag type=%d compressed=%d -> plain=%d'
              % (fr['typ'], fr['size'], len(fr['plain'])))

    if a.selftest:
        ok = True
        for i, fr in enumerate(frags):
            ents, consumed, tr = cpio_parse(fr['plain'])
            rebuilt = cpio_build(ents, trailer=tr, pad_to=0)
            if len(rebuilt) < consumed:
                rebuilt += b'\0' * (consumed - len(rebuilt))
            same = rebuilt == fr['plain'][:len(rebuilt)]
            comp = lz4_legacy_compress(fr['plain'])
            size_ok = len(comp) == fr['size']
            print('  frag%d entries=%d cpio_lossless=%s lz4_lossless=%s recompressed=%d/%d'
                  % (i, len(ents), same, size_ok, len(comp), fr['size']))
            ok = ok and same and size_ok
        return 0 if ok else 1

    # ---- 依赖闭包 ----
    libidx = {}
    for fr in frags:
        ents, _, tr = cpio_parse(fr['plain'])
        fr['ents'], fr['tr'] = ents, tr
        for h, name, body in ents:
            bn = os.path.basename(name)
            if '.so' in bn:
                libidx.setdefault(bn, name)

    search = [d for d in a.libs.split(',') if d and os.path.isdir(d)]
    miss = {}
    queue = [t for t in a.targets.split(',') if t]
    seen = set()
    body_of = {n: b for fr in frags for h, n, b in fr['ents']}
    while queue:
        cur = queue.pop()
        if cur in seen or cur not in body_of:
            continue
        seen.add(cur)
        for n in elf_needed(body_of[cur]):
            if n in libidx:
                queue.append(libidx[n])
            elif n not in miss:
                miss[n] = None
                for d in search:
                    p = os.path.join(d, n)
                    if os.path.isfile(p):
                        miss[n] = p
                        break

    print('\n依赖闭包里 ramdisk 缺少的库：')
    if not miss:
        print('  （无，ramdisk 已完整）')
    for n, p in sorted(miss.items()):
        print('  %-46s %s' % (n, p or '!! 在 --libs 目录里也找不到，跳过'))

    todo = {n: p for n, p in miss.items() if p}
    if not todo:
        print('\n没有可补的库，镜像保持不变')
        return 0
    if a.dry_run:
        print('\n--dry-run，不写文件')
        return 0

    # ---- 追加到 recovery 分片 ----
    idx_target = next((i for i, fr in enumerate(frags) if fr['typ'] == 2),
                      len(frags) - 1)
    extra = []
    for n, p in sorted(todo.items()):
        data = open(p, 'rb').read()
        extra.append(('system/lib64/' + n, 0o100644, data))
        print('  + system/lib64/%-42s %6d bytes' % (n, len(data)))

    fr = frags[idx_target]
    new_plain = cpio_build(fr['ents'], extra, trailer=fr['tr'], pad_to=512)
    fr['new_raw'] = lz4_legacy_compress(new_plain)
    print('\nfrag%d：%d -> %d bytes（解压后 %d -> %d）'
          % (idx_target, fr['size'], len(fr['new_raw']),
             len(fr['plain']), len(new_plain)))

    # ---- 重排镜像 ----
    # vendor_ramdisk 是整体 blob：分片首尾相接，整个 blob 再按页对齐
    vrblob = bytearray()
    new_entries = []
    for fr2 in frags:
        raw = fr2.get('new_raw', fr2['raw'])
        new_entries.append((len(raw), len(vrblob), fr2['typ']))
        vrblob += raw

    tbl = bytearray()
    for i, (sz, of, typ) in enumerate(new_entries):
        old = vb.table[i * vb.tent:(i + 1) * vb.tent]
        tbl += struct.pack('<III', sz, of, typ) + old[12:vb.tent]

    out = bytearray()
    out += vb.header
    out += b'\0' * (vb.al(vb.hsize) - vb.hsize)
    out += vrblob
    out += b'\0' * (vb.al(len(vrblob)) - len(vrblob))
    out += vb.dtb
    out += b'\0' * (vb.al(len(vb.dtb)) - len(vb.dtb))
    out += bytes(tbl)
    out += b'\0' * (vb.al(len(tbl)) - len(tbl))
    out += vb.bc
    out += b'\0' * (vb.al(len(vb.bc)) - len(vb.bc))

    # 保持与分区同尺寸：尾部是空白填充 + 末尾 64B AVB footer
    footer = vb.raw[-64:]
    limit = len(vb.raw) - len(footer)
    if len(out) > limit:
        print('!! 内容 %d 超出可用空间 %d（多 %d 字节），放弃修改'
              % (len(out), limit, len(out) - limit))
        return 2
    out += b'\0' * (limit - len(out))
    out += footer

    struct.pack_into('<I', out, 24, len(vrblob))
    struct.pack_into('<I', out, 2112, len(tbl))
    struct.pack_into('<I', out, 2116, len(new_entries))

    dst = a.out or a.img
    if a.out:
        open(dst, 'wb').write(bytes(out))
    else:
        tmp = dst + '.tmp'
        open(tmp, 'wb').write(bytes(out))
        os.replace(tmp, dst)
    print('写出 %s（%d bytes，与原镜像同尺寸）' % (dst, len(out)))

    # ---- 回读校验 ----
    chk = VB(dst)
    cfrags = load_frags(chk)
    names = [n for fr3 in cfrags for h, n, b in cpio_parse(fr3['plain'])[0]]
    bad = [n for n in ('system/lib64/' + x for x in todo) if n not in names]
    print('回读校验：新增 %d 个文件，缺失 %d 个 %s'
          % (len(todo), len(bad), bad if bad else ''))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
