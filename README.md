# 微信收藏表情迁移到飞书

独立读取本机微信的收藏表情，下载原图、保留 GIF 动画，按每组最多 50 张整理，再通过飞书桌面端分批添加。**不需要安装或调用 wxemoticon，也不依赖它的缓存。**

Windows 自动导出的主要适配目标为微信最新稳定版。2026-10-30 核对 [微信官网](https://pc.weixin.qq.com/)：当前公开 Windows 64 位版本为 **4.2.0**。实现针对 64 位微信 4.1+ 的 WCDB 结构探测，包含 4.1.15 和 4.2.0 的版本范围；只有候选密钥通过本机数据库校验后才读取收藏。

**验证边界：**当前开发环境为 macOS，已进行合成数据库、模拟 Windows API 和文件选择器的离线验证；尚未在 Windows 微信 4.2.0 / 4.1.15 和飞书实机验收。因此“适配目标”不代表已实测兼容，后续微信更新仍可能改变结构。

## 功能与限制

- 独立发现微信账号，按个人收藏顺序读取元数据，不把全部缓存当成收藏。
- 数据库只读快照、SQLCipher 页校验和解密、已提交 WAL 日志合并；失败时停止并报告。
- 自动从正在运行的 Windows 微信读取候选数据库密钥，仅驻留当前进程内存，不写入日志或清单；不启动、注入、终止或修改微信。
- 图片下载校验实际格式、限制响应大小、失败重试、断点继续、按内容摘要去重；不转换原始动画。
- `exported/1`、`2`、`3`……每组最多 50 张，保持已有分组稳定。
- Windows 自动导入每次明确选择当前批次的文件，最多 50 张。默认只试 1 张，添加后必须人工核查。
- 可以手动按批添加；macOS 保留分组、手动添加和逐张自动添加，独立导出加密数据库需要已有密钥文件。

飞书侧使用客户端个人表情添加入口，不调用非公开 API，也不把普通图片上传 API 当作个人表情收藏接口。添加数量、格式、大小和动画效果仍以客户端实际接受结果为准。

## Windows 安装

建议 Windows 10 / 11、64 位 Python 3.10 或以上，以及已登录的 Windows 微信和飞书。**微信自动取密钥需要 64 位 Python 和 64 位微信。**以下命令在项目目录的 PowerShell 中逐条执行。

创建本机虚拟环境，不要复制 Mac 的 `.venv`：

```powershell
py -3 -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

直接运行虚拟环境的解释器，无需激活环境，也无需修改 PowerShell 执行策略。

### 1. 检测微信版本和账号

先登录微信并打开一次收藏表情面板，再执行：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --probe
```

此命令只报告实际运行程序版本和账号数据库路径，不读取密钥。自定义数据目录需要指定 `xwechat_files` 根目录：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --probe --wechat-data-dir "D:\微信数据\xwechat_files"
```

也可只列出账号：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --list-accounts --wechat-data-dir "D:\微信数据\xwechat_files"
```

有多个账号时，导出必须用 `--wxid` 指定探测结果中的完整账号目录名。

### 2. 导出收藏并分组

```powershell
.\.venv\Scripts\python.exe .\migrate.py --export
```

指定账号和数据目录的例子：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --export --wechat-data-dir "D:\微信数据\xwechat_files" --wxid "wxid_实际账号目录名"
```

读取过程会检测正在运行的微信版本，并验证候选密钥。找不到密钥、版本/架构不受支持或读取权限不足时会明确报错。请先确认微信已登录并打开过收藏面板；不要通过替换微信文件来解决。内存读取有时间与读取量上限，不会无限扫描。

输出在当前用户主目录中：

```powershell
explorer.exe "$HOME\WeChat-Feishu-Stickers\exported"
```

缺少原图地址、链接失效、返回非图片等条目会记录失败。已成功下载的文件保留，重跑 `--export` 重试；全部下载成功后生成收藏顺序并分组，不把部分结果静默宣称为完整迁移。

已有图片可直接放入 `$HOME\WeChat-Feishu-Stickers\exported` 根目录，再运行：

```powershell
py -3 .\migrate.py --group-only
```

只有 `--group-only` 不需要第三方 Python 依赖。

### 3. 添加到飞书

**手动按批添加**不需要校准。脚本打开当前分组目录并列出本批文件，由你在飞书中明确选择这些图片。首次也只试 1 张：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --manual-import
```

确认效果后，每批最多 50 张：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --manual-import --yes --batch-size 50
```

**自动选择整批图片（实验性）**先在飞书打开“表情 → 添加的单个表情”，校准添加按钮：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --calibrate
```

按回车后有 3 秒将鼠标移到「+ 添加」按钮正中，不要点击。随后保持窗口位置、主显示器和缩放比例不变。Lark 可使用 `--calibrate --app-name Lark`，其他标题可显式指定 `--app-name "实际窗口标题"`。

先测试 1 张：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import
```

核查后再按 50 张一批操作：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --yes --batch-size 50
```

自动模式会验证窗口所属进程，打开标准文件选择器，定位分组目录，再粘贴带引号的精确文件名列表。不会在文件列表中使用全选来选入其他文件。自定义选择器、不支持多选、不同快捷键语言、缩放或界面改版可能使操作失败。

**每一批完成后都必须核查飞书，并回终端确认：**

- 输入 `y`：本批全部成功，记录为人工确认。
- 输入 `1,3`：只确认列表中第 1、3 张，其余保持未确认。
- 输入 `n` 或直接回车：不确认成功，停止后续批次。

部分成功或尚未确认时自动暂停。核查后重试：

```powershell
.\.venv\Scripts\python.exe .\migrate.py --import --yes --retry-attempted
```

重试只包含未确认图片；已经人工确认的图片仍跳过。对实际已成功但未确认的图片重试可能造成重复，需先核对。中断时保留“已尝试但未确认”，不会自动标成成功。

紧急停止：`Ctrl+C`，或将鼠标移到主屏左上角。每次终端确认后，下一批都会重新激活飞书。终端和飞书应以相同权限运行，避免 Windows 拒绝操作。

## macOS 使用

已有图片只分组可直接用标准库：

```bash
python3 migrate.py --group-only
```

需要下载解密或自动操作时，安装依赖：

```bash
python3 -m venv .venv
```

```bash
./.venv/bin/python3 -m pip install -r requirements.txt
```

macOS 当前没有自研自动获取密钥。可使用本机已有密钥文件独立导出，不调用第三方提取器；文件内容为 64 位十六进制文本，密钥不要放入命令行或提交到代码仓库：

```bash
./.venv/bin/python3 migrate.py --export --key-file "/本机私有目录/emoticon-key.txt"
```

也支持指定 `--wechat-data-dir`、`--wxid`。如果无法访问微信容器，在系统设置中为实际运行脚本的终端或 IDE 授予所需读取权限。

macOS 手动按批添加：

```bash
./.venv/bin/python3 migrate.py --import --manual-import --yes
```

macOS 自动添加沿用 `Cmd+Shift+G`，目前逐张操作，每张完成后核查。先在系统设置“隐私与安全性 → 辅助功能”允许当前终端或 IDE 控制电脑，再校准：

```bash
./.venv/bin/python3 migrate.py --calibrate
```

```bash
./.venv/bin/python3 migrate.py --import
```

从其他系统复制的坐标不能使用，必须重新校准。若终端 Python 被 ServBay 等软件代理，改用虚拟环境的明确解释器路径。

## 命令与数据

| 参数 | 作用 |
| --- | --- |
| `--probe` | 报告运行中的 Windows 微信实际版本和账号，不读取密钥 |
| `--list-accounts` | 列出包含表情数据库的账号 |
| `--export` | 独立提取、下载并分组 |
| `--group-only` | 整理已有本地图片，无第三方依赖 |
| `--calibrate` | 保存当前系统的飞书添加按钮坐标 |
| `--import` | 添加已有分组图片，默认只试 1 张 |
| `--all` | 导出、分组后添加；自动模式需事先校准 |
| `--manual-import` | 由用户按列表选择图片，无需校准 |
| `--batch-size 50` | 每批最多 50 张，可设 1 至 50 |
| `--limit 10` | 本次最多尝试 10 张 |
| `--retry-attempted` | 重试已尝试但未人工确认的图片 |
| `--pause-every 5` | 兼容旧命令，进一步把批次限制为 5 张；0 表示不缩小批次 |

首次分组遵循下载的收藏顺序；后续保留已有组号，新图片追加。分组文件优先使用硬链接，不支持时复制。旧数据不同文件名仍可能分别占用名额，导入时按内容摘要去重。

```text
~/WeChat-Feishu-Stickers/
├── exported/1/、2/、3/……    # 每组最多 50 张
├── .wxemoticon-cache/       # 原图缓存，沿用历史名称，不依赖该工具
├── download_manifest.json   # 下载状态、来源标识和摘要
├── source_order.json        # 最近一次完整导出的收藏顺序
├── group_manifest.json      # 稳定分组，兼容旧版
├── config.json              # 系统、按钮位置、等待时间
└── state.json               # 已尝试、已人工确认两类记录
```

Windows 主目录通常是 `C:\Users\用户名`。不要删除原图缓存或分组清单来重试；使用对应命令继续即可。下载清单包含本机账号来源和资源链接，分享日志时不要包含该文件。

## 开发与验证

```bash
./.venv/bin/python3 -B -m unittest discover -v
```

测试使用合成数据、图片和模拟 API，不扫描真实微信进程，不操作真实飞书，不调用参考项目。

实现分为 `wechat_source.py`（账号、数据库、收藏）、`windows_wechat.py`（Windows 版本和候选密钥）、`sticker_download.py`（图片与下载状态）、`migrate.py`（分组与导入）。设计说明见 [独立实现设计](docs/windows-migration-design.md)。

Windows 实机验收顺序：先 `--probe`，再 `--export` 核对收藏数量与动画，再导入 1 张，最后导入完整的 50 张批次。若失败，请保留错误文字、实际微信/飞书版本和失败阶段，无需发送数据库或密钥。

## 参考

文件格式与兼容性研究参考 [liusheng22/export-wechat-emoji](https://github.com/liusheng22/export-wechat-emoji) 和 [SQLite WAL 格式](https://www.sqlite.org/fileformat2.html#walformat)，实现不调用参考项目的二进制、脚本或服务。微信版本来源：[官方 Windows 下载页](https://pc.weixin.qq.com/)，核对日期 2026-10-30。
