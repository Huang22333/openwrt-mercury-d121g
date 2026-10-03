# 原厂 U-Boot 的 1 字节补丁（解除 2MB 大小限制）

## 为什么要打

原厂 U-Boot 用 TFTP 刷固件时会检查写入大小，超过限制直接拒绝：

```
Abort: bootloader size 6438363 too big!
```

反汇编引导菜单的写入路径（option 2，"system code"）：

```
bc002338: 3c02001e   lui   v0,0x1e
bc002340: 3442b001   ori   v0,v0,0xb001     -> v0 = 0x1EB001
bc002344: 8e250000   lw    a1,0(s1)         ; a1 = 文件大小
bc002348: 00a2102b   sltu  v0,a1,v0         ; 大小 < 0x1EB001 ?
bc00234c: 14400007   bnez  v0,0xbc00236c    ; 是 -> 继续
bc002354: ...        (否则打印 "Abort: bootloader size %d too big!")
```

`0x1EB001 = 0x200000 - 0x15000 + 1` —— **U-Boot 硬编码认为 flash 只有 2MB**。

- 原厂 VxWorks 载荷 1.1MB（`0x112E02`）→ 恰好在限制内，所以原厂固件能刷
- OpenWrt 内核 2.6MB / initramfs 6.4MB → **必然被拒**

## 补丁

只改 1 个字节：

```
文件偏移 0x2338:  0x1e  ->  0xfe
```

即 `lui v0,0x1e` → `lui v0,0xfe`，限制变成
`0x00FE0000 | 0xB001 = 0xFEB001 = 16,690,177` 字节 = **15.92MB**，
恰好等于 `0x1000000 - 0x15000`（整片减去前面的保留区）。

## 刷入方式

用 U-Boot 菜单的 **option 9**（写 bootloader，目标 flash `0x0`，限制 `0x200001`）。
需要把文件做成 `[补丁后的 U-Boot (0xF800)][factory (0x800)][minifs (0x5000)]` 共 `0x15000` 字节，
这样一次写入既更新了 U-Boot，又原样保留了自己的 factory。

> ⚠️ 也可以用编程器直接改片上的 `0x2338`。两种方式都行。

## 怎么自己推导（换 U-Boot 版本时必须重来）

本补丁的偏移 **与原厂 U-Boot 版本绑定**（本项目针对 `U-Boot 1.1.3 (Mar 22 2018 - 20:37:32)`）。
不同版本的偏移必须重新推导：

```sh
# 1) 读出原厂 U-Boot 分区（0x0 - 0xF800）
DD=/path/to/flashrom
$DD -p ch341a_spi -r stock.bin          # 整片
head -c 0xF800 stock.bin > uboot_raw.bin

# 2) 反汇编
OD=staging_dir/toolchain-mipsel_24kc_gcc-13.3.0_musl/bin/mipsel-openwrt-linux-objdump
$OD -D -b binary -m mips:isa32 -EL --adjust-vma=0xBC000000 uboot_raw.bin > uboot.asm

# 3) 找那个常量：搜 lui 0x1e 后面跟 ori 0xb001
grep -n -A2 "lui.*,0x1e" uboot.asm
```

找到的 `lui` 指令地址减去 `0xBC000000` 就是文件偏移。

**验证方法**：改完先别写片，用 `diff` 确认只差 1 个字节，
并且 `0x2338` 之外的字节（尤其 `0xF800` 起的 factory）完全未变。

## 参考

- 引导路径读固件的位置是 U-Boot 常量表里的 `0xBC015200`，见 [`root-cause.md`](root-cause.md)
- U-Boot 与常量表均**不包含**本仓库，请从你自己的设备 dump 中获取
