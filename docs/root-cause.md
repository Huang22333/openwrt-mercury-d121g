# 根因分析：原厂 U-Boot 引导 OpenWrt 卡在 `addr:0xbc015200`

## 现象

原厂 U-Boot 引导任何 OpenWrt 镜像都停在这里，之后串口**一个字都没有**：

```
## Booting image at bc015200 ...
addr:0xbc015200
```

换了各种格式（uImage @0x15000、uImage @0x15200、裸 LZMA @0x15200）结果完全相同。
原厂固件刷回去则一切正常。

## 排查过程中被排除的假设

| 假设 | 证据 | 结论 |
|---|---|---|
| 镜像格式不对 | 原厂 `0x15200` 是裸 LZMA（`6e 00 00 80`），uImage 各变体同样失败 | ❌ 排除 |
| 镜像太大 | 换 2.6MB 的 sysupgrade 内核仍失败；大小限制是**写入时**的检查 | ❌ 排除 |
| LZMA `props` 字节不兼容（原厂 `0x6e`=lc2，OpenWrt `0x6d`=lc1） | 原厂解压后尺寸 `0x3DD090` **只**存在于 LZMA-alone 头里（厂商头里没有），说明引导解码器必然从 LZMA-alone 头读尺寸，也就必然读 `props` | ❌ 排除 |
| 厂商容器头的长度/MD5 字段被校验 | 全片搜 U-Boot：**没有**任何 MD5 常量、MD5/SHA 字符串、CRC32 表（只有 uImage 路径的 `Bad Header Checksum` 等字符串）| ❌ 排除 |
| U-Boot 读厂商头 `+0x18` 的 load 地址 | 反汇编确认引导路径里**没有** `lui 0xbc01` / `lui 0x80c0`（唯二两处 `lui a2,0xbc01; addiu a2,a2,0x5200` 在 option 2 的**写入**路径）| ❌ 排除 |

## 真正的根因

U-Boot 的引导参数**硬编码在只读常量里**（文件 `0x9780`，虚拟地址 `0xBC009780`，小端 u32）：

```
0x9780 0x8a200000
0x9784 0x80200000
0x9788 0x8a300000
0x9780 0x88001000
0x9784 0x8b800000
0x9788 0xbbffffff
0x978C 0xbc015200   <- 固件源 = flash 0x15200
0x9790 0xbc015000
0x9794 0x80001000   <- ★ 解压 / 执行地址
0x9798 0x000186a0
0x979C 0x44000000
0x97A0 0x27051956   <- uImage magic（bootm 路径用）
0x97A4 0x80C00000   <- 解压缓冲区上界
0x97A8 0x44000040
```

关键点：**全 63KB U-Boot 二进制里，`0xBC015200` 只出现在 `0x978C` 这一处**。
引导路径既然能读到 flash `0x15200`（实测原厂固件就是这么起来的），
就必然是从这张常量表里取的值 —— 那么紧邻的 `0x9794 = 0x80001000` 就是加载/执行地址。

而 OpenWrt 的 mt7620 内核链接在 `0x80000000`：

```
$ readelf -h vmlinux      # Entry point address: 0x8060d78c
$ nm vmlinux | grep -E '__kernel_entry|kernel_entry|_text'
80000000 T _text
80000400 T __kernel_entry
80000400 T _stext
8060d78c T kernel_entry
```

vmlinux.bin 的布局也印证了这一点：偏移 `0x000-0x3FF` 全 0（MIPS 异常向量区，运行期
由 `trap_init()` 填），偏移 `0x400` 处是 `e3 35 18 08` = `j 0x8060D78C`。

**所以：内核被放到 `0x80001000`，那条 `j 0x8060D78C` 就落在了往前 0x1000 的垃圾指令上
→ 无声死机。** 一个 0x1000 的偏移。

## 解法

用 OpenWrt 官方就有的 `Build/relocate-kernel`（mt7621 机型一直在用，正是为了
"bootloader 放到 0x80001000、内核链在 0x80000000" 这种情况）：

```make
KERNEL := kernel-bin | append-dtb | relocate-kernel 0x80000000 | lzma | uImage lzma
```

它会在内核前面塞一个位置无关的桩（`target/linux/generic/image/relocate/head.S`）：

1. 用 `bal` 取实际运行地址，算出重定位偏移
2. 把「桩 + 长度字段 + 内核」整体复制到链接地址 `0x81000000`
3. 把内核复制到 `KERNEL_ADDR`（= `0x80000000`）
4. 刷 cache，`jr 0x80000000`

解压体布局实测：

```
[0x00000-0x10000] 64KB nop 滑槽（全 0，顺带给入口 ±64KB 容错）
[0x10001-0x10160] 351B 桩代码（首指令 0x40809000 = mtc0 zero,CP0_WATCHLO）
[0x10160]         u32 内核长度
[0x10164-...]     内核 + DTB；+0x400 处 = `j kernel_entry`
```

**bootloader 一个字节都不用改。**

## 自愈矩阵

这个桩（加上前面的 nop 滑槽）对下面 3 种组合都能自愈：

| U-Boot 解压到 | U-Boot 跳到 | 结果 |
|---|---|---|
| 0x80001000 | 0x80001000 | ✓ 桩在开头 |
| 0x80000000 | 0x80000000 | ✓ 桩在开头 |
| 0x80000000 | 0x80001000 | ✓ 落进 nop 滑槽，滑到桩 |
| 0x80001000 | 0x80000000 | ✗ 唯一坏情况 |

实测本机是第 1 种，一次通过。

## 复现方法

```sh
# 反汇编原厂 U-Boot（用 OpenWrt 工具链）
OD=staging_dir/toolchain-mipsel_24kc_gcc-13.3.0_musl/bin/mipsel-openwrt-linux-objdump
$OD -D -b binary -m mips:isa32 -EL --adjust-vma=0xBC000000 uboot_raw.bin > uboot.asm

# 找常量：全片搜 0xBC015200，应只有一处
python3 -c "import struct;d=open('uboot_raw.bin','rb').read();
print([hex(i) for i in range(len(d)-3) if d[i:i+4]==struct.pack('<I',0xBC015200)])"
# -> ['0x978c']

# 确认真实内核加载地址
readelf -h vmlinux | grep Entry
```

> 注意：U-Boot 里的字符串是 gp 相对寻址（`gp = 0xBC010000`），
> 用立即数直接搜字符串地址是搜不到的；要搜负偏移（如 `-0x4474`）。
