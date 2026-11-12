# AGENTS.md — 部署契约

你是一个 agent。用户把这个仓库（URL 或本地目录）交给你，要你把「视频蒸馏专家」部署到当前机器上。

**读完这一页你就能完成部署。** 不需要读源码，不需要问用户技术细节。本文只写"做什么"和"怎么判断成功"。

---

## 0. 一句话

```bash
python bootstrap.py --host <host> --json
```

`<host>` 取 `workbuddy`（默认）、`claude`、`cursor`、`generic` 之一。

在仓库根目录执行。Windows 用 `python`，macOS/Linux 若 `python` 不存在则 `python3`。

---

## 1. 先判断宿主，再执行

| 你在哪 | `--host` |
|---|---|
| WorkBuddy / 本工作台 | `workbuddy` |
| Claude Desktop | `claude` |
| Cursor | `cursor` |
| 其他 MCP 宿主、或不确定 | `generic` |

不确定时用 `generic`：它**不写任何配置文件**，只打印一段 JSON，你把这段交给用户或自己放进宿主的 MCP 配置。这比猜错路径安全。

---

## 2. 成功判据（三条全中才算完成）

1. 输出里出现 `ok   v2n <版本> imports cleanly`
2. 输出里出现 `ok   registered` 或 `ok   already registered, unchanged`（`generic` 宿主则是打印了 JSON 块）
3. 输出里出现 `ok   <7 个工具名>`，即握手真的成功了

最后一行是 JSON（因为有 `--json`），`"ok": true` 且 `"tools"` 数组长度为 7。

**"安装完成"不等于"能用"。** 第 3 条才是能用的证据，不要跳过。

---

## 3. 按退出码分支

| 退出码 | 含义 | 你该做什么 |
|---|---|---|
| `0` | 部署成功 | 向用户报告：7 个工具已就绪，需重启宿主，然后可以开始蒸馏 |
| `2` | 环境不满足 | 通常是 Python < 3.10。告诉用户当前版本与所需版本，请其安装后重试。**不要**尝试自己装 Python |
| `3` | 安装失败 | 看 `warn` / `FAIL` 行。多为网络问题（pip 无法访问 PyPI）。建议用户配置 `pip` 镜像或代理后重跑；也可用 `--wheels` 指定本地 wheel 目录 |
| `4` | 装好了但握手失败 | 这是真 bug，不要重试。把完整输出交给用户，并说明宿主启动该 server 时也会同样失败 |

重跑是安全的（幂等）：已存在的 venv 复用，配置内容相同则不重写，也不会堆出多个 `.bak`。

---

## 4. 部署后立刻做的事

### 4.1 让用户重启宿主

宿主只在启动时读取 MCP 配置。不重启，`video-distill-expert` 的工具不会出现。这一步必须由用户做，你无法代替。

### 4.2 自己验证一遍（不需要用户在场）

```bash
python demo.py --work /tmp/vde-demo
```

它会在临时目录生成一份示例转写，跑完 `prepare → submit → finalize` 全流程，并打印产物路径。看到 `gate PASS` 与三个 `.md` 文件即通过。

这一步不需要网络、不需要语音模型、不需要 API key。**如果它失败，说明部署有问题，不是用户操作有问题。**

### 4.3 告诉用户怎么用

用户下次只需要说"把这段课蒸馏成笔记"，你把 `lesson` 目录指向含 `transcript.txt` 的文件夹即可。完整蒸馏规则见 `skill/SKILL.md`。

---

## 5. 不要做的事

1. **不要装到系统 Python 或用户的全局环境。** 部署器自带 venv（默认 `~/.video-distill-expert/venv`），这是刻意的隔离。
2. **不要装 `[asr]` / `[media]` 额外依赖。** 默认部署已经能完成蒸馏。只有用户明确要求"从视频/音频直接转写"时才需要，且要提前说明它会下载数百 MB 的语音模型。
3. **不要修改 MCP 配置里其他 server 的条目。** 部署器只增改自己那一条，已有条目原样保留。
4. **不要用 `rm -rf` 清理或"重装"。** 真要重来，用 `--root` 换个目录，或让用户删掉 `~/.video-distill-expert`。
5. **不要把这个仓库的上游当依赖。** 引擎已内联在 `engine/`，部署不需要任何仓库访问凭据。
6. **不要向用户索要 API key。** 这个工具的设计前提就是"智能由你（宿主模型）提供"，不需要额外密钥。

---

## 6. 关键路径速查

| 项 | 路径 |
|---|---|
| 引擎源码 | `engine/v2n/`（35 个文件，来自上游 `27acc29`） |
| 部署器 | `bootstrap.py`（仅标准库） |
| 蒸馏规则 | `skill/SKILL.md` |
| 自检演示 | `demo.py` |
| 引擎清单 | `engine/MANIFEST.json`（逐文件 sha256） |
| 默认安装位置 | `~/.video-distill-expert/` |
| 默认 MCP 配置 | `~/.workbuddy/mcp.json`（WorkBuddy） |

---

## 7. 诊断命令

```bash
python bootstrap.py --dry-run          # 只打印计划，不动任何文件
python scripts/manifest.py verify      # 引擎是否被改动过（exit 1 = 有漂移）
python demo.py                         # 端到端自检
~/.video-distill-expert/venv/bin/python -m v2n.mcp_server   # 手工起服务，Ctrl-C 退出
```

Windows 上最后一条的路径是 `~/.video-distill-expert/venv/Scripts/python.exe`。
