"""配置层：加载 config.ini，向核心引擎提供不可变配置对象。

层级：core / 基础层
依赖：仅标准库；不依赖任何 UI 模块。

设计要点
--------
* **一处定义，多处复用**：所有可调参数（模型、上下文阈值、检索条数、MCP 超时、
  TUI 行为、会话持久化）都集中在 config.ini，代码里只保留"缺省值"；
* **首次运行自举**：当前目录没有 config.ini 时，:func:`ensure_config` 会写入一份
  带 mock 信息的完整模板，入口层据此提示用户修改后重启；
* **容错**：ini 支持行内 ``;`` 注释；非法数值给出明确的 ``ConfigError``。
"""

from __future__ import annotations

import configparser
import sys
from dataclasses import dataclass
from pathlib import Path

#: 项目根目录
#: - 开发环境：core/config.py -> parents[1] = 项目根目录
#: - PyInstaller 打包后：sys._MEIPASS 指向 _internal 目录（资源文件解压位置）
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    PROJECT_ROOT = Path(sys._MEIPASS)
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 默认配置文件位置（项目根目录）
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.ini"

#: 无法获取模型上下文窗口大小时使用的默认值（128k tokens）
DEFAULT_CONTEXT_WINDOW = 128_000

#: 配置文件模板（首次运行时写入；mock 值，必须由用户修改后重启）
CONFIG_TEMPLATE = """\
; ======================================================================
; ZYAgent 配置文件（首次运行自动生成）
;
; ⚠ 当前是 mock 配置：请把 [llm] 的 api_key / base_url / model 改成真实值，
;   然后重新启动 ZYAgent 才能正常工作。
; 以 ; 开头的行是注释；行尾 ; 之后的内容也会被当作注释忽略。
; ======================================================================

[llm]
api_key  = sk-mock-replace-me
base_url = https://api.deepseek.com
model    = deepseek-chat

; ===== 可选（括号内为默认值）=====
stream          = true     ; 是否流式输出（false 时一次性返回）
timeout         = 60       ; 单次请求超时秒数
max_tool_rounds = 20       ; 单轮提问最多允许的工具调用往返次数
context_window  = 128000   ; 模型上下文窗口（token），用于上下文占用监控
; system_prompt = 你是一个乐于助人的 AI 助手

[context]
; ===== 上下文压缩与历史检索 =====
warning_ratio          = 0.8    ; 占用达到该比例时警告并暂停普通提问（0~1）
ascii_chars_per_token  = 4      ; 无 API usage 时的估算：ASCII 字符/token
message_overhead_tokens = 4     ; 每条消息的固定结构开销（token）
archive_dir            = .agents/context   ; 原始上下文归档目录（相对项目根）
recall_max_matches     = 8      ; recallContext 单次最多返回的匹配条数
recall_max_chars       = 4000   ; recallContext 单次结果的最大字符数
recall_snippet_radius  = 120    ; 命中位置前后截取的字符数
recall_snippet_chars   = 360    ; 单条片段的最大字符数

[mcp]
; ===== MCP Server =====
enabled      = true        ; 是否启用 MCP 工具
config_file  = .mcp.json   ; MCP 配置文件（相对项目根）
init_timeout = 15          ; 连接 + list_tools 超时（秒）
call_timeout = 60          ; 单次工具调用超时（秒）
async_load   = true        ; true：先进入界面，MCP 在后台加载并动态刷新

[ui]
; ===== TUI 行为 =====
scroll_throttle   = 0.15   ; 聊天区自动滚动节流（秒）
tool_args_preview = 64     ; 工具卡片参数摘要最大字符数
sidebar_width     = 36     ; 侧栏宽度（列）
palette_max_rows  = 8      ; 斜杠命令补全面板最大行数

[sessions]
; ===== 会话持久化（/save、/new、/resume）=====
enabled    = true          ; 是否启用会话保存
auto_save  = true          ; 每轮对话结束后自动保存
dir        = .agents/sessions   ; 会话事件日志目录（相对项目根）
max_listed = 50            ; /resume 列表最多展示的会话数

[search]
; ===== 联网搜索：百度千帆「百度AI搜索」（配置 api_key 即启用 searchWeb 工具）=====
api_key  =
; provider   = baidu_qianfan
; endpoint   = https://qianfan.baidubce.com/v2/ai_search/web_search
; edition    = standard
; top_k      = 10
; recency    =
; timeout    = 20
"""


