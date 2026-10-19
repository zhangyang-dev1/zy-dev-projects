"""唯一的阶段一工具：``list_pets``。

严格适配现有 Go REST API ``GET /api/v1/pets``：

- 支持且仅支持 14 个查询参数（名称与后端完全一致，camelCase）；
- Pydantic 输入模型做严格校验：真实枚举值、分页范围、非负费用区间、
  ``min <= max``、拒绝未知字段 / NaN / Infinity / 布尔冒充数字 / 错误类型；
- 成功输出对应 Go 成功信封中的 ``data``：
  ``items / total / page / pageSize / totalPages / totalCost``；
- 兼容 Go 的 ``records`` / ``charges`` 为 ``null`` 或数组的真实 JSON 表现
  （``null`` 归一化为空数组）。
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..errors import (
    BACKEND_INVALID_RESPONSE,
    RestClientError,
    envelope,
    error_call_result,
    format_pydantic_issues,
    validation_envelope,
)
from ..logging_config import get_logger, log_tool_call

logger = get_logger(__name__)

TOOL_NAME = "list_pets"

# ---------------------------------------------------------------------------
# 与 Go 后端 internal/model、/api/v1/meta 完全一致的真实允许值
# ---------------------------------------------------------------------------

SPECIES_VALUES = ("犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他")
STATUS_VALUES = ("待就诊", "就诊中", "住院中", "已康复", "慢性病随访")
SORT_BY_VALUES = (
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
)
ORDER_VALUES = ("asc", "desc")

Species = Literal[
    "犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"
]
Status = Literal["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]
SortBy = Literal[
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
]
Order = Literal["asc", "desc"]


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------


class ListPetsInput(BaseModel):
    """``list_pets`` 的严格输入模型（额外字段一律拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, description="全文检索（跨字段，空格分词 AND）")
    name: str | None = Field(default=None, description="宠物姓名（模糊）")
    ownerName: str | None = Field(default=None, description="主人姓名（模糊）")
    ownerPhone: str | None = Field(default=None, description="主人电话（模糊）")
    species: Species | None = Field(default=None, description="种类精确筛选")
    doctor: str | None = Field(default=None, description="主治医生（模糊）")
    disease: str | None = Field(default=None, description="疾病 / 主要诊断（模糊）")
    status: Status | None = Field(default=None, description="就诊状态精确筛选")
    min: float | None = Field(default=None, ge=0, description="最低总花费（含），非负")
    max: float | None = Field(default=None, ge=0, description="最高总花费（含），非负")
    sortBy: SortBy | None = Field(default=None, description="排序字段")
    order: Order | None = Field(default=None, description="排序方向")
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    pageSize: int = Field(default=20, ge=1, le=500, description="每页条数，1-500")

    @field_validator("min", "max", "page", "pageSize", mode="before")
    @classmethod
    def _reject_bool(cls, value: Any) -> Any:
        # JSON 的 true/false 是布尔，不是数字。
        if isinstance(value, bool):
            raise ValueError("必须是数字，不能是布尔值")
        return value

    @model_validator(mode="after")
    def _check_finite_and_range(self) -> "ListPetsInput":
        for name, value in (("min", self.min), ("max", self.max)):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限数字，不能为 NaN 或 Infinity")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min 不能大于 max")
        return self

    def to_query_params(self) -> dict[str, str]:
        """转为发给 Go 后端的查询参数（空字符串参数省略；分页始终发送）。"""
        params: dict[str, str] = {"page": str(self.page), "pageSize": str(self.pageSize)}
        for key, value in (
            ("q", self.q),
            ("name", self.name),
            ("ownerName", self.ownerName),
            ("ownerPhone", self.ownerPhone),
            ("species", self.species),
            ("doctor", self.doctor),
            ("disease", self.disease),
            ("status", self.status),
            ("sortBy", self.sortBy),
            ("order", self.order),
        ):
            if value is not None and str(value).strip() != "":
                params[key] = str(value).strip()
        if self.min is not None:
            params["min"] = _format_number(self.min)
        if self.max is not None:
            params["max"] = _format_number(self.max)
        return params


def _format_number(value: float) -> str:
    # 整数浮点输出为 "1000" 而非 "1000.0"，与 Go strconv 风格接近。
    return str(int(value)) if float(value).is_integer() else str(value)


