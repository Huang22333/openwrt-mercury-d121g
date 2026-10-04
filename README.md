# OpenWrt for 水星 D121G (Mercury D121G)

MT7620DA + RTL8367S + MT7612EN 的第三方 OpenWrt 移植，**已在真机上从 flash 完整启动并验证可用**。

> 本仓库不是上游官方支持。设备未进入 OpenWrt 上游，属于社区移植（需要拆机换 16MB flash）。
> 仓库内容按 **GPL-2.0** 发布。

---

## 🚀 只想刷机？看这里

**不会编程也能完成。** 全程不需要串口线、不需要 TFTP、不需要碰 U-Boot 菜单。

👉 **[docs/刷机指南-零基础.md](docs/刷机指南-零基础.md)** ← 从零开始，照着做约 1.5 小时

大概流程：

1. 用编程器读原厂 2MB flash → 备份（**这步最关键，别跳**）
2. 在 GitHub 上点几下，让云端帮你编译固件（约 40~60 分钟）
3. 跑一个 Python 脚本，把你的原厂数据和新固件拼成 16MB 整片
4. 把整片写进新的 W25Q128，焊上去，开机

> 本仓库**不提供**任何人已经编译好的固件包 —— 因为固件里含**每台机器独有**的
> MAC 地址和 WiFi 校准数据，必须由你自己的原厂备份生成。仓库给的是工具和流程。

---

## 一、设备规格（实测）

| 项目 | 规格 |
|---|---|
| 型号 | 水星 D121G（MERCURY D121G） |
| SoC | **MT7620DA**（MIPS 24KEc 580MHz，**内置 64MB DDR2，板上无独立内存颗粒**） |
| 2.4G | MT7620 内置 2×2 802.11n |
| 5G | **MT7612EN** 2×2 802.11ac（PCIe） |
| 交换芯片 | **RTL8367S**，3× 千兆 |
| 原厂 Flash | 2MB（Eon EN25QH16，SPI）→ **需换 16MB（W25Q128）** |
| 原厂系统 | VxWorks（Realtek/Ralink SDK 私有容器） |
| 串口 | **57600 8N1** |

**3 个网口的实测映射**（丝印 ↔ RTL8367S 端口）：

| 物理口 | 交换芯片端口 |
|---|---|
| WAN | port 4 |
| LAN1 | port 3 |
| LAN2 | port 2 |
| （CPU 口） | port 7（tagged） |

即 Archer C5 v4 去掉 port 0/1 —— 同一套参考设计。

---

## 二、这个移植踩到的核心坑（重点）

原厂 U-Boot 引导镜像时，会**卡死在 `## Booting image at bc015200` / `addr:0xbc015200`**，
串口一个字都不再输出。**不是格式问题，也不是镜像大小问题**，而是一个 0x1000 的地址错位：

反汇编原厂 U-Boot 后可以看到它把引导参数写死在常量里（`0xBC009780` 那张常量表）：

| 偏移 | 值 | 含义 |
|---|---|---|
| `0x978C` | `0xBC015200` | 固件源（flash `0x15200`） |
| `0x9794` | **`0x80001000`** | **解压/执行地址** |

而 OpenWrt 的 mt7620 内核是链接在 **`0x80000000`** 的：

```
_text          = 0x80000000
__kernel_entry = 0x80000400    ← 文件偏移 0x400，内容是 `j 0x8060D78C`
kernel_entry   = 0x8060D78C
```

U-Boot 把内核放到 `0x80001000` 后，`0x80001400` 那条 `j 0x8060D78C` 就落到了
**往前 0x1000 处的垃圾指令**上 → 无声死机。

**解法：用 OpenWrt 官方的 `relocate-kernel` 桩**（mt7621 一直在用的那个），
bootloader 一个字节都不用改：

```make
KERNEL := kernel-bin | append-dtb | relocate-kernel 0x80000000 | lzma | uImage lzma
```