@dataclass(frozen=True)
class LLMConfig:
    """LLM 连接配置（对应 config.ini 的 [llm] 小节）。"""

    api_key: str
    base_url: str
    model: str
    stream: bool = True           # 是否使用流式（SSE）请求
    timeout: int = 60             # 单次请求超时（秒）
    max_tool_rounds: int = 20     # 单轮提问最多允许的工具调用往返次数
    system_prompt: str = ""       # 可选系统提示词
    context_window: int = DEFAULT_CONTEXT_WINDOW  # 模型上下文窗口（token）
    #: True 表示配置里没有显式指定 context_window，前端应提示"默认 128k"
    context_window_configured: bool = False

    @property
    def endpoint(self) -> str:
        """Chat Completions 完整地址（base_url 允许带或不带 /chat/completions）。"""
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"


@dataclass(frozen=True)
class SearchConfig:
    """联网搜索配置（对应 config.ini 的 [search] 小节，可缺省）。

    ``api_key`` 为空表示未启用 ``searchWeb`` 工具（工具不会注册给 LLM）。
    """

    provider: str = "baidu_qianfan"    # 搜索提供方（当前仅支持百度千帆）
    api_key: str = ""                  # 千帆 API Key（Bearer 鉴权）；空 = 调用时返回配置指引
    endpoint: str = "https://qianfan.baidubce.com/v2/ai_search/web_search"
    edition: str = "standard"          # 搜索版本：standard / turbo / pro
    top_k: int = 10                    # 返回的网页结果条数上限
    recency: str = ""                  # 发布时间过滤：week/month/semiyear/year；空 = 不限
    timeout: int = 20                  # 单次搜索超时（秒）

    @property
    def enabled(self) -> bool:
        """是否已配置可用的搜索服务。"""
        return bool(self.api_key.strip())


@dataclass(frozen=True)
class ContextConfig:
    """上下文监控 / 压缩 / 检索参数（对应 [context] 小节）。"""

    warning_ratio: float = 0.8          # 达到窗口的该比例 -> 警告 + 暂停提问
    ascii_chars_per_token: float = 4.0  # 本地估算：ASCII 字符数 / token
    message_overhead_tokens: int = 4    # 每条消息的固定结构开销
    archive_dir: str = ".agents/context"
    recall_max_matches: int = 8
    recall_max_chars: int = 4000
    recall_snippet_radius: int = 120
    recall_snippet_chars: int = 360


@dataclass(frozen=True)
class MCPConfig:
    """MCP 客户端参数（对应 [mcp] 小节）。"""

    enabled: bool = True
    config_file: str = ".mcp.json"
    init_timeout: float = 15.0
    call_timeout: float = 60.0
    async_load: bool = True


@dataclass(frozen=True)
class UIConfig:
    """TUI 展示参数（对应 [ui] 小节；CLI 前端也会读取其中通用项）。"""

    scroll_throttle: float = 0.15
    tool_args_preview: int = 64
    sidebar_width: int = 36
    palette_max_rows: int = 8


@dataclass(frozen=True)
class SessionConfig:
    """会话持久化参数（对应 [sessions] 小节）。"""

    enabled: bool = True
    auto_save: bool = True
    dir: str = ".agents/sessions"
    max_listed: int = 50


@dataclass(frozen=True)
class AppConfig:
    """完整应用配置（一次解析，供引擎与前端共享）。"""

    llm: LLMConfig
    search: SearchConfig = SearchConfig()
    context: ContextConfig = ContextConfig()
    mcp: MCPConfig = MCPConfig()
    ui: UIConfig = UIConfig()
    sessions: SessionConfig = SessionConfig()


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


