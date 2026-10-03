# 最终 flash 布局

```
偏移            大小        内容
0x000000        0x00F800    u-boot（含 2MB→15.92MB 的 1 字节补丁）
0x00F800        0x000800    factory  ★WiFi 校准（2.4G@+0x0, 5G@+0x400），每台唯一
0x010000        0x005000    minifs（原厂 OUI 库，OpenWrt 不用）
0x015000        0x000200    ★13 字节"假 LZMA 头" + 原厂商头其余部分
0x015200        0x27EA02    ★裸 LZMA：relocate 桩 + 内核 + DTB
0x0295000       0x3D2360    squashfs（只读根）
0x0675000       0x980000    rootfs_data（overlay，jffs2，≈9.5 MiB）
0x1000000       —           片尾
```

DTS 里对应的分区：

```dts
partition@0     { label = "u-boot";  reg = <0x0 0xf800>;     read-only; };
factory: partition@f800 { label = "factory"; reg = <0xf800 0x800>; read-only; /* nvmem-layout */ };
partition@10000 { label = "minifs";  reg = <0x10000 0x5000>; read-only; };
partition@15000 { label = "firmware"; reg = <0x15000 0xfeb000>;
                  compatible = "lzma"; };
```

内核跑起来后 `mtdsplit_lzma` 会自动把它拆成：

```
firmware      0xfeb000
├── kernel    0x280000
└── rootfs    0xd6b000
    └── rootfs_data 0x980000   (由 mtdsplit_squashfs 再拆)
```

---

## 两个不那么显然的点

### 1. 分区为什么从 `0x15000` 开始，而不是 U-Boot 实际读的 `0x15200`

`mtdpart.c` 的对齐检查是：

```c
wr_alignment = child->erasesize;               /* = 0x10000 (64K) */
tmp = mtd_get_master_ofs(child, 0);            /* 绝对偏移 */
remainder = do_div(tmp, wr_alignment);
if (remainder && wr_alignment_minor)           /* = 0x1000 (4K) */
	remainder = do_div(remainder, wr_alignment_minor);
if (remainder)
	child->flags &= ~MTD_WRITEABLE;             /* 强制只读 */
```

实测本机（`CONFIG_MTD_SPI_NOR_USE_VARIABLE_ERASE=y`、`USE_4K_SECTORS` 未开）
erasesize = 64K、minor = 4K。于是：

- `0x15200 mod 4K = 0x200` → **整个 firmware 分区被强制只读**，连带 `rootfs_data` 也只读，overlay 直接废掉
- `0x15000 mod 4K = 0` → 正常可写

代价：分区的首 13 字节不再是厂商 magic，得人工补一个**假的 LZMA 头**：

```
6d 00 00 80 00  00 00 00 00  00 00 00 00
└─props─┘└─dict─┘└size_low─┘└size_high┘
```

`mtdsplit_lzma` 只检查三件事（`props[0] < 225`、`dict` 是 2 的幂、`size_high == 0`），
**`size_low` 根本不看**，所以这个假头是常量。U-Boot 完全不读这 13 字节
（厂商容器 magic `55AA9DD1`/`A8C88331`/`70C7AA55` 在 U-Boot 二进制里**一个都搜不到**）。

假头只覆盖前 13 字节，原厂商头的其余部分（含 `+0x18 = 0x80001000`）原样保留。

### 2. squashfs 为什么要落在"分区内偏移 0x280000"（64K 的整数倍）

`mtdsplit_lzma` 拆 kernel/rootfs 用的是：

```c
mtd_find_rootfs_from(master, master->erasesize, master->size, &rootfs_offset, NULL);
```

它**只按 `master->erasesize`（= 64K）步进**去扫 squashfs 魔数，不会逐字节找。
所以打包时必须把裸 LZMA 补齐到 64K 边界再放 squashfs：

```
分区内偏移 = align_up(0x200 + len(LZMA), 0x10000)
```

本机 = `0x280000`。

### 3. `CONFIG_MTD_SPLIT_LZMA_FW=y` 必须开

`mt7620` 子目标的 `config-6.6` 默认只开了 `UIMAGE`/`JIMAGE` 解析器
（只有 `rt288x` 子目标开了 LZMA）。不开的话 `compatible = "lzma"` 的分区**不会被解析**，
kernel/rootfs 都不会出现，内核找不到根文件系统：

```
MTD: Couldn't look up '': -22
VFS: Cannot open root device "" ... error -6
Kernel panic - not syncing: VFS: Unable to mount root fs
```

---

## 厂商容器头字段（本机实测公式）

刷机镜像 = `[0x200 头][裸 LZMA][补齐][squashfs]`，其中头的两个字段要更新：

| 字段 | 值 |
|---|---|
| `+0x5C` | 裸 LZMA 长度（大端） |
| `+0x60` | `0x15000 + align_up(0x200 + len, 0x200)`（大端） |

用原厂固件反向验证：
`len = 0x112E02` → `0x15000 + align_up(0x113002, 0x200) = 0x128200` ✓ 与实测一致。

那个头上的 4 个 MD5 字段（`+0x90/+0xA0/+0xB0/+0xD0`，其中 `+0x90` 恰是 `MD5("")`）
**不被校验** —— U-Boot 里没有任何 MD5 实现。本项目保持原值不动。

---

## 自己验证布局

`tools/verify_layout.py` 在 PC 上把 16MB flash 还原出来，**逐条复刻内核逻辑**再断言：

```sh
python3 tools/verify_layout.py tftp_ow_sysup.bin
python3 tools/verify_layout.py tftp_ow_initramfs.bin --initramfs
```

它检查：

1. `mtdpart` 的对齐检查（mod 64K 再 mod 4K）
2. `mtdsplit_parse_lzma` 的三个头部条件
3. `mtd_find_rootfs_from` 按 64K 步进扫魔数
4. U-Boot 固定读的 `0x15200` 必须落在 kernel 子分区内
5. `mtdsplit_parse_squashfs` 的 `bytes_used` → `rootfs_data` 偏移/大小
6. `REQUIRE_IMAGE_METADATA=1` 需要的元数据

在真机上对应的输出（`/proc/mtd`）：

```
mtd3: 00feb000 00010000 "firmware"
mtd4: 00280000 00010000 "kernel"
mtd5: 00d6b000 00010000 "rootfs"
mtd6: 00980000 00010000 "rootfs_data"
```

> 注意 `mtdpart_get_offset()` 返回的是**相对父分区**的偏移，不是绝对偏移。
> 内核在日志里报 `rootfs_data` 是 `0x660000-0xFE0000`（相对 firmware），
> 绝对值是 `0x675000-0xFF5000`。