桩被解压到 `0x80001000` 后会：把自己搬到 `0x81000000` → 把内核搬到 `0x80000000` → 跳过去。
另外桩前还有 64KB 的 nop 滑槽，顺带给入口地址 ±64KB 的容错。

> 详细推导（含反汇编证据、常量池内容、被排除的其他假设）见 [`docs/root-cause.md`](docs/root-cause.md)。

---

## 三、仓库结构

```
configs/  d121g_defconfig                编译配置种子（fork 后改这个就行）
dts/      mt7620a_mercury_d121g.dts      设备树（分区 / 网口 / WiFi 校准绑定）
patches/  0001-mt7620.mk-*.patch         设备定义（含 relocate-kernel）
          0002-config-6.6-*.patch        开启 CONFIG_MTD_SPLIT_LZMA_FW
          0003-02_network-*.patch        网口角色映射
tools/    make_full_flash.py             ★一键生成 16MB 整片（零基础用这个，单文件）
          make_tftp_reloc.py             打包成原厂 U-Boot 能引导的镜像（进阶）
          verify_layout.py               在 PC 上模拟内核的 mtd 分区发现流程
docs/     刷机指南-零基础.md              ★完整刷机教程（含编程器、焊接、排错）
          root-cause.md                  根因分析与证据
          uboot-patch.md                 原厂 U-Boot 的 1 字节补丁（含推导方法）
          flash-layout.md                最终的 flash 布局
.github/  workflows/build.yml            GitHub Actions 在线编译
```

---

## 四、编译

### 方式 A：在线编译（推荐，不用装任何环境）

**不需要本地 Linux，也不需要会 OpenWrt 编译。** 流程：

1. 点右上角 **Fork**，把这个仓库复制到你自己的账号下
2. 按需改 `configs/d121g_defconfig`（加软件包、开中文界面等，见文件内注释）
   改完直接 commit 到自己的 `main` 分支
3. 进自己仓库的 **Actions** 页 → 左侧选 **Build OpenWrt for Mercury D121G**
   → 右侧 **Run workflow** → 绿色按钮
4. 等约 **2~4 小时**（GitHub 的 6 小时 job 上限内）
5. 编译完在该次运行的页面底部 **Artifacts** 里下载
   `openwrt-mercury_d121g`（一个 zip，含 `.bin`、`.manifest`、`sha256sums`）

> 改 `configs/**`、`patches/**`、`dts/**` 会自动触发编译，不用手动点。
>
> Artifact 保留 30 天，记得下载下来。产物是**原始编译结果**，
> 要刷进机器还需用 `tools/make_tftp_reloc.py` 转换（见第五节）。

### 方式 B：本地编译

基于 **OpenWrt v24.10.2 (r28739)**、Linux 6.6.93、`ramips/mt7620`。

```sh
git clone -b v24.10.2 https://github.com/openwrt/openwrt.git
cd openwrt
./scripts/feeds update -a && ./scripts/feeds install -a
```

打补丁 + 放设备树 + 应用配置：

```sh
for p in /path/to/repo/patches/*.patch; do patch -p1 < "$p"; done
cp /path/to/repo/dts/mt7620a_mercury_d121g.dts target/linux/ramips/dts/
cp /path/to/repo/configs/d121g_defconfig .config
make defconfig          # ← 必需：把默认值补全
```

然后编译：

```sh
make -j$(nproc)
```

产物在 `bin/targets/ramips/mt7620/`：

- `openwrt-ramips-mt7620-mercury_d121g-squashfs-sysupgrade.bin`
- `openwrt-ramips-mt7620-mercury_d121g-initramfs-kernel.bin`

> ⚠️ 这两个**不能直接喂给原厂 U-Boot**，要用 `tools/make_tftp_reloc.py` 转换（见下）。

---

## 五、刷机

**完整步骤（含编程器操作、焊接、排错）请看 [docs/刷机指南-零基础.md](docs/刷机指南-零基础.md)。**

最简形式：

