# 微信收藏表情迁移到飞书

把微信里收藏的表情导出为图片，**每组最多 50 张**，再分批添加到飞书个人表情中。支持下载续跑、内容去重和导入进度记录，保留原始 GIF 动画文件。

本项目独立读取本机微信数据，**无需安装其他微信导出工具**。提供手动选择和桌面自动操作两种添加方式，建议先用一张表情确认效果。

[Windows 使用](#windows-使用) · [已有图片直接分组](#已有图片直接分组) · [macOS 使用](#macos-使用) · [常见问题](#常见问题)

> [!IMPORTANT]
> 当前为实验性工具。Windows 微信导出和飞书添加流程已通过离线模拟测试，**尚未完成 Windows 实机验证**。自动添加是否可用取决于实际客户端界面；每批提交后都需要在飞书中核查结果。

## 能做什么

- **导出微信收藏**：读取个人收藏记录，下载图片，不把全部本地缓存当作收藏。
- **按 50 张分组**：生成 `1`、`2`、`3`……文件夹，方便遵守飞书每次最多 50 张的添加限制。
- **继续上次任务**：下载失败可重试，新增图片追加到已有分组，已确认导入的图片自动跳过。
- **选择添加方式**：手动在飞书中选择图片，或校准后由脚本操作文件选择器。

脚本保留原始图片文件，不保证飞书接受所有格式、大小或保留全部动画效果，最终以客户端结果为准。

## 支持情况

| 功能 | Windows | macOS |
| --- | --- | --- |
| 从微信自动导出 | 面向 64 位微信 4.1+，需保持微信运行并登录 | 需要提供本机已有的数据库密钥文件 |
| 已有图片分组 | 支持 | 支持 |
| 手动按批添加到飞书 | 支持，每批最多 50 张 | 支持，每批最多 50 张 |
| 桌面自动添加 | 实验性，每批最多 50 张 | 实验性，逐张添加 |

Windows 以最新稳定版为主要适配目标。2026 年 10 月 30 日核对[微信官网](https://pc.weixin.qq.com/)，公开的 Windows 64 位版本为 **4.2.0**。程序会检测实际版本并校验数据库；这不代表已经实测兼容所有 4.1+ 版本，微信更新后也可能需要适配。

## Windows 使用

### 1. 准备环境

需要：

- Windows 10 / 11。
- [Python 3.10 或以上版本](https://www.python.org/downloads/windows/)，**选择 64 位版本**。
- 已安装并登录的 **64 位微信**；添加表情时还需登录飞书桌面客户端。
- 能访问微信图片资源的网络连接。

从本仓库页面点击 **Code → Download ZIP** 并解压，或使用 Git 克隆。进入包含 `migrate.py` 的项目文件夹，在文件资源管理器地址栏输入 `powershell`，按回车打开终端。

确认 Python 可用：

```powershell
py -3 --version
```

创建虚拟环境并安装依赖，以下命令逐条执行：

```powershell
py -3 -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

后续直接使用虚拟环境的解释器，无需激活环境或修改 PowerShell 执行策略。

### 2. 检测微信并导出

保持微信运行并登录，打开一次**收藏表情面板**，再执行：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --probe
```

正常情况下会显示微信进程版本、账号目录名和表情数据库位置。这个检测命令不会读取数据库密钥。

只有一个账号且检测正常时，开始导出：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --export
```

成功后会显示图片数量、分组数量和保存路径。打开结果目录：

```powershell
explorer.exe "$HOME\WeChat-Feishu-Stickers\exported"
```

例如，导出 123 张图片后会得到：

```text
exported/
├── 1/    # 50 张
├── 2/    # 50 张
└── 3/    # 23 张
```

**找不到账号或有多个账号？** 在微信设置中查看数据保存位置，找到 `xwechat_files` 根目录，先列出账号：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --list-accounts --wechat-data-dir "D:\微信数据\xwechat_files"
```

将路径和账号目录名替换为实际值后导出：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --export --wechat-data-dir "D:\微信数据\xwechat_files" --wxid "实际账号目录名"
```

### 3. 添加到飞书

先在飞书桌面端打开个人表情的添加入口。不同版本的名称和位置可能不同，通常位于聊天窗口的“表情 → 添加的单个表情”中。

#### 方式一：手动选择图片（推荐）

不需要校准坐标。脚本打开图片所在文件夹，并在终端列出本批文件；**你需要在飞书中选择并添加这些图片**。

首次只试一张：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --manual-import
```

在飞书中确认图片已加入个人表情，并测试能否正常发送。确认效果后，再按每批最多 50 张继续：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --manual-import --yes --batch-size 50
```

请按终端列出的文件选择图片。重试或去重后，本批可能不足 50 张，不要直接全选整个文件夹。

#### 方式二：自动选择图片（实验性）

先校准飞书的「+ 添加」按钮：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --calibrate
```

回终端按回车后，有 **3 秒**将鼠标移到添加按钮中心，**不要点击**。校准后保持飞书窗口位置、主显示器和缩放比例不变。国际版 Lark 使用 `--calibrate --app-name Lark`。

先尝试一张：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import
```

核查成功后，再按每批最多 50 张继续：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --yes --batch-size 50
```

自动模式依赖标准 Windows 文件选择器；客户端界面变化、自定义弹窗或不支持多选时可能失败。遇到此类问题，改用手动模式。

### 4. 核查结果与继续任务

无论使用哪种方式，**每批添加后都要返回终端确认**：

| 输入 | 含义 |
| --- | --- |
| `y` | 已核查，本批全部成功，继续下一批 |
| `1,3` | 只有列表中的第 1、3 张成功，保存结果并暂停 |
| `n` 或直接回车 | 暂不确认，保存尝试记录并暂停 |

> [!NOTE]
> “脚本已操作”不等于“飞书已添加成功”。进度分别记录“已尝试”和“已人工确认”；默认跳过这两类图片，避免反复添加。

下载中断或失败，重新执行相同的 `--export` 命令即可；已成功下载的文件会校验后复用。若有下载失败项，脚本会报错，全部下载成功后才完成本轮分组。

添加中断或失败，先核查飞书，再重试**已尝试但未确认**的图片。手动模式：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --manual-import --yes --retry-attempted
```

自动模式：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --yes --retry-attempted
```

已人工确认的图片仍会跳过。已经添加成功但未确认的图片，重试可能造成重复，请先核对。

自动操作期间可在终端按 `Ctrl+C` 停止，也可将鼠标移到主屏左上角触发中止。

## 已有图片直接分组

已有导出的表情或本地图片时，可以跳过微信导出。

将图片放入用户主目录下的 `WeChat-Feishu-Stickers/exported` **根目录**，再运行分组命令。Windows：

```powershell
py -3 .\migrate.py --group-only
```

macOS：

```bash
python3 migrate.py --group-only
```

这个步骤只使用 Python 标准库，无需安装依赖，也不访问微信。完成后按前面的方式添加到飞书；使用脚本引导添加时仍需安装依赖。

支持整理 `.gif`、`.png`、`.jpg`、`.jpeg`、`.webp`、`.bmp`、`.apng`。这里按文件扩展名分组，不代表飞书一定接受该图片。已有分组保持稳定，新增图片继续追加。

## macOS 使用

macOS 支持本地图片分组、手动按批添加和逐张自动添加。**目前不提供自动获取微信数据库密钥的能力**；没有已有密钥文件时，可先导入本地图片。

在项目目录创建环境并安装依赖：

```bash
python3 -m venv .venv
```

```bash
./.venv/bin/python3 -m pip install -r requirements.txt
```

手动按批添加已有分组图片：

```bash
./.venv/bin/python3 migrate.py --import --manual-import --yes
```

如果已经有本机数据库密钥，可用密钥文件导出。文件应为 UTF-8 文本，仅含 64 位十六进制字符：

```bash
./.venv/bin/python3 migrate.py --export --key-file "/本机私有目录/emoticon-key.txt"
```

自定义数据目录或多个账号同样可使用 `--wechat-data-dir`、`--wxid`。

<details>
<summary>macOS 逐张自动添加</summary>

在系统设置“隐私与安全性 → 辅助功能”中，允许实际运行脚本的终端或编辑器控制电脑。打开飞书个人表情添加界面后校准：

```bash
./.venv/bin/python3 migrate.py --calibrate
```

按提示记录添加按钮位置，再试一张：

```bash
./.venv/bin/python3 migrate.py --import
```

macOS 自动模式逐张选择，每张添加后都需核查。跨系统复制的校准坐标不能使用，需在当前电脑重新校准。

</details>

## 常见问题

| 问题 | 处理方式 |
| --- | --- |
| 找不到 `py`，或找不到虚拟环境的解释器 | 安装 64 位 Python，重新打开 PowerShell；确认位于项目目录，并已完成创建虚拟环境步骤 |
| 没有发现微信账号或表情数据库 | 登录微信并打开收藏面板；用 `--wechat-data-dir` 指定 `xwechat_files` 根目录，不是里面的账号目录 |
| 提示无法确定唯一账号 | 用 `--list-accounts` 查看账号，再通过 `--wxid` 指定完整目录名 |
| 找不到通过验证的数据库密钥 | 确认使用 64 位 Python 和微信，且微信正在运行、已登录并打开收藏面板；仍失败时可能是当前微信结构尚不兼容 |
| 数据库或 WAL 校验失败 | 暂停在微信中新增、删除收藏，等待写入完成后重试；不要修改原数据库 |
| 部分图片下载失败 | 检查网络后重跑同一导出命令；缺少原图地址或资源失效的条目不会被当作成功 |
| 自动添加点错位置或无法激活窗口 | 保持终端和飞书权限一致，窗口与缩放不变，重新校准；仍失败时改用手动模式 |
| 提示前台不是飞书的标准文件选择器 | 当前界面不满足自动输入条件，改用 `--manual-import`；此前已尝试的图片需核查后配合 `--retry-attempted` 重试 |
| 飞书中未添加成功，脚本却跳过了 | 先核查飞书，再使用 `--retry-attempted`；已经人工确认的图片不会被该参数重试 |
| macOS 无法读取微信数据 | 在系统设置中检查实际终端或编辑器的文件访问权限；不要修改微信目录权限 |

反馈问题时，请附上**系统版本、微信与飞书版本、执行的命令和错误文字**。无需提供数据库、密钥或完整下载清单。

## 数据保存在哪里

图片和进度保存在当前用户主目录的 `WeChat-Feishu-Stickers` 中，不放在项目源码目录。Windows 通常为 `C:\Users\用户名\WeChat-Feishu-Stickers`，macOS 为 `~/WeChat-Feishu-Stickers`。

```text
WeChat-Feishu-Stickers/
├── exported/               # 编号分组，每组最多 50 张
├── .wxemoticon-cache/       # 原图缓存，沿用历史目录名
├── download_manifest.json  # 下载记录
├── source_order.json       # 收藏顺序
├── group_manifest.json     # 分组映射
├── config.json             # 添加按钮位置与等待时间
└── state.json              # 添加尝试与人工确认记录
```

保留这些文件即可继续任务，不要通过删除缓存或清单来重试。分组优先使用硬链接，不支持时复制；请勿直接修改分组图片，以免同时改变缓存内容。

脚本读取本机微信收藏数据，导出时会访问图片资源链接，不要求输入微信或飞书账号密码。自动获取的数据库密钥不写入日志或进度文件；下载清单含账号来源和资源链接，不宜随意分享。

## 更多命令

查看完整参数：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --help
```

常用附加参数：

| 参数 | 用途 |
| --- | --- |
| `--limit 10` | 本次最多尝试 10 张，需大于 0 |
| `--batch-size 20` | 每批最多 20 张，可设 1～50；macOS 自动模式仍逐张 |
| `--app-name Lark` | 校准时指定国际版应用名；导入读取已保存的配置 |
| `--all` | 导出后继续添加；自动模式需先校准，默认仍只试 1 张 |
| `--pause-every 5` | 兼容旧命令，将批次进一步缩小到最多 5 张；每批仍需确认 |

## 参考

文件格式研究参考 [微信表情导出项目](https://github.com/liusheng22/export-wechat-emoji) 与 [SQLite WAL 格式文档](https://www.sqlite.org/fileformat2.html#walformat)。本工具独立运行，不调用参考项目的程序、脚本或服务。

实现与验证细节见[设计说明](docs/windows-migration-design.md)。
