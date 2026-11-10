﻿# ZYAgent —— core 与 UI 分离的 Function Calling Agent

基于 `practice02/chat_files.py` 的工程化重构版：**业务核心（core）与界面（tui/cli/…）完全分离**，
核心零 UI 依赖、零第三方依赖（仅标准库）；界面层使用 [Textual](https://github.com/Textualize/textual)
开发 TUI，并为 Web / Desktop / Mobile 前端预留了同级目录与标准接入方式。

```
┌────────────┐  ┌─────┐  ┌─────────┐  ┌─────┐  ┌────────┐
│ tui(Textual)│  │ cli │  │ gui(Qt) │  │ web │  │ mobile │   ← 前端层（可任意扩展）
└─────┬──────┘  └──┬──┘  └────┬────┘  └──┬──┘  └───┬────┘
      │            │          │          │           │        只允许向下依赖
      ▼            ▼          ▼          ▼           ▼
   ┌─────────────────────────────────────────────────────┐
   │  core/  AgentEngine.ask() → AgentEvent 事件流（唯一契约）│
   └─────────────────────────────────────────────────────┘
```

## 特性

- **代码隔离**：`core/` 不 import 任何前端；前端只消费 `AgentEngine.ask()` 的事件流
- **分层清晰**：基础层（配置/事件）→ 资源层（LLM 客户端/工具）→ 领域层（会话）→ 编排层（引擎）
- **真流式**：SSE 增量输出 + 工具调用分片合并，TUI 逐字渲染、可随时停止
- **错误即数据**：工具/网络错误转成文本交由模型自我纠正，绝不崩溃界面
- **联网搜索**：`searchWeb` 工具经百度千帆「百度AI搜索」检索实时信息（摘要+标题+URL），
  Key 只从 `config.ini` 读取；缺配置/断网/API 报错都会回传清晰的错误文本给模型
- **网页阅读**：`fetchWeb` 工具用 subprocess 调本地 curl 抓页面（参数列表、无 shell=true），
  trafilatura 提取正文转 Markdown——保留标题/段落/列表/表格/链接，去除导航/广告/页脚省 token
- **MCP 接入**：读取当前项目目录 `.mcp.json`，按 MCP Python SDK 标准接口**异步**连接外部
  MCP Server（stdio / Streamable HTTP）——先进入 TUI，加载完成后动态刷新侧栏 MCP 区域；
  `list_tools()` 发现的工具自动并入注册表，与本地工具同等参与 Function Calling；
  名称冲突自动加服务器前缀；单 Server 连接失败只影响自身工具，`/mcp` 命令可查看各 Server 状态
- **会话持久化**：`/save`、`/new`（自动保存旧会话）、`/resume`（↑↓ 面板恢复）、`/sessions`、
  `/export`；每轮结束自动保存到 `.agents/sessions/<编号>.jsonl`（快照式事件日志，原子写入）
- **斜杠命令补全**：输入 `/` 弹出引导面板，↑↓ 选择、Tab 补全、Enter 直接执行
- **AGENTS.md**：首次加载会话时把项目根 `AGENTS.md` 作为一条消息注入聊天历史并持久化，
  之后全程复用（压缩后仍保留），保证 Prompt Cache 稳定
- **配置自举**：当前目录没有 `config.ini` 时自动生成完整 mock 模板并弹窗提示修改后重启；
  上下文阈值、检索长度、MCP 超时、TUI 行为、会话保存等参数全部来自 `config.ini`
- **上下文压缩**：`/compact` 手动触发——先把原始上下文完整归档到
  `.agents/context/<编号>.jsonl`，再让 LLM 生成约 5k tokens 的工作摘要替换历史
  （保留 System Prompt 与工具定义）；占用达到模型窗口 80% 时警告并暂停普通提问，
  压缩成功后自动恢复
- **历史检索**：`recallContext` 工具在归档中做本地全文关键词检索（无向量库/Embedding），
  返回片段 + 归档编号 + 消息位置，供模型补齐摘要中缺失的细节
- **可测试**：core 与 TUI 均有离线测试（假客户端，不联网）

## 目录结构

```
ZYAgent/
├── main.py                # 入口薄壳：配置 → 引擎 → 派发前端（--ui tui|cli）
├── config.ini             # 运行配置（含 API Key，勿入库）
├── config.example.ini     # 配置模板
├── requirements.txt       # textual（TUI）+ trafilatura（fetchWeb 网页正文提取）+ mcp（MCP Client）
├── .agents/skills/        # ★ Agent Skills：web-research / git-helper 样例（SKILL.md + 附加文件）
├── .agents/context/       # ★ 上下文归档（/compact 生成的 <编号>.jsonl，供 recallContext 检索）
├── .agents/sessions/      # ★ 会话事件日志（/save、/new 自动保存的 <编号>.jsonl）
├── AGENTS.md              # ★ 可选：项目说明，首次加载注入会话并全程复用
├── .mcp.json              # ★ MCP Server 配置（mcpServers，读取自当前项目目录）
│
├── core/                  # ★ 核心业务层（UI 无关，可被任意前端复用）
│   ├── __init__.py        #   公共 API + create_engine() 工厂
│   ├── config.py          #   基础层：LLMConfig / ContextConfig / MCPConfig / UIConfig / 配置自举
│   ├── events.py          #   基础层：AgentEvent 事件契约（core→UI 唯一协议）
│   ├── session.py         #   领域层：ConversationSession 消息历史
│   ├── engine.py          #   编排层：AgentEngine 主循环 + 压缩 + 会话管理 + AGENTS.md 注入
│   ├── context.py         #   资源层：Token 估算 + 原始上下文归档 + 全文检索 + 压缩摘要
│   ├── history.py         #   资源层：会话事件日志（保存 / 列表 / 恢复 / 导出）
│   ├── skills.py          #   资源层：Skill 发现（.agents/skills）+ Frontmatter 解析 + Prompt 注入
│   ├── mcp_client.py      #   资源层：MCP Client（.mcp.json 解析 + 异步连接/发现/路由）
│   ├── llm/               #   资源层：LLMClient 端口 + OpenAI 兼容适配器（SSE 流式）
│   └── tools/             #   资源层：ToolSpec/ToolRegistry + 18 个工具（search/fetch/runcli/skill/recall）
│
├── tui/                   # ★ Textual 终端前端（本次交付）
│   ├── app.py             #   应用、命令补全、会话面板、事件渲染、MCP 侧栏
│   ├── widgets.py         #   消息气泡 / 工具卡片
│   ├── styles.tcss        #   Textual CSS
│   └── __main__.py        #   python -m tui
│
├── cli/                   # 控制台前端（保留原 chat_files.py 体验，兼作参考实现）
├── gui.py                 # ★ PySide6 图形界面（默认前端，浅色/深色主题切换）
├── desktop/ · web/ · mobile/   # 未来前端（占位 + 本目录 README 有接入指南）
├── tests/                 # 离线测试（core 24 项 + tui 13 项）+ TUI 截图 + MCP/search/fetch 联调脚本
└── docs/                  # 架构图（Mermaid）
```

## 快速开始

```bash
pip install -r requirements.txt        # textual（TUI）+ trafilatura（fetchWeb）+ mcp（MCP Client）+ PySide6（GUI）

python main.py                          # 启动图形界面（默认，浅色主题，可切深色）
python main.py 当前目录下有哪些文件        # 单次提问，控制台输出后退出
python main.py --ui tui                 # 终端 TUI 界面
python main.py --ui cli                 # 交互式控制台（/help /tools /reset /quit）
python -m tui                           # 等价于启动 TUI
```

**首次运行**：当前目录没有 `config.ini` 时，程序会自动生成一份带 mock 信息的完整模板，
并弹窗提示修改 `[llm]` 的 `api_key / base_url / model` 后重新启动。
也可以手动复制模板：`copy config.example.ini config.ini`。

**启用联网搜索**：在 `config.ini` 的 `[search]` 小节填入百度千帆 API Key
（键名 `api_key` 或 `Baidu-qianfan-APIKEY` 均可）：

```ini
[search]
Baidu-qianfan-APIKEY = bce-v3/ALTAK-xxxxxxxx/xxxxxxxx  ; 千帆 API Key，留空则搜索返回配置指引
```

Key 只在运行时从 `config.ini` 读取（已 `.gitignore`，不会进仓库）；
每月 1500 次/天免费额度，详见 [计费说明](https://cloud.baidu.com/doc/qianfan/s/1mh4sv6c4)。

**其它可调参数**（全部有默认值，小节可整段删除）：

| 小节 | 关键项 | 默认 |
|---|---|---|
| `[llm]` | `context_window` / `max_tool_rounds` / `timeout` | 128000 / 20 / 60 |
| `[context]` | `warning_ratio` / `recall_max_matches` / `archive_dir` | 0.8 / 8 / `.agents/context` |
| `[mcp]` | `enabled` / `async_load` / `init_timeout` / `call_timeout` | true / true / 15 / 60 |
| `[ui]` | `scroll_throttle` / `tool_args_preview` / `sidebar_width` / `palette_max_rows` | 0.15 / 64 / 36 / 8 |
| `[sessions]` | `enabled` / `auto_save` / `dir` / `max_listed` | true / true / `.agents/sessions` / 50 |

模板见 `config.example.ini`；首次运行自动生成的 `config.ini` 与它结构一致。

## TUI 界面

```
┌ Header ── ZYAgent — deepseek-chat · 工作目录 ──────────────────── 时钟 ┐
│ ┌ 聊天区 ────────────────────────┐ ┌ 侧栏(Ctrl+B 可隐藏) ────────┐ │
│ │ 你                             │ │ ZYAgent · 18 个工具          │ │
│ │ 今年是不是闰年                  │ │ 状态/轮次/模型调用/工具调用   │ │
│ │ ▶ ✅ getDate() · 22 ms         │ │ 上次耗时/模型/接口/目录       │ │
│ │ ▼ ✅ isLeapYear(year=2026)     │ │ ── 工具（名称 + 描述）────── │ │
│ │   参数{ "year": 2026 }         │ │ getDate isLeapYear getcwd … │ │
│ │   结果 2026 年不是闰年          │ │                              │ │
│ │ ZYAgent                        │ │                              │ │
│ │ 不是。当前日期是 2026 年…       │ │                              │ │
│ └────────────────────────────────┘ └──────────────────────────────┘ │
│ 上下文 12.3k / 128k · 已占用 9.6%（估算，窗口未知，默认 128k）        │
│ [输入问题后 Enter 发送；/help 查看命令与快捷键______________________]   │
│ ● 就绪 · 1 轮 · deepseek-chat · 工具 2 次 · 2.9s · Esc 中断 · F1 帮助│
└ Footer ─ ctrl+q 退出 │ ctrl+l 清屏 │ ctrl+n 新会话 │ ctrl+s 保存 │ ctrl+b 侧栏 │ f1 帮助 ┘
```

- **流式输出**：逐字渲染 + 光标；**Esc** 随时中断（流中途生效）
- **工具卡片**：状态标记 + 耗时（ms），**点击展开**参数 JSON 与完整结果
- **上下文占用条**：输入框上方常驻显示 `上下文 12.3k / 128k · 已占用 9.6%`
  （优先模型 API 返回的 usage，否则本地估算）；未配置 `context_window` 时提示
  "窗口未知，默认 128k"；达到 80% 变红并提示执行 `/compact`
- **侧栏**：会话统计 + **MCP Server 区域**（异步加载，显示每个 Server 状态与可用工具，
  加载完成后自动刷新）+ 18 个工具清单（`loadSkill` 在存在 Skill 时出现），`Ctrl+B` 开关
- **斜杠命令补全**：输入 `/` 弹出引导面板（↑↓ 选择、Tab 补全、Enter 执行、Esc 关闭）
- **命令**（输入框直接输入）：

| 命令 | 作用 | 命令 | 作用 |
|---|---|---|---|
| `/help` | 帮助弹窗（F1 同） | `/model` | 显示模型与接口 |
| `/new [标题]` | 新建会话（自动保存当前会话，Ctrl+N） | `/status` | 会话 / 上下文 / MCP 状态 |
| `/resume` | 历史会话面板（↑↓ 选择、Enter 恢复、Esc 取消） | `/agents` | 查看 AGENTS.md 注入状态 |
| `/save [标题]` | 保存当前会话（Ctrl+S） | `/export [路径]` | 导出当前会话为 Markdown |
| `/sessions` | 列出已保存会话 | `/cwd` | 显示工作目录 |
| `/clear` | 清空界面（保留历史，Ctrl+L） | `/quit` | 退出（Ctrl+Q） |
| `/tools` | 列出已注册工具 | `/compact` | 压缩上下文（归档原始记录并生成摘要） |

## 架构与隔离规范

### 分层

| 层 | 模块 | 职责 | 依赖 |
|---|---|---|---|
| 基础层 | `core/config.py` | 加载 `config.ini` → `AppConfig`（LLM/上下文/MCP/UI/会话）；首次运行自举模板 | 仅标准库 |
| 基础层 | `core/events.py` | 定义 `AgentEvent` 事件家族（含渲染契约） | 仅标准库 |
| 资源层 | `core/llm/` | `LLMClient` 端口；`OpenAICompatClient` 适配器（流式/非流式） | 基础层 |
| 资源层 | `core/tools/` | 工具契约 `ToolSpec`、注册表路由、18 个工具实现 | 基础层 |
| 资源层 | `core/context.py` | Token 估算、原始上下文归档（JSONL）、全文关键词检索、压缩摘要组装 | 基础层 |
| 资源层 | `core/history.py` | 会话事件日志：保存 / 列表 / 恢复 / 删除（原子写入） | 基础层 |
| 资源层 | `core/skills.py` | Skill 发现（`.agents/skills`）、Frontmatter 解析、Prompt 注入 | 基础层 |
| 资源层 | `core/mcp_client.py` | MCP Client：`.mcp.json` 解析、异步连接（stdio/Streamable HTTP）、工具发现注册、名称映射 | 基础层 + 工具契约 |
| 领域层 | `core/session.py` | 会话消息历史（user/assistant/tool 追加、重置与整体替换） | 基础层 |
| 编排层 | `core/engine.py` | `ask()` 主循环；`compact()` 压缩；`new/save/resume/export` 会话管理；AGENTS.md 注入 | 以上全部 |
| 前端层 | `tui/` `cli/` | 消费事件流渲染界面 | 只依赖 core 公共 API |

**依赖规则**：前端 → core 单向依赖；core 永不 import 前端；前端之间互不依赖。

### 事件契约（`core/events.py`）

`engine.ask(question)` 返回事件迭代器，UI 逐个渲染：

| 事件 | UI 行为 |
|---|---|
| `TurnStarted` | 一轮开始 |
| `AssistantDelta` | **追加**到当前助手气泡（流式增量） |
| `AssistantMessage` | **设置**助手气泡为该完整文本（幂等；非流式时仅有此事件） |
| `ToolCallStarted` | 新建工具卡片（工具名 + 参数摘要） |
| `ToolCallFinished` | 更新卡片状态与结果（`ok=False` 为错误文本） |
| `ContextWarning` | 上下文占用达到 80%：醒目警告 + 提示 `/compact` |
| `CompactStarted` | 压缩开始：显示归档编号与"压缩中"状态 |
| `CompactFinished` | 压缩结束：`ok=False` 时原上下文保持不变，可重试 |
| `AgentError` | 显示错误（请求失败/已停止/超轮次/输入被暂停），不中断会话 |
| `TurnFinished` | 本轮结束（**任何路径都以其收尾**），UI 恢复就绪 |

事件为 frozen dataclass，`to_dict()` 可直接 JSON 序列化 → Web 前端可原样走 SSE/WebSocket。

### 引擎用法（所有前端通用）

```python
from core import create_engine

engine = create_engine()                     # 读取 config.ini
for event in engine.ask("2024 年是闰年吗？"):  # 同步事件流
    print(event.type, event.to_dict())
```

## 工具清单（18 个）

| 类别 | 工具 | 说明 |
|---|---|---|
| 系统 | `getDate` | 系统日期时间（cmd `date /t & time /t`，失败回退 datetime） |
| 系统 | `isLeapYear` | 闰年判断 |
| 文件 | `getcwd` `listdir` `getFileType` `readFile` | 查询类（readFile 拒绝二进制、utf-8/cp936 回退） |
| 文件 | `createFile` `editFile` `createDirectory` | 写入类（createFile 拒绝覆盖；editFile 先校验纯文本） |
| 文件 | `deleteDirectory` `deleteFile` `rename` | 删除/改名（deleteDirectory 拒绝磁盘根目录） |
| 搜索 | `searchWeb` | 联网搜索：百度千帆「百度AI搜索」，返回 **摘要 + 标题 + URL**（`core/tools/search.py`） |
| 网页 | `fetchWeb` | 网页阅读：curl 抓 HTML + trafilatura 提取正文转 **Markdown**（`core/tools/fetch.py`） |
| 系统 | `RunCli` | 本地命令执行：subprocess 列表执行（`shell=False`），回传 **stdout/stderr/退出码**（`core/tools/runcli.py`） |
| 技能 | `loadSkill` | 按需加载 Skill 全文/附加文件（存在 `.agents/skills/` 时由引擎注册，`core/tools/skill.py`） |
| 技能 | `createSkill` | 创建新 Skill：生成 `.agents/skills/<name>/SKILL.md`（含合规 Frontmatter），已存在不覆盖（恒注册） |
| 上下文 | `recallContext` | 在 `.agents/context/` 归档中做本地全文关键词检索，返回片段 + 归档编号 + 消息位置（恒注册） |

以上为**本地 18 个固定工具**；`.mcp.json` 中配置的 MCP Server 的工具在启动时**动态并入**
（数量不固定，见下文「MCP Client」章节）。

### `searchWeb` 细节

- **请求**：`POST https://qianfan.baidubce.com/v2/ai_search/web_search`，`Authorization: Bearer <Key>`
  （Key 来自 `config.ini [search]`，代码零硬编码）；`query` 按官方 72 字符规则截断（汉字=2）；
- **输出**：编号列表，每条含 `标题 / URL / 摘要 / 来源·日期`，另附结果总数提示；
- **错误即数据**（全部经注册表回传 `ok=False` 文本给 LLM）：
  - 缺少 `Baidu-qianfan-APIKEY` → 指引去 `config.ini [search]` 配置，并提示回答可能过时；
  - 断网 / 超时（`URLError`/`TimeoutError`）→ 明确"网络错误/超时，稍后重试"；
  - HTTP 401/403/429 → 附带原因与修复建议（Key 无效 / 未开通 / 限流）；
  - 业务错误码（`code`/`message`）→ `百度搜索返回错误 code=...: 消息`。

### `fetchWeb` 细节

- **安全调用 curl**：`subprocess.run([...], shell=False)` **参数列表方式**（URL 独立成参，
  特殊字符无需转义，杜绝命令注入）；仅放行 `http/https` URL（拒绝 `file://` 等本地协议）；
  `--compressed --location --max-time` 处理压缩/重定向/整体超时，进程级超时再兜底 5 秒；
- **正文提取**：trafilatura `output_format=markdown`，保留 **标题/段落/列表/表格/链接**，
  去除导航栏/广告/页脚/评论（`favor_precision` + 去重）；正文超 20000 字符自动截断省 token；
- **输出**：`URL` + `HTTP 状态` + `页面标题` + Markdown 正文；
- **错误即数据**（全部转 `ok=False` 清晰文本给 LLM）：
  - curl 缺失 → 指引安装；超时 → `抓取超时：N 秒内未完成`；
  - 网络失败（curl 非零退出）→ 带 stderr 原文 + 常见原因（域名不存在/无网络/拒绝访问）；
  - HTTP ≥ 400 → `目标站点返回 HTTP 404，无法访问`；非 HTML 类型（如 PDF）→ 明确拒绝；
  - 解析异常 → `HTML 解析失败：类型: 信息`；正文为空（JS 渲染/登录页）→ 建议换可直接访问的来源。

### `RunCli` 细节

- **安全调用**：`command` 与 `args[]` 拼装为**列表**，`subprocess.run(..., shell=False)`
  执行——**绝不拼接 shell 命令字符串**，参数中的 `&`、`;`、`|` 按字面量传递（防注入）；
- **结果回传**：`命令 / 退出码 / [stdout] / [stderr]` 四段结构化文本；
  退出码非 0 时附失败提示，由模型结合 stderr 判断并决定下一步；
- **上下文保护**：stdout 与 stderr 各自截断至 6000 字符，截断处注明原始长度；
- **编码兼容**：UTF-8 优先，回退 GBK/系统首选编码（中文 Windows 控制台输出可读）；
- **超时**：默认 60 秒，超时终止进程并回传部分输出；
- **错误即数据**：命令未找到 / 无权限 / 参数非法（args 传了字符串而非数组等）
  → `ok=False` 清晰文本，模型可自我纠正重试。
- ⚠️ 面向受控教学实验环境，命令以运行 ZYAgent 的当前用户权限执行。

### `loadSkill` 与 Skill 动态加载（Agent Skills）

**发现与注册**：初始化时以**程序启动时的工作目录**为项目根，扫描
`.agents/skills/` 下的各 Skill 子目录。每个 Skill 至少包含 `SKILL.md`，
其开头以 YAML Frontmatter 声明必须字段 `name` 与 `description`
（支持引号标量与 `|`/`>` 块标量；缺字段 / 缺 SKILL.md 的目录自动跳过）。
**发现阶段仅加载 name + description，不读取正文**。

**System Prompt 注入**：发现的技能整理为清单追加到 System Prompt（原提示词为空时即为其内容）：

```
You have access to the following skills:
- 'web-research': 联网调研工作流：组合 searchWeb 与 fetchWeb……
- 'git-helper': Git 仓库操作助手：通过 RunCli 安全执行 git 命令……
Use the 'loadSkill' tool to load the full instructions of a skill when needed.
You can also load additional files referenced by a skill.
```

**`loadSkill` 工具**（`core/tools/skill.py`）：

- 仅 `name` → 返回该 Skill 的 `SKILL.md` **全文**；
- `name` + `filename` → 返回 Skill 目录内的**附加文件**（不重复加载 SKILL.md）；
- 路径兼容：相对 Skill 目录 / 项目相对路径 / 绝对路径；只给文件名时在 Skill 目录内
  **递归搜索**——唯一命中才读取，**多个同名返回候选路径**由 LLM 决定；
- **读取范围限定在该 Skill 目录内**：越界绝对路径/`../` 逃逸直接拒绝；
  未命中时列出该 Skill 全部可用文件辅助模型自我纠正；
- 文件按 UTF-8（回退 GBK）读取，超 50000 字符截断保护上下文。

**动态加载原则**：Skill 完整内容**只有**在 LLM 调用 `loadSkill` 时才进入对话上下文；
现有 Agent 工作流保持不变，Skill 可与 `RunCli`、文件工具、`searchWeb`/`fetchWeb`
等协同工作（样例：`git-helper` 指导用 `RunCli` 执行 git，`web-research` 组合两个联网工具）。
无 `.agents/skills/` 目录时 `loadSkill` 不注册、System Prompt 不变（`createSkill` 恒在，
可从零创建技能）。

### `createSkill` 细节

- **用途**：用户用自然语言要求"创建/新增技能"时，LLM 调用本工具落盘 SKILL.md；
- **参数**：`name`（Skill 名称）+ `skillContext`（写入 SKILL.md 的完整内容）；
- **名称规范化**：统一为**小写字母、数字、连字符**（`"My Cool Skill"` → `my-cool-skill`），
  目录名与 Frontmatter `name` 强制一致；
- **Frontmatter 保障**：自带 `description` 时原样保留；缺 Frontmatter 或缺 `description`
  时自动补齐（description 取正文首行），保证产出**符合 Agent Skills 规范**、可被
  `discover_skills` 直接发现；
- **防覆盖**：目标 `SKILL.md` 已存在 → 返回明确提示（引导改用 `editFile`），分毫不写；
  写入用独占创建模式（`open("x")`），杜绝并发覆盖；
- **职责边界**：只创建 SKILL.md——`references/`、`scripts/` 等目录与辅助文件由模型
  继续调用现有 `createDirectory` / `createFile` 工具完成；
- **生效时机**：技能清单在启动时注入 System Prompt，新技能**下次启动**出现在清单中
  （当前会话不热更新，工具会如实告知模型）。

### 上下文压缩与历史检索（`/compact` + `recallContext`）

**为什么需要**：长任务会不断累积对话与工具输出，最终超出模型上下文窗口。ZYAgent 用
"归档 + 摘要 + 按需检索"的简单方案延续任务，不引入向量数据库或 Embedding。

**1. 上下文监控**（`core/engine.py`）

- 每轮提问结束后检查上下文用量：**优先**取模型 API 返回的 `usage`（流式请求带
  `stream_options.include_usage`），拿不到时本地估算（System Prompt + 对话历史 +
  工具调用消息 + 工具声明；ASCII≈4 字符/token，中文≈1 字/token）；
- 达到模型窗口 **80%** 时发出 `ContextWarning` 并**暂停接受普通提示词**（引擎与 TUI
  双重拦截），直到 `/compact` 成功；
- 窗口大小取自 `config.ini [llm] context_window`；未配置时默认 **128k**，TUI 会显示
  "窗口未知，默认 128k"。

**2. `/compact` 执行流程**（`AgentEngine.compact()`）

1. 生成唯一编号（`YYYYMMDD-HHMMSS`，同秒冲突自动追加 `-1`）；
2. **先**把当前全部消息按顺序写入 `.agents/context/<编号>.jsonl`（每行一条消息，
   保留 `role` / `content` / `tool_calls` / `tool_call_id`），**归档失败即中止**；
3. 仅调用一次 LLM 生成摘要（请求**不携带工具声明**，压缩期间不可能发生工具调用）；
   压缩提示词见 `core/context.py` 的 `COMPACT_PROMPT`，要求输出约 5,000 Tokens 的
   Markdown 工作摘要；
4. 用摘要替换历史：保留原 System Prompt 与工具注册表，摘要末尾追加归档说明
   （编号 + 时间 + `recallContext` 指引），并解除输入限制；
5. 任何失败（归档/请求/空摘要）都保留原上下文，返回 `AgentError` +
   `CompactFinished(ok=False)`，可重新执行 `/compact`。

压缩后历史示例：

```
system: 你是一个……（原 System Prompt 不变）
user:   以下内容是此前对话的压缩摘要，作为新上下文的起点：
        ## 用户目标 ……（约 5k tokens）
        ---
        此前上下文已于 2026-10-22 15:30:00 完成压缩，原始上下文保存在编号为
        20261022-153000 的归档文件中。如需查找历史信息，请使用 recallContext 工具。
```

**3. `recallContext` 工具**（`core/tools/recall.py` + `core/context.py`）

- 参数：`text`（必填关键词）+ `id`（可选归档编号；省略则检索全部归档，新→旧）；
- 本地全文匹配：按空白/标点拆分关键词做包含匹配，命中关键词越多越靠前；
- 返回：`[归档编号] 第 N 条消息 · role=...` + 命中位置附近片段（压平空白 + 省略号）；
- 结果受控：最多 8 条 / 4000 字符，超出时提示"仅显示前 N 条"，避免再次占满上下文；
- 错误即数据：空关键词 / 归档不存在 / 尚无归档都返回清晰文本（可指导模型换词或先压缩）。

### 会话持久化与斜杠命令

- 每轮结束自动保存（`[sessions] auto_save = true`），文件
  `.agents/sessions/<编号>.jsonl`：首行是 meta（标题/创建时间/更新时间/模型/消息数），
  后续每行一条消息；采用「临时文件 + `os.replace`」原子重写，压缩后快照依然准确；
- `/save [标题]` 手动保存；`/new [标题]` **先保存旧会话**再新建；`/resume` 打开历史
  会话面板（↑↓ 选择、Enter 恢复、Esc 取消）；`/sessions` 输出文本列表；
  `/export [路径]` 导出 Markdown；`/status` 汇总会话 / 上下文 / MCP 状态；
- 标题默认取首条用户消息（跳过 AGENTS.md），可用 `/save 标题` 覆盖；
- 斜杠命令补全：输入 `/` 弹出引导面板，`[ui] palette_max_rows` 控制最大行数。

### AGENTS.md：一次注入，全程复用

- 会话首次加载时读取项目根 `AGENTS.md`（UTF-8 / 带 BOM 均可），作为一条 `user`
  消息追加到聊天历史（前缀 `【项目上下文 · AGENTS.md】`），并随会话保存进事件日志；
- 后续轮次**不再重复注入**，保持消息前缀稳定以命中 Prompt Cache；
- `/compact` 压缩时该消息被单独保留（System Prompt → AGENTS.md → 摘要）；
- `/agents` 查看是否发现/注入；新建会话重新注入，恢复旧会话不会重复注入。

### MCP Client 细节

- **配置**：读取**当前项目目录**（程序启动时的工作目录，与 `.agents/` 同根）下的
  `.mcp.json`（文件不存在 = 未配置，正常状态；路径可用 `[mcp] config_file` 修改）：

  ```json
  {
    "mcpServers": {
      "Bazi-MCP": {
        "type": "streamable_http",
        "url": "https://example.com/your-endpoint/mcp"
      },
      "local-tools": {
        "type": "stdio",
        "command": "python",
        "args": ["-m", "your_mcp_server"],
        "env": {"KEY": "value"}
      }
    }
  }
  ```

  支持 `stdio` 与 `streamable_http` 两种协议（别名 `streamable-http`/`http`，
  缺 `type` 时按 `url`/`command` 自动推断）；`headers` 可选，逐项配置错误会被记录
  且不阻断其他 Server；文件为 UTF-8/UTF-8 BOM 均可；
- **发现与注册**：连接后调用 `list_tools()` 取各工具的 `name`/`description`/
  `inputSchema`，注册进现有 `ToolRegistry`——LLM 侧与 `RunCli`/`fetchWeb`/`loadSkill`
  等本地工具**完全同等**（同一份 Schema 声明、同一条调用路径、同一套 UI 卡片渲染）；
- **异步加载**：`[mcp] async_load = true` 时引擎把连接放到后台线程，TUI 先进入界面；
  侧栏每 0.35s 读取 `MCPManager.snapshot()` 的 `revision`，变化时动态刷新
  「MCP Server + 可用工具」区域与工具清单，不会阻塞启动；
- **名称映射**：内部维护 `暴露名 → (Server, 原始工具名)` 路由表；工具名与本地工具或
  其他 Server 冲突时才加**服务器名前缀**（如 `Bazi-MCP_bazi`），并统一清洗为
  `[A-Za-z0-9_-]`，保证符合 Function Calling 命名约束；
- **调用转发**：MCP SDK 是 asyncio 接口，`MCPManager` 用**专属后台线程 + 独立事件循环**
  把同步工具处理器桥接到 `session.call_tool()`（`run_coroutine_threadsafe`），
  引擎与前端零改动；返回内容整理为：文本块按序拼接、`structuredContent` 以 JSON 附加、
  图片/资源给出占位说明，`isError` → `ok=False` 错误文本（错误即数据）；
- **错误隔离**：每个 Server 独立连接栈；单个 Server 连接失败（超时/断网/端点无效）只让
  **该 Server 的工具**不注册或返回清晰错误文本，本地工具与其他 Server 不受牵连
  （已实测：端点失效时 Agent 照常完成本地工具调用）；`/mcp` 命令查看各 Server 连接状态；
- **依赖**：MCP Python SDK（`requirements.txt` 的 `mcp`），代码内**惰性导入**——
  SDK 缺失只影响 MCP 工具，本地工具照常运行；
- **联调**：`python tests/live_mcp.py [工具名] [参数JSON]` 连接 `.mcp.json` 中的
  Server 列工具并调用（环境变量 `ZY_MCP_ROOT` 可指向其他项目根）；
  `python tests/_local_mcp_server.py stdio|http` 启动本地 MCP Server 作为联调对象
  （含文本/结构化/抛错/同名冲突四类样例工具）。

**新增工具只需一步**：在 `core/tools/` 新增 `ToolSpec` 并注册——声明即契约，
Schema、路由、UI 展示自动生效，引擎与前端零改动。

## 新增前端（web / desktop / mobile）

各前端目录已预留占位与接入文档（见 `web/README.md`、`desktop/README.md`、`mobile/README.md`），通用步骤：

1. 每个会话创建一个 `AgentEngine`（引擎持有会话历史，不可跨会话共享）；
2. 在后台线程/异步任务中逐步消费 `engine.ask()`（参考 `tui/app.py` 的 `asyncio.to_thread` 步进法）；
3. 按事件契约渲染；跨进程传输用 `event.to_dict()`；
4. 在 `main.py` 的 `FRONTENDS` 注册新前端选项。

## 测试

```bash
python tests/test_core.py    # 24 项：注册表/事件流/错误取消/搜索×3/RunCli/Skill×4/MCP×2/上下文×4/配置/会话/AGENTS.md（离线）
python tests/test_tui.py     # 13 项：对话 / 卡片 / 中断 / 清屏 / 侧栏 / 命令 / 布局 / 换行 / 压缩 / 补全 / 会话恢复 / MCP 侧栏（Pilot，离线）
python tests/make_screenshot.py  # 生成 tests/tui_preview.svg 供人工查看界面效果
python tests/live_search.py 北京今天天气   # searchWeb 真实联调（消耗 1 次免费额度）
python tests/live_fetch.py https://example.com   # fetchWeb 真实联调（curl+trafilatura）
python tests/live_mcp.py                  # MCP 真实联调（连接 .mcp.json 的 Server，列工具并调用）
```

## 与 chat_files.py 的对照

| chat_files.py（单文件 368 行） | ZYAgent |
|---|---|
| `TOOLS` 全局列表 + `call_tool` if 分派 | `ToolSpec` + `ToolRegistry` 注册表（声明即路由） |
| `send()` 直接请求、不支持流式 | `OpenAICompatClient`：SSE 流式 + 工具分片合并 + 非流式回退 |
| `main()` 双层 while 循环混杂 IO | `AgentEngine.ask()` 事件流；UI 与业务彻底解耦 |
| print 逐字符模拟打字 | 真流式增量；TUI 光标 + 工具卡片 |
| 无法复用到其他界面 | core 可直接被 web/desktop/mobile 复用 |

## 安全提示

- `config.ini` 含 LLM 与百度千帆搜索 Key，已加入 `.gitignore`，请勿提交仓库（代码中零硬编码）；
- 工具具备真实文件删除/覆盖能力，Web 化时务必增加路径白名单与操作确认；
- 生产使用建议增加：请求重试、会话持久化、审计日志、并发锁。
