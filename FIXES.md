# 修复说明：Recovery Build 工作流

修的是 `Recovery Build.yml`（run [43826104628](https://github.com/P-cell-sourc/Action-TWRP-Builder/actions/runs/43826104628) 里 job `133160218033` 失败的那条）。`Recovery Build (Legacy).yml` 未改动。

## 一、失败点定位

job 元数据里能看到的步骤结果：

| # | 步骤 | 结果 | 耗时 |
|---|---|---|---|
| 4 | Cleanup (slimhub) | success | 3m55s |
| **5** | **Prepare the environment** | **failure** | **6 秒** |
| 6 及以后 | Install OpenJDK / repo / sync / build | skipped | — |

6 秒就退，说明根本没进编译阶段，是 `apt` 一进去就非零退出（`sudo apt update && sudo apt upgrade -y` 是 `&&` 串联，前面失败后面全不执行）。注：Actions 的原始日志需要仓库 admin 权限（API 返回 `Must have admin rights to Repository.`），所以下面是按包索引逐个核对得出的结论，不是从日志里抄的。

我拉取了 `ubuntu-24.04`（noble）的 `Packages` 索引逐条比对原脚本的包名：

- `gcc-10-multilib`、`g++-10-multilib`、`libstdc++-10-dev`、`lib32ncurses-dev`、`lib32readline-dev`、`libsdl1.2-dev`、`liblz4-tool`、`freeglut3-dev` —— **全部存在**，不是 "Unable to locate package" 的元凶；
- `linux-modules-extra-$(uname -r)` —— **这是元凶**。它展开成 `linux-modules-extra-6.x.y-<n>-azure`，而 noble-updates 里 azure 内核的 `-extra` 包只保留部分版本（版本号是断层的），runner 当前内核一旦不在保留列表里，`apt` 立刻 `E: Unable to locate package` 并以 100 退出，整步 6 秒结束。这条是从 AOSP 通用文档抄来的，**编 TWRP 完全用不到**（它是给在通用主机上编内核模块用的）。

另外 `apt update && apt upgrade -y` 这种写法本身也是隐患：runner 镜像里的第三方源（azure-cli、microsoft 等）签名失效或 403 时 `apt update` 就会返回 100，同样把这个步骤打死；`upgrade -y` 还会顺带换内核、删掉 runner 自带工具，并且多花 10~20 分钟。

## 二、改了什么

### 1. `Prepare the environment`（主修复）

| 改动 | 原因 |
|---|---|
| 删除 `linux-modules-extra-$(uname -r)` | noble 上无法解析，直接导致整步失败 |
| 删除 `apt upgrade -y` | 慢、可能换内核/删工具，且非必需 |
| `apt update` 失败只告警不中断 | 单个第三方源坏掉不该拖死整个构建 |
| 批量安装失败 → 自动退化为逐包安装并列出失败包 | 一个包名过时不会让整个步骤 100 退出 |
| 安装后校验 `git/bc/gperf/make/ccache/build-essential` | 关键工具真缺了要立刻报错，而不是等到编译 30 分钟后 |
| 补 `git-lfs`、`device-tree-compiler`、`bison/flex/zlib1g-dev/libssl-dev` 等 | 现代设备树/内核构建实际会用到 |

### 2. 其他必然踩坑的地方（顺手修掉）

| 位置 | 原问题 | 后果 |
|---|---|---|
| Upload to Release | 上传 `vendorboot.img` | `make vendorbootimage` 的产物是 **`vendor_boot.img`**（已核对 AOSP `core/main.mk:1547` 与 `core/Makefile:1430`），文件匹配不上，发布步骤必挂 |
| Upload to Release | 目录写死 `out/target/product/$DEVICE_NAME` | 默认 `DEVICE_NAME=I003D` 但设备是 `PD2551`，实际产物在 `out/target/product/PD2551`，同样找不到文件 |
| Building recovery | 只试 `<MAKEFILE_NAME>-eng` | twrp-14/15 源的 lunch 是 `xxx-bp2a-eng` 三段式，会 `Invalid lunch combo` 直接失败 |
| Clone common tree | 条件写 `!= null` | workflow_dispatch 未填的输入是**空字符串**而不是 null，条件恒真 → 用空的 URL 去 `git clone` |
| Clone common tree | 分支复用 `DEVICE_TREE_BRANCH` | common 树分支常与设备树不同；新增 `COMMON_TREE_BRANCH`（留空则回退） |
| Repo Sync / Initialize | 无重试 | 大仓库同步偶发失败即整轮作废；现为重试 3 次 |
| Install repo | `mkdir ~/bin` 无 `-p` | 目录已存在时报错 |
| `scripts/convert.sh` | `grep remote \| cut -d'"' -f4` | 取到的是 `<remote fetch="...">` 的 **fetch 地址**，生成 `remote="https://github.com/"` 这种非法属性，`repo sync` 会报错 |
| `scripts/convert.sh` | 固定 `for i in {0..5}` | 超过 6 个依赖项目被**静默丢弃**，编到一半才缺模块 |
| `scripts/convert.sh` | sed 拼字符串 | 分支名/路径含特殊字符时产生非法 XML |
| Install OpenJDK | `JAVA_VERSION` 写死 11 | Android 14 源需要 JDK 17，已提为输入项（默认 17，twrp-11/12.1 请填 11） |

`convert.sh` 已改为用 `python3` 解析 XML：支持 `<manifest>` 根节点或裸 `<project>` 行、`branch=` 当作 `revision=`、`remote` 若是 URL 会自动派生出独立 remote、与已有 `roomservice.xml` 做**幂等合并**（重复执行不会重复追加）。已用 6 组用例验证（8 个 project 不再被截断、重复执行新增 0 条、坏文件/空文件干净报错退出 1）。

## 三、后续补丁（实跑后追加）

`f9ff604` 跑起来后，环境步骤已通过，编译步骤报：

```
build/envsetup.sh: line 21: TOP: unbound variable
```

原因是我在该步骤用了 `set -uo pipefail`，而 AOSP 的 `build/envsetup.sh` 会引用未定义变量（`$TOP` 等），nounset 下 `source` 直接退出。已改为 `set -o pipefail` + `set +u`（保留 `-e`，`lunch`/`make` 失败仍会中止），提交 `233dfaa`。

接着 `lunch` 探测失败，报：

```
device/vivo/PD2551/twrp_PD2551.mk:16: error: build/make/target/product/embedded.mk does not exist.
```

设备树是旧版 twrpdtgen 模板生成的，继承了 `$(SRC_TARGET_DIR)/product/embedded.mk`。该文件 **Android 9 起已从 AOSP 删除**（7.1/8.0/8.1 有，9/10/12.1/13/14/15 全部 404；twrp-14.1 使用的 `TeamWin/android_build@android-14.1` 也没有）。提交 `33d5daa`：

- 自动把 `embedded.mk` 改写为语义等价、且仍然存在的 `base.mk`（兼容 `build/make/target/product/embedded.mk` 老写法）；
- 新增继承预检：扫出设备树引用的所有 `$(SRC_TARGET_DIR)/product/*.mk`，缺失的逐个列名告警。

再跑一次，环境与设备树解析都通过，卡在 BoardConfig 阶段：

```
build/make/core/board_config.mk:758: error:
TARGET_COPY_OUT_SYSTEM_EXT must be set to 'system_ext' to use a system_ext image.
```

`check_image_config`（`board_config.mk:394-404`）的规则是：只要声明了 `BOARD_<P>IMAGE_FILE_SYSTEM_TYPE`（或 `BOARD_PREBUILT_<P>IMAGE`），就必须把该分区作为独立分区输出；否则它会被当成 system 下的子目录，配置自相矛盾，直接 `$(error)`。设备树里 `BOARD_SYSTEM_EXTIMAGE_FILE_SYSTEM_TYPE := erofs`、`BOARD_MAIN_LIST` 也含 `system_ext`，但 `TARGET_COPY_OUT_*` 只补了 vendor/product/odm/vendor_dlkm，漏了 system_ext。提交 `28ebe48`：

- 新增 `scripts/fix_copy_out.py`：对 system_ext / product / odm / vendor_dlkm / odm_dlkm / system_dlkm 逐个检查，声明了镜像类型却没有 `TARGET_COPY_OUT_<P>` 的，就在设备树 `BoardConfig.mk` 末尾追加 `TARGET_COPY_OUT_<P> := <P>`（幂等，只碰 `BoardConfig*.mk`）；
- lunch 失败的诊断输出改为打印最后一次真实尝试的完整报错、release 配置目录、以及 BoardConfig 里的 `TARGET_COPY_OUT_*` / `IMAGE_*` 行，不再硬编码重试 `ap2a`。

再跑一次，这次没有脚本层面报错，任务以 `The hosted runner lost communication with the server` 结束。这条不是脚本 bug，而是 **runner agent 停止心跳**（被 OOM 杀掉），元凶是编译阶段的并行度与 swap 配置。提交 `73d4760`：

- `make -j$(nproc --all)` 改为按内存算：默认 `MemTotal/4`、上限 `nproc`、下限 2（16GB/4 核 → `-j4`），可用新输入项 `BUILD_JOBS` 手动覆盖；
- 去掉 `pierotofy/set-swap-space@master`（未固定版本），改成内联步骤：swap 大小 = `min(8GB, 可用磁盘 - 16GB)`，`fallocate` 失败退化为 `dd`，`vm.swappiness=10`，失败不影响任务。原来的固定 12GB swap 在 14GB 根分区上本身就会把盘写满；
- 编译前打印 CPU / 内存 / 可用磁盘，低于 15GB 时 `::warning::`。

紧接着一次运行以 `Error: Process completed with exit code 143` 结束。`143 = 128 + 15`，即 **SIGTERM**：进程被外部信号终止（任务取消 / 超时 / runner 被回收），不是脚本自己 exit 出来的错误，而原来的写法会把这个信号吞在管道里、日志断在半截。提交 `dc9c6dc`：

- 编译命令改为 `make ... 2>&1 | tee build.log`，并用 `PIPESTATUS` 原样透传 make 的真实退出码；
- 增加 `trap TERM INT`：收到信号时打印内存、磁盘、内存占用 top10、内核 OOM 记录；
- 新增 `Diagnose unexpected termination`（`failure() || cancelled()`）：补采 `dmesg` 的 OOM 记录、`ps` top10、`build.log` 最后 60 行，并把 `build.log` 作为 artifact 上传，方便事后看现场。

复跑时日志停在 `[ 99% 1161/1162] cp out/host/linux-x86/bin/soong_build`，随后仍是 143。这一行是 **soong 自举完成**的时刻（1161/1162），紧接着就要执行 `soong_build` 解析整棵 `Android.bp`——这是整个构建的第一个内存峰值，而这段过程 soong 一行输出都没有，日志"停在这里"既可能是被 OOM 也可能是被外部信号杀掉，从日志无法区分。提交 `73f26a0`：

- 心跳子进程：每 60s 打一行「可用内存 / 剩余磁盘」，进程被外部杀掉时日志虽断，但内存趋势可见；
- `GOMEMLIMIT` 默认设为物理内存的 70%：soong_build 是 Go 程序，堆会涨到存活对象的数倍，给它软上限让它多做 GC，而不是一路涨到被内核或 OOM 守护进程杀掉；
- `stop_hb()` 连子进程的 `sleep` 一起清理，避免步骤结束时挂在孤儿写者上。

第二次 143 仍然停在 `cp out/host/linux-x86/bin/soong_build`。这个位置是已知的**内存杀手**：soong 自举完成后，`soong_build` 要解析整棵 `Android.bp`（TWRP 树约需 4-6GB 堆），而这一段**一行日志都不输出**，所以任何终止看起来都停在自举那行。社区经验是编译 AOSP 时 **RAM + swap ≥ 32GB**，而 runner 只有 16GB + 8GB。

更关键的是：上一版加的 `GOMEMLIMIT` **完全没有生效**。`build/soong/ui/build/soong.go` 里 `invocationEnv := make(map[string]string)` 默认是空的，而 bootstrap 用 **`env -i`** 启动 `soong_build`——父进程环境被整个清空，shell 里 export 的变量传不进去。提交 `a0b9f78`：

- 新增 `scripts/patch_soong_memlimit.py`：在 `primaryBuilderInvocation()` 里插入一段，把 `GOMEMLIMIT` / `GOGC` 写进 `invocationEnv`（已用 `gofmt` 验证补丁后语法与格式均正确）；
- 编译步骤同时导出 `GOGC=80`（更早、更频繁 GC，用时间换内存峰值）；
- swap 从 8GB 改回 12GB（合计 28GB），`vm.swappiness` 从 10 改回 60——之前设成 10 是错的，内存吃紧时内核宁愿 OOM 也不换出冷页。

这次真正进了 `ninja`，跑了 3 分 56 秒后报：

```
FAILED: ninja: '.../android.security.apc-ndk_platform.so.toc', needed by
'.../libtar.so', missing and no known rule to make it
```

原因在 `bootable/recovery/libtar/Android.mk`：`TW_INCLUDE_CRYPTO_FBE` 分支里写的是 `android.security.apc-ndk_platform`、`android.system.keystore2-V1-ndk_platform`、`android.security.authorization-ndk_platform`、`android.security.maintenance-ndk_platform`。**AOSP 已经删掉了 AIDL 的 `ndk_platform` 后端**（`build/make/target/product/gsi/Android.mk` 原文：`*-ndk_platform.so libraries no longer exist`），现存的名字是 `-ndk`。名字对不上，kati 就会生成一条指向不存在产物的依赖，ninja 直接报 `no known rule`。根 `Android.mk` 的 FBE 块里也有同样的 3 处。提交 `614a32c`：

- 新增 `scripts/fix_ndk_platform.py`：在 `bootable/recovery`、`vendor/twrp`、`device/` 下的 `.mk`/`.bp` 中把 `<module>-ndk_platform` 改写为 `<module>-ndk`（幂等，只碰这几棵我们自己的树），改完再到 `system/security` 里确认对应 AIDL 接口确实存在，缺失就告警（提示清单没同步仓库）；
- 编译前新增一个步骤执行它，修补失败只告警、不中断；
- `Diagnose unexpected termination` 增加 `FAILED:` / `missing and no known rule` 摘要，并直接列出缺失的模块名。

再跑一次（14 分 47 秒）后失败，日志只留下 `ninja: build stopped: subcommand failed`——真正的 `FAILED:` 块在几千行之前，`tail` 和步骤日志都把它切掉了。提交 `144bf3e`（纯诊断增强，不改构建行为）：

- 编译步骤失败时自动摘出前 3 个 `FAILED` 块（遇到下一条进度行或 23 行即截止）+ 前 10 条 `error:` 行，并镜像写入 `$GITHUB_STEP_SUMMARY`，运行页面直接可见；
- `Diagnose unexpected termination` 用同一套摘取逻辑，额外写入 `build-errors.txt`；
- `build-errors.txt` 与 `build.log` 一起作为 artifact 上传。

真正的报错随之浮出来了（14 分 47 秒）：

```
FAILED: .../unsupportedappusage-annotation-processor-lib.jar
module unsupportedappusage-annotation-processor-lib missing dependencies: guava
```

这条占位规则只在 `ALLOW_MISSING_DEPENDENCIES=true`（我们设了）时出现：模块名在 `static_libs` 里但源码中没有定义，soong 会生成一条构建期必然失败的规则，而不是在解析期直接报错。`guava` 出自 `external/guava`——清单里有这个仓库，但 TWRP 的 `remove-minimal.xml` 会把它 remove 掉，所以精简/不完整 sync 之后该模块就不存在，所有 java 目标都会挂在这里。提交 `473ffc2`：

- 新增 `scripts/ensure_guava.py`：检查 `external/guava/Android.bp` 是否真的定义了 `name: "guava"`；缺失时打印诊断（目录状态、哪个清单 remove 了它），按主清单里的 remote/revision 生成 `.repo/local_manifests/zz-ensure-guava.xml` 并 `repo sync`，最后用 `git clone` 兜底；全程只告警不中断；
- 工作流在 `Repo Sync` 之后插入该步骤；
- 失败摘要与 `Diagnose` 步骤新增 `missing dependencies:` 行摘取——这类错误不带 `error:` 前缀，之前会被漏掉。

### 11. `libtar` 的 `fscrypt_policy` 编译失败（TWRP 14.1 上游自身不同步）

guava 修好之后，构建推进到 77%（35 分钟）才失败，这次是纯编译错误，和内存、磁盘都无关：

```
FAILED: out/target/product/PD2551/obj/SHARED_LIBRARIES/libtar_intermediates/block.o
bootable/recovery/libtar/libtar.h:74:2: error: must use 'struct' tag to refer to type 'fscrypt_policy_v1'
   74 |         fscrypt_policy  *fep;
bionic/libc/kernel/uapi/linux/fscrypt.h:116:24: note: expanded from macro 'fscrypt_policy'
  116 | #define fscrypt_policy fscrypt_policy_v1
bootable/recovery/libtar/block.c:365:13: error: call to undeclared function 'get_policy'
bootable/recovery/libtar/block.c:365:47: error: call to undeclared function 'fscrypt_policy_size'
bootable/recovery/libtar/block.c:585:16: error: call to undeclared function 'get_policy_descriptor'
bootable/recovery/libtar/append.c:152:43: error: incompatible pointer types passing
    'struct fscrypt_policy_v1 *' to parameter of type 'struct fscrypt_policy_v2 *'
```

**根因是 TWRP 自己的两个仓库版本对不上**（不是设备树、不是我们的脚本）：

| 仓库 | 分支 | 状态 |
|---|---|---|
| `TeamWin/android_system_vold` | `android-14.1` | 已把 `fscrypt_policy.h` 改成 `struct fscrypt_policy_v1/v2` 两套接口，`union fscrypt_policy` 和 `get_policy` / `get_policy_size` / `get_policy_descriptor` / `fscrypt_policy_size` / `get_policy_content` 全部删除（`fscrypt_policy.cpp` 里的实现也删了） |
| `TeamWin/android_bootable_recovery` | `android-14.1` | `libtar/{append,block}.c`、`libtar.h` 仍在调用上面那套旧接口，并且仍 `#include "fscrypt_policy.h"` |

叠加 bionic 的兼容宏 `#define fscrypt_policy fscrypt_policy_v1`，`libtar.h` 里的 `fscrypt_policy *fep;` 在 C 下直接变成 `fscrypt_policy_v1 *fep;`（缺 `struct` 关键字），一个类型名都活不下来。

`ALLOW_MISSING_DEPENDENCIES=true` 在这里帮不上忙：这不是模块缺失，是源码语法错误。

修法（提交 `fix-libtar-fscrypt`）：新增 `scripts/fix_libtar_fscrypt.py`，在编译前于 `bootable/recovery/libtar/` 生成 `fscrypt_compat.h`：

- `#undef fscrypt_policy` 解除 uapi 宏，重新 `typedef union fscrypt_policy { version; v1; v2; }`；
- 用 `static inline` 补回 5 个纯计算的 helper（实现取自 TWRP 12.1 的 `fscrypt_policy.cpp`，逻辑就是按 `version` 取字段，不涉及 ioctl）；
- `fscrypt_policy_get_struct` / `_set_struct` 用同名宏外加一次 cast，把 `union*` 转成 vold 期望的 `struct fscrypt_policy_v2 *`（`USE_FSCRYPT_POLICY_V1` 时自动切 v1）——日志里那条 `-Werror,-Wincompatible-pointer-types` 就是这么消掉的；
- `lookup_ref_key` / `lookup_ref_tar` 在 14.1 里被拆成了两步（先由 policy 反查 policy_type 串，再由串取 raw key），而 libtar 还在用老的一步式 `lookup_ref_tar(fep, policy)`；兼容层把这套接起来：`libtar_lookup_ref_tar` 先调 `lookup_ref_key` 拿到 `"2CE0"` 这类串，再调 vold 的 `lookup_ref_tar`，恢复加密目录策略的能力保持完整，不降级；
- 源码侧只把 `#include "fscrypt_policy.h"` 换成 `#include "fscrypt_compat.h"`，不动任何业务代码；
- 幂等（带 `LIBTAR_FSCRYPT_COMPAT_H` 标记），目录不存在时静默跳过。

第二轮（`extract.c`）暴露出 `lookup_ref_tar` 同样是对不上的：vold 新签名是 `lookup_ref_tar(const uint8_t *policy_type, uint8_t *policy)`，libtar 却把 `union fscrypt_policy *` 直接当第一个参数传，触发同一条 `-Werror,-Wincompatible-pointer-types`。这一版把 `lookup_ref_key` / `lookup_ref_tar` 都接上了真实实现（`Decrypt.cpp:131` / `:179`），不是占位。

本地已用 TWRP `android-14.1` 的真实 `libtar.h`/`block.c`/`append.c` + vold 的 `fscrypt_policy.h` 跑过脚本，并在 `-DUSE_FSCRYPT_POLICY_V1` 与 `-DUSE_FSCRYPT_POLICY_V2` 两种配置下用 `gcc -Werror=incompatible-pointer-types` 做了语法验证。

### 12. `libtar` 链接失败：`libvold` 在 AOSP 14 新增的三个依赖

`fscrypt` 兼容层生效后，`append.c` / `block.c` / `extract.c` / `libtar.h` / `output.c` 全部编译通过，构建推进到 80%（44 分钟），卡在 `libtar.so` 的**链接**阶段：

```
FAILED: out/target/product/PD2551/obj/SHARED_LIBRARIES/libtar_intermediates/LINKED/libtar.so
ld.lld: error: undefined symbol: _ZN7android3hal17BootControlClient14WaitForServiceEv
>>> referenced by Checkpoint.cpp:141 (system/vold/Checkpoint.cpp:141)
ld.lld: error: undefined symbol: _ZN12NetlinkEvent4dumpEv
ld.lld: error: undefined symbol: _ZN12NetlinkEvent9findParamEPKc
>>> referenced by VolumeManager.cpp:211
ld.lld: error: undefined symbol: async_safe_format_log
>>> referenced by VolumeManager.cpp:538
```

`libtar` 的 `Android.mk` 用 `LOCAL_STATIC_LIBRARIES += libvold` 把**整个** vold 静态库链进来（为了复用 `Decrypt.cpp` / `fscrypt_policy.cpp` 的实现），但依赖表还停在 Android 12。AOSP 14 的 `system/vold/Android.bp` 里 `vold_default_libs` 已经多了三项：

| 符号 | 提供者 | 类型 |
|---|---|---|
| `BootControlClient::WaitForService()` | `libboot_control_client` | shared |
| `NetlinkEvent::dump()` / `findParam()` | `libsysutils` | shared |
| `async_safe_format_log` | `libasync_safe`（bionic） | static |

这些库 vold 自己能链，是因为 `vold_default_libs` 里有；`libtar` 复用了 `libvold.a` 却只继承了这个静态库本身，依赖表没跟过来。

**这是链接错误，不是模块缺失**，`ALLOW_MISSING_DEPENDENCIES` 依然无效。

修法：新增 `scripts/fix_libtar_link.py`，在 `Android.mk` 的 `ifeq ($(TW_INCLUDE_CRYPTO_FBE), true)` 分支（也就是 `libvold` 真正被链接的分支）追加两行：

```make
LOCAL_STATIC_LIBRARIES += libasync_safe
LOCAL_SHARED_LIBRARIES += libboot_control_client libsysutils
```

- 只改 shared library 那一段（`libtar_static` 不链 `libvold`，不需要）；
- 三个库名按整词判重，已存在则不重复添加；
- 带 `# [twrp-builder-fix]` 标记，重复执行直接跳过；
- 找不到 `Android.mk`、没有 `TW_INCLUDE_CRYPTO_FBE` 分支、或该分支没链 `libvold` 时静默退出（退出码 0）。

验证：拿 TWRP `android-14.1` 真实的 `libtar/Android.mk` 跑脚本，`make` 解析出的变量为

```
STATIC_LIBS = libasync_safe libvold libscrypt_static ...
SHARED_LIBS = ... libboot_control_client libsysutils android.hardware.boot@1.0 ...
```

重复执行无二次修改。

### 13. `soong bootstrap failed`：chained vbmeta 分区没有签名 key

libtar 链接依赖补上之后，流程走到 `Building recovery`，但这次只跑了 **1 分 6 秒**就退出，停在 soong 解析 Android.bp 的阶段：

```
[100% 132/132] analyzing Android.bp files and generating ninja file at out/soong/build.twrp_PD2551.ninja
FAILED: out/soong/build.twrp_PD2551.ninja
env -i "$BUILDER" --top "$TOP" ... Android.bp
error: build/soong/fsgen/Android.bp:32:1: module "soong_filesystem_creator":
  No key found for chained avb partition "vbmeta_system"
04:37:10 soong bootstrap failed with: exit status 1
```

原因是设备树 `BoardConfig.mk` 里只写了 chained 分区，没给它的 key：

```make
BOARD_AVB_ENABLE := true
BOARD_AVB_VBMETA_SYSTEM := system                                  # ← 声明了
BOARD_AVB_VENDOR_BOOT_KEY_PATH := external/avb/test/data/testkey_rsa2048.pem
BOARD_AVB_VENDOR_BOOT_ALGORITHM := SHA256_RSA2048
# 缺 BOARD_AVB_VBMETA_SYSTEM_KEY_PATH
```

AOSP 14 的 `build/soong/fsgen` 会为每个 `BOARD_AVB_VBMETA_*` 分区准备签名参数（生成 `vbmeta_system` 的签 rules），拿不到 `KEY_PATH` 就在 bootstrap 阶段直接 panic。它发生在编译开始后 1 分钟，很容易被误判成“环境/内存问题”，其实是设备树配置不全。

修法：新增 `scripts/fix_device_avb.py`，在 `Patch device tree` 步骤里扫描设备树所有 `BoardConfig*.mk`：

- 找出所有 `BOARD_AVB_VBMETA_<X> := ...` 声明（排除 `_KEY_PATH` / `_ALGORITHM` / `_ROLLBACK_INDEX` 等属性变量）；
- 按 `build/make/core/Makefile` 里 `add-vbmeta-image` 的四项非空校验逐个补齐，缺哪个补哪个：
  - `_KEY_PATH` 跟随设备树已有的 `BOARD_AVB_VENDOR_BOOT_KEY_PATH`（没有则用 `external/avb/test/data/testkey_rsa2048.pem`）；
  - `_ALGORITHM` 跟随 `BOARD_AVB_VENDOR_BOOT_ALGORITHM`（没有则 `SHA256_RSA2048`）；
  - `_ROLLBACK_INDEX := 0`；
  - `_ROLLBACK_INDEX_LOCATION` 取一个未被占用的编号（从 1 开始，跳过设备树里已有的值）；
- 幂等：缺什么补什么，四项齐全就不动文件；没有 chained 分区或没有 `BoardConfig` 时静默退出。

对当前 PD2551 设备树的实际效果是追加四行：

```make
# [twrp-builder-fix] chained vbmeta 分区的签名 key
BOARD_AVB_VBMETA_SYSTEM_KEY_PATH := external/avb/test/data/testkey_rsa2048.pem
BOARD_AVB_VBMETA_SYSTEM_ALGORITHM := SHA256_RSA2048
BOARD_AVB_VBMETA_SYSTEM_ROLLBACK_INDEX := 0
BOARD_AVB_VBMETA_SYSTEM_ROLLBACK_INDEX_LOCATION := 1
```

**第二轮**：只补 `KEY_PATH` / `ALGORITHM` 时 soong 能过，但 make 阶段会接着报

```
build/make/core/Makefile:4815: error: BOARD_AVB_VBMETA_SYSTEM_ROLLBACK_INDEX is not defined.
```

因为 `add-vbmeta-image` 对四个变量都做了 `$(if ...)` 非空校验，缺一个就 `$(error)`。所以脚本改成四项全覆盖。

### 14. 换清华源时 `sed` 报 `unknown option to 's'`

```
switch to TUNA mirror
sed: -e expression #1, char 30: unknown option to `s'
Error: Process completed with exit code 1.
```

**原因**：`sed -E` 下 `|` 是正则里的“或”，却被当成了替换命令的分隔符：

```bash
# 错：分隔符 | 与 alternation | 冲突，表达式在 (archive 处被截断
sudo sed -i -E 's|https?://(archive|security|azure\.archive)\.ubuntu\.com/ubuntu|https://...|g' ...
```

**改法**：换一个 URL 里不会出现的字符作分隔符（`#`），并把表达式抽成变量复用：

```bash
TUNA_SED='s#https?://(archive|security|azure\.archive)\.ubuntu\.com/ubuntu#https://mirrors.tuna.tsinghua.edu.cn/ubuntu#g'
sudo sed -i -E "$TUNA_SED" /etc/apt/sources.list || echo "::warning::换源失败，继续使用默认源"
```

同时补了两处健壮性：换源失败只 warning 不中断；换完校验是否真的出现 `tuna.tsinghua.edu.cn`，没有就打印当前生效的源，方便 runner 改域名时快速定位。

### 15. `partition.cpp` 报一串 `use of undeclared identifier`（FDE API 被 vold 删了）

```
bootable/recovery/partition.cpp:711:5: error: use of undeclared identifier 'set_partition_data'
bootable/recovery/partition.cpp:712:9: error: use of undeclared identifier 'cryptfs_check_footer'
bootable/recovery/partition.cpp:718:46: error: use of undeclared identifier 'cryptfs_get_password_type'
bootable/recovery/partition.cpp:2163:7: error: use of undeclared identifier 'delete_crypto_blk_dev'
bootable/recovery/partitionmanager.cpp:635:24: error: use of undeclared identifier 'cryptfs_get_password_type'
bootable/recovery/partitionmanager.cpp:2187:13: error: use of undeclared identifier 'cryptfs_check_passwd'
```

**原因**：又是同一分支内两个仓库不同步，这次是 FDE（全盘加密）那一套 API。

| 仓库 @android-14.1 | 状态 |
|---|---|
| `TeamWin/android_system_vold` | `cryptfs.h` 只剩 `cryptfs_setup_ext_volume` / `cryptfs_get_keygen`，`set_partition_data`、`cryptfs_check_footer`、`cryptfs_get_password_type`、`delete_crypto_blk_dev`、`cryptfs_check_passwd` 连同 `cryptfs.cpp` 里的实现全部删除 |
| `TeamWin/android_bootable_recovery` | `partition.cpp` / `partitionmanager.cpp` 仍在 `#ifdef TW_INCLUDE_CRYPTO` 里调用这 5 个函数 |

AOSP 13 起 vold 已彻底不支持 FDE，所有设备都是 FBE；FBE 解密走 `Decrypt_DE()` / `Decrypt_CE()` + keymaster/gatekeeper，不经过 crypto footer。这些调用点全部位于「FBE 建立失败 → 回退尝试 FDE」的兜底分支。

**改法**：不删代码、不改调用点（删代码会破坏 `if/else if` 结构，风险更高），而是新增 `scripts/fix_recovery_cryptfs.py`，在 `bootable/recovery/cryptfs_fde_compat.h` 里给 5 个函数提供语义正确的 `static inline` 占位，然后在原本 `#include "cryptfs.h"` 的下一行插一行 include：

```c
#include "cryptfs.h"
#include "cryptfs_fde_compat.h"   /* ← 新增，缩进与原行一致 */
```

占位返回值都和「没有 FDE」这一事实一致：

| 函数 | 返回 | 上层行为 |
|---|---|---|
| `set_partition_data()` | 空实现 | — |
| `cryptfs_check_footer()` | `-1` | 判定为没有 crypto footer，走提示分支 |
| `cryptfs_get_password_type()` | `-1` | 不误判成「默认密码加密」，避免触发必然失败的解密 |
| `delete_crypto_blk_dev()` | `0` | 无 dm 设备可删，视为成功，不刷错误日志 |
| `cryptfs_check_passwd()` | `-1` | FDE 密码校验失败，与真实状态一致 |

vold 的 `cryptfs.h` 仍然先生效（`CRYPT_TYPE_*`、`KeyBuffer` 等照旧可用），只在缺函数时由兼容头补位。头里还兜底了 `CRYPT_TYPE_DEFAULT`（现在只定义在 `fscrypt-common.h`）。

验证：用 TWRP `android-14.1` 的真实 `partition.cpp` / `partitionmanager.cpp` 跑脚本，插入位置正确、缩进一致；重复执行不再插入；`bootable/recovery` 不存在时静默退出。并用 `g++ -std=gnu++17 -Wall -Wextra -Werror` 和 `gcc -std=gnu11 -Werror` 对模拟调用点做了语法校验，两种语言都通过（包括 `(char*)("userdata")` 传 `char*` 这种实参形态）。

### 16. 链接 `recovery` 可执行文件：`NetlinkEvent::dump()` / `findParam()` 未定义

```
FAILED: out/target/product/PD2551/obj/EXECUTABLES/recovery_intermediates/LINKED/recovery
ld.lld: error: undefined symbol: _ZN12NetlinkEvent4dumpEv
>>> referenced by VolumeManager.cpp:208 (system/vold/VolumeManager.cpp:208)
ld.lld: error: undefined symbol: _ZN12NetlinkEvent9findParamEPKc
>>> referenced by VolumeManager.cpp:211 (system/vold/VolumeManager.cpp:211)
clang++: error: linker command failed with exit code 1
```

构建到 **96%**，前面所有源码级错误都已通过。

**原因**：`bootable/recovery/Android.mk` 里 recovery 也静态链了 vold：

```make
LOCAL_STATIC_LIBRARIES += libguitwrp libvold
```

`libvold.a` 的 `VolumeManager.cpp` 在 AOSP 14 调用了 `NetlinkEvent::dump()` 和 `findParam()`，这两个符号由 `libsysutils` 提供。但 recovery 的 `LOCAL_SHARED_LIBRARIES` 依赖表停在 Android 12，没有 `libsysutils`。

第 12 条修的是 **libtar.so** 的链接（同一批符号），recovery 是另一条独立的链接线，必须单独补——`TWRP_REQUIRED_MODULES` 里的 `libsysutils` 只是保证模块被编译出来，不等于被链接进来。

**改法**：新增 `scripts/fix_recovery_link.py`，在 `LOCAL_MODULE := recovery` 模块块内（到 `include $(BUILD_` 为止）找到第一条 `LOCAL_SHARED_LIBRARIES` 行，行尾追加 `libsysutils`：

```make
LOCAL_SHARED_LIBRARIES += libz libc libcutils ... libtwadbbu libsysutils
```

只改这一行：条件分支都排在它之后，因此不会把库加进 `ifneq` 分支；`libtar` 和其他模块不受影响。

验证：用 TWRP 真实 `Android.mk` 跑，diff 只有一行、单空格、追加在行尾；重复执行输出“已含 libsysutils，无需修改”；`Android.mk` 不存在时静默退出。

### 17. `make` 退出码 0，但 `Collect outputs` 报“没有生成任何 .img”

**现象**

```
===== 产物目录：out/target/product/twrp_PD2551 =====
Error: 没有生成任何 .img，请检查上面的编译日志
Error: Process completed with exit code 1.
```

编译那一步是绿的，打包这一步才炸。

**根因**：`vendorbootimage` 在 `build/make/core` 里是按 `BOARD_USES_VENDOR_BOOTIMAGE` 条件定义的。设备树没开这个开关时，这个目标仍然存在但**既没有依赖也没有配方**，`make` 只会打印

```
make: Nothing to be done for 'vendorbootimage'.
```

然后 `exit 0`。ninja 压根没跑打包，`out/target/product/<product>/` 里自然一个 `.img` 都没有——这不是编译失败，是**目标名和设备真实分区对不上**。

**改法**（`Recovery Build.yml`，三处）：

1. 构建前用 `get_build_var BOARD_USES_VENDOR_BOOTIMAGE` 探测，未开启时把 `MAKE_TARGET` 从 `vendorbootimage` 自动切到 `bootimage`，并打 `::warning::`；
2. `make` 之后立刻自检产物目录：返回 0 但 0 个 `.img` 时，打印 `build.log` 尾部 + `Nothing to be done` 检测结果 + `out/target/product` 目录列表，然后按 `bootimage → recoveryimage → vendorbootimage → droid` 逐级补编（对象都在，补编只是打包，很快）；
3. `Collect outputs` 不再硬拼 `out/target/product/$TARGET_PRODUCT`，改用 `get_build_var PRODUCT_OUT`；找不到就在 `out/target/product` 下全量搜索 `.img`（>1MB），统一收口到 `dist/`，artifact / Release 都只从 `dist/` 取。

**顺带说清楚**：`BUILD_TARGET` 要填设备**真实有**的那个分区。vivo 这类把 recovery 塞进 `boot` 的机型填 `boot`；确实有独立 `vendor_boot` 分区（GKI 机型常见）才填 `vendorboot`。

### 18. 要 `vendor_boot.img`，但设备树里变量名写错了

**现象**：`BUILD_TARGET=vendorboot` 跑完，`BOARD_USES_VENDOR_BOOTIMAGE=<未定义>`，只得到 `boot.img`。

**根因**：设备树里写的是

```make
BOARD_USES_VENDOR_BOOT := true        # ← AOSP 没有这个变量
```

而 `build/make/core/Makefile` 判断的是 **`BOARD_USES_VENDOR_BOOTIMAGE`**（多一个 `IMAGE`）。名字不对 → 整个 vendor_boot 分支被跳过 → `vendorbootimage` 成为空目标，`make` 返回 0 却不出镜像，和第 17 条是同一个表现、不同根源。

**改法**：新增 `scripts/fix_device_vendor_boot.py`，接在 `Patch device tree` 的第 (5) 步，对设备树做四件事（幂等，带 `# [twrp-builder-fix]` 标记）：

1. `BOARD_USES_VENDOR_BOOT := true` → `BOARD_USES_VENDOR_BOOTIMAGE := true`（没有就补上）；
2. 补 `BOARD_MOVE_RECOVERY_RESOURCES_TO_VENDOR_BOOT := true`，把 TWRP 的 `recovery/root` 与 ramdisk 挪进 vendor_boot；
3. `BOARD_BOOT_HEADER_VERSION` 不是 3/4 时纠正为 4（vendor_boot 只支持 v3/v4）；
4. `BOARD_AVB_ENABLE=true` 时补齐 vendor_boot 的 AVB 四项：`_KEY_PATH` / `_ALGORITHM` / `_ROLLBACK_INDEX`（沿用 boot 的写法）/ `_ROLLBACK_INDEX_LOCATION`（自动取 1..15 里最小的未占号，本例 1、2、3 已被 vbmeta_system / vbmeta_vendor / boot 占掉，取 4）。

**为什么 AVB 那四项一定不能漏**：有些机型不允许刷 `vbmeta` 去关校验（`fastboot --disable-verification flash vbmeta` 会失败或直接不启动）。这种情况下镜像自身必须带 hash footer，否则 bootloader 连"结构合法"这一步都过不了。

### 19. 产物里多出一个 `ramdisk.img`，是不是没合进 vendor_boot

**现象**：`vendor_boot.img` 旁边还躺着个 2.8 MB 的 `ramdisk.img`，一起被打进了 artifact / Release。

**结论：不用合，它本来就已经在 `vendor_boot.img` 里了。**

`ramdisk.img` 是 `INSTALLED_RAMDISK_TARGET`，也就是通用 ramdisk 的**打包中间件**，位于 `PRODUCT_OUT` 顶层（和 `boot.img` 同级），所以被 `cp "$OUT"/*.img` 一起捡走了。它有两个特征说明它不是给人刷的：

1. 它是 LZ4 legacy 帧压缩的 cpio，不是 boot image 格式 —— `fastboot flash` 没有对应的分区可以接收它；
2. 它的内容已经作为 vendor_boot 的 **platform 片段**（v4 分片表 `ramdisk_type=0`）编进去了，加上 recovery 片段后才是完整的 `/init` + TWRP。

拆开验证过（第 18 条那次产物，128 MB / header v4）：

| 片段 | 类型 | 压缩后 | 解压后 | 内容 |
|---|---|---|---|---|
| 0（空名） | platform | 20.0 MB | 68.2 MB | `first_stage_ramdisk/*`（linker64、libc、libc++、fstab）+ `lib/modules/6.6-gki` 61 MB |
| 1 `recovery` | recovery | 66.6 MB | 182.0 MB | `init` → `system/bin/init`、`system/bin/recovery` 2.69 MB、`system/bin/twrp`、`init.recovery.mt6899.rc`、`twres/`、`system/etc/recovery.fstab` |

**改法**（workflow `Collect outputs`）：从「复制 `$OUT` 下全部 `.img`」改为白名单，只收真正能刷的分区镜像 —— `boot / vendor_boot / recovery / dtbo / lk / vbmeta* / super_empty` 和 `.zip`；`ramdisk*.img`、`userdata.img`、`system.img` 等一律跳过，并在日志里打一行 `::notice::` 列出被跳过的中间件。同时把构建后自检的 `count_imgs` 也改成只数可交付镜像，避免"只有 ramdisk.img 也算编成功"。

### 20. 刷完黑屏：TWRP 起来了，但 9 毫秒后 exit 1

**现场（PD2551 的 `console-ramoops-0`，1741 行，UTF-16LE）**：

```
[    4.942837][    T1] init: First stage mount skipped (recovery mode)
[    5.040408][    T1] init: Added '/init.recovery.mt6899.rc' to import list
[    5.236904][    T1] init: starting service 'recovery'...
[    5.240836][  T230] KernelSU: hook_manager: unmark 230 exec /system/bin/recovery
[    5.249641][    T1] init: Service 'recovery' (pid 230) exited with status 1
```

结论：**不是 AVB、不是镜像结构、也不是 ramdisk 没进去** —— recovery 模式确实进了，TWRP 二进制确实被 exec 了，但约 9 ms 就 `exit 1`，init 每 5 秒重启一次 → 黑屏死循环。

**根因：recovery 里一个内核模块都没加载。**

```
[    4.942181][    T1] init: module bootprof can't be loaded;
                          can't access /lib/modules/bootprof.ko: No such file or directory
```

拆开 `vendor_boot.img` 确认，模块实际在 `lib/modules/6.6-gki/`（266 个，含 `mediatek-drm.ko`、`mtk_disp_notify.ko`、`panel_common.ko`、`vivo_display.ko`），而 AOSP first stage init 只找 `/lib/modules/<name>.ko` 这种**平铺路径**，于是整份清单只尝试了第一个就断了。连带后果：

| 缺的模块 | 后果 |
|---|---|
| `mediatek-drm.ko` 等显示链 | 没有 fb / DRM 节点 → TWRP minui 初始化失败 → `exit(1)` |
| `extcon-mtk-usb.ko` 等 | 没有 UDC（`UDC core: g1: couldn't find an available UDC`）→ recovery 里 adb 起不来，`/tmp/recovery.log` 拿不到 |

旁证：pstore 的 `pmsg` 里 `adbd: timed out while waiting for FUNCTIONFS_BIND`，以及所有依赖 TEE 的服务（mobicore / gatekeeper / keymint）也一并 exit 1 —— 这些是 recovery 下密钥服务本来就起不来的正常现象，不要误判。

**改法**：新增 `scripts/fix_recovery_init.py`，接在 `Building recovery` 之前的 5.13 步，做两件事：

1. 往设备树 `recovery/root/system/bin/` 写一个 `twrp-modules.sh`，并在 `bootable/recovery/etc/init.rc` 追加 `on fs` 的 `exec ... /system/bin/sh /system/bin/twrp-modules.sh`：auto 找 `/lib/modules/*/modules.load.recovery`（没有就 `modules.load`），逐个 `insmod`，最后把 `insmod ok=N fail=M`、`/dev/graphics/fb0`、`/dev/dri/card0`、`/sys/class/udc` 的内容 echo 出去。init 的 `exec` 继承 stdout(`/dev/kmsg`)，所以下次从 pstore 就能看到这些统计。
2. 给 `etc/init.recovery.service22.rc` 的 `service recovery` 加 `stdio_to_kmsg`：TWRP 的 stdout/stderr 不再丢进 `/dev/null`，它 exit(1) 的真正原因会直接进 `console-ramoops`。

脚本的几处细节（都是踩过坑才这么写的）：

| 细节 | 原因 |
|---|---|
| 优先 `modules.load.recovery` | 实测 `lib/modules/6.6-gki/` 里两份清单都有，`modules.load.recovery` 是 286 条，`modules.load` 只有 266 条，前者才是 MTK 给 recovery 准备的 |
| `insmod` 报 `File exists` 记为 skip 而不是 fail | 大量模块在 vendor_boot 的 first stage 已内建或被依赖链带进来，全算 fail 会让日志失真 |
| 失败只记前 8 个名字 | 286 条全打印会把 kmsg 冲掉，反而看不清 |
| 用 `/system/bin/sh script` 而不是直接 `exec` 脚本 | cpio 打包时 `recovery/root` 里的文件可能丢可执行位，显式走解释器最稳（已确认 ramdisk 里 `system/bin/sh`、`toybox`、`insmod` 都在） |
| 开头 `exec 1>/dev/kmsg` | init 的 `exec` 子进程一般继承 `/dev/kmsg`，但不同版本可能指到 `/dev/null`，显式重定向一次保证日志一定能看到 |
| 结尾 `sleep 1` 再查节点 | ueventd 建 `/dev/graphics/fb0` 是异步的，立刻查会误报 MISSING |

**为什么选 `on fs` 而不是 `late-init`**：TWRP 的服务挂在 `boot` 触发器上，而 `boot` 是在 `late-init` 段里 `trigger` 出来的，追加到 `late-init` 尾部容易和它抢顺序；`fs` 阶段（约 5.2 s）早于 `boot`（5.23 s），且 ueventd 已在 `early-init` 起来，模块注册时的 uevent 能立刻生成 `/dev/graphics/fb0`。

### 21. first stage 一条模块都加载不了：清单路径与加载路径不是同一个

**现场**（同一次 `console-ramoops`）：

```
init: alias lines in modules.alias must have 3 entries, not 5
init: softdep lines in modules.softdep must have at least 4 entries
init: module bootprof can't be loaded; can't access /lib/modules/bootprof.ko
```

注意只有**一条** `can't be loaded`：`LoadModulesFromFile()` 读到第一条文件不存在就整体返回失败，后面 265 条根本不再尝试。

**根因**：init 读清单时用的是 `/lib/modules/6.6-gki/modules.load`（能读到，所以才会解析 alias/softdep 并报格式警告），但拼加载路径时用的是 `/lib/modules/<name>.ko`（不带 `6.6-gki`）。两边不一致，清单第一条 `bootprof.ko` 就找不到。

**先确认是不是硬故障**：拆 `vendor_boot.img` 的 platform 片段，核对 `modules.dep` ——

```
清单 286 条，不在 dep 里的 0 条
dep 里 f2fs*: []   ext4*: []   erofs*: []
```

`ext4` / `erofs` / `f2fs` 都不在 `modules.dep` 里，说明它们是 **builtin**。再对照 `fstab.mt6899`（system/system_ext/vendor/product/odm/*_dlkm 是 ext4/erofs，metadata/data 是 f2fs），first stage 挂载 logical 分区**不需要任何 vendor 模块**。所以这条报错是软故障：只丢 MTK 的崩溃转储与性能跟踪。

**为什么不能「全平铺」**：303 个 `.ko` 共 **61 MB**，`vendor_boot` 分区 128 MB，当时产物已 87 MB（其中 61 MB 是 platform 与 recovery 两个 fragment 各存了一份）。全平铺会直接超限，`add_hash_footer` 报 image too large。

**改法**：新增 `scripts/fix_first_stage_modules.py`，接在 `fix_device_vendor_boot.py` 之后（5.5 步）：

1. 用 `modules.dep` 建依赖图，保留集 = 清单里的关键文件系统模块（`f2fs/ext4/erofs/squashfs`，仅在它们是模块时才保留）+ MTK 的 `bootprof/mrdump/aee_aed`，再取依赖闭包；
2. 按原清单顺序重写 `modules.load`（依赖天然在前）；
3. 把保留的 `.ko` **平铺复制**到 `lib/modules/` 根下，对上 init 拼的路径。

实测这台机器保留 3 条、308 KB：`bootprof.ko mrdump.ko aee_aed.ko`。第二次运行输出「modules.load 已是最新 / 新复制 0」。

| 坑 | 说明 |
|---|---|
| 不能把 `aee_rs.ko` 加进保留集 | 它在 `modules.dep` 里依赖整条显示栈（`mediatek-drm` / `vivo_display` / `mtk_sync` 等 46 个），一加就是 51 条、十几 MB |
| 文件系统是模块的机型要保留依赖闭包 | 否则 `f2fs.ko` 找不到 `crc32.ko` / `xxhash.ko`，挂载失败 —— 脚本对这种情况会自动带上（已用假 dep 图验证顺序） |
| `modules.load.recovery` 不能动 | 那是 second stage 给 TWRP 用的，`twrp-modules.sh` 按 `6.6-gki` 子目录加载 |
| `modules.alias` / `modules.softdep` 的格式警告消不掉 | MTK 用 modprobe 风格生成（5~6 个字段），AOSP init 的解析器只认 3/4 个字段，会跳过这些行；recovery 不走 `modprobe`，无影响 |

### 22. 精简了半天 `modules.load`，recovery 模式压根不读它

**现场**（`console-ramoops`，4.87 s）：

```
init: Loading module /lib/modules/bootprof.ko with args ''
init: Loaded kernel module /lib/modules/bootprof.ko
init: Loading module /lib/modules/mrdump.ko with args ''
init: Loaded kernel module /lib/modules/mrdump.ko
init: Loading module /lib/modules/aee_aed.ko with args ''
init: Loaded kernel module /lib/modules/aee_aed.ko
init: module vivo_bsp_engine can't be loaded; can't access /lib/modules/vivo_bsp_engine.ko: No such file or directory
init: Loaded 3 modules from /lib/modules/6.6-gki
init: Failed to load kernel modules
Kernel panic - not syncing: Attempted to kill init! exitcode=0x00007f00
```

前 3 条全 `Loaded`，第 4 条 `vivo_bsp_engine` 找不到，然后 init 退出、内核 panic（`0x7f00` = exit 127）。first stage 的模块加载是**全有或全无**，没有「跳过失败项继续」的开关。

**排查弯路**：一开始怀疑是 `modules.dep` 里 `aee_aed.ko` 标了 `vivo_bsp_engine` 依赖 —— 查设备树，`aee_aed.ko:` 后面是空的，不成立。又怀疑脚本把一条不存在的 `.ko` 写进了清单 —— 构建日志里 `Copy: .../6.6-gki/vivo_bsp_engine.ko` 说明这个文件**存在**。两条都不对。

**真因**：`system/core/init/first_stage_init.cpp` 的 `GetModuleLoadList()`：

```cpp
switch (boot_mode) {
    case BootMode::NORMAL_MODE:   module_load_file = "modules.load"; break;
    case BootMode::RECOVERY_MODE: module_load_file = "modules.load.recovery"; break;
    case BootMode::CHARGER_MODE:  module_load_file = "modules.load.charger"; break;
}
if (module_load_file != "modules.load") {
    // Fall back to modules.load if the other files aren't accessible
    if (stat((dir_path + "/" + module_load_file).c_str(), &fileStat))
        module_load_file = "modules.load";
}
```

本机 cmdline 里 `recoverymode=1`，所以走的是 **`modules.load.recovery`（286 条原始清单）**，`modules.load`（被精简成 3 条）只有在 recovery 清单不存在时才会被 fall back 到。构建日志白纸黑字写着 `✓ 重写 modules.load（266 -> 3 条）`，但那份文件根本没被读。

对照一下，两边前四行完全一致，正好是设备上的 3 成功 + 1 失败：

```
modules.load            modules.load.recovery
bootprof.ko             bootprof.ko
mrdump.ko               mrdump.ko
aee_aed.ko              aee_aed.ko
vivo_bsp_engine.ko      vivo_bsp_engine.ko   ← 失败在这里
```

**改法**（`scripts/fix_first_stage_modules.py` 新增 1b 段）：

1. 把原 `modules.load.recovery`（286 条）另存为 `modules.load.twrp`；
2. `modules.load.recovery` 改写成与 `modules.load` 相同的精简清单（3 条，全是已平铺的 `.ko`）；
3. 设备树 `recovery/root/system/bin/twrp-modules.sh` 的清单查找顺序改成
   `modules.load.twrp` → `modules.load.recovery` → `modules.load`。

这样 first stage 只加载 3 个平铺的模块（必然成功），进入 second stage 后由
`twrp-modules.sh` 按 `modules.load.twrp` 全量加载 286 个（drm / panel / usb 都在里面）。

| 坑 | 说明 |
|---|---|
| 同名文件承担两种用途 | `modules.load.recovery` 既是 first stage 的加载清单，又被 TWRP 当成全量清单，必须先拆成两份 |
| 日志里 `Loaded N modules from /lib/modules/6.6-gki` 具有误导性 | 它只表示「这个目录加载成功了 N 个」，不代表清单只有 N 条 |
| `.ko` 在子目录、清单在子目录、加载路径却是平铺 | 三者不在同一层，改哪个都要先弄清 init 拼的是哪条路径 |

### 23. init 拒绝执行 twrp-modules.sh：`/system/bin/sh` 没有 SELinux 标签

**现场**（`console-ramoops`，5.139 s，紧跟在 `processing action (fs)` 之后）：

```
init: Command 'exec - root root -- /system/bin/sh /system/bin/twrp-modules.sh'
      action=fs (/system/etc/init/hw/init.rc:223) took 0ms and failed:
      Could not start exec service: File /system/bin/sh(labeled "u:object_r:rootfs:s0")
      has incorrect label or no domain transition from u:r:init:s0 to another SELinux
      domain defined. Have you configured your service correctly?
      Note: this error shows up even in permissive mode in order to make auditing
      denials possible.
```

同一份日志里还有两个对照点，正好用来定位：

- 同目录的 `prepdecrypt.sh`（pid 193）**起得来** —— 它走的是 `service` + `seclabel`，不靠文件标签；
- `exec u:r:ksu:s0 root -- /data/adb/ksud ...` 的失败原因是 `Cannot find '/data/adb/ksud'`，**不是标签错误** —— 说明 `exec <seclabel> <user> <group> --` 这套语法本身可用。

**根因**：recovery 的 ramdisk 是 rootfs，`/system/bin/sh` 没有 file_contexts 规则，标签落成 `u:object_r:rootfs:s0`。`exec` 不带 seclabel 时，init 靠文件标签推断 domain，推不出来就拒绝启动。这条检查在 **permissive 下照样执行**（init 自己的硬校验，不是 SELinux 强制），所以 `setenforce 0` 也没用。

**改法**：`exec` 显式带上 seclabel，用 recovery 服务自己的域：

```diff
 on fs
-    exec - root root -- /system/bin/sh /system/bin/twrp-modules.sh
+    exec u:r:recovery:s0 root root -- /system/bin/sh /system/bin/twrp-modules.sh
```

`u:r:recovery:s0` 是 `init.recovery.service.rc` 里 `service recovery` 用的同一个域，必然存在、必然允许 init 切换。仍然是**同步**执行，TWRP 启动前模块一定加载完（换成 `service` + `start` 是异步的，prepdecrypt 只比 recovery 早 13 ms，来不及）。

`fix_recovery_init.py` 里同时处理了已存在的旧写法（`OLD_EXEC` → `NEW_EXEC`），重跑不会重复追加。

**顺带确认的两件事**（这次日志里都有）：

| 项 | 证据 |
|---|---|
| `modules.load.twrp` 确实进了 ramdisk | 构建日志 `Copy: out/target/product/PD2551/vendor_ramdisk/lib/modules/6.6-gki/modules.load.twrp` |
| first stage 那 3 条是必然成功的 | `init: Loaded 3 modules from /lib/modules/6.6-gki`，无 `Failed to load kernel modules`、无 panic |

**为什么没有 `[twrp-fix]` 输出**：脚本压根没被执行，不是执行了没输出。先看 `on fs` 那条 `exec` 有没有报错，再看脚本自己的 log —— 这两步不能跳。

### 24. 脚本终于跑起来了：清单顺序是错的 + 一个模块 insmod 直接把内核打死

**现场**（`console-ramoops`，5.22 s → 9.58 s）：

SELinux 那道关已经过了，exec 真的起来了：

```
5.218876 init: starting service 'exec 1 (/system/bin/sh /system/bin/twrp-modules.sh)'...
5.220946 init: SVC_EXEC service 'exec 1 (...)' pid 202 (uid 0 gid 0+0 context u:r:recovery:s0) started; waiting...
```

加载随之开始，但模块一个接一个报 `Unknown symbol`：

```
5.476839 zcache:        Unknown symbol rsc_qos_enable / get_task_qos
5.705587 mtk_iommu:     Unknown symbol mtk_smi_dbg_register_notifier / mtk_smi_dbg_hang_detect
6.581737 mtk_mml:       Unknown symbol lcm_sw_id / get_vivo_display
6.685988 vivo_display:  Unknown symbol track_event
6.698209 panel_common:  Unknown symbol lcm_sw_id / lcm_dev_register
6.865300 mediatek_drm:  Unknown symbol lcm_sw_id / mmprofile_log_ex / get_vivo_display / cmdq_*
9.573562 Internal error: BRK handler: 00000000f2000100 [#1] PREEMPT SMP
9.586938 pc : cleanup_module+0x152c4/0x153ac [fp_dispatch_event]
9.586948 lr : init_module+0x1a8/0xf18 [fp_dispatch_event]
```

没有 `[twrp-fix]` 输出的原因很简单：脚本的统计只在**循环结束**时打，而循环没走完 —— 第 ~250 个模块 insmod 时内核就 panic 了。

**两个独立问题**

**① 清单顺序不是加载顺序。** vendor 那 286 条既不是拓扑序，也不完整：

| 模块 | 清单位置 | 依赖 | 依赖在清单里的位置 |
|---|---|---|---|
| `vivo_display.ko` | 90 | `event_track.ko`（导出 `track_event`） | **267** |
| `mediatek-drm.ko` | 103 | `mtk-mml.ko` / `cmdq-*` / `vivo_display.ko` | 82/86/90 |
| `zcache.ko` | 18 | `vivo_rsc.ko`（导出 `rsc_qos_enable`） | **不在清单里** |
| `system_heap.ko` | — | `thp_pool.ko`（导出 `thppool_*`） | **不在清单里** |

`lcm_sw_id` / `get_vivo_display` / `lcm_dev_register` 三个符号的导出者都是 `vivo_display.ko`（`nm` 查 `__ksymtab_*` 确认）。它因为 `track_event` 没进来而加载失败，于是 `panel_common` / `mtk-mml` / `mediatek-drm` 全部级联失败 —— **整条显示栈零加载，这才是黑屏的直接原因**，跟 fb/DRM 后端无关。

**② `fp_dispatch_event.ko` insmod 会 BUG。** 它是 vivo 指纹事件分发，`init_module` 里直接 `BUG()`（`BRK handler: f2000100`），走 `mrdump_common_die` → panic。与显示/输入/USB 无关，必须拉黑。

**改法**

`scripts/fix_first_stage_modules.py` 新增 1c 段，写 `modules.load.twrp` 之前：

1. 解析 `modules.dep` 建图，按 **DFS 拓扑排序**输出（依赖一定在前）；
2. 依赖闭包里「清单没列但目录里有」的模块一起补进来（`vivo_rsc` / `thp_pool` / `mtk_slbc`）；
3. `BLACKLIST = ("fp_dispatch_event.ko",)` 剔除；
4. 清单里重复的 `mtk_dramc.ko` 顺带去重。实测 286 条 → 287 条（-1 黑名单 -1 重复 +3 补齐），**顺序违例 0**。

`twrp-modules.sh`（CI 的 `LOADER` 模板与设备树同步）：

- 每个模块打一行 `[twrp-fix] [i/N] ok|skip|FAIL name`，崩溃时 rammops 最后一行就是元凶；
- 运行期再兜一层黑名单（万一清单是旧的）；
- 失败行截取 `insmod` 第一行错误，最多记 20 个。

**教训**：`insmod` 一个依赖未满足的模块只会返回 `Unknown symbol`，脚本正确地把它记成失败继续往下走 —— 所以「脚本跑完了、ok 也不少」并不代表加载链完整。判据要看**关键模块是否成功**（`mediatek-drm` / `vivo_display` / `mtu3`），不是看总数。

## 四、怎么用

二选一：

```bash
# 方式 A：直接覆盖（本目录已按仓库结构放好）
cp -r Action-TWRP-Builder/.github Action-TWRP-Builder/scripts <你的仓库>/

# 方式 B：在仓库根目录打补丁
git apply /path/to/fix.patch
```

推送后重新 Run workflow，参数保持默认即可（`PD2551 / twrp_PD2551 / vendorboot / JDK 17`）。

## 五、还需要注意的两点（代码改不了的部分）

1. **`DEVICE_NAME` 必须等于 lunch 出来的 `TARGET_PRODUCT`**。默认值我改成了 `PD2551`；若你的设备树实际产品名不是它，运行时会打印 `TARGET_PRODUCT=...`，按那个填。上传目录现在用构建时探测到的真实产品名，即使填错也不会丢产物。
2. **`ANDROID 14` 源 + vivo vendor_boot 这类设备**，如果编译阶段报 `PRODUCT_STATIC_BOOT_CONTROL_HAL` 或 bootctrl 模块缺失，脚本里的 `Patch device tree` 步骤会自动清理（已设为 `continue-on-error`，不会反过来把构建搞挂）。若仍失败，把 `Building recovery` 那一步最后 50 行日志贴给我，我按实际报错继续修——那是设备树内容问题，不是脚本问题。

另外，如果你的 runner 拉 `archive.ubuntu.com` 很慢（国内网络常见），workflow 的 `Prepare the environment (TUNA mirror)` 步骤已经内置换源，无需再手动加。若要自己写，注意分隔符不要用 `|`（`sed -E` 下它是 alternation，会被误认为分隔符）：

```yaml
        sudo sed -i -E 's#http://(archive|security)\.ubuntu\.com#https://mirrors.tuna.tsinghua.edu.cn#g' \
          /etc/apt/sources.list
```
