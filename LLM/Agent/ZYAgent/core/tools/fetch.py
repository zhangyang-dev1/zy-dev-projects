"""网页抓取工具：``fetchWeb`` —— curl 抓取 HTML，trafilatura 提取正文并转 Markdown。

层级：core / 资源层。
依赖：标准库 subprocess + 第三方 trafilatura（缺失时给出安装指引）。

流程::

    URL -> subprocess(curl, 参数列表, shell=False) -> HTML 字节
        -> trafilatura(output_format=markdown, include_links/tables)
        -> "URL/标题 + Markdown 正文" 回传 LLM

设计要点
--------
* **安全调用 curl**：参数列表方式执行，**永不使用 ``shell=True``**（防命令注入）；
  仅放行 http/https URL，拒绝 file:// 等本地协议。
* **降噪省 token**：trafilatura 去除导航栏/广告/页脚等噪音，保留标题、段落、
  列表、表格、链接；正文超过上限自动截断，保护上下文窗口。
* **错误即数据**：curl 缺失/超时/解析失败/HTTP 错误/正文为空等全部转成
  :class:`ToolError`，由注册表回传 ``ok=False`` 清晰文本给模型。
* **可测试**：curl 执行器（``runner``）可注入，测试零网络、零子进程。
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable
from urllib.parse import urlparse

from .base import ToolError, ToolSpec

#: 对外暴露的工具名（Function Calling 路由键）
FETCH_TOOL_NAME = "fetchWeb"

#: 模拟浏览器的 UA（部分站点拒绝 curl 默认 UA）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: curl --write-out 元信息哨兵：与正文用 rpartition 分离
_META_SENTINEL = "\n__ZYAGENT_CURL_META__"

#: 回传正文上限（字符），超出截断以保护 LLM 上下文
_MAX_CONTENT_CHARS = 20_000

#: curl 连接阶段超时（秒）
_CONNECT_TIMEOUT = 10

#: 默认整体抓取超时（秒）
_DEFAULT_TIMEOUT = 30.0

#: 从 HTML 头部兜底提取 <title>
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)

#: curl 执行器注入签名（测试用）：与 subprocess.run 兼容
Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


# ====================================================================== curl 层
def build_curl_command(url: str, timeout: float) -> list[str]:
    """构造 curl 参数列表（**列表方式，永不经过 shell 解释**）。"""
    return [
        "curl",
        "--silent", "--show-error", "--location", "--compressed",
        "--connect-timeout", str(_CONNECT_TIMEOUT),
        "--max-time", str(max(1, int(timeout))),
        "--user-agent", _USER_AGENT,
        "--header", "Accept: text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "--write-out", f"{_META_SENTINEL}%{{http_code}}|%{{content_type}}",
        url,                       # URL 作为独立参数传入，特殊字符无需转义
    ]


def fetch_url(
    url: str,
    timeout: float = _DEFAULT_TIMEOUT,
    runner: Runner | None = None,
) -> tuple[bytes, int, str]:
    """用 curl 抓取 URL，返回 ``(HTML字节, HTTP状态码, Content-Type)``。

    :raises ToolError: curl 缺失 / 超时 / 网络失败 / HTTP >= 400 / 非网页类型。
    """
    run: Runner = runner or subprocess.run
    try:
        proc = run(
            build_curl_command(url, timeout),
            capture_output=True,          # 捕获 stdout/stderr，不污染终端
            shell=False,                  # 明确禁用 shell（参数列表执行）
            timeout=timeout + 5,          # 进程级兜底超时（curl 自身 --max-time 更早触发）
        )
    except FileNotFoundError as exc:
        raise ToolError("本机未找到 curl 程序，无法抓取网页（Windows 10+ 通常已自带）") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"抓取超时：{timeout:.0f} 秒内未完成，请稍后重试") from exc

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise ToolError(
            f"curl 抓取失败（exit {proc.returncode}）：{err[:300] or '未知原因'}"
            "。常见原因：域名不存在、本机无网络、站点拒绝访问"
        )

    body, _, meta = (proc.stdout or b"").rpartition(_META_SENTINEL.encode("utf-8"))
    if not meta:
        raise ToolError("无法解析 curl 输出：缺少状态元信息")
    code_str, _, ctype = meta.decode("utf-8", "replace").partition("|")
    try:
        status = int(code_str)
    except ValueError:
        status = 0

    if status >= 400:
        raise ToolError(f"目标站点返回 HTTP {status}，无法访问该页面")
    if ctype and not any(x in ctype for x in ("html", "xml", "text/")):
        raise ToolError(f"该地址返回的是非网页内容（{ctype}），fetchWeb 仅支持 HTML 页面")
    return body, status, ctype.strip()


# ====================================================================== 提取层
def extract_markdown(html: bytes, url: str) -> tuple[str, str]:
    """trafilatura 提取正文并转 Markdown，返回 ``(markdown, 页面标题)``。

    保留标题/段落/列表/表格/链接，去除导航栏/广告/页脚等噪音。

    :raises ToolError: 依赖缺失 / 解析异常 / 正文无法提取。
    """
    try:
        import trafilatura
        from trafilatura.metadata import extract_metadata
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ToolError("缺少依赖 trafilatura，请执行：pip install trafilatura") from exc

    try:
        markdown = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,        # 保留正文中的链接
            include_tables=True,       # 保留表格
            include_comments=False,    # 丢弃评论区
            include_images=False,      # 图片占 token 且多为装饰，丢弃
            favor_precision=True,      # 优先精度：更严格地剔除噪音
            deduplicate=True,          # 去重（导航重复块等）
        )
    except Exception as exc:  # noqa: BLE001 - 解析异常统一转错误文本
        raise ToolError(f"HTML 解析失败：{type(exc).__name__}: {exc}") from exc

    # 标题：元信息优先，<title> 兜底
    title = ""
    try:
        meta = extract_metadata(html)
        title = (meta.title or "").strip() if meta is not None else ""
    except Exception:  # noqa: BLE001 - 元信息失败不致命
        title = ""
    if not title:
        m = _TITLE_RE.search(html[:4096])
        if m:
            title = m.group(1).decode("utf-8", "replace").strip()

    if not markdown or not markdown.strip():
        raise ToolError(
            "未能从页面提取到正文：页面可能由 JavaScript 动态渲染、需要登录、"
            "或是纯图片/空白页，请改用可直接访问的来源，或告知用户该页面无法抓取"
        )

    markdown = markdown.strip()
    if len(markdown) > _MAX_CONTENT_CHARS:
        markdown = markdown[:_MAX_CONTENT_CHARS] + f"\n\n…（正文过长，已截断至 {_MAX_CONTENT_CHARS} 字符）"
    return markdown, title


# ====================================================================== 工具构造
def build_fetch_tool(
    timeout: float = _DEFAULT_TIMEOUT,
    runner: Runner | None = None,
) -> ToolSpec:
    """构造 ``fetchWeb`` 工具；``runner``/``timeout`` 仅用于测试注入。"""

    def handler(args: dict[str, Any]) -> str:
        # ---- 1. URL 校验（仅 http/https，杜绝本地文件协议）
        url = str(args.get("url") or "").strip()
        if not url:
            raise ToolError("url 不能为空，请提供完整的网页地址，如 https://example.com/article")
        if url.lower().startswith("www."):
            url = "https://" + url
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ToolError(f"仅支持 http/https 网址，收到：{url[:80]}")

        # ---- 2. curl 抓取 HTML
        body, status, _ctype = fetch_url(url, timeout=float(timeout), runner=runner)

        # ---- 3. 提取正文并转 Markdown
        markdown, title = extract_markdown(body, url)

        # ---- 4. 输出：原始 URL + 元信息 + Markdown 正文
        head = f"URL: {url}\nHTTP: {status}"
        if title:
            head += f"\n标题: {title}"
        return f"{head}\n\n{markdown}"

    return ToolSpec(
        name=FETCH_TOOL_NAME,
        description=(
            "网页抓取工具：给定一个网页URL，下载页面并提取主要正文（转为Markdown格式）。"
            "适用于：用户给出链接要求阅读/总结/翻译/分析；或 searchWeb 搜索结果需要查看原文详情。"
            "正文已去除导航栏、广告等噪音，保留标题、段落、列表、表格和链接。"
            f"返回内容含原始URL与页面标题，超过 {_MAX_CONTENT_CHARS} 字符会截断。"
            "注意：不支持需要登录或完全由 JavaScript 渲染的页面。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的完整网页地址（http 或 https，如 https://example.com/article）",
                }
            },
            "required": ["url"],
        },
        handler=handler,
    )