# ====================================================================== 路径
def resolve_config_path(path: str | Path | None = None) -> Path:
    """确定配置文件路径。

    * 显式参数优先；
    * 否则固定使用**当前工作目录**的 ``config.ini``——运行 ZYAgent 的项目目录
      就是它的配置目录；不存在时由 :func:`ensure_config` 自动生成。
    """
    if path:
        return Path(path).expanduser()
    return Path.cwd() / "config.ini"


def ensure_config(path: str | Path | None = None) -> tuple[Path, bool]:
    """确保配置文件存在；不存在时写入完整模板。

    :returns: ``(配置文件路径, 是否为本次新建)``
    """
    cfg_path = resolve_config_path(path)
    if cfg_path.is_file():
        return cfg_path, False
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return cfg_path, True


# ====================================================================== 解析
def _read_parser(cfg_path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(
        inline_comment_prefixes=(";",),  # 允许 "key = value  ; 注释"
        interpolation=None,              # 值里可能出现 %（如 URL 编码）
    )
    try:
        parser.read(cfg_path, encoding="utf-8")
    except configparser.Error as exc:  # pragma: no cover - ini 语法错误
        raise ConfigError(f"配置文件解析失败: {cfg_path} ({exc})") from exc
    return parser


def _positive_int(
    section: configparser.SectionProxy, key: str, default: int, cfg_path: Path
) -> int:
    try:
        value = section.getint(key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"配置项必须为整数: {key} ({cfg_path})") from exc
    if value <= 0:
        raise ConfigError(f"配置项必须为正整数: {key} ({cfg_path})")
    return value


def _positive_float(
    section: configparser.SectionProxy, key: str, default: float, cfg_path: Path
) -> float:
    try:
        value = section.getfloat(key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"配置项必须为数字: {key} ({cfg_path})") from exc
    if value <= 0:
        raise ConfigError(f"配置项必须为正数: {key} ({cfg_path})")
    return value


def _build_llm(parser: configparser.ConfigParser, cfg_path: Path) -> LLMConfig:
    if "llm" not in parser:
        raise ConfigError(f"配置文件缺少 [llm] 小节: {cfg_path}")
    section = parser["llm"]

    missing = [key for key in ("api_key", "base_url", "model")
               if not section.get(key, "").strip()]
    if missing:
        raise ConfigError(f"[llm] 缺少必填项 {', '.join(missing)}: {cfg_path}")

    context_window = _positive_int(
        section, "context_window", DEFAULT_CONTEXT_WINDOW, cfg_path
    )
    return LLMConfig(
        api_key=section.get("api_key", "").strip(),
        base_url=section.get("base_url", "").strip(),
        model=section.get("model", "").strip(),
        stream=section.getboolean("stream", fallback=True),
        timeout=_positive_int(section, "timeout", 60, cfg_path),
        max_tool_rounds=_positive_int(section, "max_tool_rounds", 20, cfg_path),
        system_prompt=section.get("system_prompt", fallback="").strip(),
        context_window=context_window,
        context_window_configured="context_window" in section,
    )


def _build_search(
    parser: configparser.ConfigParser, cfg_path: Path
) -> SearchConfig:
    if "search" not in parser:
        return SearchConfig()

    section = parser["search"]
    # 键名别名：api_key（标准名）或 Baidu-qianfan-APIKEY（用户自定义名，ini 键名不区分大小写）
    api_key = ""
    for alias in ("api_key", "baidu-qianfan-apikey", "baidu_qianfan_apikey", "apikey"):
        api_key = section.get(alias, fallback="").strip()
        if api_key:
            break
    recency = section.get("recency", fallback="").strip().lower()
    if recency and recency not in ("week", "month", "semiyear", "year"):
        raise ConfigError(
            f"[search] recency 非法: {recency}（可选 week/month/semiyear/year）: {cfg_path}"
        )

    endpoint = section.get("endpoint", fallback="").strip() or SearchConfig.endpoint
    return SearchConfig(
        provider=section.get("provider", fallback="baidu_qianfan").strip() or "baidu_qianfan",
        api_key=api_key,
        endpoint=endpoint,
        edition=section.get("edition", fallback="standard").strip() or "standard",
        top_k=_positive_int(section, "top_k", 10, cfg_path),
        recency=recency,
        timeout=_positive_int(section, "timeout", 20, cfg_path),
    )


def _build_context(
    parser: configparser.ConfigParser, cfg_path: Path
) -> ContextConfig:
    if "context" not in parser:
        return ContextConfig()
    section = parser["context"]
    try:
        ratio = section.getfloat("warning_ratio", fallback=0.8)
    except ValueError as exc:
        raise ConfigError(f"[context] warning_ratio 必须为数字: {cfg_path}") from exc
    if not 0 < ratio <= 1:
        raise ConfigError(f"[context] warning_ratio 必须在 (0, 1] 之间: {cfg_path}")
    return ContextConfig(
        warning_ratio=ratio,
        ascii_chars_per_token=_positive_float(
            section, "ascii_chars_per_token", 4.0, cfg_path
        ),
        message_overhead_tokens=_positive_int(
            section, "message_overhead_tokens", 4, cfg_path
        ),
        archive_dir=section.get("archive_dir", fallback="").strip() or ".agents/context",
        recall_max_matches=_positive_int(section, "recall_max_matches", 8, cfg_path),
        recall_max_chars=_positive_int(section, "recall_max_chars", 4000, cfg_path),
        recall_snippet_radius=_positive_int(section, "recall_snippet_radius", 120, cfg_path),
        recall_snippet_chars=_positive_int(section, "recall_snippet_chars", 360, cfg_path),
    )


def _build_mcp(parser: configparser.ConfigParser, cfg_path: Path) -> MCPConfig:
    if "mcp" not in parser:
        return MCPConfig()
    section = parser["mcp"]
    return MCPConfig(
        enabled=section.getboolean("enabled", fallback=True),
        config_file=section.get("config_file", fallback="").strip() or ".mcp.json",
        init_timeout=_positive_float(section, "init_timeout", 15.0, cfg_path),
        call_timeout=_positive_float(section, "call_timeout", 60.0, cfg_path),
        async_load=section.getboolean("async_load", fallback=True),
    )


def _build_ui(parser: configparser.ConfigParser, cfg_path: Path) -> UIConfig:
    if "ui" not in parser:
        return UIConfig()
    section = parser["ui"]
    return UIConfig(
        scroll_throttle=_positive_float(section, "scroll_throttle", 0.15, cfg_path),
        tool_args_preview=_positive_int(section, "tool_args_preview", 64, cfg_path),
        sidebar_width=_positive_int(section, "sidebar_width", 36, cfg_path),
        palette_max_rows=_positive_int(section, "palette_max_rows", 8, cfg_path),
    )


def _build_sessions(
    parser: configparser.ConfigParser, cfg_path: Path
) -> SessionConfig:
    if "sessions" not in parser:
        return SessionConfig()
    section = parser["sessions"]
    return SessionConfig(
        enabled=section.getboolean("enabled", fallback=True),
        auto_save=section.getboolean("auto_save", fallback=True),
        dir=section.get("dir", fallback="").strip() or ".agents/sessions",
        max_listed=_positive_int(section, "max_listed", 50, cfg_path),
    )


def load_app_config(path: str | Path | None = None) -> AppConfig:
    """读取完整应用配置（一次解析，供引擎与前端共享）。"""
    cfg_path = resolve_config_path(path)
    if not cfg_path.is_file():
        raise ConfigError(
            f"配置文件不存在: {cfg_path}（首次运行请让 ZYAgent 自动生成）"
        )
    parser = _read_parser(cfg_path)
    return AppConfig(
        llm=_build_llm(parser, cfg_path),
        search=_build_search(parser, cfg_path),
        context=_build_context(parser, cfg_path),
        mcp=_build_mcp(parser, cfg_path),
        ui=_build_ui(parser, cfg_path),
        sessions=_build_sessions(parser, cfg_path),
    )


def load_settings(path: str | Path | None = None) -> LLMConfig:
    """读取 ini 配置并返回 :class:`LLMConfig`（向后兼容入口）。"""
    return load_app_config(path).llm


def load_search_config(path: str | Path | None = None) -> SearchConfig:
    """读取 ini 中的 ``[search]`` 小节（向后兼容入口）。"""
    return load_app_config(path).search
