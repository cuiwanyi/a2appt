"""A2A over HTTP：标准 AgentCard 发现 + JSON-RPC 2.0 任务委派。

协议落点（对齐 A2A 规范，不依赖任何 SDK 的易变 API）：
    GET  /.well-known/agent-card.json   -> AgentCard（智能体发现）
    POST /  message/send                -> Task（含 artifacts / status）
    POST /  tasks/get | tasks/cancel    -> Task

部署：4 个 Agent 各占一个 127.0.0.1 端口，以线程方式跑在同一进程。
同进程只决定「部署拓扑」，不决定「协议」：发现、委派、状态迁移、Artifact
回传全部走 HTTP + JSON-RPC；把 server 拆到别的机器只需换 url_by_skill。

为什么不用 a2a-sdk：本机装到的 1.3.1 已改成 gRPC 向结构，
a2a.server.apps / DefaultRequestHandler / A2AClient 这些旧 API 全部不存在。
与其绑死在一个大版本上，不如按规范实现一个可验证的最小 transport。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

import config
from .bus import A2ABus
from .types import (
    AgentCard,
    Artifact,
    Message,
    Part,
    Task,
    TaskState,
    _new_id,
    _now,
)

# ---------------------------------------------------------------- 数据转换
def part_to_a2a(p: Part) -> dict:
    if p.kind == "text":
        return {"kind": "text", "text": p.text}
    if p.kind == "data":
        return {"kind": "data", "data": p.data}
    if p.kind == "file":
        path = Path(p.path or "")
        return {"kind": "file", "file": {
            "name": path.name, "mimeType": p.mime or "application/octet-stream",
            "uri": path.resolve().as_uri() if path.exists() else str(path),
        }}
    return {"kind": "text", "text": str(p.data)}


def _uri_to_path(uri: str) -> str:
    if not uri or not uri.startswith("file:"):
        return uri
    p = unquote(urlparse(uri).path)
    if re.match(r"^/[A-Za-z]:", p):
        p = p[1:]
    return p


def a2a_to_parts(raw: list[dict]) -> list[Part]:
    out = []
    for p in raw or []:
        kind = p.get("kind")
        if kind == "text":
            out.append(Part(kind="text", text=p.get("text")))
        elif kind == "data":
            out.append(Part(kind="data", data=p.get("data"), mime="application/json"))
        elif kind == "file":
            f = p.get("file") or {}
            out.append(Part(kind="file", path=_uri_to_path(f.get("uri") or f.get("path") or ""),
                            mime=f.get("mimeType")))
    return out


def task_to_a2a(task: Task) -> dict:
    return {
        "kind": "task",
        "id": task.id,
        "contextId": task.context_id,
        "status": {"state": task.state.value, "timestamp": task.updated_at},
        "artifacts": [
            {"artifactId": f"{task.id}-art{i}", "name": a.name,
             "parts": [part_to_a2a(p) for p in a.parts], "metadata": a.metadata}
            for i, a in enumerate(task.artifacts)
        ],
        "history": [m.to_dict() for m in task.history],
        "metadata": task.metadata,
    }


def standard_card(card: AgentCard, url: str) -> dict:
    return {
        "name": card.name,
        "description": card.description,
        "version": card.version,
        "url": url,
        "protocolVersion": "0.3.0",
        "capabilities": card.capabilities,
        "defaultInputModes": ["text", "data", "file"],
        "defaultOutputModes": ["text", "data", "file"],
        "skills": [
            {"id": s.id, "name": s.name, "description": s.description,
             "tags": ["pptx", "layout", "a2a"], "inputModes": ["text", "data"],
             "outputModes": ["text", "data", "file"]}
            for s in card.skills
        ],
        "provider": {"organization": "a2appt", "url": url},
    }


# ---------------------------------------------------------------- HTTP Server
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/.well-known/agent-card.json":
            self._reply(self.server.card)
        elif path == "/health":
            self._reply({"status": "ok", "agent": self.server.card.get("name")})
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        method, params, rid = payload.get("method"), payload.get("params") or {}, payload.get("id")
        try:
            result = self.server.dispatch(method, params)
            self._reply({"jsonrpc": "2.0", "id": rid, "result": result})
        except Exception as exc:  # noqa: BLE001
            self._reply({"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})

    def log_message(self, fmt: str, *args) -> None:
        pass  # 静音，trace 由 A2ABus 统一记录


class _A2AHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, dispatch):
        self.dispatch = dispatch
        self.card: dict = {}
        super().__init__(addr, _Handler)


class A2AServer:
    """把一个 BaseAgent 暴露成标准 A2A 服务端。"""

    def __init__(self, agent, host: str = "127.0.0.1", port: int = 0):
        self.agent = agent
        self.tasks: dict[str, Task] = {}
        self.httpd = _A2AHTTPServer((host, port), self._dispatch)
        self.port = self.httpd.server_address[1]
        self.url = f"http://{host}:{self.port}"
        self.card = standard_card(agent.card, self.url)
        self.httpd.card = self.card
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "A2AServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "message/send":
            meta = dict(params.get("metadata") or {})
            skill = meta.pop("skill", "")
            raw_msg = params.get("message") or {}
            task = Task(
                skill=skill,
                context_id=params.get("contextId") or _new_id("ctx"),
                message=Message(role=raw_msg.get("role", "user"),
                                parts=a2a_to_parts(raw_msg.get("parts") or [])),
                metadata=meta,
            )
            self.agent.bus.emit("a2a.server.received", agent=self.agent.NAME,
                                skill=skill, task_id=task.id, transport="http")
            task.state = TaskState.SUBMITTED
            task.state = TaskState.WORKING
            self.agent.handle(task)
            task.state = TaskState.COMPLETED
            task.updated_at = _now()
            self.tasks[task.id] = task
            return task_to_a2a(task)
        if method == "tasks/get":
            tid = (params.get("id") or "").strip()
            if tid not in self.tasks:
                raise KeyError(f"task not found: {tid}")
            return task_to_a2a(self.tasks[tid])
        if method == "tasks/cancel":
            tid = (params.get("id") or "").strip()
            if tid not in self.tasks:
                raise KeyError(f"task not found: {tid}")
            t = self.tasks[tid]
            t.state = TaskState.CANCELED
            return task_to_a2a(t)
        raise ValueError(f"method not found: {method}")


def start_agent_servers(ctx, bus, host: str = "127.0.0.1") -> tuple[list[A2AServer], dict[str, str]]:
    """启动 4 个 Agent 的 A2A Server，返回 (servers, {skill_id: url})。"""
    from agents import AGENT_CLASSES

    servers: list[A2AServer] = []
    url_by_skill: dict[str, str] = {}
    for cls in AGENT_CLASSES:
        agent = cls(ctx, bus)
        srv = A2AServer(agent, host=host, port=0).start()  # port=0 让 OS 分配，避免端口冲突
        servers.append(srv)
        for sk in agent.card.skills:
            url_by_skill[sk.id] = srv.url
    return servers, url_by_skill


# ---------------------------------------------------------------- HTTP Client
class A2AClient:
    def __init__(self, base_url: str, timeout: int | None = None):
        self.base_url = base_url.rstrip("/")
        # 一次 message/send 里可能串N次生图/渲染，默认超时必须按最慢的那步给
        self.timeout = timeout if timeout is not None else config.HTTP_TIMEOUT
        self.session = requests.Session()
        self._card: dict | None = None

    def get_card(self) -> dict:
        if self._card is None:
            r = self.session.get(self.base_url + "/.well-known/agent-card.json", timeout=self.timeout)
            r.raise_for_status()
            self._card = r.json()
        return self._card

    def rpc(self, method: str, params: dict) -> dict:
        payload = {"jsonrpc": "2.0", "id": _new_id("req"), "method": method, "params": params}
        r = self.session.post(self.base_url + "/", json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"]["message"])
        return data["result"]


# ---------------------------------------------------------------- HTTP Bus
class HttpA2ABus(A2ABus):
    """把 A2ABus.send() 换成的 HTTP 委派；trace / emit 语义与进程内模式完全一致。"""

    def __init__(self, context_id=None, trace_file=None, timeout: int | None = None):
        super().__init__(context_id=context_id, trace_file=trace_file)
        self.url_by_skill: dict[str, str] = {}
        self.remote_cards: dict[str, dict] = {}
        self._clients: dict[str, A2AClient] = {}
        self.timeout = timeout if timeout is not None else config.HTTP_TIMEOUT

    def set_endpoints(self, url_by_skill: dict[str, str]) -> None:
        self.url_by_skill = dict(url_by_skill)

    def _client(self, url: str) -> A2AClient:
        if url not in self._clients:
            self._clients[url] = A2AClient(url, timeout=self.timeout)
        return self._clients[url]

    def register(self, agent: Any) -> None:
        """HTTP 模式下 Agent 由各自 Server 持有，不在总线本地注册。"""
        self.emit("agent.exposed_over_http", agent=agent.NAME, url=self.url_by_skill)

    def cards(self) -> list[dict]:
        return list(self.remote_cards.values())

    def discover(self, skill: str) -> list[dict]:
        url = self.url_by_skill.get(skill)
        if not url:
            return []
        card = self._client(url).get_card()
        self.remote_cards[card["name"]] = card
        self.emit("a2a.discovery", skill=skill, url=url, agent=card.get("name"),
                  skills=[s["id"] for s in card.get("skills", [])])
        return [card]

    def send(self, skill: str, *parts: Part, target: str | None = None, **metadata) -> Task:
        task = self.new_task(skill, *parts, **metadata)
        url = self.url_by_skill.get(skill)
        if not url:
            task.state = TaskState.FAILED
            task.error = f"no HTTP endpoint for skill '{skill}'"
            self.emit("task.failed", task_id=task.id, skill=skill, error=task.error)
            raise RuntimeError(task.error)

        self.discover(skill)
        self._transition(task, TaskState.SUBMITTED, transport="http", endpoint=url)
        self._transition(task, TaskState.WORKING, transport="http", endpoint=url)
        message = {
            "kind": "message", "role": "user", "messageId": task.message.message_id,
            "parts": [part_to_a2a(p) for p in task.message.parts],
        }
        result = self._client(url).rpc(
            "message/send",
            {"message": message, "metadata": {"skill": skill, **metadata},
             "contextId": task.context_id},
        )
        for a in result.get("artifacts", []):
            art = Artifact(name=a.get("name", "artifact"),
                           parts=a2a_to_parts(a.get("parts", [])),
                           metadata=a.get("metadata", {}))
            task.artifacts.append(art)
        self._transition(task, TaskState.COMPLETED, transport="http",
                         artifacts=[a.name for a in task.artifacts],
                         remote_task_id=result.get("id"))
        return task
