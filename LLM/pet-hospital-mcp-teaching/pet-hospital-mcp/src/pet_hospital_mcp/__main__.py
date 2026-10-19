"""入口：``python -m pet_hospital_mcp`` 启动无状态 Streamable HTTP 服务。"""

from __future__ import annotations

from .config import Settings
from .logging_config import configure_logging, get_logger
from .server import build_server


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    logger = get_logger("pet_hospital_mcp")

    server = build_server(settings)
    logger.info(
        "starting pet-hospital-mcp",
        extra={
            "event": "startup",
            "detail": {
                "host": settings.host,
                "port": settings.port,
                "mcp_path": settings.mcp_path,
                "backend_base_url": settings.backend_base_url,
                "stateless_http": True,
                "json_response": True,
            },
        },
    )
    # 传输选项全部在 run() 上（2.x 规定），不传给 MCPServer 构造器。
    server.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
