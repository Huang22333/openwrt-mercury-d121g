#!/usr/bin/env python3
"""在 PC 上模拟内核的 mtd 分区发现流程，验证 flash 镜像布局真的能被拆开。

复刻三处内核逻辑（全部按本机实测的 erasesize 参数）：
  1) mtdsplit_parse_lzma()  @ drivers/mtd/mtdsplit/mtdsplit_lzma.c
       读分区头 13 字节 -> props[0] < 225、dict 是 2 的幂、size_high == 0
       然后 mtd_find_rootfs_from(master, master->erasesize, master->size, ...)
       按 erasesize 步进找 squashfs 魔数
  2) mtdpart.c 的 4K 对齐检查
       offset mod erasesize(64K)，有余再 mod erasesize_minor(4K)，
       两边都不整除 -> MTD_WRITEABLE 被清掉（分区只读）
  3) mtdsplit_parse_squashfs()  @ mtdsplit_squashfs.c
       读 squashfs superblock 的 bytes_used，算 rootfs_data 的起点

实测参数（来自内核 .config）：
  CONFIG_MTD_SPI_NOR_USE_VARIABLE_ERASE=y / USE_4K_SECTORS 未开
  W25Q128: INFO(0xef4018,0,64*1024,256) + SECT_4K  -> erasesize=64K, minor=4K

用法: python3 verify_layout.py tftp_ow_sysup.bin
"""
import struct
import sys

FLASH = 16 * 1024 * 1024
OFF_HDR = 0x15000          # firmware 分区起点（DTS: partition@15000）
OFF_PAYLOAD = 0x15200      # U-Boot 固定读 LZMA 的位置
SQUASHFS_MAGIC = 0x73717368
ERASE = 0x10000            # erasesize
ERASE_MINOR = 0x1000       # erasesize_minor
PROPS_MAX = 9 * 5 * 5


def roundup_eb(x):
    return (x + ERASE - 1) & ~(ERASE - 1)


def rounddown_eb(x):
    return x & ~(ERASE - 1)


def next_eb(x):
    return rounddown_eb(x) + ERASE