# ---------------------------------------------------------------------------
# 成功输出模型（对应 Go store.Result 与 model.Pet）
# ---------------------------------------------------------------------------


class Treatment(BaseModel):
    """消费明细（Go model.Treatment）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    item: str = ""
    category: str = ""
    amount: float = 0
    doctor: str = ""
    date: str = ""
    note: str = ""


class MedicalRecord(BaseModel):
    """历史病历（Go model.MedicalRecord）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    visitDate: str = ""
    doctor: str = ""
    diagnosis: str = ""
    symptoms: str = ""
    treatment: str = ""
    prescription: list[str] = Field(default_factory=list)
    weightKg: float | None = None
    temperature: float | None = None
    followUp: str = ""
    charge: float = 0
    createdAt: str = ""


class Pet(BaseModel):
    """宠物档案（Go model.Pet）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    species: str = ""
    breed: str = ""
    gender: str = ""
    ageMonths: int = 0
    color: str = ""
    chipNo: str = ""
    ownerName: str
    ownerPhone: str
    ownerAddr: str = ""
    doctor: str
    disease: str
    status: str
    allergy: str = ""
    note: str = ""
    # Go 中无 omitempty：nil 切片会序列化为 null，这里同时接受 null 与数组。
    records: list[MedicalRecord] = Field(default_factory=list)
    charges: list[Treatment] = Field(default_factory=list)
    totalCost: float = 0
    visitCount: int = 0
    createdAt: str = ""
    updatedAt: str = ""

    @field_validator("records", "charges", mode="before")
    @classmethod
    def _null_means_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class ListPetsOutput(BaseModel):
    """``GET /api/v1/pets`` 成功信封中的 ``data``。"""

    model_config = ConfigDict(extra="ignore")

    items: list[Pet]
    total: int
    page: int
    pageSize: int
    totalPages: int
    totalCost: float


# ---------------------------------------------------------------------------
# 工具描述
# ---------------------------------------------------------------------------

LIST_PETS_DESCRIPTION = """\
查询宠物医院的宠物档案列表（代理 Go REST API 的 GET /api/v1/pets）。

用途：按关键词、主人、种类、医生、疾病、就诊状态、总花费区间进行筛选，\
并支持排序与分页；返回当前页档案（含历史病历 records 与消费明细 charges）、\
总数、总页数以及结果集花费合计 totalCost。

参数（全部可选，名称与后端一致）：
- q: 全文检索关键词（跨字段，空格分词 AND）
- name / ownerName / ownerPhone / doctor / disease: 对应字段模糊匹配
- species: 种类，仅允许 犬/猫/兔/鸟/仓鼠/爬宠/其他
- status: 就诊状态，仅允许 待就诊/就诊中/住院中/已康复/慢性病随访
- min / max: 总花费闭区间，非负数字，且 min <= max
- sortBy: 仅允许 id/name/ownerName/species/doctor/disease/status/\
totalCost/visitCount/createdAt/updatedAt
- order: asc 或 desc（后端默认 desc）
- page: 页码，>= 1（默认 1）
- pageSize: 每页条数，1-500（默认 20）

适用场景：AI Agent 需要检索“某位主人的宠物”“某医生接诊的犬”“花费超过阈值的档案”\
“某种疾病 / 某状态的档案分页列表”等只读查询场景。本工具不做任何写操作。

