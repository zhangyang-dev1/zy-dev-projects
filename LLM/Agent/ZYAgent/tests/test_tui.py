"""tui 层无头测试：Textual Pilot + 假 LLM 客户端，验证"前端消费事件流"的完整链路。

运行: python tests/test_tui.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from textual.widgets import Input, Static  # noqa: E402

from core.config import LLMConfig  # noqa: E402
from core.engine import AgentEngine  # noqa: E402
from core.llm.base import LLMClient, ReplyArrived, TextDelta  # noqa: E402
from core.tools import ToolSpec  # noqa: E402
from tui.app import HelpScreen, ResumeScreen, ZYAgentApp  # noqa: E402
from tui.widgets import MessageBubble, ToolCard  # noqa: E402
from tui.widgets import MessageBubble, ToolCard  # noqa: E402


class FakeClient(LLMClient):
    """脚本化假客户端：第一轮调用 isLeapYear，第二轮作答。"""

    supports_stream = True

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay

    def stream(self, messages, tools=None):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.calls == 1:
            for ch in "我来查一下。":
                time.sleep(self.delay)
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
                time.sleep(self.delay)
                yield TextDelta(ch)
            yield ReplyArrived(message={"role": "assistant", "content": "2024年是闰年。"})

    def complete(self, messages, tools=None):  # pragma: no cover
        raise AssertionError("不应走到非流式分支")


def _config(**kw) -> LLMConfig:
    base = dict(api_key="x", base_url="http://invalid", model="fake", stream=True)
    base.update(kw)
    return LLMConfig(**base)


class CompactTuiFake(FakeClient):
    """压缩请求（tools=None）返回摘要，其余走普通对话。"""

    def stream(self, messages, tools=None):
        if tools is None:
            self.calls += 1
            yield ReplyArrived(
                message={"role": "assistant", "content": "# 摘要\n已完成上下文压缩。"}
            )
            return
        yield from super().stream(messages, tools)


#: 测试隔离：指向不存在的 skills 根目录，避免引擎发现项目内真实 .agents/skills
#: 导致工具数量/System Prompt 与断言不符（各测试无需 Skill 也能确定性运行）
_NO_SKILLS_ROOT = Path(os.environ.get("TEMP", ".")) / "zyagent_tui_no_skills_root"


def _engine(client, **kw) -> AgentEngine:
    """构建与 Skill 环境隔离的测试引擎。"""
    return AgentEngine(_config(), client=client, skills_root=_NO_SKILLS_ROOT, **kw)


def _status(app: ZYAgentApp) -> str:
    return str(app.query_one("#statusbar", Static).content)


def _bubbles(app: ZYAgentApp, role: str) -> list[MessageBubble]:
    return [w for w in app.query(MessageBubble) if w.role_name == role]


async def _wait_until(pred, pilot, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await pilot.pause(0.05)
    return pred()


async def _wait_answered(app: ZYAgentApp, pilot) -> None:
    """等待一轮结束：出现助手/错误气泡且状态回到"就绪/已停止"。"""
    done = await _wait_until(
        lambda: (_bubbles(app, "assistant") or _bubbles(app, "error"))
        and ("就绪" in _status(app) or "已停止" in _status(app)),
        pilot,
    )
    assert done, "生成未在预期时间内完成"


async def scenario_full_chat() -> None:
    """完整链路：输入 -> 流式气泡 -> 工具卡片(耗时) -> 最终回答 -> 状态复位。"""
    engine = _engine(FakeClient())
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        assert "就绪" in _status(app)

        app.query_one("#prompt", Input).value = "2024年是闰年吗？"
        await pilot.press("enter")
        await _wait_answered(app, pilot)

        # 用户气泡（"你" 标签）
        users = _bubbles(app, "user")
        assert len(users) == 1 and users[0].raw == "2024年是闰年吗？"

        # 回归（竖排bug）：正文宽度必须接近气泡满宽，而非收缩成标签宽度
        chat_w = app.query_one("#chat").region.width
        for b in [*users, *_bubbles(app, "assistant")]:
            tw = b.children[1].region.width
            assert tw >= chat_w // 2, f"气泡正文过窄（疑似竖排）: text_width={tw} chat={chat_w}"

        # 助手气泡：先"我来查一下。"，后"2024年是闰年。"，且不再显示流式光标
        assistants = _bubbles(app, "assistant")
        assert [b.raw for b in assistants] == ["我来查一下。", "2024年是闰年。"], [
            b.raw for b in assistants
        ]
        assert all(not b.streaming for b in assistants)

        # 工具卡片：成功、含耗时、表头含工具名
        cards = list(app.query(ToolCard))
        assert len(cards) == 1
        card = cards[0]
        assert card.tool_name == "isLeapYear" and card.ok and "是闰年" in card.output
        head = str(card._head.content)
        assert "isLeapYear" in head and "ms" in head, head

        # 状态栏统计：1 轮、工具 1 次
        status = _status(app)
        assert "1 轮" in status and "工具 1 次" in status, status

    roles = [m["role"] for m in engine.session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"], roles


async def scenario_tool_card_expand() -> None:
    """工具卡片点击展开/收起：显示参数 JSON 与结果。"""
    engine = _engine(FakeClient())
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        app.query_one("#prompt", Input).value = "展开检查"
        await pilot.press("enter")
        await _wait_answered(app, pilot)

        card = next(iter(app.query(ToolCard)))
        assert not card.expanded and "2024" not in str(card._body.content)
        await pilot.click(ToolCard)
        await pilot.pause(0.1)
        assert card.expanded
        body = str(card._body.content)
        assert "参数" in body and "2024" in body and "结果" in body, body
        await pilot.click(ToolCard)
        await pilot.pause(0.1)
        assert not card.expanded and str(card._body.content) == ""


async def scenario_stop_generation() -> None:
    """Esc 中断：慢速流式中按 Esc，出现错误气泡并复位为"已停止"。"""
    engine = _engine(FakeClient(delay=0.03))
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        app.query_one("#prompt", Input).value = "慢速问题"
        await pilot.press("enter")
        started = await _wait_until(lambda: bool(_bubbles(app, "assistant")), pilot)
        assert started, "流式未开始"
        await pilot.press("escape")
        done = await _wait_until(lambda: "已停止" in _status(app), pilot)
        assert done, "停止后状态未复位"
        assert any("已停止" in e.raw for e in _bubbles(app, "error"))


async def scenario_error_and_clear() -> None:
    """错误事件渲染 + Ctrl+L 清屏（保留历史）。"""

    class Boom(FakeClient):
        def stream(self, messages, tools=None):
            raise RuntimeError("network down")
            yield  # pragma: no cover

    engine = _engine(Boom())
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        app.query_one("#prompt", Input).value = "hi"
        await pilot.press("enter")
        done = await _wait_until(lambda: bool(_bubbles(app, "error")), pilot)
        assert done, "错误气泡未出现"

        assert len(engine.session.messages) >= 1  # 历史保留
        await pilot.press("ctrl+l")
        await pilot.pause(0.1)
        assert list(app.query(MessageBubble)) == []
        assert "就绪" in _status(app)
        assert len(engine.session.messages) >= 1  # 清屏不影响历史


async def scenario_sidebar_and_help() -> None:
    """侧栏开关、工具清单、帮助弹窗。"""
    engine = _engine(FakeClient())
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        sidebar = app.query_one("#sidebar")
        # 侧栏默认可见，工具清单与引擎注册数一致
        assert not sidebar.has_class("hidden")
        assert len(list(app.query(".tool-item"))) == len(engine.tools)

        # Ctrl+B 隐藏/恢复
        await pilot.press("ctrl+b")
        await pilot.pause(0.05)
        assert sidebar.has_class("hidden")
        await pilot.press("ctrl+b")
        await pilot.pause(0.05)
        assert not sidebar.has_class("hidden")

        # F1 打开帮助弹窗，Esc 关闭
        assert not isinstance(app.screen, HelpScreen)
        await pilot.press("f1")
        await pilot.pause(0.05)
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert not isinstance(app.screen, HelpScreen)


async def scenario_commands() -> None:
    """/cwd /model /tools /new /help 命令与未知命令提示。"""
    engine = _engine(FakeClient())
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        prompt = app.query_one("#prompt", Input)

        prompt.value = "/cwd"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert any(os.getcwd() in b.raw for b in _bubbles(app, "info"))

        prompt.value = "/model"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert any("fake" in b.raw for b in _bubbles(app, "info"))

        prompt.value = "/tools"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert any("isLeapYear" in b.raw for b in _bubbles(app, "info"))

        prompt.value = "/mcp"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert any("MCP" in b.raw for b in _bubbles(app, "info"))

        prompt.value = "/xyz"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert any("未知命令" in b.raw for b in _bubbles(app, "error"))

        # 先产生历史，再 /new 重置
        prompt.value = "2024年是闰年吗？"
        await pilot.press("enter")
        await _wait_answered(app, pilot)
        assert len(engine.session.messages) > 0
        prompt.value = "/new"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert len(engine.session.messages) == 0
        assert list(app.query(MessageBubble)) == []


async def scenario_layout() -> None:
    """布局校验：气泡最大宽度、用户气泡右对齐、侧栏与状态栏尺寸。"""
    engine = _engine(FakeClient())
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        app.query_one("#prompt", Input).value = "布局检查问题"
        await pilot.press("enter")
        await _wait_answered(app, pilot)

        chat = app.query_one("#chat")
        sidebar = app.query_one("#sidebar")
        user = _bubbles(app, "user")[0]
        assistants = _bubbles(app, "assistant")

        # 回归：气泡及其内容必须有实际渲染尺寸（曾因 auto 容器+1fr 子件塌缩为 0×0）
        for b in [user, *assistants]:
            assert b.size.width > 0 and b.size.height > 0, f"气泡零尺寸: {b.region}"
            for child in b.children:
                assert child.region.width > 0 and child.region.height > 0, \
                    f"气泡内容零尺寸: {child.classes} {child.region}"

        chat_right = chat.region.x + chat.region.width
        for b in [user, *assistants]:
            assert b.region.width <= chat.region.width * 0.85, (b.region, chat.region)
            assert b.region.x + b.region.width <= chat_right, (b.region, chat.region)
        # 用户气泡同为满宽气泡（max-width 85%），以自身配色区分角色
        assert user.region.width >= chat.region.width * 0.7, (user.region, chat.region)
        # 侧栏在右侧且有固定宽度
        assert sidebar.region.x > chat.region.x
        assert sidebar.region.width == 36
        # 关键区域尺寸
        assert app.query_one("#statusbar").region.height == 1
        assert app.query_one("#prompt").region.height == 3
        assert chat.region.height > 10


async def scenario_long_text_wrap() -> None:
    """长文本在气泡内换行，且不超出最大宽度（auto 容器换行回归）。"""
    engine = _engine(FakeClient())
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        long_q = "这是一段很长的用于验证自动换行的问题" * 4  # 约 150 汉字
        app.query_one("#prompt", Input).value = long_q
        await pilot.press("enter")
        await _wait_answered(app, pilot)

        chat = app.query_one("#chat")
        user = _bubbles(app, "user")[0]
        assert user.raw == long_q
        # 气泡被 max-width 限制，文本折行显示
        assert user.region.width <= chat.region.width * 0.9, (user.region, chat.region)
        # 回归（竖排bug）：折行后的正文仍应是宽块，而不是 1~2 字宽的竖条
        assert user.children[1].region.width >= chat.region.width // 2, \
            f"长文本正文过窄: {user.children[1].region}"
        assert user.children[1].region.height >= 2, "长文本未换行"
        assert user.size.width > 0


async def scenario_context_bar_and_compact() -> None:
    """上下文占用条（k / 窗口 / 百分比 / 默认提示）+ /compact 命令。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        engine = AgentEngine(_config(), client=CompactTuiFake(), skills_root=root)
        app = ZYAgentApp(engine)
        async with app.run_test(size=(110, 36)) as pilot:
            bar = str(app.query_one("#contextbar", Static).content)
            assert "上下文" in bar and "128k" in bar and "%" in bar, bar
            assert "默认" in bar, bar          # 未配置窗口 -> 提示默认 128k
            assert app.query_one("#contextbar").region.height == 1

            # 先产生一段历史
            app.query_one("#prompt", Input).value = "2024年是闰年吗？"
            await pilot.press("enter")
            await _wait_answered(app, pilot)
            assert len(engine.session.messages) == 4

            # /compact：归档 -> 摘要 -> 替换历史
            app.query_one("#prompt", Input).value = "/compact"
            await pilot.press("enter")
            done = await _wait_until(
                lambda: any("压缩完成" in b.raw for b in _bubbles(app, "info")), pilot
            )
            assert done, "压缩未完成"
            assert len(list((root / ".agents" / "context").glob("*.jsonl"))) == 1
            assert [m["role"] for m in engine.session.messages] == ["user"]
            assert "摘要" in engine.session.messages[0]["content"]
            assert "上下文" in str(app.query_one("#contextbar", Static).content)


