# 视频蒸馏专家

把一门课的转写，变成**每条都能回链到原文**的学习笔记。

这个名字里的"专家"指的是：**你不带模型来，把模型交给宿主**。引擎负责清洗、切块、校验和排版；判断哪句话是重点这件事，交给正在读这段话的 agent（WorkBuddy、Claude、Cursor，或任何支持 MCP 的宿主）。

---

## 它解决什么

市面上的"AI 笔记"通常有两个毛病：读起来很顺，但你找不到它凭哪句话这么说；或者换个模型，产物质量就整体漂移。

这个项目把两件事分开：

| 谁 | 负责 |
|---|---|
| 引擎 | 切块、校验、渲染、断点续跑、拒绝不合格产物 |
| 你（宿主模型） | 判断重点、组织主题、写命题 |

引擎不相信模型写的内容，只相信**能从转写里找到出处**的内容。一条命题如果既不是原文子串，与之的字符二元组重合度又低于 50%，就会被直接丢弃并计数——不软化、不改写、不"大概意思对就行"。

所以换个模型只会影响笔记写得漂不漂亮，不会影响它可不可信。

---

## 部署

**不需要 API key，不需要 ffmpeg，不需要语音模型，不需要访问任何私有仓库。**

```bash
git clone https://github.com/Current-off/video-distill-expert.git
cd video-distill-expert
python bootstrap.py
```

`bootstrap.py` 做四件事，然后自证：建独立 venv → 装引擎 → 写 MCP 配置（保留你已有的其他 server，改前先备份，原子写）→ 装 skill 文件 → **真的握手一次**，打印宿主将会看到的 7 个工具。

默认目标是 WorkBuddy（`~/.workbuddy/mcp.json`）。换宿主：

```bash
python bootstrap.py --host claude     # Claude Desktop
python bootstrap.py --host cursor     # Cursor
python bootstrap.py --host generic    # 不写文件，只打印 JSON 片段
```

先看它打算做什么，不落盘：

```bash
python bootstrap.py --dry-run
```

装完**重启宿主**，然后让它跑一次 `v2n_doctor`。看到 7 个工具就是好了。

### 部署完先自检

```bash
python demo.py
```

它用内置示例转写跑完整的 `prepare → submit → finalize`，打印 `gate PASS` 和三个 `.md` 产物。这一步失败说明部署有问题，不是你的操作有问题。

---

## 用起来是什么样

你把一个含 `transcript.txt` 的目录指给 agent，说"把这段课蒸馏成笔记"，它会：

1. `v2n_prepare` —— 把转写切成工单（默认 20 段一块，切法是确定的，重跑不变）
2. `v2n_get_chunk` —— 逐块取原文和指令
3. `v2n_submit` —— 逐块交命题，每条引用一个段号
4. `v2n_finalize` —— 汇总、过质量闸门、渲染三种格式

产物：`outline.md`（提纲）、`timeline.md`（时间轴）、`cheatsheet.md`（速查表）。

中途断了不用重来：工单和结果都在 `<lesson>/_agent/`，重跑只补没交的块。重复提交同一块是**替换**而不是追加，所以工具调用被重试也不会让命题翻倍。

### 没有 `transcript.txt` 怎么办

转写需要语音模型，不在默认部署里。两种情况：

- 你已经有转写（B 站字幕、会议纪要、别人给的稿子）→ 整理成这个格式即可：

  ```
  [00:12-00:35] 文本内容
  [01:20-01:45] 文本内容
  ```

- 你要从视频/音频直接来 → 需要 `pip install -e "engine[asr,media]"`，它会拉 `faster-whisper`（数百 MB）和 ffmpeg 二进制。这一步要你自己决定，部署脚本不会擅自装。

---

## 三条硬约束

这三条不是配置项，是设计前提。改不了，也不提供开关。

1. **不碰鉴权。** 只处理你已持有的文件或你已获授权的地址。工具里没有课程目录、没有账号、没有凭据，也**不做资产发现**。想"把这个平台的课全下下来"的请求，会被明确拒绝。
2. **蒸馏是旁路。** 关掉蒸馏，`notes.md` 字节不变。这条有回归测试守着，不是口头承诺。
3. **不合格产物宁可不出，而且是真的不出。** 覆盖率不足、拒绝率过高、还有块没交——一律 `gate FAIL` 并说明原因。闸门在渲染**之前**执行：判 FAIL 就不写任何 `.md`，上一轮留下的同名文件也会被删掉，只留 `distill.json` 供排查。这是被独立质检打出来的补丁——在此之前，6% 覆盖率的提纲仍会写到 `outline.md`，只有返回值里写着 FAIL。

---

## 目录结构

```
video-distill-expert/
├── bootstrap.py            一键部署（仅标准库，幂等，可 dry-run）
├── demo.py                 端到端自检，不需要网络和模型
├── AGENTS.md               给 agent 的部署契约（成功判据、退出码分支、禁止事项）
├── engine/                 引擎源码（内联，部署不依赖上游私有仓库）
│   ├── v2n/                MCP server + 工单桥接 + 蒸馏 + 渲染
│   ├── pyproject.toml
│   └── MANIFEST.json       上游 commit 与逐文件 sha256
├── skill/SKILL.md          蒸馏规则：什么时候用、怎么切块、质量红线
├── examples/               示例转写
└── scripts/manifest.py     清单校验与上游同步
```

`engine/` 是上游 [video2notes](https://github.com/Current-off/video2notes) 的**逐字副本**，来源 commit 记在 `MANIFEST.json` 里，`python scripts/manifest.py verify` 可以逐文件校验。内联而不是依赖，是因为上游是私有仓库——要"给个链接就能装"，就不能要求对方的 agent 拿到我的凭据。

---

## 命令速查

```bash
python bootstrap.py --dry-run              # 看计划，不动文件
python bootstrap.py --host cursor          # 换宿主部署
python demo.py --keep                      # 自检并保留产物目录
python scripts/manifest.py verify          # 引擎是否被改过（exit 1 = 漂移）
python scripts/manifest.py sync --from ../video2notes   # 从上游同步引擎
```

部署后引擎也能单独用（venv 在 `~/.video-distill-expert/venv`）：

```bash
~/.video-distill-expert/venv/bin/v2n agent status --lesson demo_out
```

卸载：删掉 `~/.video-distill-expert`，再从 MCP 配置里移除 `video-distill-expert` 那一条。不会留别的东西。

---

## 常见问题

**宿主里看不到工具？** 必须重启宿主。MCP 配置只在启动时读一次。

**`bootstrap.py` 报 exit 3？** 多半是 pip 连不上 PyPI。配好镜像或代理后重跑即可，重跑是幂等的。离线环境用 `pip download PyYAML requests -d wheels/`，再 `--wheels wheels/`。

**和 video2notes 冲突吗？** 不冲突。部署走独立 venv，两条命令（`v2n` / `v2n-mcp`）在 venv 内部解析，不污染全局环境。但如果你在同一个 Python 里同时 `pip install` 两个包，它们会争同一个 `v2n` 模块名——别那么做。

**为什么 serverInfo 显示 `video2notes`？** 那是引擎的协议标识，故意保持与上游一致以便校验。宿主里你看到的 server 名是 `video-distill-expert`。

**支持 Windows 吗？** 是。CI 在 ubuntu / windows / macOS × Python 3.10 / 3.13 上跑完整的部署加自检。

---

## License

MIT
