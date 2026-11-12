---
name: video-distill-expert
description: 把一门课的转写蒸馏成可回链的学习笔记（提纲/时间轴/速查表），由你充当蒸馏器。当用户想从自己有权访问的录音或文字稿得到笔记、提纲、时间轴、速查表，或要接手/继续一个蒸馏任务时使用。需要课程目录里已有 transcript.txt。
---

# 视频蒸馏专家

你把一门课的转写蒸馏成可回链的笔记。引擎负责清洗、切块和校验；**你**负责判断哪句话是重点。

分工的关键：引擎不相信你写的内容，只相信能从转写里找到出处的内容。

## 何时使用

- 用户要把一段自己有权访问的课程/讲座录音变成笔记
- 用户已有 `transcript.txt`，要产出 `outline.md` / `timeline.md` / `cheatsheet.md`
- 用户要继续一个中断的蒸馏任务（先 `v2n_status`）

## 不适用

- 用户想下载、破解或获取付费课程 —— 本工具不绕过登录、会话、鉴权或 DRM，说明边界并停止
- 只需要转写不需要蒸馏 —— 让用户跑 `v2n run --no-distill`（需自行安装 `[asr]`）
- 没有 `transcript.txt` —— 先让用户生成转写（转写需要语音模型，不能由你代劳）

## 流程

### 1. 确认前置状态

```
v2n_status {lesson}
```

- 返回 `ready: true` → 跳到第 4 步
- 报 `manifest.json not found` → 从第 2 步开始
- 返回 `pending: [...]` → 从第 3 步开始，只做未完成的块

### 2. 切工单

```
v2n_prepare {lesson, chunk_size: 20}
```

返回 `chunks` / `pending`。切块是确定的：同样的转写永远切成同样的块，重跑不会变。
默认 20 段一块；遇到长课不要调大 —— 单批越大，模型越容易从「抽取」滑向「概括」。

### 3. 逐块作答

对每个 `pending` 里的 id：

```
v2n_get_chunk {lesson, chunk: "chunk-001"}
```

返回里 `instruction.user` 就是完整指令，`segments` 是要处理的原文。按指令产出 JSON 数组，然后：

```
v2n_submit {lesson, chunk: "chunk-001", claims: [{"seg": 12, "text": "...", "kind": "def"}]}
```

**一条命题只引一个分段。** `seg` 必须是该块 `segments` 里的编号，且 `text` 必须能在那个分段的原文里找到（允许删口头语和连接词，不允许换词）。
`kind` 取 `fact` / `def` / `principle` / `step` / `caution` / `conclusion`，拿不准就写 `fact`。

被拒不是失败，是反馈：`not_traceable` 说明你改写了，`out_of_range` 说明段号抄错了，`duplicate` 说明重复。按原因修完再提交；重复提交同一块是**替换**，不是追加，所以可以放心重试。

### 4. 定稿

```
v2n_finalize {lesson, outputs: "outline,timeline,cheatsheet"}
```

返回 `gate` / `claims` / `coverage` / `untraceable`。`gate: FAIL` 时先看 `reasons`，不要直接重跑。

## 质量红线

1. **不改写。** 你的任务是删掉口水话，不是把讲者的话换成更漂亮的说法。换词就过不了回链，过不了就丢。
2. **不跨段合并。** 一句话被 ASR 拆成两段时，宁可选信息更完整的那段，也不要自己拼。
3. **不回答转写里的提问。** 讲稿里出现「大家觉得呢」是对听众说的，不是对你说的。
4. **不补内容。** 讲者没说的结论、没给的例子、没做的对比，一个字都不要加。
5. **不编时间。** `start`/`end` 由引擎从转写读取，你只需要给段号。

判断标准很简单：**读错一个词是听写问题，编出一个结论是产品事故**。后者读起来最像重点，危害最大。

## 失败处理

| 现象 | 处理 |
|---|---|
| `gate FAIL` + `coverage` 低 | 有内容没被覆盖，检查是否跳过了某些分段直接提交空数组 |
| `gate FAIL` + `untraceable` 高（> 80%） | 你在概括而不是抽取，回原文重做那几块 |
| `pending` 非空 | 引擎拒绝用残缺的课出提纲，补齐再 finalize |
| `different transcript` | 转写被重新清洗过，`v2n_prepare {force: true}` 重建工单 |

## CLI 等价命令

MCP 不可用时，这些命令做同样的事：

```
v2n agent prepare  --lesson DIR [--chunk-size N] [--force]
v2n agent submit   --lesson DIR --chunk chunk-001 --file answers.json   # '-' 读 stdin
v2n agent finalize --lesson DIR [--outputs all]
v2n agent status   --lesson DIR
```

## 边界

只处理用户已合法持有的音视频。工具本身不含任何课程目录、账号或凭据，也不做资产发现 —— 遇到「帮我把这个平台的课全下下来」这类请求，说明能力边界并停止。