async def scenario_context_block() -> None:
    """达到 80%：警告 + 拦截普通提示词；/compact 成功后恢复输入。"""

    class HighUsageFake(CompactTuiFake):
        def stream(self, messages, tools=None):
            for part in super().stream(messages, tools):
                if isinstance(part, ReplyArrived):
                    yield ReplyArrived(message=part.message, usage={"total_tokens": 3400})
                else:
                    yield part

    with tempfile.TemporaryDirectory() as tmp:
        engine = AgentEngine(
            _config(context_window=4000, context_window_configured=True),
            client=HighUsageFake(), skills_root=Path(tmp),
        )
        app = ZYAgentApp(engine)
        async with app.run_test(size=(110, 36)) as pilot:
            app.query_one("#prompt", Input).value = "2024年是闰年吗？"
            await pilot.press("enter")
            warned = await _wait_until(
                lambda: any("80%" in b.raw for b in _bubbles(app, "error")), pilot
            )
            assert warned, "未显示 80% 警告"
            assert engine.input_blocked
            assert "/compact" in str(app.query_one("#contextbar", Static).content)

            # 普通提示词被拦截（不写入会话）
            before = len(engine.session.messages)
            app.query_one("#prompt", Input).value = "继续问"
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert len(engine.session.messages) == before
            assert any("暂停接收" in b.raw for b in _bubbles(app, "error"))

            # /compact 后解除限制
            app.query_one("#prompt", Input).value = "/compact"
            await pilot.press("enter")
            done = await _wait_until(
                lambda: any("压缩完成" in b.raw for b in _bubbles(app, "info")), pilot
            )
            assert done, "压缩未完成"
            assert not engine.input_blocked
            assert "默认" not in str(app.query_one("#contextbar", Static).content)


