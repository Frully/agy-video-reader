# Agy Video Reader

`agy-video-reader` 是一个供 Codex 使用的视频理解 Skill。它把本地视频安全地附加到 Google Antigravity CLI（`agy`），让 Codex 可以根据画面和声音完成总结、时间线整理、内容问答和重点提取。

它主要解决两个问题：

- 通过受控的 macOS 剪贴板流程向 `agy` 附加本地视频，并验证附件和结构化分析结果；
- 当视频超过 `agy` 的 50 MiB 单附件限制时，先在本地生成适合分析的压缩副本或重叠分段，再逐段分析和合并结果。

原始视频不会被修改或删除。不过，原视频或本地生成的分析副本会上传到 Google/Antigravity，因此本 Skill **不适用于要求视频字节完全留在本机的任务**。

## 能做什么

- 总结一个本地视频；
- 按时间顺序整理事件和关键节点；
- 同时理解画面与声音；
- 回答关于视频内容的具体问题；
- 处理超过 50 MiB 的视频；
- 校验实际上传文件和返回结果，避免把文件名、终端文字或不完整结果当作视频证据。

目前只正式支持 macOS，以及以下本地视频格式：

- MP4
- MOV
- WebM
- AVI

不负责下载 URL 视频，不读取直播流，也不会在本地通过抽帧、OCR 或单独转录来代替 Antigravity 的视频理解。

## 工作方式

```text
本地视频
  ├─ ≤ 50 MiB：直接上传原视频
  └─ > 50 MiB：本地预处理
       ├─ 单个完整压缩副本能够保持最低分析码率：上传 1 个副本
       └─ 否则：生成带 2 秒重叠的多个压缩分段，逐段上传
                         ↓
              Google Antigravity 分析
                         ↓
              校验、校准时间戳并合并结果
```

每个上传文件的硬限制是 50 MiB。生成文件以 47 MiB 为目标，为封装波动预留空间。分段最多 24 个；如果需要更多分段，Skill 会停止而不是牺牲到无法接受的分析码率。

## 依赖

### 必需依赖

| 依赖 | 要求 | 用途 |
| --- | --- | --- |
| macOS | 当前正式支持的平台 | 使用经过验证的 macOS 文件附件和剪贴板流程 |
| Codex | 支持本地 Skills | 识别并执行 `agy-video-reader` |
| Python | 3.10 或更高版本 | 运行预处理器、控制器和校验逻辑 |
| Antigravity CLI | **必须为 `agy 1.1.1`** | 实际的视频与音频理解后端 |
| Google/Antigravity 账号 | 已登录并可使用目标模型 | 运行 Antigravity 分析 |
| `swiftc` | Xcode Command Line Tools 提供 | 构建 macOS 剪贴板附件桥接程序 |

控制器固定使用 `Gemini 3.5 Flash (High)`，并且会拒绝未经兼容性验证的 `agy` 版本。`agy` 可能自动更新；如果 `agy --version` 不再是 `1.1.1`，Skill 会安全停止，需要先完成新版本兼容验证再升级本项目。

### 大文件依赖

视频超过 50 MiB 时，还必须安装：

- `ffmpeg`
- `ffprobe`
- `libx264` 编码器
- AAC 编码器

50 MiB 以内的视频不需要 FFmpeg。

## 安装依赖

### 1. 安装 Antigravity CLI

Google 官方的 macOS/Linux 安装命令是：

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

默认安装位置是 `~/.local/bin/agy`。确认该目录在 `PATH` 中：

```bash
export PATH="$HOME/.local/bin:$PATH"
agy --version
```

本 Skill 当前要求输出为：

```text
1.1.1
```

