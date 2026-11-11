# Z.A.T.O. Android 构建工具

为 Ren'Py 视觉小说 [*Z.A.T.O. // I Love the World and Everything in It*](https://nopanamaman.itch.io/z-a-t-o-i-love-the-world-and-everything-in-it)
构建 Android APK。

> 本仓库只包含**构建脚本与 GitHub Actions 工作流**，不包含游戏本体、素材或任何
> 预编译产物。构建所需的发行版由使用者自行从游戏发布页获取。

*Build an Android APK from the official Ren'Py distribution of Z.A.T.O. This
repository ships build scripts and a CI workflow only — no game assets and no
prebuilt APK.*

---

## 概述

Ren'Py 官方支持 Android，但把本作的发行版直接交给 RAPT 打包并不能得到可用的
APK。本仓库在打包前对项目做两处修正，并封装了完整的构建流程：

- `scripts/prepare.py` —— 解包 `.rpa` 归档、将非 ASCII 资源名改为 ASCII、生成
  RAPT 所需的 `android.json`；
- `scripts/build.sh` —— 下载官方 Ren'Py SDK 与 RAPT、安装 Android SDK 组件、
  生成签名密钥，并调用 Ren'Py 自带的 `android_build` 命令；
- `scripts/fetch.py` —— 并发分段下载器，用于绕过 `update.renpy.org` 的单连接限速；
- `.github/workflows/build-apk.yml` —— 手动触发的 CI 构建。

构建流程已在 Ren'Py 8.5.3 下完整验证：生成的 APK 可安装、启动并进入游戏主菜单。

## 关于中文翻译补丁

构建流程本身与语言无关：**不带翻译补丁时照常工作**，产出即原版游戏；带补丁时，
`prepare.py` 会额外处理补丁引入的中文字体与资源。

中文翻译补丁是独立于游戏发行版与本仓库的第三方内容，版权归其译者所有，本仓库
不包含也不分发它：

- 发布页（Steam 创意工坊）：<https://steamcommunity.com/sharedfiles/filedetails/?id=3614810823>
- 翻译项目（Paratranz）：<https://paratranz.cn/projects/17099>

补丁以 `.rpa` 归档形式提供（通常为 `z_SC_Main.rpa` 与 `z_SC_OP.rpa`），放入游戏的
`game/` 目录后即可生效。其内容包括：

- `tl/Chinese/` 下的各章节翻译脚本；
- 中文字体（`京華老宋体GB.ttf`、`博雅宋_Cour_Final.ttf`）以及一个切换字体的钩子脚本；
- 片头动画等替换资源。

`prepare.py` 会自动把它们整理成可打包的形式：字体移入 `game/fonts/` 并改为 ASCII
文件名，钩子脚本中的字体路径同步改写（原因见「工作原理」第 2 节）；其余非 ASCII
文件名（如 `恐龙.txt`）同样会被改名。不含补丁的发行版没有这些文件，该步骤不会
产生任何改动。

## 目录结构

```
.
├── scripts/
│   ├── prepare.py      # 项目预处理（解包、重命名、android.json）
│   ├── build.sh        # 下载依赖并构建 APK
│   └── fetch.py        # 并发分段下载器
├── .github/workflows/
│   └── build-apk.yml   # GitHub Actions 工作流
└── README.md
```

## 构建产物

| 项目 | 值 |
| --- | --- |
| 应用包名 | 可配置，默认 `org.zato.android` |
| 版本 | 取自游戏自身的 `config.version` |
| minSdk / targetSdk | 21 / 36 |
| ABI | `arm64-v8a`、`armeabi-v7a`、`x86_64` |
| 签名 | 构建时生成的临时密钥（alias 与口令均为 `android`） |
| APK 体积 | 约 337 MB（322 MiB，含中文补丁的发行版实测） |

## 工作原理

### 1. APK 内的 `.rpa` 不会被加载

Ren'Py 8.5.x 的 `renpy.loader` 只扫描文件系统上的归档：
只有 `scandirfiles_from_filesystem()` 会向 `arc_files` 追加条目，而
`scandirfiles_from_apk()` 仅登记 APK 资源条目、不修改 `arc_files`。
因此打进 APK 的 `.rpa` 永远不会被索引，游戏启动后会在
`renpy/common/00start.rpy` 处报 `LabelNotFound: could not find label 'start'`。