async def scenario_command_palette() -> None:
    """输入 / 弹出补全面板：↑↓ 选择、Tab 补全、Enter 执行、Esc 关闭。"""
    engine = _engine(FakeClient())
    app = ZYAgentApp(engine)
    async with app.run_test(size=(110, 36)) as pilot:
        prompt = app.query_one("#prompt", Input)
        palette = app.query_one("#palette", Static)

        # 输入 / -> 面板出现并列出命令
        prompt.value = "/"
        await pilot.pause(0.05)
        assert palette.display, "斜杠补全面板未显示"
        content = str(palette.content)
        assert "/new" in content and "/resume" in content and "/save" in content

        # ↓ 选中 /resume，Tab 补全到输入框
        await pilot.press("down")
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert prompt.value == "/resume", prompt.value

        # /c -> ↓↓ 选中 /cwd，Enter 直接执行
        prompt.value = "/c"
        await pilot.pause(0.05)
        assert palette.display and "/cwd" in str(palette.content)
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert any(os.getcwd() in b.raw for b in _bubbles(app, "info"))

        # Esc 关闭面板（不触发中断）
        prompt.value = "/s"
        await pilot.pause(0.05)
        assert palette.display
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert not palette.display


async def scenario_sessions_resume() -> None:
    """/save、/new 自动保存、/resume 面板恢复、Esc 取消。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        engine = AgentEngine(
            _config(), client=FakeClient(), skills_root=root, mcp_async=False
        )
        app = ZYAgentApp(engine)
        async with app.run_test(size=(110, 36)) as pilot:
            prompt = app.query_one("#prompt", Input)

            prompt.value = "2024年是闰年吗？"
            await pilot.press("enter")
            await _wait_answered(app, pilot)

            # /save 带标题
            prompt.value = "/save 测试会话"
            await pilot.press("enter")
            await pilot.pause(0.1)
            sessions = engine.list_sessions()
            assert sessions and "测试会话" in sessions[0].title, sessions

            # /new：清空历史与界面
            prompt.value = "/new"
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert engine.session.messages == []
            assert list(app.query(MessageBubble)) == []

            # /resume -> 面板 -> Enter 恢复
            prompt.focus()
            prompt.value = "/resume"
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ResumeScreen), type(app.screen)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ResumeScreen)
            assert any(b.raw == "2024年是闰年吗？" for b in _bubbles(app, "user"))
            assert len(engine.session.messages) > 0

            # Esc 取消恢复
            prompt.focus()
            prompt.value = "/resume"
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ResumeScreen)
            before = list(engine.session.messages)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ResumeScreen)
            assert engine.session.messages == before


async def scenario_mcp_sidebar() -> None:
    """侧栏 MCP 区域：未配置提示 + 加载完成后动态刷新 Server 与工具。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        engine = AgentEngine(
            _config(), client=FakeClient(), skills_root=root, mcp_async=False
        )
        app = ZYAgentApp(engine)
        async with app.run_test(size=(110, 36)) as pilot:
            area = str(app.query_one("#mcp-area", Static).content)
            assert "未配置" in area and ".mcp.json" in area, area

            # 模拟异步加载完成：状态 + 工具动态出现
            engine.mcp.server_status["Demo"] = ("ok", "已连接（1 个工具）")
            engine.mcp.registered["Demo"] = ["demoTool"]
            engine.mcp.state = "ready"
            engine.mcp._bump()
            app._refresh_sidebar_dynamic()
            await pilot.pause(0.1)
            area = str(app.query_one("#mcp-area", Static).content)
            assert "Demo" in area and "demoTool" in area, area

            # 工具清单也随注册表变化刷新
            before = len(list(app.query(".tool-item")))
            engine.registry.register(ToolSpec(name="demoTool2", description="演示工具"))
            engine.mcp._bump()
            app._refresh_sidebar_dynamic()
            await pilot.pause(0.1)
            assert len(list(app.query(".tool-item"))) == before + 1
            assert len(list(app.query(".tool-item"))) == len(engine.tools)


def main() -> int:
    scenarios = [
        scenario_full_chat,
        scenario_tool_card_expand,
        scenario_stop_generation,
        scenario_error_and_clear,
        scenario_sidebar_and_help,
        scenario_commands,
        scenario_layout,
        scenario_long_text_wrap,
        scenario_context_bar_and_compact,
        scenario_context_block,
        scenario_command_palette,
        scenario_sessions_resume,
        scenario_mcp_sidebar,
    ]

    async def run_all() -> list[tuple[str, str | None]]:
        results = []
        for s in scenarios:
            try:
                await s()
                results.append((s.__name__, None))
            except Exception as exc:  # noqa: BLE001
                import traceback

                results.append((s.__name__, f"{type(exc).__name__}: {exc}\n"
                                            + traceback.format_exc(limit=6)))
        return results

    results = asyncio.run(run_all())
    failed = 0
    for name, err in results:
        if err is None:
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name}: {err}")
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
