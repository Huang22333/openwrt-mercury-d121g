#!/usr/bin/env python3
"""打包原厂 U-Boot 能引导的 D121G OpenWrt 镜像（含 relocate 桩）。

原厂 U-Boot 的引导常量表（文件 0x9780 / 虚拟地址 0xBC009780，LE 字）:
    0x9780  0x88001000
    0x9784  0x8B800000
    0x9788  0xBBFFFFFF
    0x978C  0xBC015200   <- 固件源（flash 0x15200）
    0x9790  0xBC015000
    0x9794  0x80001000   <- ★解压/执行地址（不是 0x80000000！）
    0x9798  0x000186A0
    0x979C  0x44000000
    0x97A0  0x27051956   <- uImage magic（bootm 路径用）
    0x97A4  0x80C00000   <- 解压缓冲区上界

关键点：解压目标是 0x80001000，而 OpenWrt 的 mt7620 内核链接在 0x80000000
（ELF: _text=0x80000000, __kernel_entry=0x80000400, kernel_entry=0x8060d78c）。
直接放 0x80001000 会让 0x80001400 处那条 `j 0x8060D78C` 落到 0x1000 之前的
垃圾指令上 -> 无声死机（就是实测卡在 addr:0xbc015200 的原因）。

因此内核镜像首部必须带 OpenWrt 官方 relocate 桩:
    KERNEL := kernel-bin | append-dtb | relocate-kernel 0x80000000 | lzma | uImage lzma
桩被解压到 0x80001000 后：自身搬到 0x81000000 -> 内核搬到 0x80000000 -> 跳过去。

flash 布局（DTS 的 firmware 分区 = 0x15000..0x1000000，compatible = "lzma"）:
    0x15000  前 13 字节写成"假 LZMA 头"，其余保留原厂商头
    0x15200  真·裸 LZMA（relocate 桩 + 内核 + DTB）
    X        squashfs，X 必须 = 0x15000 + 64KB 的整数倍

为什么分区从 0x15000 而不是 0x15200 开始：
    mtdpart 对齐检查 = offset mod erasesize(64K)，不整除再 mod
    erasesize_minor(4K)。实测 0x15200 mod 4K = 0x200 -> 整个 firmware
    分区被强制只读，连带 rootfs_data 也只读，overlay 直接废掉。
    0x15000 mod 4K = 0 -> 正常可写。代价是分区首 13 字节不是真 LZMA 头，
    所以人工补一个假的（mtdsplit_lzma 只查 props[0]<225、dict 是 2 的幂、
    size_high==0，size_low 压根不看）。U-Boot 完全不读这 13 字节。

为什么 squashfs 要 64KB 对齐：mtdsplit_lzma 用 mtd_find_rootfs_from()，
只按 master->erasesize(=64KB) 步进扫 squashfs 魔数。

厂商头字段（实测公式，已用原厂 + 上一版镜像双向验证）:
    +0x5C = 裸 LZMA 长度                        （大端）
    +0x60 = 0x15000 + align_up(0x200 + len, 0x200)

用法:
    python3 make_tftp_reloc.py <原厂2MB dump> <sysupgrade.bin> <输出.bin> [--initramfs]
"""
import hashlib
import lzma
import struct
import sys

BLOCK = 0x10000
OFF_HDR = 0x15000
LEN_HDR = 0x200
OFF_PAYLOAD = 0x15200
KERNEL_ADDR = 0x80000000
UIMAGE_MAGIC = 0x27051956
SQUASHFS_MAGIC = b"hsqs"