def writable(flash, off, size):
    """复刻 mtdpart.c 的对齐检查，返回 (可写?, 原因)"""
    for label, addr in (("起点", off), ("终点", off + size)):
        rem = addr % ERASE
        if rem:
            rem %= ERASE_MINOR
        if rem:
            return False, "%s 0x%X 不对齐 (mod 64K=%#x, mod 4K=%#x)" % (
                label, addr, addr % ERASE, addr % ERASE_MINOR)
    return True, "起点/终点都对齐"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "tftp_ow_sysup.bin"
    initramfs = "--initramfs" in sys.argv
    img = open(path, "rb").read()
    print("镜像 %s : %d B" % (path, len(img)))

    # 还原成 16MB flash（0xFF 填充），镜像写在 0x15000
    flash = bytearray(b"\xff" * FLASH)
    flash[OFF_HDR:OFF_HDR + len(img)] = img

    part_size = FLASH - OFF_HDR
    print("\n--- firmware 分区: flash 0x%X - 0x%X (size 0x%X) ---"
          % (OFF_HDR, FLASH, part_size))

    ok, why = writable(flash, OFF_HDR, part_size)
    print("① 可写性检查: %s  %s" % ("✓ 可写" if ok else "✗ 只读!", why))
    if not ok:
        return 1

    # ---- ② mtdsplit_parse_lzma 的头部校验 ----
    props = flash[OFF_HDR:OFF_HDR + 5]
    dict_sz = struct.unpack("<I", flash[OFF_HDR + 1:OFF_HDR + 5])[0]
    size_high = struct.unpack("<I", flash[OFF_HDR + 9:OFF_HDR + 13])[0]
    print("② mtdsplit_lzma 头部校验 @0x%X: props=%#04x dict=%#x size_high=%d"
          % (OFF_HDR, props[0], dict_sz, size_high))
    assert props[0] < PROPS_MAX, "props[0] 太大 -> 解析器会放弃"
    assert dict_sz and not (dict_sz & (dict_sz - 1)), "dict 不是 2 的幂 -> 放弃"
    assert size_high == 0, "size_high != 0 -> 放弃"
    print("   ✓ 通过（解析器会继续去找 rootfs）")

    # ---- ③ mtd_find_rootfs_from: 从 erasesize 起按 64K 步进 ----
    found = None
    off = ERASE
    scanned = 0
    while off < part_size:
        scanned += 1
        if struct.unpack("<I", flash[OFF_HDR + off:OFF_HDR + off + 4])[0] == SQUASHFS_MAGIC:
            found = off
            break
        off = next_eb(off)
    print("③ mtd_find_rootfs_from: 扫了 %d 个 64K 候选" % scanned)
    if found is None:
        if initramfs:
            print("   没有 squashfs 魔数 —— initramfs 版本来就这样"
                  "（根文件系统打在内核里），到此为止 ✓")
            return 0
        raise AssertionError("没找到 squashfs 魔数 -> 不会产生 rootfs 分区")
    print("   找到 squashfs，分区内偏移 0x%X (绝对 0x%X)" % (found, OFF_HDR + found))
    print("   拆出: kernel = 0x000000..0x%06X  rootfs = 0x%06X..0x%06X"
          % (found, found, part_size))

    # ---- ④ U-Boot 的固定读取偏移必须落在 kernel 分区里 ----
    rel = OFF_PAYLOAD - OFF_HDR
    assert rel < found, "U-Boot 读的 0x15200 跑到 rootfs 里了"
    print("④ U-Boot 固定读 LZMA 的位置 0x%X -> 分区内 0x%X，落在 kernel 区内 ✓"
          % (OFF_PAYLOAD, rel))

    # ---- ⑤ mtdsplit_squashfs: rootfs -> rootfs + rootfs_data ----
    m = struct.unpack("<I", flash[OFF_HDR + found:OFF_HDR + found + 4])[0]
    bytes_used = struct.unpack("<Q", flash[OFF_HDR + found + 40:OFF_HDR + found + 48])[0]
    print("⑤ squashfs superblock: magic=%#x bytes_used=%d (0x%X)"
          % (m, bytes_used, bytes_used))
    assert m == SQUASHFS_MAGIC
    assert 0 < bytes_used <= part_size - found, "bytes_used 越界"
    abs_end = OFF_HDR + found + bytes_used
    rootfs_size = part_size - found
    # mtdsplit_parse_squashfs 用的是 mtdpart_get_offset(master)，返回的是
    # "相对父分区(firmware)"的偏移，不是绝对偏移（已用实测 /proc/mtd 反推确认）：
    #   rd_rel = roundup_eb(rootfs_rel + bytes_used)          <- 相对 firmware
    #   part.offset(相对 rootfs) = rd_rel - rootfs_rel
    #   size = rounddown(rootfs_size - part.offset)
    rootfs_rel = found
    rd_rel = roundup_eb(rootfs_rel + bytes_used)
    rd_off = rd_rel - rootfs_rel
    rd_abs = OFF_HDR + found + rd_off
    rd_size = (rootfs_size - rd_off) & ~(ERASE - 1)
    print("   rootfs 分区 : 绝对 0x%X - 0x%X (size 0x%X)"
          % (OFF_HDR + found, OFF_HDR + found + rootfs_size, rootfs_size))
    print("   rootfs_data: 绝对 0x%X - 0x%X  (size 0x%X = %.1f MB)"
          % (rd_abs, rd_abs + rd_size, rd_size, rd_size / 1048576))
    print("                (内核按『相对 firmware 偏移』报的是 0x%X-0x%X)"
          % (rd_abs - OFF_HDR, rd_abs - OFF_HDR + rd_size))
    assert rd_abs + rd_size <= FLASH, "rootfs_data 越过 flash 末尾"

    ok, why = writable(flash, OFF_HDR + found + rd_off, rd_size)
    print("⑥ rootfs_data 可写性: %s  %s" % ("✓ 可写" if ok else "✗ 只读!", why))
    if not ok:
        return 1

    # ---- ⑦ 元数据（系统内 sysupgrade 需要 REQUIRE_IMAGE_METADATA=1）----
    has_md = b"metadata_version" in img
    print("⑦ sysupgrade 元数据: %s" % ("✓ 有" if has_md else "✗ 没有（系统内 sysupgrade 会被拒）"))
    assert has_md

    print("\n=== 布局模拟全部通过：内核能拆出 kernel / rootfs / rootfs_data，"
          "且三个分区都可写 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
