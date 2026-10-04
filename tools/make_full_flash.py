#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
========================================================================
 D121G 一键生成刷机镜像（单文件版，不需要其它脚本）
========================================================================

把你用编程器读出来的「原厂 2MB 备份」+「GitHub 上编译出来的 sysupgrade.bin」
拼成一片可以直接写进新 W25Q128（16MB）的完整镜像。

这样刷机就完全不需要：串口线、TFTP 服务器、U-Boot 菜单。

------------------------------------------------------------------------
 怎么用（Windows）
------------------------------------------------------------------------
1. 装 Python：https://www.python.org/downloads/
   安装时**务必勾选** "Add Python to PATH"
2. 把本文件、原厂备份、编译出的 sysupgrade.bin 放到同一个文件夹（比如 D:\d121g）
3. 在那个文件夹里按住 Shift + 右键 → 「在此处打开 PowerShell 窗口」
4. 输入（文件名按实际的改）：

   python make_full_flash.py stock.bin openwrt-ramips-mt7620-mercury_d121g-squashfs-sysupgrade.bin

5. 生成的 d121g_full_16m.bin 就是最终要写进新 flash 的文件

------------------------------------------------------------------------
 参数
------------------------------------------------------------------------
  python make_full_flash.py <原厂2MB备份> <sysupgrade.bin> [输出文件名]
