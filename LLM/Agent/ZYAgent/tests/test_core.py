"""core 层离线冒烟测试（不联网）：假 LLM 客户端验证引擎事件流契约。

运行: python -m tests.test_core   或   python tests/test_core.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import (  # noqa: E402
    ContextConfig,
    LLMConfig,
    SearchConfig,
    ensure_config,
    load_app_config,
)
from core.context import (  # noqa: E402
    build_compacted_messages,
    estimate_messages_tokens,
    estimate_tokens,
    format_tokens_k,
    new_archive_id,
    save_archive,
    usage_total_tokens,
)
from core.engine import AGENTS_MD_PREFIX, AgentEngine  # noqa: E402
from core.events import (  # noqa: E402
    AgentError,
    AssistantDelta,
    AssistantMessage,
    CompactFinished,
    ContextWarning,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from core.llm.base import LLMClient, LLMError, LLMReply, ReplyArrived, TextDelta  # noqa: E402
from core.skills import (  # noqa: E402
    build_skills_prompt,
    discover_skills,
    parse_frontmatter,
    split_frontmatter,
)
from core.tools import (  # noqa: E402
    RECALL_TOOL_NAME,
    ToolError,
    ToolRegistry,
    build_curl_command,
    build_create_skill_tool,
    build_default_registry,
    build_fetch_tool,
    build_load_skill_tool,
    build_recall_tool,
    build_run_cli_tool,
    build_search_tool,
    extract_markdown,
    fetch_url,
)
from core.tools.search import clip_query, format_references  # noqa: E402

#: 测试隔离：指向一个不存在的 skills 根目录——避免引擎自动发现项目内的
#: 真实 .agents/skills 样例而影响断言（各 Skill 专属测试使用临时目录）
_NO_SKILLS_ROOT = Path(tempfile.gettempdir()) / "zyagent_test_no_skills_root"

#: 模拟百度千帆 web_search 的成功响应
_FAKE_SEARCH_RESPONSE = {
    "request_id": "test-req-id",
    "references": [
        {
            "id": 1,
            "title": "百度搜索开放平台",
            "url": "https://example.com/baidu-search",
            "snippet": "  摘要第一行\n第二行   压平空白 ",
            "website": "example.com",
            "date": "2026-10-20 08:00:00",
            "type": "web",
        },
        {"id": 2, "title": "参考来源二", "url": "https://example.org/two", "content": "内容摘要二"},
    ],
}


class FakeClient(LLMClient):
    """脚本化假客户端：第一轮发起工具调用，第二轮给出最终回答。"""

    supports_stream = True

    def __init__(self) -> None:
        self.calls = 0

    def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            # 模型先说一句话，再要求调用 isLeapYear
            for ch in "我来查一下。":
                yield TextDelta(ch)
            yield ReplyArrived(
                message={
                    "role": "assistant",
                    "content": "我来查一下。",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "isLeapYear", "arguments": '{"year": 2024}'},
                        }
                    ],
                }
            )
        else:
            for ch in "2024年是闰年。":
                yield TextDelta(ch)
            yield ReplyArrived(
                message={"role": "assistant", "content": "2024年是闰年。"}
            )

    def complete(self, messages, tools=None):  # pragma: no cover - 本测试走流式
        raise AssertionError("不应走到非流式分支")


class CompactFake(FakeClient):
    """压缩摘要用假客户端：请求不传工具（tools=None）时返回 Markdown 摘要。"""

    def __init__(self, summary: str = "# 工作摘要\n\n- 用户目标：测试上下文压缩。") -> None:
        super().__init__()
        self.summary = summary
        self.summary_calls = 0

    def stream(self, messages, tools=None):
        if tools is None:  # 压缩请求不传工具声明
            self.summary_calls += 1
            for ch in self.summary:
                yield TextDelta(ch)
            yield ReplyArrived(message={"role": "assistant", "content": self.summary})
            return
        yield from super().stream(messages, tools)


class UsageFake(CompactFake):
    """每次请求都返回固定 API usage，用于验证 80% 阈值与暂停输入。"""

    def __init__(self, total_tokens: int = 1700) -> None:
        super().__init__()
        self.total_tokens = total_tokens

    def stream(self, messages, tools=None):
        for part in super().stream(messages, tools):
            if isinstance(part, ReplyArrived):
                yield ReplyArrived(
                    message=part.message,
                    finish_reason=part.finish_reason,
                    usage={
                        "prompt_tokens": self.total_tokens - 200,
                        "completion_tokens": 200,
                        "total_tokens": self.total_tokens,
                    },
                )
            else:
                yield part


def test_registry() -> None:
    reg = build_default_registry()
    assert len(reg) == 15, f"应注册 15 个工具，实际 {len(reg)}"
    assert "readFile" in reg and "getDate" in reg and "searchWeb" in reg \
        and "fetchWeb" in reg and "RunCli" in reg
    # createSkill / loadSkill 由引擎按 Skill 环境注册，不在默认注册表
    assert "createSkill" not in reg and "loadSkill" not in reg
    assert len(reg.schemas) == 15
    assert reg.schemas[0]["function"]["name"] == "getDate"
    r = reg.execute("isLeapYear", {"year": 2024})
    assert r.ok and "是闰年" in r.output, r
    r = reg.execute("isLeapYear", {"year": "abc"})
    assert not r.ok and r.output.startswith("错误"), r
    r = reg.execute("nope", {})
    assert not r.ok and "未知工具" in r.output, r

    # 缺少配置：searchWeb 仍注册，调用返回清晰的配置指引（模型可转述给用户）
    r = reg.execute("searchWeb", {"query": "今日新闻"})
    assert not r.ok and r.output.startswith("错误"), r
    assert "Baidu-qianfan-APIKEY" in r.output and "config.ini" in r.output, r


def test_engine_event_stream() -> None:
    config = LLMConfig(api_key="x", base_url="http://invalid", model="fake", stream=True)
    engine = AgentEngine(config, client=FakeClient(), skills_root=_NO_SKILLS_ROOT)
    events = list(engine.ask("2024年是闰年吗？"))

    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "TurnStarted", kinds
    assert kinds[-1] == "TurnFinished", kinds
    assert kinds.count("AssistantDelta") == len("我来查一下。") + len("2024年是闰年。")
    assert kinds.count("ToolCallStarted") == 1
    assert kinds.count("ToolCallFinished") == 1

    started = next(e for e in events if isinstance(e, ToolCallStarted))
    assert started.name == "isLeapYear" and started.arguments == {"year": 2024}
    finished = next(e for e in events if isinstance(e, ToolCallFinished))
    assert finished.ok and "是闰年" in finished.output

    final = next(e for e in events if isinstance(e, TurnFinished))
    assert final.answer == "2024年是闰年。", final.answer

    # 会话历史应包含 user / assistant(+tool_calls) / tool / assistant
    roles = [m["role"] for m in engine.session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"], roles


def test_engine_error_and_cancel() -> None:
    config = LLMConfig(api_key="x", base_url="http://invalid", model="fake")

    class Boom(FakeClient):
        def stream(self, messages, tools=None):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    engine = AgentEngine(config, client=Boom(), skills_root=_NO_SKILLS_ROOT)
    events = list(engine.ask("hi"))
    assert isinstance(events[0], TurnStarted)
    assert any(isinstance(e, AgentError) for e in events), [type(e).__name__ for e in events]
    assert isinstance(events[-1], TurnFinished), [type(e).__name__ for e in events]

    # 取消：第一轮工具执行前停止
    engine2 = AgentEngine(config, client=FakeClient(), skills_root=_NO_SKILLS_ROOT)
    events2 = list(engine2.ask("hi", is_cancelled=lambda: True))
    assert any(isinstance(e, AgentError) for e in events2)
    assert isinstance(events2[-1], TurnFinished)

    # 空问题
    engine3 = AgentEngine(config, client=FakeClient(), skills_root=_NO_SKILLS_ROOT)
    events3 = list(engine3.ask("   "))
    assert isinstance(events3[0], AgentError) and isinstance(events3[-1], TurnFinished)


def test_events_serializable() -> None:
    from core.events import AssistantDelta

    data = AssistantDelta(text="你好").to_dict()
    assert data == {"type": "AssistantDelta", "text": "你好"}, data
    import json

    json.dumps(data, ensure_ascii=False)  # 必须可 JSON 序列化


def test_search_pure_helpers() -> None:
    """searchWeb 纯函数：query 截断规则 + 响应整理（摘要/标题/URL）。"""
    # 1汉字 = 2 字符单位，72 单位 = 36 汉字；ASCII = 1
    assert clip_query("你好") == "你好"
    assert len(clip_query("汉" * 100)) == 36, len(clip_query("汉" * 100))
    assert len(clip_query("a" * 100)) == 72
    assert clip_query("  空格  ") == "空格"
    # 混合截断不切半汉字
    clipped = clip_query("汉字" * 50)
    assert len(clipped) == 36 and clipped.endswith("字")

    text = format_references(_FAKE_SEARCH_RESPONSE)
    assert text.startswith("共 2 条搜索结果"), text[:40]
    assert "[1] 百度搜索开放平台" in text
    assert "URL: https://example.com/baidu-search" in text
    assert "摘要: 摘要第一行 第二行 压平空白" in text      # 换行被压平
    assert "来源: example.com · 2026-10-20 08:00:00" in text
    assert "[2] 参考来源二" in text and "内容摘要二" in text  # snippet 缺失时回退 content

    # 空结果 -> 引导换词；错误码 / 仅有 message -> ToolError（错误即数据）
    assert "未找到相关搜索结果" in format_references({"references": []})
    try:
        format_references({"code": "PermissionLimitExceeded", "message": "配额不足"})
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "配额不足" in str(exc)
    try:
        format_references({"message": "rate limit exceeded"})  # 无 references 也视为错误
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "rate limit exceeded" in str(exc)


def test_search_tool() -> None:
    """searchWeb 工具：请求载荷、鉴权头、结果整理、错误路径（全部离线）。"""
    captured: dict = {}

    def fake_transport(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return _FAKE_SEARCH_RESPONSE

    cfg = SearchConfig(api_key="bce-v3/TESTKEY", top_k=5, recency="month")
    spec = build_search_tool(cfg, transport=fake_transport)

    # ---- 契约：名称 / 描述 / 参数 Schema
    assert spec.name == "searchWeb"
    schema = spec.to_schema()
    assert schema["function"]["name"] == "searchWeb"
    assert "时效" in schema["function"]["description"]
    assert schema["function"]["parameters"]["required"] == ["query"]

    # ---- 正常执行：请求要素 + 输出含摘要/标题/URL
    out = spec.handler({"query": "今日新闻 " * 30})
    assert "共 2 条搜索结果" in out and "URL:" in out and "摘要:" in out
    assert captured["url"] == cfg.endpoint
    assert captured["headers"]["Authorization"] == "Bearer bce-v3/TESTKEY"
    assert captured["payload"]["search_source"] == "baidu_search_v2"
    assert captured["payload"]["search_recency_filter"] == "month"
    assert captured["payload"]["resource_type_filter"] == [{"type": "web", "top_k": 5}]
    # query 已按 72 字符规则截断（"今日新闻 " = 9 单位 => 至多 8 次）
    sent_query = captured["payload"]["messages"][0]["content"]
    assert len(sent_query) <= 72 and sent_query.startswith("今日新闻")

    # ---- 错误路径：注册表统一兜底为 ok=False 文本（错误即数据）
    from core.tools import ToolRegistry

    def broken_transport(url, headers, payload, timeout):
        raise ToolError("搜索接口 HTTP 429: 触发限流")

    reg_bad = ToolRegistry().register(build_search_tool(cfg, transport=broken_transport))
    r = reg_bad.execute("searchWeb", {"query": "今日新闻"})
    assert not r.ok and "429" in r.output, r

    # 空 query -> 报错且不应发起网络请求（注册表兜底）
    reg = ToolRegistry().register(spec)
    r = reg.execute("searchWeb", {"query": "   "})
    assert not r.ok and "query 不能为空" in r.output, r

    # 传输层网络错误 -> ToolError（本地不可达端口，无外网依赖；
    # 拒绝连接 -> 网络错误；防火墙丢包 -> 超时，二者都必须是清晰文本）
    from core.tools.search import http_transport

    try:
        http_transport("http://127.0.0.1:1/search", {}, {}, timeout=2)
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "网络错误" in str(exc) or "超时" in str(exc), exc


def test_search_registry_and_engine() -> None:
    """注册表恒注册 searchWeb；配置状态决定其结果；引擎端到端把搜索结果喂回 LLM。"""
    # 未配置 / 已配置 -> 都是 15 个工具，searchWeb 恒在
    for cfg in (None, SearchConfig(), SearchConfig(api_key="bce-v3/TESTKEY")):
        reg = build_default_registry(cfg)
        assert len(reg) == 15, (cfg, len(reg))
        assert "searchWeb" in reg.names
        assert "searchWeb" in [s["function"]["name"] for s in reg.schemas]

    # 缺少配置 -> 清晰的配置指引（经注册表转成 ok=False 文本）
    r = build_default_registry(SearchConfig()).execute("searchWeb", {"query": "x"})
    assert not r.ok and "Baidu-qianfan-APIKEY" in r.output, r
    assert "百度千帆" in r.output and "config.ini" in r.output, r

    # ---- 引擎端到端（注入假传输，零网络）
    def fake_transport(url, headers, payload, timeout):
        return _FAKE_SEARCH_RESPONSE

    reg = build_default_registry()
    reg.register(build_search_tool(SearchConfig(api_key="k"), transport=fake_transport))

    class SearchFake(FakeClient):
        """第一轮调用 searchWeb，第二轮基于搜索结果作答。"""

        def stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ReplyArrived(message={
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "id": "call_s", "type": "function",
                        "function": {"name": "searchWeb", "arguments": '{"query": "最新科技新闻"}'},
                    }],
                })
            else:
                # 第二轮能看到 tool 结果（含 URL），证明结果已回填会话
                last = messages[-1]
                assert last["role"] == "tool" and "example.com/baidu-search" in last["content"]
                for ch in "根据搜索结果：https://example.com/baidu-search":
                    yield TextDelta(ch)
                yield ReplyArrived(
                    message={"role": "assistant",
                             "content": "根据搜索结果：https://example.com/baidu-search"})

    config = LLMConfig(api_key="x", base_url="http://invalid", model="fake", stream=True)
    engine = AgentEngine(config, registry=reg, client=SearchFake(), skills_root=_NO_SKILLS_ROOT)
    events = list(engine.ask("今天有什么新闻？"))

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert len(finished) == 1
    assert finished[0].name == "searchWeb" and finished[0].ok, finished[0]
    assert "共 2 条搜索结果" in finished[0].output
    assert finished[0].duration_ms >= 0

    final = next(e for e in events if isinstance(e, TurnFinished))
    assert "example.com/baidu-search" in final.answer
    roles = [m["role"] for m in engine.session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"], roles


def test_fetch_curl_command_and_errors() -> None:
    """fetchWeb 的 curl 层：参数列表执行（无 shell）、各类失败的清晰错误。"""
    import subprocess
    from types import SimpleNamespace

    # ---- 命令构造：列表形式、关键参数齐全、URL 独立成参
    cmd = build_curl_command("https://example.com/a?x=1&y=2", timeout=30)
    assert isinstance(cmd, list) and cmd[0] == "curl"
    assert "--max-time" in cmd and "--location" in cmd and "--compressed" in cmd
    assert cmd[-1] == "https://example.com/a?x=1&y=2"      # URL 不经过 shell 解释
    assert any("__ZYAGENT_CURL_META__" in c for c in cmd)  # 状态元信息哨兵

    # ---- 成功抓取：正文与状态元信息分离
    calls: dict = {}

    def ok_runner(cmd, **kw):
        calls.update(cmd=cmd, **kw)
        return SimpleNamespace(
            returncode=0,
            stdout=b"<html><title>T</title><body>hi</body></html>\n__ZYAGENT_CURL_META__200|text/html; charset=utf-8",
            stderr=b"",
        )

    body, status, ctype = fetch_url("https://example.com", timeout=5, runner=ok_runner)
    assert body.startswith(b"<html>") and status == 200 and "text/html" in ctype
    assert calls["shell"] is False and calls["capture_output"] is True  # 明确无 shell
    assert isinstance(calls["cmd"], list)

    # ---- HTTP 错误码
    def resp_404(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=b"not found\n__ZYAGENT_CURL_META__404|text/html", stderr=b"")

    try:
        fetch_url("https://example.com/gone", runner=resp_404)
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "HTTP 404" in str(exc), exc

    # ---- 非网页内容类型
    def resp_pdf(cmd, **kw):
        return SimpleNamespace(returncode=0, stdout=b"%PDF\n__ZYAGENT_CURL_META__200|application/pdf", stderr=b"")

    try:
        fetch_url("https://example.com/a.pdf", runner=resp_pdf)
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "非网页内容" in str(exc), exc

    # ---- 网络失败（curl 非零退出，如域名无法解析）
    def resp_dns(cmd, **kw):
        return SimpleNamespace(returncode=6, stdout=b"", stderr=b"curl: (6) Could not resolve host: no.such.host")

    try:
        fetch_url("https://no.such.host", runner=resp_dns)
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "Could not resolve" in str(exc) and "域名" in str(exc), exc

    # ---- 超时 / curl 缺失
    def resp_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    try:
        fetch_url("https://slow.example.com", runner=resp_timeout)
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "超时" in str(exc), exc

    def resp_missing(cmd, **kw):
        raise FileNotFoundError("curl")

    try:
        fetch_url("https://example.com", runner=resp_missing)
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "curl" in str(exc), exc

    # ---- handler 的 URL 校验
    spec = build_fetch_tool(runner=ok_runner)
    for bad in ("", "   ", "ftp://example.com/x", "file:///etc/passwd"):
        r_msg = ""
        try:
            spec.handler({"url": bad})
            raise AssertionError(f"应拒绝 URL: {bad!r}")
        except ToolError as exc:
            r_msg = str(exc)
        assert "不能为空" in r_msg or "仅支持 http/https" in r_msg, (bad, r_msg)

    # www. 前缀自动补全 https://（空正文确保走到"正文无法提取"报错）
    captured: dict = {}

    def capture_runner(cmd, **kw):
        captured["url"] = cmd[-1]
        return SimpleNamespace(
            returncode=0,
            stdout=b"<html><head><title>p</title></head><body></body></html>\n__ZYAGENT_CURL_META__200|text/html",
            stderr=b"",
        )

    try:
        build_fetch_tool(runner=capture_runner).handler({"url": "www.example.com/page"})
        raise AssertionError("空正文应报错")
    except ToolError:
        pass
    assert captured["url"] == "https://www.example.com/page", captured


def test_fetch_extract_markdown() -> None:
    """fetchWeb 提取层：保留标题/段落/链接/列表/表格，去除导航/广告/页脚。"""
    html = (
        "<html><head><meta charset='utf-8'/><title>天气预报-示例网</title></head><body>"
        "<nav>首页 关于我们 联系 广告合作</nav>"
        "<div class='ad'>点击进入购物广告位</div>"
        "<article>"
        "<h1>北京今日天气</h1>"
        "<p>今天北京晴转多云，最高气温 <strong>18℃</strong>，详见"
        "<a href='https://example.com/detail'>详情页</a>。</p>"
        "<h2>出行建议</h2>"
        "<ul><li>早晚注意保暖</li><li>白天适宜出行</li></ul>"
        "<table><tr><th>城市</th><th>气温</th></tr><tr><td>北京</td><td>7~18℃</td></tr></table>"
        "</article>"
        "<footer>版权所有 © 2026 示例网 · 京ICP备0000号</footer>"
        "</body></html>"
    ).encode("utf-8")

    markdown, title = extract_markdown(html, "https://example.com/weather")
    # 保留：标题/段落/链接/列表/表格
    assert "北京今日天气" in markdown
    assert "详情页" in markdown and "https://example.com/detail" in markdown
    assert "- " in markdown and "早晚注意保暖" in markdown
    assert "| " in markdown and "18℃" in markdown  # 表格保留（~ 按 Markdown 规范转义为 \~）
    # 去除：导航/广告/页脚
    for noise in ("广告合作", "购物广告位", "版权所有", "京ICP备"):
        assert noise not in markdown, (noise, markdown)
    # 标题：优先 h1 正文标题，<title> 兜底
    assert "北京今日天气" in title or "天气预报" in title, title

    # 空白页面 -> 正文无法提取的清晰错误
    try:
        extract_markdown(b"<html><body><script>var x=1;</script></body></html>",
                         "https://example.com/js-only")
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "未能从页面提取到正文" in str(exc) and "JavaScript" in str(exc), exc

    # 超长正文 -> 截断保护上下文
    long_html = ("<html><head><meta charset='utf-8'/></head><body><article><h1>长文</h1>" + "".join(
        f"<p>{'内容句子示例，用于凑足长度。' * 18}</p>" for _ in range(120)
    ) + "</article></body></html>").encode("utf-8")
    md, _ = extract_markdown(long_html, "https://example.com/long")
    assert len(md) <= 20_200 and "已截断" in md, len(md)


def test_run_cli() -> None:
    """RunCli：列表执行（无 shell）、stdout/stderr/退出码回传、截断、超时与参数校验。"""
    import subprocess as sp
    from types import SimpleNamespace

    # ---- 命令拼装：列表 + shell=False，参数按字面量传递
    captured: dict = {}

    def make_runner(stdout=b"", stderr=b"", returncode=0):
        def runner(cmd, **kw):
            captured.update(cmd=cmd, **kw)
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        return runner

    spec = build_run_cli_tool(runner=make_runner(stdout=b"Python 3.13.15\n"))
    out = spec.handler({"command": "python", "args": ["--version"]})
    assert captured["cmd"] == ["python", "--version"], captured["cmd"]
    assert captured["shell"] is False and captured["capture_output"] is True
    # 结构化结果：命令 / 退出码 / stdout / stderr
    assert "命令: python --version" in out
    assert "退出码: 0" in out and "[stdout]" in out and "Python 3.13.15" in out
    assert "[stderr]" in out and "(空)" in out

    # ---- 退出码非 0：完整回传并附失败提示（ok=True，由模型判断）
    spec2 = build_run_cli_tool(runner=make_runner(stderr=b"error: bad option", returncode=1))
    out2 = spec2.handler({"command": "tool", "args": ["--bad"]})
    assert "退出码: 1" in out2 and "error: bad option" in out2
    assert "提示:" in out2 and "退出码 1" in out2

    # ---- 中文 GBK 输出正确解码（中文 Windows 控制台场景）
    spec3 = build_run_cli_tool(runner=make_runner(stdout="目录内容：中文测试".encode("gbk")))
    out3 = spec3.handler({"command": "dir", "args": []})
    assert "目录内容：中文测试" in out3, out3

    # ---- 超长输出截断并注明原始长度
    spec4 = build_run_cli_tool(runner=make_runner(stdout=b"x" * 10000))
    out4 = spec4.handler({"command": "big", "args": []})
    assert "已截断" in out4 and "原始共 10000 字符" in out4 and len(out4) < 8000, len(out4)

    # ---- 超时：进程终止 + 清晰错误（含部分输出）
    def timeout_runner(cmd, **kw):
        raise sp.TimeoutExpired(cmd=cmd, timeout=60, output=b"partial out")

    try:
        build_run_cli_tool(runner=timeout_runner).handler({"command": "slow", "args": []})
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "超时" in str(exc) and "已被终止" in str(exc), exc

    # ---- 命令未找到
    def missing_runner(cmd, **kw):
        raise FileNotFoundError("no such program")

    try:
        build_run_cli_tool(runner=missing_runner).handler({"command": "nope.exe", "args": []})
        raise AssertionError("应抛出 ToolError")
    except ToolError as exc:
        assert "未找到命令" in str(exc) and "nope.exe" in str(exc), exc

    # ---- 参数校验（错误即数据，提示模型自我纠正）
    for bad_args in ({"command": "", "args": []},
                     {"command": "python\nrm", "args": []},
                     {"command": "python", "args": "--version"},
                     {"command": "python", "args": {"a": 1}}):
        try:
            spec.handler(bad_args)
            raise AssertionError(f"应拒绝参数: {bad_args}")
        except ToolError as exc:
            assert "不能为空" in str(exc) or "非法字符" in str(exc) or "字符串数组" in str(exc), exc

    # ---- 真实子进程冒烟（本机 python，无网络依赖）
    real = build_run_cli_tool(timeout=30)
    out5 = real.handler({"command": sys.executable, "args": ["--version"]})
    assert "退出码: 0" in out5 and "Python" in out5, out5


def test_skills_discovery_and_prompt() -> None:
    """Skill 发现：Frontmatter 解析、必须字段校验、System Prompt 注入片段。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skills_dir = root / ".agents" / "skills"

        # alpha：标准 SKILL.md（引号标量）+ 附加文件
        alpha = skills_dir / "alpha"
        alpha.mkdir(parents=True)
        (alpha / "SKILL.md").write_text(
            '---\nname: "alpha"\ndescription: \'Alpha 技能：处理 A 类任务\'\n---\n'
            "\n# Alpha\n\n完整正文不应在发现阶段被读取。\n",
            encoding="utf-8")
        (alpha / "notes.md").write_text("alpha notes", encoding="utf-8")

        # beta：| 块标量 description（多行）
        beta = skills_dir / "beta"
        beta.mkdir(parents=True)
        (beta / "SKILL.md").write_text(
            "---\nname: beta\ndescription: |-\n  Beta 技能。\n  支持多行描述。\n---\n\n# Beta\n",
            encoding="utf-8")

        # omega：小写 skill.md（大小写不敏感兜底）
        omega = skills_dir / "omega"
        omega.mkdir(parents=True)
        (omega / "skill.md").write_text(
            "---\nname: omega\ndescription: Omega 技能\n---\n", encoding="utf-8")

        # gamma：缺 description（必须字段缺失 -> 跳过）
        gamma = skills_dir / "gamma"
        gamma.mkdir(parents=True)
        (gamma / "SKILL.md").write_text("---\nname: gamma\n---\n", encoding="utf-8")

        # delta：无 SKILL.md（跳过）；顶层散文件（忽略）
        delta = skills_dir / "delta"
        delta.mkdir(parents=True)
        (delta / "README.md").write_text("not a skill", encoding="utf-8")
        (skills_dir / "stray.md").write_text("stray", encoding="utf-8")

        skills = discover_skills(root)
        assert [s.name for s in skills] == ["alpha", "beta", "omega"], [s.name for s in skills]
        assert skills[0].description == "Alpha 技能：处理 A 类任务"
        assert skills[1].description == "Beta 技能。\n支持多行描述。"   # 块标量保留多行
        assert skills[0].path.name == "SKILL.md" and skills[0].dir == alpha
        assert skills[2].path.name.lower() == "skill.md"             # 大小写不敏感（Windows 上直接命中）
        assert discover_skills(root / "nope") == []                   # 无 skills 目录

        # Frontmatter 纯函数：无 frontmatter / 注释 / 列表项
        assert parse_frontmatter("无 frontmatter 的普通文本") == {}
        assert parse_frontmatter(
            "---\n# 注释\nname: x\nignored:\n- a\n- b\ndescription: 说明\n---\n正文")["name"] == "x"

        # System Prompt 片段：清单 + loadSkill 指引；无 Skill 时为空
        fragment = build_skills_prompt(skills)
        assert fragment.startswith("You have access to the following skills:")
        assert "- 'alpha': Alpha 技能：处理 A 类任务" in fragment
        assert "- 'beta':" in fragment and "- 'omega':" in fragment
        assert "loadSkill" in fragment and "additional files" in fragment
        assert "完整正文不应在发现阶段" not in fragment               # 正文不进入发现阶段
        assert build_skills_prompt([]) == ""


