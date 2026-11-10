"""本地命令执行工具：``RunCli`` —— 以当前用户权限运行已安装的命令行程序。

层级：core / 资源层。
依赖：仅标准库 subprocess。

流程::

    {command, args[]} -> subprocess.run([command, *args], shell=False)
                      -> stdout / stderr / 退出码（超长截断后）回传 LLM

设计要点
--------
* **安全调用**：程序名与参数拼装为**列表**执行，``shell=False``，**绝不拼接
  shell 命令字符串**——参数中的 ``&``、``;``、``|`` 等字符均按字面量传递，
  不会被解释执行（防命令注入）。
* **结果完整**：stdout、stderr、退出码三者一并回传；退出码非 0 时附失败提示，
  由模型自行判断并决定下一步（重试 / 换参数 / 报告用户）。
* **上下文保护**：stdout 与 stderr 各自截断（默认 6000 字符）并注明原始长度，
  避免大量终端输出挤占 LLM 上下文。
* **错误即数据**：命令未找到 / 超时 / 参数非法等转成 :class:`ToolError`
  （``ok=False`` 文本），由模型自行纠正。
* **编码兼容**：中文 Windows 控制台常以 GBK 输出，先按 UTF-8 严格解码，
  失败回退 GBK/系统首选编码，最后才替换字符，保证中文可读。
* **可测试**：``runner`` 可注入，测试零子进程。

安全声明：本工具面向受控教学实验环境，命令以运行 ZYAgent 的当前用户权限执行，
请勿在不受信任的输入或生产环境中开放。
"""

from __future__ import annotations

import locale
import subprocess
from typing import Any, Callable

from .base import ToolError, ToolSpec

#: 对外暴露的工具名（Function Calling 路由键）
RUN_CLI_TOOL_NAME = "RunCli"

#: 默认整体执行超时（秒）
_DEFAULT_TIMEOUT = 60.0

#: stdout / stderr 各自的最大保留字符数（超出截断并注明）
_MAX_STREAM_CHARS = 6_000

#: 执行器注入签名（测试用）：与 subprocess.run 兼容
Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


# ====================================================================== 辅助
def decode_output(raw: bytes | str | None) -> str:
    """解码命令输出：UTF-8 优先，回退 GBK/系统首选编码，最后替换字符。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def truncate_stream(text: str, limit: int = _MAX_STREAM_CHARS) -> str:
    """截断超长输出并注明原始长度。"""
    text = text.rstrip("\n")
    if len(text) <= limit:
        return text
    head = text[:limit]
    return f"{head}\n…（输出过长已截断，原始共 {len(text)} 字符）"


def format_result(command: str, args: list[str], proc: Any) -> str:
    """把执行结果整理成 LLM 友好的结构化文本（stdout / stderr / 退出码）。"""
    cmdline = " ".join([command, *args]) if args else command
    stdout = truncate_stream(decode_output(getattr(proc, "stdout", None)))
    stderr = truncate_stream(decode_output(getattr(proc, "stderr", None)))
    code = getattr(proc, "returncode", None)

    parts = [
        f"命令: {cmdline}",
        f"退出码: {code}",
        "[stdout]",
        stdout or "(空)",
        "[stderr]",
        stderr or "(空)",
    ]
    if code not in (0, None):
        parts.append(f"提示: 命令执行失败（退出码 {code}），请结合 stderr 判断原因，"
                     "或调整参数/换用其他命令后重试。")
    return "\n".join(parts)


# ====================================================================== 工具构造
def build_run_cli_tool(
    timeout: float = _DEFAULT_TIMEOUT,
    runner: Runner | None = None,
) -> ToolSpec:
    """构造 ``RunCli`` 工具；``runner``/``timeout`` 仅用于测试注入。"""
    run: Runner = runner or subprocess.run

    def handler(args: dict[str, Any]) -> str:
        # ---- 1. 参数校验（错误即数据：提示模型如何自我纠正）
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("command 不能为空，请提供要执行的程序名（如 python、ipconfig）")
        if "\x00" in command or "\n" in command or "\r" in command:
            raise ToolError("command 含有非法字符（换行/空字节），请提供单一程序名")

        raw_args = args.get("args") or []
        if isinstance(raw_args, str):
            raise ToolError("args 必须是字符串数组而非字符串，"
                            '例如 ["--version"] 或 ["-c", "print(1)"]')
        if not isinstance(raw_args, (list, tuple)):
            raise ToolError("args 必须是字符串数组")
        arg_list = [str(a) for a in raw_args]
        if any("\x00" in a or "\n" in a or "\r" in a for a in arg_list):
            raise ToolError("args 含有非法字符（换行/空字节）")

        # ---- 2. 列表方式执行（shell=False，参数按字面量传递）
        argv = [command, *arg_list]
        try:
            proc = run(
                argv,
                capture_output=True,
                shell=False,          # 明确禁用 shell：绝不拼接命令字符串
                timeout=float(timeout),
            )
        except FileNotFoundError as exc:
            raise ToolError(f"未找到命令 '{command}'，请确认程序已安装并在 PATH 中") from exc
        except PermissionError as exc:
            raise ToolError(f"没有权限执行 '{command}'（访问被拒绝）") from exc
        except subprocess.TimeoutExpired as exc:
            partial = ""
            if exc.stdout or exc.stderr:
                partial = "\n" + format_result(command, arg_list, exc)
            raise ToolError(
                f"命令执行超时（超过 {timeout:.0f} 秒），进程已被终止。"
                f"请换用耗时更短的命令，或为长时间任务增加筛选参数。{partial}"
            ) from exc
        except OSError as exc:  # 不支持的可执行格式、盘符不存在等
            raise ToolError(f"无法执行 '{command}'：{type(exc).__name__}: {exc}") from exc

        # ---- 3. 结构化结果：stdout / stderr / 退出码
        return format_result(command, arg_list, proc)

    return ToolSpec(
        name=RUN_CLI_TOOL_NAME,
        description=(
            "本地命令执行工具：以当前用户权限运行本机已安装的命令行程序（CLI）。"
            "传入程序名 command 与参数列表 args（args 是字符串数组，不是拼接的命令串），"
            "返回该命令的 stdout、stderr 与退出码，请据此判断执行结果并决定下一步。"
            "输出可能被截断（截断处会注明原始长度）。"
            "注意：不经过 shell，不要传入管道、重定向等 shell 语法；"
            "本工具用于受控教学实验环境，请只执行可预期的本地命令。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的程序名或可执行文件路径（如 python、pip、ipconfig）",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "传给程序的参数列表，每个元素一个参数（如 [\"--version\"]）；无参数时传空数组 []",
                },
            },
            "required": ["command", "args"],
        },
        handler=handler,
    )