========================================================================
"""
import hashlib
import os
import struct
import sys

# ---------------------------------------------------------------- 常量
FLASH_SIZE = 16 * 1024 * 1024

LEN_UBOOT = 0xF800            # U-Boot 区长度
OFF_FACTORY = 0xF800          # WiFi 校准区起点（每台唯一）
LEN_FACTORY = 0x800
OFF_MINIFS = 0x10000
LEN_MINIFS = 0x5000
OFF_FW = 0x15000              # 固件区起点（分区从 0x15000 开始，不是 0x15200）
OFF_HDR = 0x15000             # 厂商头
LEN_HDR = 0x200
OFF_PAYLOAD = 0x15200         # U-Boot 固定从这个位置读 LZMA
BLOCK = 0x10000               # 64KB，squashfs 必须对齐到这里
KERNEL_ADDR = 0x80000000
UIMAGE_MAGIC = 0x27051956
SQUASHFS_MAGIC = b"hsqs"

# 原厂 U-Boot 里 "flash 只有 2MB" 的上限常量（磁盘上的小端字节）：
#   lui v0,0x1e ; ori v0,v0,0xb001  ->  0x1EB001 = 0x200000 - 0x15000 + 1
# 把 0x1e 换成 0xfe 得 0xFEB001（15.92MB），U-Boot 才肯接受大镜像。
LUI_OLD = bytes.fromhex("1e00023c")
LUI_NEW = bytes.fromhex("fe00023c")
ORI_MARK = bytes.fromhex("01b04234")


def say(msg=""):
    print(msg, flush=True)


def die(msg, code=1):
    say("")
    say("  !! 出错了：" + msg)
    say("")
    input("  按回车键退出...") if sys.stdin.isatty() else None
    sys.exit(code)


def align_up(v, a):
    return (v + a - 1) // a * a


# ------------------------------------------------- U-Boot 补丁
def find_uboot_limit(ub):
    hits, i = [], ub.find(LUI_OLD)
    while i >= 0:
        if ORI_MARK in ub[i:i + 12]:
            hits.append(i)
        i = ub.find(LUI_OLD, i + 1)
    return hits


# ------------------------------------------------- 固件段打包
def build_fw_section(stock, fw_path):
    """把 sysupgrade.bin 变成 [0x200 厂商头][裸 LZMA][补齐][squashfs]"""
    image = open(fw_path, "rb").read()
    if len(image) < 64:
        die("这个 bin 文件太小，不像是编译产物。")

    magic, hcrc, t, size, load, ep, dcrc, os_, arch, typ, comp, name = \
        struct.unpack(">IIIIIIIBBBB32s", image[:64])
    if magic != UIMAGE_MAGIC:
        die("这不是一个 uImage 文件（magic 对不上）。\n"
            "    请确认你下的是编译产物里的\n"
            "    openwrt-ramips-mt7620-mercury_d121g-squashfs-sysupgrade.bin")
    if load != KERNEL_ADDR:
        die("内核加载地址是 0x%08X，本机需要 0x%08X。\n"
            "    镜像来源不对，请重新编译。" % (load, KERNEL_ADDR))

    lzma = image[64:64 + size]
    squashfs = image[64 + size:]
    say("  uImage 大小 : %d 字节" % len(image))
    say("  压缩载荷    : %d 字节" % len(lzma))
    say("  根文件系统  : %d 字节" % len(squashfs))

    if squashfs[:4] != SQUASHFS_MAGIC:
        die("没在编译产物里找到 squashfs。文件可能不完整。")

    hdr = bytearray(stock[OFF_HDR:OFF_HDR + LEN_HDR])
    # 前 13 字节写成"假 LZMA 头"。原因：分区必须从 0x15000 开始才可写，
    # 但 0x15000 这里原本是厂商头，不是 LZMA 头。
    # mtdsplit_lzma 只检查 props[0]<225、dict 是 2 的幂、size_high==0。
    hdr[0:13] = lzma[0:5] + bytes(8)
    if not (hdr[0] < 9 * 5 * 5
            and struct.unpack("<I", hdr[1:5])[0]
            and not (struct.unpack("<I", hdr[1:5])[0]
                     & (struct.unpack("<I", hdr[1:5])[0] - 1))):
        die("假 LZMA 头生成失败。")

    # squashfs 必须落在"分区内偏移是 64KB 整数倍"的位置
    sq_off = align_up(LEN_HDR + len(lzma), BLOCK)
    pad = sq_off - LEN_HDR - len(lzma)
    body = lzma + b"\xff" * pad + squashfs

    # 更新厂商头里的两个长度字段
    hdr[0x5C:0x60] = struct.pack(">I", len(lzma))
    hdr[0x60:0x64] = struct.pack(">I", OFF_HDR + align_up(LEN_HDR + len(lzma), 0x200))

    say("  填充        : %d 字节 -> squashfs 落在分区内偏移 0x%X" % (pad, sq_off))
    return bytes(hdr) + body


# ------------------------------------------------- 主流程
def main():
    if len(sys.argv) < 3:
        say(__doc__)
        return 1

    dump_path, fw_path = sys.argv[1], sys.argv[2]
    out_path = sys.argv[3] if len(sys.argv) > 3 else "d121g_full_16m.bin"

    # ---- 1 ----
    say("=" * 64)
    say(" 第 1 步 / 共 5 步：读原厂 2MB 备份")
    say("=" * 64)
    if not os.path.isfile(dump_path):
        die("找不到文件：%s" % dump_path)
    stock = open(dump_path, "rb").read()
    say("  文件     : %s" % dump_path)
    say("  大小     : %d 字节 (%.2f MB)" % (len(stock), len(stock) / 1048576))
    if len(stock) != 2097152:
        die("原厂备份必须是 2MB（2097152 字节），这个文件是 %d 字节。\n"
            "    请确认你读的是拆下来的那颗原厂片（Eon EN25QH16）。" % len(stock))
    if stock[OFF_FACTORY:OFF_FACTORY + 2] == b"\x20\x76":
        say("  ✓ WiFi 校准头正确（20 76）")
    else:
        say("  ⚠ 警告：0xF800 处不是预期的 WiFi 校准头（读到 %s）"
            % stock[OFF_FACTORY:OFF_FACTORY + 2].hex(" "))
        say("    这很可能不是 D121G 的原厂片。继续下去会把别人的校准数据写进去。")
        if (input("    确实要继续吗？输入 yes 继续：").strip().lower() != "yes"):
            die("已取消。", 2)

    # 板型串校验 —— 这是区分"是不是 D121G"最可靠的依据。
    # 注意：别的 MT7620 机器（比如 TP-Link 的 AP）在 0xF800 也可能是 20 76，
    # 所以光看校准头不够，必须看板型串。
    if b"devModel:D121G" not in stock[:LEN_UBOOT]:
        die("这份备份里找不到 D121G 的板型串（devModel:D121G）。\n"
            "    说明它不是水星 D121G 的原厂固件 —— 可能是别的机器的片。\n"
            "    用别的机器的备份会把它自己的 MAC 和 WiFi 校准写进你的路由器。\n"
            "    请用你自己那台 D121G 拆下来的那颗原厂片重新读取。")
    say("  ✓ 板型串确认：devModel:D121G")

    # ---- 2 ----
    say("")
    say("=" * 64)
    say(" 第 2 步 / 共 5 步：给 U-Boot 打 1 字节补丁")
    say("=" * 64)
    ub = bytearray(stock[0:LEN_UBOOT])
    hits = find_uboot_limit(ub)
    if not hits:
        die("在这个 U-Boot 里找不到那个 2MB 上限常量。\n"
            "    说明你这台机器的 U-Boot 版本和本项目验证过的不一样。\n"
            "    请不要硬刷，到仓库提 issue 并附上你的原厂备份。")
    if len(hits) > 1:
        die("找到多处候选（%s），无法确定改哪个。请不要硬刷。"
            % ", ".join("0x%X" % h for h in hits))
    off = hits[0]
    ub[off:off + 4] = LUI_NEW
    diff = [i for i in range(LEN_UBOOT) if ub[i] != stock[i]]
    if diff != [off]:
        die("补丁影响范围异常（差了 %d 个字节）。已中止。" % len(diff))
    say("  文件偏移 : 0x%X   （原值 1e  ->  新值 fe）" % off)
    say("  效果     : flash 上限 2MB -> 15.92MB")
    say("  ✓ 自检通过：全片只改了这 1 个字节")

    # ---- 3 ----
    say("")
    say("=" * 64)
    say(" 第 3 步 / 共 5 步：生成固件段")
    say("=" * 64)
    if not os.path.isfile(fw_path):
        die("找不到文件：%s" % fw_path)
    fw = build_fw_section(stock, fw_path)
    if OFF_FW + len(fw) > FLASH_SIZE:
        die("固件太大，放不进 16MB。")

    # ---- 4 ----
    say("")
    say("=" * 64)
    say(" 第 4 步 / 共 5 步：拼成 16MB 全片")
    say("=" * 64)
    out = bytearray(b"\xff" * FLASH_SIZE)
    out[0:LEN_UBOOT] = ub
    out[OFF_FACTORY:OFF_FACTORY + LEN_FACTORY] = \
        stock[OFF_FACTORY:OFF_FACTORY + LEN_FACTORY]
    out[OFF_MINIFS:OFF_MINIFS + LEN_MINIFS] = \
        stock[OFF_MINIFS:OFF_MINIFS + LEN_MINIFS]
    out[OFF_FW:OFF_FW + len(fw)] = fw
    say("  0x%06X - 0x%06X   U-Boot（含 1 字节补丁）" % (0, LEN_UBOOT))
    say("  0x%06X - 0x%06X   factory  ★WiFi 校准，来自你的原厂备份"
        % (OFF_FACTORY, OFF_FACTORY + LEN_FACTORY))
    say("  0x%06X - 0x%06X   minifs" % (OFF_MINIFS, OFF_MINIFS + LEN_MINIFS))
    say("  0x%06X - 0x%06X   固件段（内核 + 根文件系统）"
        % (OFF_FW, OFF_FW + len(fw)))
    say("  其余              0xFF（空白）")

    # ---- 5 ----
    say("")
    say("=" * 64)
    say(" 第 5 步 / 共 5 步：自检")
    say("=" * 64)
    checks = [
        ("U-Boot 补丁（0x%X = fe）" % off, out[off] == 0xFE),
        ("WiFi 校准原样保留", out[OFF_FACTORY:OFF_FACTORY + 2] == b"\x20\x76"),
        ("假 LZMA 头（13 字节）",
         out[OFF_FW:OFF_FW + 13] == bytes.fromhex("6d00008000") + bytes(8)),
        ("真 LZMA 载荷（0x15200 = 6d）", out[OFF_PAYLOAD] == 0x6D),
        ("squashfs 魔数（0x295000 = hsqs）",
         out[0x295000:0x295004] == SQUASHFS_MAGIC),
    ]
    for name, good in checks:
        say("  %s %s" % ("✓" if good else "✗", name))
    if not all(g for _, g in checks):
        die("自检没过，产物不可信。请勿写入 flash。")

    with open(out_path, "wb") as f:
        f.write(out)
    digest = hashlib.sha256(out).hexdigest()
    with open(out_path + ".sha256", "w") as f:
        f.write("%s  %s\n" % (digest, os.path.basename(out_path)))

    say("")
    say("=" * 64)
    say(" 全部完成！")
    say("=" * 64)
    say("  镜像文件 : %s" % os.path.abspath(out_path))
    say("  大小     : %d 字节（正好 16MB）" % len(out))
    say("  SHA256   : %s" % digest)
    say("")
    say("  下一步：用编程器把这个文件写进新的 W25Q128，写完务必让编程器做一次校验。")
    say("")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
