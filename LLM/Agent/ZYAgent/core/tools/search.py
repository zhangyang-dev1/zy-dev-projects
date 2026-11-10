"""联网搜索工具：``searchWeb`` —— 调用百度千帆「百度AI搜索」检索互联网实时信息。

层级：core / 资源层。
依赖：仅标准库（urllib）+ config.SearchConfig；不依赖任何 UI。

API 规格（百度千帆·百度AI搜索 web_search v2，2026-06 官方文档）::

    POST https://qianfan.baidubce.com/v2/ai_search/web_search
    Authorization: Bearer <千帆API Key>
    Content-Type: application/json

    {
      "messages": [{"role": "user", "content": "<query>"}],
      "search_source": "baidu_search_v2",
      "edition": "standard",
      "resource_type_filter": [{"type": "web", "top_k": 10}]
    }

    -> {"references": [{"id", "title", "url", "snippet/content",
                        "website", "date", "type"}], "request_id": "..."}

设计要点
--------
* **错误即数据**：网络/HTTP/业务错误全部转成 :class:`ToolError`，
  由注册表回传 ``ok=False`` 文本，模型自行换词重试或如实告知用户。
* **可测试**：HTTP 传输通过 ``transport`` 可注入，测试零网络。
* **契约即提示**：``description`` 明确告知 LLM 何时调用、结果如何组织。
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib import error, request

from ..config import SearchConfig
from .base import ToolError, ToolSpec

#: 对外暴露的工具名（Function Calling 路由键）
SEARCH_TOOL_NAME = "searchWeb"

#: 官方限制：query 长度 72 字符（1 个汉字占 2 个字符）
_QUERY_UNIT_LIMIT = 72

#: 传输函数签名: (url, headers, payload, timeout) -> 解析后的响应 dict
SearchTransport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]

#: 常见 HTTP 状态码的修复建议（拼进错误文本，便于模型/用户定位问题）
_HTTP_HINTS = {
    401: "API Key 无效或已失效，请检查 config.ini [search] 的 Baidu-qianfan-APIKEY",
    403: "无权限调用，请确认千帆控制台已开通「百度AI搜索」",
    404: "接口地址不存在，请检查 endpoint 配置",
    429: "触发限流或免费额度用尽，稍后重试或开通后付费",
}


# ====================================================================== HTTP 传输
def http_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """默认传输：POST JSON 并返回解析后的响应（错误转 ToolError）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001 - 读取失败忽略正文
            pass
        hint = _HTTP_HINTS.get(exc.code, "")
        raise ToolError(
            f"搜索接口 HTTP {exc.code}: {detail or exc.reason}"
            + (f"（{hint}）" if hint else "")
        ) from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise ToolError(f"搜索接口超时（{timeout:.0f}s 无响应），请稍后重试") from exc
        raise ToolError(f"搜索接口网络错误: {reason}（请检查本机网络）") from exc
    except TimeoutError as exc:  # 已建立连接后读取超时
        raise ToolError(f"搜索接口超时（{timeout:.0f}s 内未返回数据），请稍后重试") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(f"搜索接口返回非 JSON: {raw[:200]}") from exc
    if not isinstance(data, dict):
        raise ToolError(f"搜索接口返回结构异常: {raw[:200]}")
    return data


# ====================================================================== 纯函数（可单测）
def clip_query(query: str, limit: int = _QUERY_UNIT_LIMIT) -> str:
    """按官方计数规则截断 query（汉字=2 字符单位，ASCII=1）。"""
    used = 0
    out: list[str] = []
    for ch in query:
        width = 2 if ord(ch) > 0x2E7F else 1
        if used + width > limit:
            break
        used += width
        out.append(ch)
    return "".join(out).strip()


