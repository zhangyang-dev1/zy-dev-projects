"""无 Go 环境时的本地模拟后端（仅用于体验/教学，非交付物）。

用纯标准库模拟 Go 宠物医院 REST API 的 ``GET /api/v1/pets`` 契约：

- 统一信封 ``{"code","message","data","time"}``；
- 14 个查询参数（q/name/ownerName/ownerPhone/species/doctor/disease/status/
  min/max/sortBy/order/page/pageSize）语义与 Go 实现一致；
- ``data.totalCost`` 为**当前页**结果集花费合计；
- 默认监听 127.0.0.1:8080（与 Go 服务默认地址相同，MCP 无需改配置）。

用法::

    python scripts/mock_backend.py            # 监听 127.0.0.1:8080
    python scripts/mock_backend.py -addr 127.0.0.1:18080
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SPECIES = ("犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他")
STATUS = ("待就诊", "就诊中", "住院中", "已康复", "慢性病随访")

_PETS = [
    {
        "id": "p-001", "name": "旺财", "species": "犬", "breed": "金毛寻回犬",
        "gender": "公", "ageMonths": 36, "color": "金黄", "chipNo": "156000000000001",
        "ownerName": "张伟", "ownerPhone": "13800000001", "ownerAddr": "朝阳区望京西路 12 号",
        "doctor": "李医生", "disease": "髋关节发育不良", "status": "住院中",
        "allergy": "", "note": "需控制体重",
        "records": [
            {"id": "r-001", "visitDate": "2026-08-01", "doctor": "李医生",
             "diagnosis": "髋关节发育不良", "symptoms": "后肢跛行", "treatment": "手术+康复",
             "prescription": ["止痛药", "关节宝"], "weightKg": 32.5, "temperature": 38.6,
             "followUp": "两周后复查", "charge": 4800.0, "createdAt": "2026-08-01T09:00:00Z"},
        ],
        "charges": [
            {"id": "c-001", "item": "髋关节手术", "category": "手术", "amount": 4200.0,
             "doctor": "李医生", "date": "2026-08-02", "note": ""},
            {"id": "c-002", "item": "X 光检查", "category": "检查", "amount": 600.0,
             "doctor": "李医生", "date": "2026-08-01", "note": ""},
        ],
        "totalCost": 4800.0, "visitCount": 3,
        "createdAt": "2026-07-01T08:00:00Z", "updatedAt": "2026-08-05T10:00:00Z",
    },
    {
        "id": "p-002", "name": "咪咪", "species": "猫", "breed": "英国短毛猫",
        "gender": "母", "ageMonths": 18, "color": "蓝灰", "chipNo": "156000000000002",
        "ownerName": "王芳", "ownerPhone": "13800000002", "ownerAddr": "海淀区中关村大街 5 号",
        "doctor": "陈医生", "disease": "猫鼻支", "status": "就诊中",
        "allergy": "", "note": "",
        "records": [
            {"id": "r-002", "visitDate": "2026-09-10", "doctor": "陈医生",
             "diagnosis": "猫鼻支", "symptoms": "打喷嚏、流泪", "treatment": "抗病毒+眼药水",
             "prescription": ["泛昔洛韦", "滴眼液"], "weightKg": 4.2, "temperature": 39.4,
             "followUp": "一周后复诊", "charge": 560.0, "createdAt": "2026-09-10T14:00:00Z"},
        ],
        "charges": [
            {"id": "c-003", "item": "PCR 检测", "category": "检查", "amount": 360.0,
             "doctor": "陈医生", "date": "2026-09-10", "note": ""},
            {"id": "c-004", "item": "抗病毒药物", "category": "药品", "amount": 200.0,
             "doctor": "陈医生", "date": "2026-09-10", "note": ""},
        ],
        "totalCost": 560.0, "visitCount": 2,
        "createdAt": "2026-07-02T08:00:00Z", "updatedAt": "2026-09-10T15:00:00Z",
    },
    {
        "id": "p-003", "name": "雪球", "species": "兔", "breed": "垂耳兔",
        "gender": "母", "ageMonths": 10, "color": "白", "chipNo": "",
        "ownerName": "刘洋", "ownerPhone": "13800000003", "ownerAddr": "西城区金融街 8 号",
        "doctor": "陈医生", "disease": "牙齿过长", "status": "已康复",
        "allergy": "", "note": "",
        "records": [], "charges": [
            {"id": "c-005", "item": "磨牙处理", "category": "处置", "amount": 180.0,
             "doctor": "陈医生", "date": "2026-06-15", "note": ""},
        ],
        "totalCost": 180.0, "visitCount": 1,
        "createdAt": "2026-07-03T08:00:00Z", "updatedAt": "2026-06-20T09:00:00Z",
    },
    {
        "id": "p-004", "name": "皮皮", "species": "鸟", "breed": "玄凤鹦鹉",
        "gender": "公", "ageMonths": 14, "color": "灰白", "chipNo": "",
        "ownerName": "赵敏", "ownerPhone": "13800000004", "ownerAddr": "东城区东直门内大街 3 号",
        "doctor": "周医生", "disease": "嗉囊炎", "status": "待就诊",
        "allergy": "", "note": "",
        "records": [], "charges": [],
        "totalCost": 0.0, "visitCount": 0,
        "createdAt": "2026-07-04T08:00:00Z", "updatedAt": "2026-07-04T08:00:00Z",
    },
    {
        "id": "p-005", "name": "团团", "species": "仓鼠", "breed": "金丝熊",
        "gender": "母", "ageMonths": 8, "color": "米黄", "chipNo": "",
        "ownerName": "孙丽", "ownerPhone": "13800000005", "ownerAddr": "丰台区方庄路 20 号",
        "doctor": "周医生", "disease": "湿尾症", "status": "慢性病随访",
        "allergy": "", "note": "注意垫料卫生",
        "records": [
            {"id": "r-005", "visitDate": "2026-05-20", "doctor": "周医生",
             "diagnosis": "湿尾症", "symptoms": "腹泻", "treatment": "补液+抗生素",
             "prescription": ["口服补液盐"], "weightKg": 0.12, "temperature": 37.8,
             "followUp": "每月随访", "charge": 90.0, "createdAt": "2026-05-20T11:00:00Z"},
        ],
        "charges": [
            {"id": "c-006", "item": "粪便检查", "category": "检查", "amount": 60.0,
             "doctor": "周医生", "date": "2026-05-20", "note": ""},
            {"id": "c-007", "item": "抗生素", "category": "药品", "amount": 30.0,
             "doctor": "周医生", "date": "2026-05-20", "note": ""},
        ],
        "totalCost": 90.0, "visitCount": 2,
        "createdAt": "2026-07-05T08:00:00Z", "updatedAt": "2026-06-01T09:00:00Z",
    },
    {
        "id": "p-006", "name": "阿绿", "species": "爬宠", "breed": "绿鬣蜥",
        "gender": "公", "ageMonths": 30, "color": "绿", "chipNo": "",
        "ownerName": "周杰", "ownerPhone": "13800000006", "ownerAddr": "石景山区鲁谷路 1 号",
        "doctor": "李医生", "disease": "代谢性骨病", "status": "就诊中",
        "allergy": "", "note": "需补充 UVB 与钙",
        "records": [], "charges": [
            {"id": "c-008", "item": "血钙检测", "category": "检查", "amount": 240.0,
             "doctor": "李医生", "date": "2026-09-01", "note": ""},
        ],
        "totalCost": 240.0, "visitCount": 1,
        "createdAt": "2026-07-06T08:00:00Z", "updatedAt": "2026-09-01T10:00:00Z",
    },
    {
        "id": "p-007", "name": "豆豆", "species": "犬", "breed": "柯基",
        "gender": "公", "ageMonths": 24, "color": "黄白", "chipNo": "156000000000007",
        "ownerName": "张伟", "ownerPhone": "13800000001", "ownerAddr": "朝阳区望京西路 12 号",
        "doctor": "周医生", "disease": "肠胃炎", "status": "已康复",
        "allergy": "鸡肉", "note": "",
        "records": [], "charges": [
            {"id": "c-009", "item": "血常规", "category": "检查", "amount": 150.0,
             "doctor": "周医生", "date": "2026-08-20", "note": ""},
            {"id": "c-010", "item": "处方粮", "category": "药品", "amount": 320.0,
             "doctor": "周医生", "date": "2026-08-20", "note": ""},
        ],
        "totalCost": 470.0, "visitCount": 1,
        "createdAt": "2026-07-07T08:00:00Z", "updatedAt": "2026-08-25T09:00:00Z",
    },
    {
        "id": "p-008", "name": "花花", "species": "猫", "breed": "中华田园猫",
        "gender": "母", "ageMonths": 60, "color": "三花", "chipNo": "156000000000008",
        "ownerName": "王芳", "ownerPhone": "13800000002", "ownerAddr": "海淀区中关村大街 5 号",
        "doctor": "李医生", "disease": "慢性肾病", "status": "慢性病随访",
        "allergy": "", "note": "低磷饮食",
        "records": [
            {"id": "r-008", "visitDate": "2026-09-05", "doctor": "李医生",
             "diagnosis": "慢性肾病 III 期", "symptoms": "多饮多尿", "treatment": "饮食管理+输液",
             "prescription": ["肾脏处方粮"], "weightKg": 3.8, "temperature": 38.2,
             "followUp": "每月复查生化", "charge": 720.0, "createdAt": "2026-09-05T16:00:00Z"},
        ],
        "charges": [
            {"id": "c-011", "item": "生化全项", "category": "检查", "amount": 520.0,
             "doctor": "李医生", "date": "2026-09-05", "note": ""},
            {"id": "c-012", "item": "皮下输液", "category": "处置", "amount": 200.0,
             "doctor": "李医生", "date": "2026-09-05", "note": ""},
        ],
        "totalCost": 720.0, "visitCount": 6,
        "createdAt": "2026-07-08T08:00:00Z", "updatedAt": "2026-09-05T17:00:00Z",
    },
]

_SORT_KEYS = {
    "id": lambda p: p["id"],
    "name": lambda p: p["name"],
    "ownerName": lambda p: p["ownerName"],
    "species": lambda p: p["species"],
    "doctor": lambda p: p["doctor"],
    "disease": lambda p: p["disease"],
    "status": lambda p: STATUS.index(p["status"]) if p["status"] in STATUS else -1,
    "totalCost": lambda p: p["totalCost"],
    "visitCount": lambda p: p["visitCount"],
    "createdAt": lambda p: p["createdAt"],
    "updatedAt": lambda p: p["updatedAt"],
}


def _first(params: dict, key: str, default: str = "") -> str:
    values = params.get(key)
    return values[0] if values else default


def _query_float(params: dict, key: str) -> float | None:
    raw = _first(params, key)
    if raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _match(p: dict, token: str) -> bool:
    haystack = " ".join(
        str(p[k]) for k in (
            "id", "name", "species", "breed", "gender", "color", "chipNo",
            "ownerName", "ownerPhone", "ownerAddr", "doctor", "disease",
            "status", "allergy", "note",
        )
    ).lower()
    return token.lower() in haystack


def list_pets(params: dict) -> dict:
    items = list(_PETS)

    q = _first(params, "q").strip()
    if q:
        tokens = [t for t in q.split() if t]
        for token in tokens:
            items = [p for p in items if _match(p, token)]

    for key in ("name", "ownerName", "ownerPhone", "doctor", "disease"):
        value = _first(params, key).strip()
        if value:
            items = [p for p in items if value.lower() in str(p[key]).lower()]

    species = _first(params, "species").strip()
    if species:
        items = [p for p in items if p["species"] == species]
    status = _first(params, "status").strip()
    if status:
        items = [p for p in items if p["status"] == status]

    min_cost = _query_float(params, "min")
    max_cost = _query_float(params, "max")
    if min_cost is not None:
        items = [p for p in items if p["totalCost"] >= min_cost]
    if max_cost is not None:
        items = [p for p in items if p["totalCost"] <= max_cost]

    sort_by = _first(params, "sortBy").strip() or "createdAt"
    order = _first(params, "order").strip() or "desc"
    key_fn = _SORT_KEYS.get(sort_by, _SORT_KEYS["createdAt"])
    items.sort(key=key_fn, reverse=(order != "asc"))

    try:
        page = int(_first(params, "page", "1"))
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    try:
        page_size = int(_first(params, "pageSize", "20"))
    except ValueError:
        page_size = 20
    if page_size < 1:
        page_size = 20
    if page_size > 500:
        page_size = 500

    total = len(items)
    total_pages = math.ceil(total / page_size) if page_size else 0
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
        "totalCost": round(sum(p["totalCost"] for p in page_items), 2),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MockPetHospital/1.0"

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/pets":
            data = list_pets(parse_qs(parsed.query))
            self._json(200, {"code": 200, "message": "ok", "data": data})
        elif parsed.path == "/api/v1/meta":
            self._json(200, {"code": 200, "message": "ok", "data": {
                "species": list(SPECIES), "status": list(STATUS),
                "sortBy": list(_SORT_KEYS.keys()), "order": ["asc", "desc"],
            }})
        else:
            self._json(404, {"code": 404, "message": "接口不存在", "data": None})

    def _json(self, status: int, body: dict):
        payload = dict(body)
        payload["time"] = datetime.now(timezone.utc).isoformat()
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # noqa: A002
        print("[mock-backend] %s %s" % (self.command, self.path))


def main():
    parser = argparse.ArgumentParser(description="Go 宠物医院 API 的本地模拟后端")
    parser.add_argument("-addr", default="127.0.0.1:8080", help="监听地址，默认 127.0.0.1:8080")
    args = parser.parse_args()
    host, _, port = args.addr.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port or 8080)), Handler)
    print(f"[mock-backend] 模拟 Go 宠物医院 API 已启动: http://{host or '127.0.0.1'}:{port or 8080}")
    print("[mock-backend] 数据为内置 8 条模拟档案；Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
