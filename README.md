# Agy Video Reader

`agy-video-reader` 是一个通用的视频理解 Agent Skill。它使用 Google Antigravity CLI（`agy`）读取本地视频，可完成内容总结、时间线整理、画面与声音理解，以及针对视频内容的问答。

支持 macOS 上的 MP4、MOV、WebM 和 AVI 文件。

## 让 AI 安装

把下面这句话发给能够访问网络、执行命令和安装 Agent Skills 的 AI：

```text
请安装并配置这个 Agent Skill：https://github.com/Frully/agy-video-reader

读取仓库中的 SKILL.md，将它安装到你的 Skills 搜索目录，检查并补齐依赖，然后运行验证。只有在 agy 登录或工作区信任必须由我交互时再提示我。
```

安装 AI 应该自动完成以下工作：

1. 克隆仓库到当前宿主的 Agent Skills 目录，并保持目录名为 `agy-video-reader`；
2. 阅读 `SKILL.md`，按其中的约束检查运行环境；
3. 检查 Python、`agy`、`swiftc`，以及大文件所需的 FFmpeg；
4. 建立稳定的 Antigravity 工作区和私有临时目录；
5. 运行 Skill 校验与测试，并让宿主重新加载 Skills；
6. 仅在 Google 登录或 Antigravity 工作区信任需要人工交互时请求用户操作。

不同 Agent 宿主的 Skills 目录并不相同，因此不应让用户手工猜目录。安装 AI 应先读取宿主配置或文档，再选择正确位置。

## 依赖

- macOS；
- Python 3.10 或更高版本；
- Google Antigravity CLI `agy 1.1.1`，且账号可正常使用；
- Xcode Command Line Tools 提供的 `swiftc`；
- 视频超过 50 MiB 时，需要带 `libx264` 和 AAC 编码器的 `ffmpeg`、`ffprobe`。

`agy` 的登录和工作区信任是交互式操作，AI 可以准备环境并打开流程，但可能仍需要用户亲自确认。当前 Skill 只验证了 `agy 1.1.1`；版本不匹配时会停止，而不会冒险绕过兼容性检查。

## 使用

在对话中指定 Skill、视频绝对路径和分析目标：

```text
使用 agy-video-reader 分析 /Users/me/Videos/demo.mp4，按时间线总结关键内容。
```

也可以提出具体问题：

```text
使用 agy-video-reader 阅读 /Users/me/Videos/meeting.mov。
重点回答：演示了哪些功能、分别出现在哪个时间点、有哪些不确定信息？
```

明确提出视频分析请求后，Skill 会直接完成必要的本地准备、上传和分析。它会告知上传内容、分段数量、质量影响和可能的额度消耗，但不会为压缩、分段、并发或正常的 Google/Antigravity 上传再次请求确认。只有缺少文件、目标不明确、宿主权限受阻，或 `agy` 登录和工作区信任必须人工处理时才会暂停。

## 大文件处理

- 不超过 50 MiB：直接上传原视频；
- 超过 50 MiB：先生成最高 480p、H.264/AAC 的均衡压缩副本；
- 一个副本无法在质量下限内满足限制时，生成尽可能少、固定重叠 5 秒的分段；
- 分段最多使用 5 路并发分析，最后统一校准时间戳并合并结果；
- 原视频不会被修改或删除。

压缩可能降低小字、快速运动和细微画面的识别质量。Skill 会保留完整时长并控制最低分析码率，不会把压缩结果描述为无损。

## 隐私与费用

原视频或本地生成的分析副本会上传到 Google/Antigravity，可能产生会话历史并消耗额度。多分段会产生多次上传，重新运行也可能再次计费。因此，本 Skill 不适用于要求视频内容完全留在本机的任务。

完整的执行、安全和结果校验契约见 [`SKILL.md`](SKILL.md)。

## 手动安装（备用）

只有在 AI 无法自行安装时，才需要把仓库克隆到所用宿主的 Skills 目录：

```bash
git clone https://github.com/Frully/agy-video-reader.git \
  /your/agent/skills/agy-video-reader
```

随后让宿主重新加载 Skills，并让 AI 按 `SKILL.md` 检查依赖和验证安装。