```sh
# 1. 用「你从自己设备读出的原厂 2MB 备份」+「编译产物」拼出 16MB 整片
python3 tools/make_full_flash.py stock.bin \
        openwrt-ramips-mt7620-mercury_d121g-squashfs-sysupgrade.bin

# 2. 用编程器把生成的 d121g_full_16m.bin 写进新的 W25Q128，校验，焊上，开机
#    浏览器打开 http://192.168.1.1
```

> 为什么必须先有原厂备份：固件里含**每台机器独有**的 MAC 和 WiFi 校准数据。
> 工具会把你的校准原样保留、并顺手给 U-Boot 打上解除 2MB 限制的 1 字节补丁。

**以后的升级**不需要再拆机 —— 系统内用**编译出来的原始** `...sysupgrade.bin`
（不是 `d121g_full_16m.bin`）：

- LuCI: System → Backup / Flash Firmware → Flash image
- 或 `sysupgrade -n /tmp/...sysupgrade.bin`

---

## 六、最终 flash 布局

```
0x000000-0x00F800  u-boot      （含上面的 1 字节补丁）
0x00F800-0x010000  factory     ★WiFi 校准，绝不要动
0x010000-0x015000  minifs
0x015000-0x015200  ★13 字节"假 LZMA 头"（见下）
0x015200-0x0293C02 ★裸 LZMA：relocate 桩 + 内核 + DTB（U-Boot 固定读这里）
0x0295000-0x06766E5 squashfs（分区内偏移 0x280000，64KB 整数倍）
0x0675000-0xFF5000 rootfs_data（overlay，9.5MiB，jffs2）
```

两个不那么显然的点：

- **分区从 `0x15000` 而不是 `0x15200` 开始。** `mtdpart` 的对齐检查是
  `offset mod 64K`，不整除再 `mod 4K`；`0x15200 mod 4K = 0x200` 会导致**整个固件分区被强制只读**，
  overlay 直接废掉。`0x15000 mod 4K = 0` 才正常可写。代价是分区首 13 字节不是真 LZMA 头，
  所以人工补一个假的（`mtdsplit_lzma` 只查 `props[0]<225`、`dict` 是 2 的幂、`size_high==0`）。
  U-Boot 完全不读这 13 字节。
- **`CONFIG_MTD_SPLIT_LZMA_FW=y` 必须开。** mt7620 子目标默认只有 UIMAGE/JIMAGE 解析器，
  不开的话 `compatible = "lzma"` 的分区不会被拆分，内核找不到根文件系统会 panic。

详细说明见 [`docs/flash-layout.md`](docs/flash-layout.md)。

---

## 七、已知限制

- 上游 OpenWrt **没有** D121G 机型支持，本仓库是第三方移植
- 需要**拆机更换 16MB flash**（原厂 2MB 装不下）
- U-Boot 补丁偏移与 U-Boot 版本绑定（本仓库针对 `1.1.3 (Mar 22 2018 20:37:32)`）
- `factory` 里的校准数据**每台不同**，不要跨机器复制固件
- 仓库**不包含**任何水星/Realtek 的私有二进制（U-Boot、minifs 等）

## 八、参考

- 模板机型：Archer C5 v4（`mt7620a_tplink_archer-c5-v4`，同为 MT7620A + RTL8367S）
- `Build/relocate-kernel`：`target/linux/ramips/image/Makefile` + `target/linux/generic/image/relocate/`
- `mtdsplit_lzma`：`target/linux/generic/files/drivers/mtd/mtdsplit/mtdsplit_lzma.c`

## License

GPL-2.0-only（与 OpenWrt 一致）。设备树、补丁与工具脚本均为本项目原创贡献。

---

<sub>English: Third-party OpenWrt port for the Mercury D121G (MT7620DA + RTL8367S + MT7612EN).
Root cause of the stock U-Boot boot hang: it loads the kernel at `0x80001000` while the
OpenWrt mt7620 kernel is linked at `0x80000000`; solved by the upstream `relocate-kernel`
stub, no bootloader change required. Requires a 16MB flash swap. GPL-2.0.</sub>
