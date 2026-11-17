import hmac

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import HttpSettings


class _RequireBearerToken:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            values = Headers(scope=scope).getlist("authorization")
            if len(values) == 1:
                scheme, _, token = values[0].partition(" ")
                if scheme.lower() == "bearer" and hmac.compare_digest(token.encode("latin-1"), self.token):
                    await self.app(scope, receive, send)
                    return
            response = Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_http_app(server: MCPServer, settings: HttpSettings) -> Starlette:
    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=[f"127.0.0.1:{settings.port}"]),
    )
    app.add_middleware(_RequireBearerToken, token=settings.token)
    return app


async def serve_http(server: MCPServer, settings: HttpSettings) -> None:
    config = uvicorn.Config(
        create_http_app(server, settings),
        host="127.0.0.1",
        port=settings.port,
        access_log=False,
        proxy_headers=False,
    )
    await uvicorn.Server(config).serve()