def align_up(v, a):
    return (v + a - 1) // a * a


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    dump, fw, out = sys.argv[1:4]
    initramfs = "--initramfs" in sys.argv

    stock = open(dump, "rb").read()
    image = open(fw, "rb").read()

    magic, hcrc, t, size, load, ep, dcrc, os_, arch, typ, comp, name = \
        struct.unpack(">IIIIIIIBBBB32s", image[:64])
    assert magic == UIMAGE_MAGIC, "输入不是 uImage"
    assert comp == 3, "uImage 压缩方式不是 LZMA (comp=%d)" % comp
    assert load == KERNEL_ADDR, "uImage load=0x%08X，期望 0x%08X" % (load, KERNEL_ADDR)
    lzma_payload = image[64:64 + size]
    squashfs = image[64 + size:]
    print("uImage      : %d B, LZMA payload %d B, load=0x%08X ep=0x%08X"
          % (len(image), size, load, ep))

    dec = lzma.decompress(lzma_payload, format=lzma.FORMAT_ALONE)
    print("解压后      : %d B (0x%X)" % (len(dec), len(dec)))

    # relocate 桩布局: [0x10000 字节 nop 滑槽][桩代码][u32 内核长度][内核+DTB]
    # 前 64KB 全 0 是正常的 —— 那是入口的 nop 滑槽，给了 ±64KB 的入口容错。
    assert dec[0:0x10000] == bytes(0x10000), \
        "前 64KB 不是全 0 —— 内核没编 relocate-kernel！"
    # 桩尾没有符号，靠 "u32 == 其后剩余长度" 这个自洽条件定位内核起点
    kstart = None
    for pos in range(0x10000, min(0x20000, len(dec) - 4)):
        if struct.unpack("<I", dec[pos:pos + 4])[0] == len(dec) - (pos + 4):
            kstart = pos + 4
            break
    assert kstart, "找不到 relocate 桩尾的 u32 内核长度字段"
    ksize = len(dec) - kstart
    code = next(i for i, b in enumerate(dec) if b)
    assert dec[kstart:kstart + 0x400] == bytes(0x400), \
        "内核头部 0x400 字节应全是 0（MIPS 异常向量区）"
    w = struct.unpack("<I", dec[kstart + 0x400:kstart + 0x404])[0]
    assert (w >> 26) == 2, "内核 +0x400 不是 j 指令 (word=%#010x)" % w
    jtarget = KERNEL_ADDR | ((w & 0x03FFFFFF) << 2)
    assert KERNEL_ADDR <= jtarget < KERNEL_ADDR + ksize, \
        "kernel_entry 跳转目标 %#010x 越出内核范围" % jtarget
    print("  nop 滑槽  : 0x00000 - 0x%05X   (入口 ±64KB 容错)" % code)
    print("  桩代码    : 0x%05X - 0x%05X   (%d B)"
          % (code, kstart - 4, kstart - 4 - code))
    print("  内核+DTB  : 0x%05X - 0x%05X   (%d B)" % (kstart, len(dec), ksize))
    print("  +0x400    : j %#010x  (= kernel_entry，在内核范围内 ✓)" % jtarget)

    hdr = bytearray(stock[OFF_HDR:OFF_HDR + LEN_HDR])
    print("分区起点    : flash 0x15000（厂商容器头所在）")
    print("  原厂商头  : load/entry=0x%08X / 0x%08X"
          % (struct.unpack(">I", hdr[0x18:0x1C])[0],
             struct.unpack(">I", hdr[0x1C:0x20])[0]))

    # 把前 13 字节改写成"假 LZMA 头"，好让 mtdsplit_lzma 认下 0x15000 这个分区。
    # 它只检查 props[0] < 225、dict 是 2 的幂、size_high == 0；size_low 根本不看。
    # U-Boot 完全不读这 13 字节（厂商容器 magic 在 U-Boot 里一个都不存在，
    # 且它用的地址全部来自自己代码里的常量池）。其余字节（含 +0x18 的 load）
    # 原样保留，以防万一。
    hdr[0:13] = lzma_payload[0:5] + bytes(8)
    dict_sz = struct.unpack("<I", hdr[1:5])[0]
    print("  假 LZMA 头: %s   (props=%#04x dict=%#x size_high=0)"
          % (hdr[0:13].hex(" "), hdr[0], dict_sz))
    assert hdr[0] < 9 * 5 * 5, "假 LZMA 头 props 非法"
    assert dict_sz and not (dict_sz & (dict_sz - 1)), "假 LZMA 头 dict 不是 2 的幂"
    assert hdr[9:13] == bytes(4), "假 LZMA 头 size_high 必须为 0"

    if initramfs:
        body = lzma_payload
        print("模式        : initramfs（无 squashfs，靠内核内建 ramdisk）")
    else:
        assert squashfs[:4] == SQUASHFS_MAGIC, "sysupgrade 里没找到 squashfs"
        # squashfs 必须落在"相对分区起点 64KB 的整数倍"处 ——
        # mtdsplit_lzma 的 mtd_find_rootfs_from() 只按 master->erasesize(=64KB) 步进扫描。
        # 分区起点 = flash 0x15000，文件里对应 flash 0x15000。
        sq_off = align_up(LEN_HDR + len(lzma_payload), BLOCK)
        pad = sq_off - LEN_HDR - len(lzma_payload)
        body = lzma_payload + b"\xff" * pad + squashfs
        print("模式        : sysupgrade（持久化）")
        print("  裸 LZMA   : flash 0x%X - 0x%X (%d B)"
              % (OFF_HDR + LEN_HDR, OFF_HDR + LEN_HDR + len(lzma_payload), len(lzma_payload)))
        print("  填充      : %d B -> squashfs 落在分区内偏移 0x%X（64KB 整数倍）"
              % (pad, sq_off))
        print("  squashfs  : flash 0x%X - 0x%X (%d B)"
              % (OFF_HDR + sq_off, OFF_HDR + len(body), len(squashfs)))

    hdr[0x5C:0x60] = struct.pack(">I", len(lzma_payload))
    hdr[0x60:0x64] = struct.pack(">I", OFF_HDR + align_up(LEN_HDR + len(lzma_payload), 0x200))
    print("头字段更新  : +0x5C=0x%X  +0x60=0x%X"
          % (len(lzma_payload), OFF_HDR + align_up(LEN_HDR + len(lzma_payload), 0x200)))

    out_bytes = bytes(hdr) + body
    with open(out, "wb") as fh:
        fh.write(out_bytes)

    print()
    print("输出        : %s" % out)
    print("TFTP 文件   : %d B (%.2f MB)  -> option 2 写入 flash 0x15000"
          % (len(out_bytes), len(out_bytes) / 1048576))
    print("SHA256      : %s" % hashlib.sha256(out_bytes).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