`prepare.py` 解析 RPA-3.0 索引并把所有归档展开成散装文件，随后删除归档。

### 2. 非 ASCII 资源名在 APK 中无法按名查找

aapt 写入资源名时使用 UTF-8 字节，但没有设置 ZIP 的 UTF-8 标志位（bit 11）。
Python 的 `zipfile` 按 cp437 解码文件名，于是 `京華老宋体GB.ttf` 在 APK 内变成
乱码，Ren'Py 按原文件名查找时必然失败：

```
Exception: Could not find font 'fonts/京華老宋体GB.ttf'.
```

`prepare.py` 将所有非 ASCII 资源改为 ASCII 名称，同步重写 `.rpy` 源文件中对应
的路径引用，并删除其过期的 `.rpyc` 以触发重新编译。翻译字体统一移动到
`game/fonts/`，从而对任意语言前缀均可见。

### 3. 必须使用官方 SDK，而非 git checkout

Ren'Py 的 git 仓库不包含 `lib/python3.12` 标准库与 `lib/py3-linux-x86_64/`
预编译文件。用 git 树构建出的 APK 会在 Python 初始化阶段失败
（`ModuleNotFoundError: No module named 'encodings'`，随后是 `ecdsa`、`jnius`）。
`build.sh` 默认下载官方 SDK 发行包，以规避该问题。

## 环境要求

- Linux x86_64（其他平台未经验证）
- JDK 17–25，推荐 21（Gradle 9 与 AGP 8.13 的要求）
- Python 3.9+，以及 Pillow（`pip install pillow`，用于自动生成启动器图标）
- 约 6 GB 可用磁盘（首次构建缓存约 4 GB）
- 可访问 `update.renpy.org`、`dl.google.com`、`services.gradle.org` 与 Maven 仓库

需要代理时设置 `https_proxy` 即可；`build.sh` 会将它同时传递给 `sdkmanager`
与 Gradle。

## 本地构建

### 1. 获取发行版

