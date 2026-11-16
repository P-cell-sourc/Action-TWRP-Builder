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

另外，如果你的 runner 拉 `archive.ubuntu.com` 很慢（国内网络常见），可以只在 `Prepare the environment` 开头加一行换源，不影响其他逻辑：

```yaml
        sudo sed -i 's|http://archive.ubuntu.com|https://mirrors.tuna.tsinghua.edu.cn|g; s|http://security.ubuntu.com|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/ubuntu.sources
```