def test_load_skill_tool() -> None:
    """loadSkill：SKILL.md 全文 / 附加文件解析 / 多候选 / 越界防护。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sdir = root / ".agents" / "skills" / "demo"
        sdir.mkdir(parents=True)
        (sdir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: 演示技能\n---\n\n# Demo\n\n按步骤执行。\n",
            encoding="utf-8")
        (sdir / "refs").mkdir()
        (sdir / "refs" / "template.md").write_text("模板正文", encoding="utf-8")
        (sdir / "refs" / "extra").mkdir()
        (sdir / "refs" / "extra" / "template.md").write_text("另一模板", encoding="utf-8")
        (sdir / "unique.md").write_text("唯一文件内容", encoding="utf-8")

        spec = build_load_skill_tool(discover_skills(root), root=root)

        # ---- 契约：名称 / 描述 / 参数 Schema（filename 可选）
        assert spec.name == "loadSkill"
        fn = spec.to_schema()["function"]
        assert fn["parameters"]["required"] == ["name"]
        assert "filename" in fn["parameters"]["properties"] and "loadSkill" in fn["description"]

        # 1) 仅 name -> SKILL.md 全文
        out = spec.handler({"name": "demo"})
        assert "# Demo" in out and "按步骤执行" in out

        # 2) name + 相对路径 -> 精确命中优先（同名文件存在也不歧义）
        assert spec.handler({"name": "demo", "filename": "refs/template.md"}) == "模板正文"

        # 3) 仅文件名 -> Skill 目录内递归唯一命中
        assert spec.handler({"name": "demo", "filename": "unique.md"}) == "唯一文件内容"

        # 4) 同名多文件 -> 返回候选路径由 LLM 决定
        try:
            spec.handler({"name": "demo", "filename": "template.md"})
            raise AssertionError("多候选应报错")
        except ToolError as exc:
            assert "多个匹配" in str(exc), exc
            assert "refs/template.md" in str(exc) and "refs/extra/template.md" in str(exc), exc

        # 5) 项目相对路径
        assert spec.handler({"name": "demo", "filename": ".agents/skills/demo/unique.md"}) == "唯一文件内容"

        # 6) 绝对路径：目录内允许、越界拒绝
        assert spec.handler({"name": "demo", "filename": str(sdir / "unique.md")}) == "唯一文件内容"
        try:
            spec.handler({"name": "demo", "filename": str(root / "outside.md")})
            raise AssertionError("越界应报错")
        except ToolError as exc:
            assert "越界" in str(exc), exc

        # 7) 未命中 -> 列出可用文件辅助重试
        try:
            spec.handler({"name": "demo", "filename": "missing.md"})
            raise AssertionError("未命中应报错")
        except ToolError as exc:
            assert "未找到文件" in str(exc) and "unique.md" in str(exc), exc

        # 8) 未知 Skill / 空 name / 大小写不敏感
        for bad in ({"name": "nope"}, {"name": ""}):
            try:
                spec.handler(bad)
                raise AssertionError(f"应报错: {bad}")
            except ToolError as exc:
                assert "未找到" in str(exc) or "不能为空" in str(exc), exc
        assert spec.handler({"name": "DEMO"}) == out

        # 9) 注册表兜底（错误即数据）
        from core.tools import ToolRegistry

        r = ToolRegistry().register(spec).execute("loadSkill", {"name": "nope"})
        assert not r.ok and r.output.startswith("错误"), r


def test_engine_skills_integration() -> None:
    """引擎：发现 Skill -> System Prompt 注入 + loadSkill 注册 + 端到端加载。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sdir = root / ".agents" / "skills" / "demo"
        sdir.mkdir(parents=True)
        (sdir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: 演示技能\n---\n\n# Demo\n\n技能正文内容。\n",
            encoding="utf-8")

        config = LLMConfig(api_key="x", base_url="http://invalid", model="fake", stream=True,
                            system_prompt="你是 ZYAgent。")

        # ---- System Prompt 注入 + loadSkill 注册（15 + 1 = 16）
        engine = AgentEngine(config, client=FakeClient(), skills_root=root)
        system = engine.session.messages[0]
        assert system["role"] == "system" and "你是 ZYAgent。" in system["content"]
        assert "You have access to the following skills:" in system["content"]
        assert "'demo': 演示技能" in system["content"]
        assert "loadSkill" in system["content"]
        assert "loadSkill" in engine.registry.names and len(engine.tools) == 18
        assert "createSkill" in engine.registry.names            # 恒注册
        assert "recallContext" in engine.registry.names          # 恒注册

        # reset 保留含技能清单的 System Prompt
        engine.reset()
        assert engine.session.messages[0]["role"] == "system"
        assert "'demo'" in engine.session.messages[0]["content"]

        # ---- 端到端：模型调用 loadSkill -> SKILL.md 正文回填会话
        class SkillFake(FakeClient):
            def stream(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield ReplyArrived(message={
                        "role": "assistant", "content": "",
                        "tool_calls": [{
                            "id": "call_k", "type": "function",
                            "function": {"name": "loadSkill", "arguments": '{"name": "demo"}'},
                        }],
                    })
                else:
                    last = messages[-1]
                    assert last["role"] == "tool" and "技能正文内容" in last["content"]
                    yield ReplyArrived(message={"role": "assistant", "content": "已加载 demo 技能。"})

        engine2 = AgentEngine(config, client=SkillFake(), skills_root=root)
        events = list(engine2.ask("加载演示技能"))
        finished = [e for e in events if isinstance(e, ToolCallFinished)]
        assert len(finished) == 1 and finished[0].name == "loadSkill" and finished[0].ok, finished

    # ---- 无 skills 目录 -> System Prompt 不变、无 loadSkill，但 createSkill 恒在
    empty_root = Path(tempfile.mkdtemp())
    engine3 = AgentEngine(config, client=FakeClient(), skills_root=empty_root)
    assert engine3.session.messages[0]["content"] == "你是 ZYAgent。"
    assert "loadSkill" not in engine3.registry.names and len(engine3.tools) == 17
    assert "createSkill" in engine3.registry.names


def test_create_skill() -> None:
    """createSkill：名称规范化、Frontmatter 保障、防覆盖、注册表兜底。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spec = build_create_skill_tool(root=root)

        # ---- 契约：两个必需参数
        fn = spec.to_schema()["function"]
        assert spec.name == "createSkill"
        assert fn["parameters"]["required"] == ["name", "skillContext"]
        assert "createDirectory" in fn["description"]   # 附加文件交给 fileOps

        # 1) 自带 Frontmatter：name 规范化 + 强制与目录一致，正文原样保留
        out = spec.handler({
            "name": "My Cool Skill",
            "skillContext": "---\nname: My Cool Skill\ndescription: 这是一个很酷的技能\n---\n"
                            "\n# 正文\n\n步骤一。\n",
        })
        assert "已创建" in out and "my-cool-skill" in out, out
        md = root / ".agents" / "skills" / "my-cool-skill" / "SKILL.md"
        assert md.is_file(), "应创建 .agents/skills/<name>/SKILL.md"
        meta, body = split_frontmatter(md.read_text(encoding="utf-8"))
        assert meta["name"] == "my-cool-skill"               # 与目录名一致
        assert meta["description"] == "这是一个很酷的技能"      # 保留原 description
        assert "# 正文" in body and "步骤一" in body           # 正文不丢

        # 创建结果符合规范，可被发现机制直接读取
        skills = discover_skills(root)
        assert [s.name for s in skills] == ["my-cool-skill"]
        assert skills[0].description == "这是一个很酷的技能"

        # 2) 无 Frontmatter -> 自动合成，description 取正文首行
        spec.handler({"name": "plain-skill", "skillContext": "# plain技能\n\n做某件事的流程。\n"})
        meta2, body2 = split_frontmatter(
            (root / ".agents/skills/plain-skill/SKILL.md").read_text(encoding="utf-8"))
        assert meta2["name"] == "plain-skill"
        assert meta2["description"] == "plain技能"
        assert "做某件事的流程" in body2

        # 3) Frontmatter 缺 description -> 从正文首行补齐
        spec.handler({"name": "no-desc",
                      "skillContext": "---\nname: no-desc\n---\n\n这是补充说明第一句。\n\n更多内容。\n"})
        meta3, body3 = split_frontmatter(
            (root / ".agents/skills/no-desc/SKILL.md").read_text(encoding="utf-8"))
        assert meta3["description"] == "这是补充说明第一句。"
        assert "更多内容" in body3

        # 4) 已存在 -> 明确提示且绝不覆盖
        before = md.read_text(encoding="utf-8")
        try:
            spec.handler({"name": "my-cool-skill",
                          "skillContext": "---\nname: x\ndescription: 覆盖测试\n---\nnew"})
            raise AssertionError("已存在应报错")
        except ToolError as exc:
            assert "已存在" in str(exc) and "不会覆盖" in str(exc) and "editFile" in str(exc), exc
        assert md.read_text(encoding="utf-8") == before        # 原内容分毫未动
        assert len(discover_skills(root)) == 3                 # 未产生重复技能

        # 5) 非法名称 / 空 skillContext（错误即数据，模型可自我纠正）
        for bad in ({"name": "！！！", "skillContext": "内容"},
                    {"name": "ok-skill", "skillContext": "   "},
                    {"name": "", "skillContext": "内容"}):
            try:
                spec.handler(bad)
                raise AssertionError(f"应拒绝参数: {bad}")
            except ToolError as exc:
                assert "无法生成合法" in str(exc) or "不能为空" in str(exc), exc

        # 6) 注册表兜底（错误即数据）
        from core.tools import ToolRegistry

        r = ToolRegistry().register(spec).execute("createSkill", {"name": "", "skillContext": "x"})
        assert not r.ok and r.output.startswith("错误"), r


def test_mcp_config_and_expose() -> None:
    """MCP 配置解析（stdio / streamable_http / 别名 / 错误）与工具名冲突映射。"""
    import json

    from core.mcp_client import expose_tool_name, load_mcp_config

    with tempfile.TemporaryDirectory() as tmp:
        cfg_file = Path(tmp) / ".mcp.json"

        # 文件不存在 -> 未配置（正常状态）
        assert load_mcp_config(cfg_file) == ([], [])

        # 正常：stdio + streamable_http + type 别名 + 缺 type 自动推断
        cfg_file.write_text(json.dumps({"mcpServers": {
            "A": {"type": "stdio", "command": "python", "args": ["-m", "srv"],
                  "env": {"K": "V"}},
            "B": {"type": "streamable-http", "url": "https://x.example/mcp"},
            "C": {"url": "http://y.example/mcp"},
            "D": {"command": "node"},
        }}), encoding="utf-8")
        servers, errors = load_mcp_config(cfg_file)
        assert errors == [], errors
        assert [s.name for s in servers] == ["A", "B", "C", "D"]
        assert servers[0].type == "stdio" and servers[0].command == "python"
        assert servers[0].args == ("-m", "srv") and servers[0].env == {"K": "V"}
        assert servers[1].type == "streamable_http"
        assert servers[1].url == "https://x.example/mcp"
        assert servers[2].type == "streamable_http" and servers[3].type == "stdio"

        # 非法项逐条记录问题，但不阻断合法 Server
        cfg_file.write_text(json.dumps({"mcpServers": {
            "bad1": {"type": "sse", "url": "https://x"},
            "bad2": {"type": "stdio"},
            "bad3": {"type": "streamable_http", "url": "ftp://x"},
            "ok": {"type": "stdio", "command": "python"},
        }}), encoding="utf-8")
        servers, errors = load_mcp_config(cfg_file)
        assert [s.name for s in servers] == ["ok"], servers
        assert len(errors) == 3, errors
        assert any("不支持的 type" in e for e in errors)
        assert any("command" in e for e in errors)
        assert any("http/https" in e for e in errors)

        # JSON 非法 / 缺 mcpServers
        cfg_file.write_text("{不是 json", encoding="utf-8")
        servers, errors = load_mcp_config(cfg_file)
        assert servers == [] and "解析失败" in errors[0], errors
        cfg_file.write_text("{}", encoding="utf-8")
        _, errors = load_mcp_config(cfg_file)
        assert "mcpServers" in errors[0]

    # ---- 名称映射：无冲突原名 / 冲突加服务器前缀 / 清洗 / 前缀再去重
    taken = {"getDate", "bazi_1"}
    assert expose_tool_name("S1", "weather", taken) == "weather"
    assert expose_tool_name("S1", "getDate", taken) == "S1_getDate"
    assert expose_tool_name("S 2!", "we/ather", taken) == "we_ather"     # 非法字符清洗
    assert expose_tool_name("S 2!", "getDate", taken) == "S_2__getDate"  # 冲突 + 清洗
    assert expose_tool_name("S1", "getDate", {"getDate", "S1_getDate"}) == "S1_getDate_2"  # 前缀也冲突
    assert expose_tool_name("S1", "my.tool", {"my_tool"}) == "S1_my_tool"  # 清洗制造的冲突


def test_mcp_manager_bridge() -> None:
    """MCPManager：工具注册（前缀映射）、异步调用桥接、超时/错误隔离、引擎集成。"""
    import asyncio
    import json
    from types import SimpleNamespace

    from core.mcp_client import MCPManager, MCPServerConfig, format_call_result

    class FakeSession:
        """假 MCP 会话：记录调用，返回/抛出预设结果。"""

        def __init__(self, result=None, exc: Exception | None = None, delay: float = 0.0):
            self.result = result
            self.exc = exc
            self.delay = delay
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.exc:
                raise self.exc
            return self.result

    ok_result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="排盘: 甲子年")],
        is_error=False,
        structured_content={"year": "甲子"},
    )

    # ---- 注册：冲突自动加服务器前缀，Schema 原样透传，路由表建立
    manager = MCPManager(init_timeout=3, call_timeout=5)
    reg = build_default_registry()
    cfg = MCPServerConfig(name="Bazi-MCP", type="streamable_http", url="https://example/mcp")
    fake_tools = [
        SimpleNamespace(name="bazi", description="八字排盘",
                        input_schema={"type": "object",
                                      "properties": {"birth": {"type": "string"}},
                                      "required": ["birth"]}),
        SimpleNamespace(name="getDate", description="与本地同名", input_schema={}),
    ]
    manager._register(cfg, fake_tools, reg)
    assert "bazi" in reg and "Bazi-MCP_getDate" in reg, reg.names
    assert manager._routes["bazi"] == ("Bazi-MCP", "bazi")
    assert manager._routes["Bazi-MCP_getDate"] == ("Bazi-MCP", "getDate")
    assert manager.registered["Bazi-MCP"] == ["bazi", "Bazi-MCP_getDate"]
    schema = next(s for s in reg.schemas if s["function"]["name"] == "bazi")
    assert schema["function"]["parameters"]["required"] == ["birth"]

    # ---- 同步工具处理器 -> 后台线程事件循环的异步桥接
    manager._ensure_loop()
    session = FakeSession(result=ok_result)
    manager._sessions["Bazi-MCP"] = session

    r = reg.execute("bazi", {"birth": "1990-05-01"})
    assert r.ok and "排盘: 甲子年" in r.output, r
    assert "结构化数据" in r.output and "甲子" in r.output      # 文本 + 结构化都回传
    assert session.calls == [("bazi", {"birth": "1990-05-01"})]

    # ---- 服务器返回 isError -> ok=False（错误即数据）
    manager._sessions["Bazi-MCP"] = FakeSession(result=SimpleNamespace(
        content=[SimpleNamespace(type="text", text="出生时间无效")],
        is_error=True, structured_content=None))
    r = reg.execute("bazi", {"birth": ""})
    assert not r.ok and "出生时间无效" in r.output, r

    # ---- 传输异常 -> 清晰文本（含 Server 名）
    manager._sessions["Bazi-MCP"] = FakeSession(exc=RuntimeError("connection lost"))
    r = reg.execute("bazi", {})
    assert not r.ok and "connection lost" in r.output and "Bazi-MCP" in r.output, r

    # ---- 调用超时 -> 清晰文本
    manager.call_timeout = 0.2
    manager._sessions["Bazi-MCP"] = FakeSession(result=ok_result, delay=1.0)
    r = reg.execute("bazi", {})
    assert not r.ok and "超时" in r.output, r
    manager.call_timeout = 5

    # ---- Server 未连接 -> 清晰文本
    manager._sessions.pop("Bazi-MCP")
    r = reg.execute("bazi", {})
    assert not r.ok and "未连接" in r.output, r
    manager.stop()

    # ---- 结果整理纯函数：图片占位 / 空内容兜底
    img = SimpleNamespace(content=[SimpleNamespace(type="image", mimeType="image/png")],
                          is_error=False, structured_content=None)
    assert "[图片 image/png" in format_call_result(img)
    empty = SimpleNamespace(content=[], is_error=False, structured_content=None)
    assert "没有返回内容" in format_call_result(empty)

    # ---- 无法连接的 Server：错误清晰、本地工具完好、不抛异常
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "dead": {"type": "streamable_http", "url": "http://127.0.0.1:1/mcp"},
        }}), encoding="utf-8")
        reg2 = build_default_registry()
        m2 = MCPManager(init_timeout=3, call_timeout=3)
        m2.start(root, reg2)                     # 不应抛出
        assert len(reg2) == 15, reg2.names        # 本地工具完好
        kind, detail = m2.server_status["dead"]
        assert kind == "error" and detail, detail
        assert "FAIL dead" in m2.status()
        m2.stop()

        # ---- 引擎集成：坏 MCP 配置不拖垮引擎（15 本地 + createSkill，MCP 工具不注册）
        engine = AgentEngine(
            LLMConfig(api_key="x", base_url="http://invalid", model="fake"),
            client=FakeClient(), skills_root=root / "no-skills", mcp_root=root,
            mcp_async=False,
        )
        assert len(engine.tools) == 17, len(engine.tools)
        assert "FAIL dead" in engine.mcp.status()
        engine.mcp.stop()

    # ---- 无配置 -> 零副作用（不启动后台线程）
    with tempfile.TemporaryDirectory() as tmp:
        m3 = MCPManager()
        m3.start(Path(tmp), build_default_registry())
        assert m3._loop is None
        assert "未配置 .mcp.json" in m3.status()


def test_context_token_estimation() -> None:
    """Token 估算 / API usage 解析 / k 格式化 / 压缩后消息组装。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100          # ASCII 约 4 字符 1 token
    assert estimate_tokens("中" * 100) == 100         # 中文约 1 字 1 token
    assert estimate_tokens("abc中文") == 3            # round(3/4) + 2
    assert estimate_messages_tokens([{"role": "user", "content": "你好"}]) > 0

    assert usage_total_tokens({"total_tokens": 15, "prompt_tokens": 10}) == 15
    assert usage_total_tokens({"prompt_tokens": 10, "completion_tokens": 5}) == 15
    assert usage_total_tokens({}) is None
    assert usage_total_tokens(None) is None

    assert format_tokens_k(128_000) == "128k"
    assert format_tokens_k(12_345) == "12.3k"
    assert format_tokens_k(950) == "950"

    messages = build_compacted_messages(
        "你是 ZYAgent。", "## 摘要\n- 已完成", "20261022-153000", "2026-10-22 15:30:00"
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "## 摘要" in messages[1]["content"]
    assert "20261022-153000" in messages[1]["content"]
    assert "recallContext" in messages[1]["content"]
    assert "2026-10-22 15:30:00" in messages[1]["content"]


def test_context_archive_and_recall() -> None:
    """归档 JSONL 完整性 + recallContext 全文检索（编号/位置/片段/长度限制）。"""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / ".agents" / "context"
        messages = [
            {"role": "system", "content": "你是 ZYAgent"},
            {"role": "user", "content": "请修改 config.ini 里的模型名称"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "editFile", "arguments": '{"path":"config.ini"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "已写入 config.ini"},
        ]

        # 编号格式与唯一性
        archive_id = new_archive_id(directory, datetime(2026, 10, 22, 15, 30, 0))
        assert archive_id == "20261022-153000"
        path = save_archive(directory, archive_id, messages)
        assert path.name == "20261022-153000.jsonl"
        assert new_archive_id(directory, datetime(2026, 10, 22, 15, 30, 0)) == \
            "20261022-153000-1"

        # 归档内容：角色/内容/工具参数/工具结果/顺序全部保留
        records = [json.loads(line) for line in
                   path.read_text(encoding="utf-8").splitlines()]
        assert [r["role"] for r in records] == ["system", "user", "assistant", "tool"]
        assert [r["index"] for r in records] == [0, 1, 2, 3]
        assert records[2]["tool_calls"][0]["function"]["name"] == "editFile"
        assert records[3]["content"] == "已写入 config.ini"

        # ---- recallContext 工具
        spec = build_recall_tool(tmp)
        assert spec.name == RECALL_TOOL_NAME
        schema = spec.to_schema()["function"]
        assert schema["parameters"]["required"] == ["text"]
        assert "id" in schema["parameters"]["properties"]

        out = spec.handler({"text": "config.ini"})
        assert archive_id in out and "role=user" in out and "config.ini" in out
        assert spec.handler({"text": "editFile"}).count("editFile") >= 1
        assert "role=tool" in spec.handler({"text": "已写入"})
        assert archive_id in spec.handler({"text": "模型名称", "id": archive_id})
        assert "未在" in spec.handler({"text": "完全不存在的关键词"})

        # 错误路径（错误即数据）
        reg = ToolRegistry().register(spec)
        r = reg.execute("recallContext", {"text": "x", "id": "nope"})
        assert not r.ok and "未找到编号" in r.output, r
        r = reg.execute("recallContext", {"text": "   "})
        assert not r.ok and "不能为空" in r.output, r
        assert "尚无上下文归档" in build_recall_tool(Path(tmp) / "none").handler(
            {"text": "x"}
        )

        # 结果长度受控：大量命中时不会无限返回
        many = [{"role": "user", "content": "关键词" * 500} for _ in range(30)]
        save_archive(directory, "20261023-090000", many)
        result = spec.handler({"text": "关键词"})
        assert len(result) <= 4500, len(result)
        assert "仅显示前" in result


def test_compact_flow() -> None:
    """完整 /compact 流程：归档 → LLM 摘要 → 替换上下文 → 失败保持原状。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = LLMConfig(
            api_key="x", base_url="http://invalid", model="fake", stream=True,
            system_prompt="你是 ZYAgent。",
        )
        client = CompactFake()
        engine = AgentEngine(config, client=client, skills_root=root)
        list(engine.ask("2024年是闰年吗？"))
        assert len(engine.session.messages) == 5      # system + user + assistant + tool + assistant
        assert client.summary_calls == 0

        events = list(engine.compact())
        kinds = [type(e).__name__ for e in events]
        assert kinds[0] == "CompactStarted" and kinds[-1] == "CompactFinished", kinds
        assert client.summary_calls == 1               # 压缩只调用一次 LLM 生成摘要

        finished = events[-1]
        assert finished.ok and finished.archive_id, finished

        # 原始上下文归档：顺序、工具调用参数、工具结果完整保留
        archive = root / ".agents" / "context" / f"{finished.archive_id}.jsonl"
        assert archive.is_file()
        records = [json.loads(line) for line in
                   archive.read_text(encoding="utf-8").splitlines()]
        assert [r["role"] for r in records] == \
            ["system", "user", "assistant", "tool", "assistant"]
        assert records[2]["tool_calls"][0]["function"]["name"] == "isLeapYear"
        assert "是闰年" in records[3]["content"]

        # 会话替换：保留 System Prompt；摘要末尾带归档说明
        messages = engine.session.messages
        assert [m["role"] for m in messages] == ["system", "user"]
        assert messages[0]["content"] == "你是 ZYAgent。"
        content = messages[1]["content"]
        assert "工作摘要" in content
        assert finished.archive_id in content and "recallContext" in content
        assert "完成压缩" in content
        assert not engine.input_blocked
        assert engine.context_usage().used < 3000

        # 摘要生成失败：保留原上下文、报告错误、可重试
        class BadCompact(CompactFake):
            def stream(self, messages, tools=None):
                if tools is None:
                    raise LLMError("summary boom")
                yield from super().stream(messages, tools)

        engine2 = AgentEngine(config, client=BadCompact(), skills_root=root)
        list(engine2.ask("2024年是闰年吗？"))
        before = engine2.session.messages
        events2 = list(engine2.compact())
        assert any(isinstance(e, AgentError) for e in events2)
        assert isinstance(events2[-1], CompactFinished) and not events2[-1].ok
        assert engine2.session.messages == before
        assert len(list((root / ".agents" / "context").glob("*.jsonl"))) >= 2

        # 没有可压缩的历史
        empty = AgentEngine(config, client=CompactFake(), skills_root=root)
        events3 = list(empty.compact())
        assert isinstance(events3[-1], CompactFinished) and not events3[-1].ok


def test_context_threshold_block() -> None:
    """API usage 达到 80%：警告 + 暂停普通提问；/compact 成功后解除限制。"""
    with tempfile.TemporaryDirectory() as tmp:
        config = LLMConfig(
            api_key="x", base_url="http://invalid", model="fake", stream=True,
            context_window=4000, context_window_configured=True,
        )
        engine = AgentEngine(config, client=UsageFake(3400), skills_root=Path(tmp))

        events = list(engine.ask("2024年是闰年吗？"))
        usage = engine.context_usage()
        assert usage.source == "api" and usage.used == 3400, usage
        assert abs(usage.percent - 85.0) < 0.01
        assert engine.input_blocked
        assert any(isinstance(e, ContextWarning) for e in events)
        assert isinstance(events[-1], TurnFinished)    # TurnFinished 仍为最后一个事件

        # 暂停接受新的普通提示词：不写入历史
        before = len(engine.session.messages)
        events2 = list(engine.ask("再问一个问题"))
        assert isinstance(events2[0], AgentError) and "compact" in events2[0].message
        assert isinstance(events2[-1], TurnFinished)
        assert len(engine.session.messages) == before

        # /compact 成功 -> 解除限制
        events3 = list(engine.compact())
        assert isinstance(events3[-1], CompactFinished) and events3[-1].ok
        assert not engine.input_blocked
        assert engine.context_usage().used < 3200

        events4 = list(engine.ask("压缩后继续"))
        assert not any(
            isinstance(e, AgentError) and "暂停接收" in e.message for e in events4
        )


def test_config_bootstrap_and_sections() -> None:
    """首次运行生成完整 config.ini（mock），并能解析所有小节与默认值。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.ini"
        path, created = ensure_config(cfg)
        assert created and path == cfg and cfg.is_file()
        text = cfg.read_text(encoding="utf-8")
        assert "sk-mock-replace-me" in text and "[context]" in text
        assert "[mcp]" in text and "[ui]" in text and "[sessions]" in text

        app = load_app_config(cfg)
        assert app.llm.api_key == "sk-mock-replace-me"
        assert app.llm.context_window_configured is True
        assert app.context.warning_ratio == 0.8
        assert app.context.recall_max_matches == 8
        assert app.mcp.init_timeout == 15 and app.mcp.async_load is True
        assert app.ui.sidebar_width == 36
        assert app.sessions.auto_save is True and app.sessions.enabled is True

        # 已存在 -> 不覆盖、不重复创建
        before = cfg.read_text(encoding="utf-8")
        _path, created2 = ensure_config(cfg)
        assert not created2 and cfg.read_text(encoding="utf-8") == before

        # 行内 ; 注释应被正确忽略（配置示例也依赖该行为）
        assert app.llm.stream is True


def test_session_store_lifecycle() -> None:
    """会话保存 / 自动保存 / 新建 / 列表 / 恢复 / 导出。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = LLMConfig(api_key="x", base_url="http://invalid", model="fake")
        engine = AgentEngine(
            config, client=FakeClient(), skills_root=root, mcp_async=False
        )
        list(engine.ask("2024年是闰年吗？"))
        first_id = engine.session_id

        # 每轮结束自动保存
        assert engine.history.exists(first_id)
        path = engine.save_session("闰年测试")
        assert path.is_file() and path.parent == root / ".agents" / "sessions"

        sessions = engine.list_sessions()
        assert sessions and sessions[0].id == first_id
        assert "闰年测试" in sessions[0].title
        assert sessions[0].messages == 4

        # /new：先保存旧会话，再换新编号、清空历史
        saved = engine.new_session()
        assert saved is not None and saved.is_file()
        assert engine.session_id != first_id
        assert engine.session.messages == []

        # /resume：恢复消息与编号
        info = engine.resume_session(first_id)
        assert info.id == first_id and engine.session_id == first_id
        assert [m["role"] for m in engine.session.messages] == \
            ["user", "assistant", "tool", "assistant"]

        # 导出 Markdown
        md = engine.export_markdown()
        body = md.read_text(encoding="utf-8")
        assert "2024年是闰年" in body and "isLeapYear" in body


def test_agents_md_once_and_persisted() -> None:
    """AGENTS.md：首次注入一次、持久化、压缩后保留、恢复不重复。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "AGENTS.md").write_text(
            "# 项目约定\n\n- 始终使用中文回答。\n", encoding="utf-8"
        )
        config = LLMConfig(
            api_key="x", base_url="http://invalid", model="fake",
            system_prompt="你是 ZYAgent。",
        )
        engine = AgentEngine(
            config, client=CompactFake(), skills_root=root, mcp_async=False
        )

        # 首次加载即注入，且只有一条
        messages = engine.session.messages
        assert messages[0]["role"] == "system"
        assert len(messages) == 2 and messages[1]["role"] == "user"
        assert messages[1]["content"].startswith(AGENTS_MD_PREFIX)
        assert "始终使用中文回答" in messages[1]["content"]
        assert engine.agents_md_injected
        assert not engine._ensure_agents_md()          # 幂等

        # 持久化到会话事件日志
        list(engine.ask("2024年是闰年吗？"))
        info, loaded = engine.history.load(engine.session_id)
        agents = [m for m in loaded if str(m.get("content", "")).startswith(AGENTS_MD_PREFIX)]
        assert len(agents) == 1 and "项目约定" in agents[0]["content"]

        # 压缩后仍然保留（一次注入，全程复用）
        finished = list(engine.compact())[-1]
        assert isinstance(finished, CompactFinished) and finished.ok
        compacted = engine.session.messages
        agents_after = [m for m in compacted
                        if str(m.get("content", "")).startswith(AGENTS_MD_PREFIX)]
        assert len(agents_after) == 1
        assert [m["role"] for m in compacted] == ["system", "user", "user"]

        # 新建会话重新注入；恢复旧会话不重复注入
        engine.new_session()
        assert engine.agents_md_injected
        assert len(engine.session.messages) == 2
        engine.resume_session(info.id)
        assert sum(1 for m in engine.session.messages
                   if str(m.get("content", "")).startswith(AGENTS_MD_PREFIX)) == 1

        # /agents 状态文本
        status = engine.agents_md_status()
        assert "AGENTS.md" in status and "已注入" in status


def test_context_config_applied() -> None:
    """config 中的上下文参数会传入引擎（阈值 / 估算 / 检索限制）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = ContextConfig(
            warning_ratio=0.5, ascii_chars_per_token=2.0,
            message_overhead_tokens=8, recall_max_matches=2,
        )
        config = LLMConfig(
            api_key="x", base_url="http://invalid", model="fake",
            context_window=1000, context_window_configured=True,
        )
        engine = AgentEngine(
            config, client=FakeClient(), skills_root=root,
            context_config=cfg, mcp_async=False,
        )
        usage = engine.context_usage()
        assert usage.threshold == 0.5
        assert usage.window == 1000
        assert usage.used > 0
        # 估算时使用配置的字符/token 比例：2.0 比默认 4.0 估算更多
        default_est = estimate_messages_tokens(engine.session.messages)
        assert usage.used >= default_est


def main() -> int:
    tests = [test_registry, test_engine_event_stream, test_engine_error_and_cancel,
             test_events_serializable, test_search_pure_helpers, test_search_tool,
             test_search_registry_and_engine, test_fetch_curl_command_and_errors,
             test_fetch_extract_markdown, test_run_cli,
             test_skills_discovery_and_prompt, test_load_skill_tool,
             test_engine_skills_integration, test_create_skill,
             test_mcp_config_and_expose, test_mcp_manager_bridge,
             test_context_token_estimation, test_context_archive_and_recall,
             test_compact_flow, test_context_threshold_block,
             test_config_bootstrap_and_sections, test_session_store_lifecycle,
             test_agents_md_once_and_persisted, test_context_config_applied]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