从[游戏发布页](https://nopanamaman.itch.io/z-a-t-o-i-love-the-world-and-everything-in-it)
下载发行包。脚本同时接受 `.zip` 与已解压的目录：

```bash
python3 scripts/prepare.py \
    --source /path/to/ZATO-multiplatform.zip \
    --out build/project
```

### 2. 构建

```bash
bash scripts/build.sh --project build/project --out build/out
```

首次运行会下载 Ren'Py SDK（约 160 MB）、RAPT（约 68 MB）、Android SDK 组件
与 Gradle 依赖，随后执行 `assembleRelease`。缓存位于 `.cache/`，重复构建会复用。

### 3. 安装

```bash
adb install -r build/out/*.apk
```

## 参数

### `prepare.py`

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--source` | 必填 | 发行版 `.zip`，或包含 `game/` 的目录 |
| `--out` | 必填 | 生成的项目目录 |
| `--package` | `org.zato.android` | Android 包名 |
| `--name` | 游戏全名 | 应用名称（启动器显示） |
| `--icon-name` | `Z.A.T.O.` | 图标下方显示的名称 |
| `--version` | `1.0` | 版本名 |
| `--keep-archives` | 关闭 | 不解包 `.rpa`（仅在上游修复该问题后才有意义） |

### `build.sh`

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--project` | 必填 | 已准备好的项目目录 |
| `--out` | 必填 | APK 输出目录 |
| `--renpy-version` | `8.5.3` | 使用的 Ren'Py 版本 |
| `--sdk-zip` / `--rapt-zip` | 自动下载 | 使用本地已有的依赖包 |
| `--cache` | `.cache/` | 依赖缓存目录 |
| `--gradle-home` | `.cache/gradle-home` | `GRADLE_USER_HOME` |
| `--keys` | 项目目录 | 签名密钥存放目录 |
| `--bundle` | 关闭 | 同时生成 `.aab` |

`build.sh` 使用独立的 `GRADLE_USER_HOME`，以避免用户全局 `init.gradle`
注入仓库（这会与 AGP 8.13 的 settings 仓库模式冲突）。若手动调用 Gradle，
请同样设置该变量。

## GitHub Actions

工作流仅支持手动触发（Actions → *Build Android APK* → *Run workflow*）。

由于游戏素材不能进入仓库，工作流需要一个可直接下载发行包 zip 的 URL：

- 在仓库 **Settings → Secrets and variables → Actions** 中设置 `GAME_URL`；或
- 触发时通过 `game_url` 输入框提供。

itch.io 的下载链接通常与会话绑定，不适合直接用于 CI。建议将 zip 托管在文件
服务器、对象存储，或私有仓库的 Release 资源中。

工作流会缓存 Ren'Py SDK、RAPT、Android SDK 与 Gradle 缓存；其余步骤与本地
构建完全一致。

## 自定义

### 图标

`prepare.py` 会从游戏自带的美术资源（`game/gui/icon.icns`、`icon.ico` 或
`window_icon.png`，取分辨率最高的一张）自动生成两套 432×432 的自适应图标分层：

| 文件 | 说明 |
| --- | --- |
| `android-icon_foreground.png` | 透明背景，主体位于中央 66/108 安全区，边缘做了羽化 |
| `android-icon_background.png` | 以画面平均色加深得到的纯色底，带轻微高光 |

输出位于项目目录（`build/project/`），随后由 RAPT 生成各 DPI 的 mipmap 与
旧式图标。自定义方式有两种：

- 在 `--source` 指向的发行版根目录放置同名文件，`prepare.py` 会原样复制；
- 直接修改 `build/project/` 中的文件后运行 `build.sh`，`prepare.py` 不会覆盖已存在的文件。

`android-icon_mask.png` 为可选项，默认使用 RAPT 的圆角方形遮罩。若未安装 Pillow
或找不到美术资源，会保留 RAPT 的默认图标并给出提示。

### 签名

`build.sh` 会在项目目录生成 `android.keystore` 与 `bundle.keystore`
（PKCS#12，alias 与口令均为 `android`）。如需长期维护同一签名的更新，
可通过 `--keys` 指定持久目录并妥善保管。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| `LabelNotFound: could not find label 'start'` | `.rpa` 未解包。确认 `prepare.py` 输出 “Unpacking N archive(s)”，且 `build/project/game/` 下不存在 `.rpa`。 |
| `Could not find font 'fonts/...ttf'` | 非 ASCII 文件名未被重命名。确认 `prepare.py` 输出了 `renamed ... -> fonts/...`，且 `game/fonts/` 中均为 ASCII 文件名。 |
| `ModuleNotFoundError: No module named 'encodings'`（或 `ecdsa` / `jnius`） | 使用了 Ren'Py 的 git checkout。改用官方 SDK 发行包。 |
| `.../lib/py3-linux-x86_64/renpy does not exist` | 同上，git 树缺少预编译文件。 |
| Gradle 报 `repository 'maven' was added by initialization script` | 全局 `~/.gradle/init.gradle` 与 AGP 8.13 冲突。使用独立的 `GRADLE_USER_HOME`（`build.sh` 默认如此）。 |
| 依赖下载过慢 | `update.renpy.org` 对单连接限速。`fetch.py` 默认 16 路并发，可用 `FETCH_CONNECTIONS=32` 提高。 |

## 已知限制

- 首次冷启动需要解包资源并编译着色器，耗时与设备性能相关；在容器/模拟器环境中
  实测可能达到数分钟，之后的启动会快很多。
- 当前仅在 Android 13（x86_64）环境(waydroid) 以及 arm64-v8a 真机实测。
- 上游 Ren'Py 若修复 APK 内归档索引问题，可通过 `--keep-archives` 跳过解包。

## 法律与许可

游戏 *Z.A.T.O. // I Love the World and Everything in It* 的版权归作者
**nopanamaman** 所有；本仓库仅包含构建脚本，不主张对上述内容的任何权利。

除非获得作者或是相关翻译补丁作者的许可，否则请勿分发构建出的 APK。

本仓库的脚本以 MIT 协议发布，详见 [LICENSE](LICENSE)。

## 致谢

- 游戏：[nopanamaman](https://nopanamaman.itch.io/z-a-t-o-i-love-the-world-and-everything-in-it)
- 中文翻译补丁：坚果及相关工作人员 —— [Steam 创意工坊发布页](https://steamcommunity.com/sharedfiles/filedetails/?id=3614810823)、[Paratranz 翻译项目](https://paratranz.cn/projects/17099)
- 引擎：[Ren'Py](https://www.renpy.org/) 8.5.3