def format_references(data: dict[str, Any], max_snippet: int = 300) -> str:
    """把 API 响应整理成 LLM 友好的文本：**摘要 + 标题 + URL**。

    :raises ToolError: 响应携带错误码 ``code`` 时。
    """
    if data.get("code") or (data.get("message") and not data.get("references")):
        code = data.get("code") or "UnknownError"
        message = data.get("message") or "未知错误"
        raise ToolError(f"百度搜索返回错误 code={code}: {message}")

    refs = data.get("references") or []
    if not refs:
        return "未找到相关搜索结果。可尝试更换关键词（更具体或更宽泛）后重试。"

    blocks: list[str] = []
    for i, ref in enumerate(refs, 1):
        if not isinstance(ref, dict):
            continue
        title = str(ref.get("title") or ref.get("web_anchor") or "(无标题)").strip()
        url = str(ref.get("url") or "").strip()
        snippet = str(ref.get("snippet") or ref.get("content") or "").strip()
        snippet = " ".join(snippet.split())          # 压平换行
        if len(snippet) > max_snippet:
            snippet = snippet[: max_snippet - 1] + "…"
        meta = " · ".join(x for x in (str(ref.get("website") or "").strip(),
                                      str(ref.get("date") or "").strip()) if x)

        lines = [f"[{i}] {title}", f"    URL: {url or '(无)'}"]
        if snippet:
            lines.append(f"    摘要: {snippet}")
        if meta:
            lines.append(f"    来源: {meta}")
        blocks.append("\n".join(lines))

    if not blocks:
        return "未找到可用的搜索结果。"
    return f"共 {len(blocks)} 条搜索结果（时效性以发布日期为准）：\n\n" + "\n\n".join(blocks)


# ====================================================================== 工具构造
def build_search_tool(
    config: SearchConfig,
    transport: SearchTransport | None = None,
) -> ToolSpec:
    """构造 ``searchWeb`` 工具；``transport`` 仅用于测试注入。

    * 未配置 api_key 时**仍注册工具**：调用会返回清晰的配置指引文本
      （错误即数据），让模型把修复方法转述给用户，而不是静默消失。
    * 执行中的任何异常由注册表转成 ``ok=False`` 文本回传给 LLM。
    """
    send: SearchTransport = transport or http_transport

    def handler(args: dict[str, Any]) -> str:
        # ---- 1. 缺少配置：给出可操作的配置指引
        if not config.enabled:
            raise ToolError(
                "未配置搜索服务，无法联网检索。请在 config.ini 的 [search] 小节设置 "
                "Baidu-qianfan-APIKEY（百度千帆 API Key，Bearer 格式如 bce-v3/...）"
                "后重启程序；在此之前请仅用已有知识回答，并告知用户该信息可能不是最新的"
            )

        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query 不能为空，请提供搜索关键词或问题")
        clipped = clip_query(query)
        if not clipped:
            raise ToolError("query 全部为无效字符，无法搜索")

        # ---- 2. 组装请求（官方规格）
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": clipped}],
            "search_source": "baidu_search_v2",
            "edition": config.edition,
            "resource_type_filter": [{"type": "web", "top_k": max(1, config.top_k)}],
        }
        if config.recency:
            payload["search_recency_filter"] = config.recency

        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

        # ---- 3. 发起搜索：网络/HTTP/业务错误均已在内部转为 ToolError
        data = send(config.endpoint, headers, payload, float(config.timeout))
        return format_references(data)

    return ToolSpec(
        name=SEARCH_TOOL_NAME,
        description=(
            "联网搜索工具：通过百度搜索检索互联网实时信息，返回结果的摘要、来源标题和URL。"
            "当问题涉及时效性信息（新闻、天气、股价、日期、赛况、最新版本等）"
            "或你的知识库之外的事实时调用；知识范围内的常识性问题无需调用。"
            f"每次最多返回 {config.top_k} 条结果，请综合多条结果回答并注明来源URL。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题；用最能命中权威来源的简洁表述，"
                                   f"不超过 { _QUERY_UNIT_LIMIT // 2 } 个汉字（过长会被截断）",
                }
            },
            "required": ["query"],
        },
        handler=handler,
    )