返回值：成功时结构化输出 {items, total, page, pageSize, totalPages, totalCost}；\
失败时返回 is_error=true 的统一错误信封 {error: {code, message, details}}，\
code 取值 VALIDATION_ERROR / BACKEND_TIMEOUT / BACKEND_UNAVAILABLE / \
BACKEND_API_ERROR / BACKEND_INVALID_RESPONSE / INTERNAL_ERROR。\
"""


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register(server: Any) -> None:
    """把 ``list_pets`` 注册到 MCPServer 实例。

    函数参数即广告给客户端的 JSON Schema（与 ListPetsInput 严格对齐）；
    工具体内仍以 ctx 中的**原始 arguments** 重新做一次完整 Pydantic 校验，
    以覆盖 SDK 层不处理的未知字段、NaN、跨字段约束等情况。
    """

    @server.tool(
        name=TOOL_NAME,
        description=LIST_PETS_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def list_pets(  # noqa: PLR0913 - 参数与后端查询参数一一对应
        q: Annotated[str | None, Field(description="全文检索关键词（跨字段，空格 AND）")] = None,
        name: Annotated[str | None, Field(description="宠物姓名（模糊）")] = None,
        ownerName: Annotated[str | None, Field(description="主人姓名（模糊）")] = None,
        ownerPhone: Annotated[str | None, Field(description="主人电话（模糊）")] = None,
        species: Annotated[Species | None, Field(description="种类：犬/猫/兔/鸟/仓鼠/爬宠/其他")] = None,
        doctor: Annotated[str | None, Field(description="主治医生（模糊）")] = None,
        disease: Annotated[str | None, Field(description="疾病/主要诊断（模糊）")] = None,
        status: Annotated[Status | None, Field(description="就诊状态")] = None,
        min: Annotated[float | None, Field(ge=0, description="最低总花费（含），非负")] = None,
        max: Annotated[float | None, Field(ge=0, description="最高总花费（含），非负")] = None,
        sortBy: Annotated[SortBy | None, Field(description="排序字段")] = None,
        order: Annotated[Order | None, Field(description="排序方向：asc/desc")] = None,
        page: Annotated[int, Field(ge=1, description="页码，从 1 开始")] = 1,
        pageSize: Annotated[int, Field(ge=1, le=500, description="每页条数，1-500")] = 20,
        ctx: Context | None = None,
    ) -> CallToolResult:
        # noqa: D401 - MCP 工具说明见 LIST_PETS_DESCRIPTION
        return await _run_list_pets(
            server,
            ctx,
            injected={
                "q": q,
                "name": name,
                "ownerName": ownerName,
                "ownerPhone": ownerPhone,
                "species": species,
                "doctor": doctor,
                "disease": disease,
                "status": status,
                "min": min,
                "max": max,
                "sortBy": sortBy,
                "order": order,
                "page": page,
                "pageSize": pageSize,
            },
        )


# ---------------------------------------------------------------------------
# 执行体（独立于装饰器，便于阅读与复用）
# ---------------------------------------------------------------------------


def _raw_arguments(ctx: Context | None) -> dict[str, Any] | None:
    """从请求上下文取出未经 SDK 处理的原始 arguments（保留未知字段）。"""
    request_context = getattr(ctx, "_request_context", None)
    if request_context is None:
        return None
    params = getattr(request_context, "params", None)
    if isinstance(params, Mapping) and isinstance(params.get("arguments"), dict):
        return dict(params["arguments"])
    return None


async def _run_list_pets(
    server: Any, ctx: Context | None, *, injected: dict[str, Any]
) -> CallToolResult:
    started = time.perf_counter()
    raw = _raw_arguments(ctx)
    if raw is None:
        # 无请求上下文（例如绕过协议直接调用）时回退到注入的已解析参数。
        raw = {key: value for key, value in injected.items() if value is not None}

    try:
        arguments = ListPetsInput.model_validate(raw)
    except ValidationError as exc:
        env = validation_envelope(format_pydantic_issues(exc))
        log_tool_call(
            logger,
            tool_name=TOOL_NAME,
            params=raw,
            status="validation_error",
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code=env["error"]["code"],
        )
        return error_call_result(env)

    query = arguments.to_query_params()

    try:
        data = await server.rest_client.list_pets(query)
    except RestClientError as exc:
        log_tool_call(
            logger,
            tool_name=TOOL_NAME,
            params=query,
            status="backend_error",
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code=exc.code,
        )
        return error_call_result(exc.to_envelope())

    try:
        output = ListPetsOutput.model_validate(data)
    except ValidationError as exc:
        env = envelope(
            BACKEND_INVALID_RESPONSE,
            "后端返回的数据不符合 list_pets 输出模型。",
            {"issues": format_pydantic_issues(exc)},
        )
        log_tool_call(
            logger,
            tool_name=TOOL_NAME,
            params=query,
            status="invalid_response",
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code=BACKEND_INVALID_RESPONSE,
        )
        return error_call_result(env)

    payload = output.model_dump(mode="json", by_alias=True)
    log_tool_call(
        logger,
        tool_name=TOOL_NAME,
        params=query,
        status="ok",
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
    )