官方文档：[Antigravity CLI Getting Started](https://antigravity.google/docs/cli-getting-started)

### 2. 完成首次信任和登录

`agy` 的项目授权与当前工作目录有关。本 Skill 始终使用一个固定工作目录；请在第一次分析前手动完成一次信任和登录：

```bash
mkdir -p "$HOME/Library/Caches/agy-video-reader/workspace"
cd "$HOME/Library/Caches/agy-video-reader/workspace"
agy
```

在 TUI 中信任这个目录、完成 Google 登录，确认可以正常进入后退出。不要改成每次随机使用 `/tmp` 目录，否则 `agy` 可能反复要求项目授权。

可以再运行一次预检：

```bash
agy models
```

### 3. 安装 Xcode Command Line Tools

如果系统中没有 `swiftc`：

```bash
xcode-select --install
```

安装后验证：

```bash
swiftc --version
```

### 4. 安装 FFmpeg（处理大文件时需要）

使用 Homebrew：

```bash
brew install ffmpeg
```

验证所需工具和编码器：

```bash
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -encoders | grep -E 'libx264|aac'
```

## 安装 Skill

克隆到 Codex 的 Skills 目录：

```bash
mkdir -p "$HOME/.codex/skills"
git clone https://github.com/Frully/agy-video-reader.git \
  "$HOME/.codex/skills/agy-video-reader"
```

如果已经安装，可以更新：

```bash
git -C "$HOME/.codex/skills/agy-video-reader" pull --ff-only
```

然后重新打开 Codex，或新建一个任务，使 Skill 列表重新加载。Skill 名称必须保持为 `agy-video-reader`。

## 使用方法

在 Codex 中提供一个本地视频的绝对路径，并明确调用 `$agy-video-reader`：

```text
使用 $agy-video-reader 总结 /Users/me/Videos/demo.mp4，按时间线列出关键内容。
```

也可以提出具体问题：

```text
使用 $agy-video-reader 阅读 /Users/me/Videos/meeting.mov。
重点回答：演示了哪些功能、每个功能出现在哪个时间点、有哪些不确定信息？
```

```text
使用 $agy-video-reader 分析 /Users/me/Videos/tutorial.webm，输出中文操作步骤。
```

Skill 会自动：

1. 检查文件类型、大小和依赖；
2. 决定上传原视频、一个完整压缩副本，还是多个压缩分段；
3. 在上传前说明哪些文件会离开本机，以及可能产生的历史记录和额度消耗；
4. 多分段时告知确切数量，并在首次上传前等待确认；
5. 逐个运行 Antigravity、校验结果、调整分段时间戳并去除重叠内容；
6. 清理临时副本和结果，保留原视频不变。

通常不应手工调用 `scripts/run_antigravity_video.py`。`SKILL.md` 包含完整的安全顺序、校验要求和清理约束，让 Codex 负责整个流程。

## 大文件与分析质量

超过 50 MiB 时，预处理器会生成 H.264/AAC MP4：

- 最大分辨率为 1280×720；
- 最大帧率为 30 fps；
- 音频使用 AAC 96 kbps；
- 优先保留完整时长；
- 单文件码率过低时改为带 2 秒重叠的分段；
- 原文件始终留在本地且保持不变。

这种策略适合一般实拍、访谈、课程和演示视频，但不能保证无损。小字号文字、屏幕录制、快速运动、HDR、噪声较大的音频或非常细微的画面变化可能受到压缩影响。Skill 会把预处理产生的质量警告展示给用户，不会把压缩结果描述为无损分析。

如果素材特别依赖小字或细节，优先提供较短的原始片段，使每个片段低于 50 MiB；不要为了绕过限制而无限降低码率。

## 隐私、上传与费用

- 原视频或准备后的代理视频会发送到 Google/Antigravity；
- 每次运行可能创建 Antigravity 会话历史并消耗额度；
- 重新运行会再次上传，并可能再次计费；
- 多分段视频会产生多次上传，所以必须先确认分段数量；
- 本地 FFmpeg 预处理不代表整个分析过程都在本地；
- 附件成功也不代表逐帧、无损或完整理解，时间戳和置信度仍是近似值。

macOS 的系统剪贴板是全局资源。控制器会备份、使用并立即恢复剪贴板，但其他应用在同一时刻改写剪贴板时仍可能发生竞争；检测到恢复风险时，Skill 会停止并保留恢复所需信息。

## 常见问题

### 每次都要求授权或信任项目

不要把 `agy` 的工作目录切换到随机临时目录。按“完成首次信任和登录”一节，在下面这个固定目录手动完成一次设置：

```text
~/Library/Caches/agy-video-reader/workspace
```

### 提示 `AGY_VERSION_UNSUPPORTED`

运行：

```bash
agy --version
```

当前只验证了 `1.1.1`。不要直接放宽版本检查；不同版本的 TUI 文本、附件确认和授权流程可能变化。

### 提示 `AGY_SETUP_REQUIRED` 或 `AGY_AUTH_REQUIRED`

进入固定工作目录手动运行 `agy`，完成项目信任和 Google 登录，然后退出并重试。

### 提示 `FFMPEG_NOT_FOUND`

这只会影响超过 50 MiB 的视频。安装包含 `ffmpeg`、`ffprobe`、`libx264` 和 AAC 编码器的 FFmpeg 发行版。

### 视频仍提示 `VIDEO_TOO_LARGE`

预处理后的每个附件都应该小于 50 MiB。如果仍出现此错误，说明后端限制或本地契约可能发生变化。停止运行并检查日志，不要临时再次压缩或绕过校验。

### 分段数量过多

Skill 最多生成 24 个分段。超出时应提供更短的源视频，或在明确知道内容边界的前提下先由用户自行拆分素材。

## 开发与测试

安装测试依赖并运行：

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

真实 Antigravity 端到端测试会上传视频并可能消耗额度，因此默认测试不会执行真实上传。详细实现契约位于：

- [`SKILL.md`](SKILL.md)
- [`references/media-preparation-contract.md`](references/media-preparation-contract.md)
- [`references/antigravity-tui-contract.md`](references/antigravity-tui-contract.md)
- [`references/output-schema.json`](references/output-schema.json)
