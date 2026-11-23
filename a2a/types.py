"""A2A 协议语义原语（进程内实现）。

数据模型对齐 Google A2A 规范的核心概念：
    AgentCard / AgentSkill  -> 智能体发现（agent discovery）
    Message / Part          -> 任务输入与协商
    Task / TaskState        -> 任务生命周期
    Artifact                -> 任务产出物
    TaskStatusUpdateEvent   -> 状态流（本实现落到 trace.jsonl）

这里是「进程内总线」，不是 HTTP 服务；换成 a2a-sdk 的 HTTP  transport 时，
这些数据结构与 Orchestrator 的调用顺序都不需要改（见 README 升级路径）。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class Part:
    """A2A Part：text / data / file 三种载荷。"""

    kind: str
    text: str | None = None
    data: Any = None
    path: str | None = None
    mime: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def text_part(value: str) -> Part:
    return Part(kind="text", text=value)


def data_part(value: Any) -> Part:
    return Part(kind="data", data=value, mime="application/json")


def file_part(path: str | bytes | Any, mime: str = "application/octet-stream") -> Part:
    p = str(path)
    return Part(kind="file", path=p, mime=mime)


@dataclass
class Message:
    role: str  # "user" | "agent"
    parts: list[Part] = field(default_factory=list)
    message_id: str = field(default_factory=lambda: _new_id("msg"))
    created_at: str = field(default_factory=_now)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if p.kind == "text" and p.text)

    @property
    def data(self) -> Any:
        for p in self.parts:
            if p.kind == "data":
                return p.data
        return None

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "message_id": self.message_id,
            "created_at": self.created_at,
            "parts": [p.to_dict() for p in self.parts],
        }


def user_message(*parts: Part) -> Message:
    return Message(role="user", parts=list(parts))


@dataclass
class Artifact:
    name: str
    parts: list[Part] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "metadata": self.metadata,
            "parts": [p.to_dict() for p in self.parts],
        }


@dataclass
class Task:
    """一次 Agent-to-Agent 委派。"""

    skill: str
    context_id: str
    message: Message | None = None
    id: str = field(default_factory=lambda: _new_id("task"))
    state: TaskState = TaskState.SUBMITTED
    artifacts: list[Artifact] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    error: str | None = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def add_artifact(self, name: str, *parts: Part, **metadata) -> Artifact:
        art = Artifact(name=name, parts=list(parts), metadata=metadata)
        self.artifacts.append(art)
        return art

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "context_id": self.context_id,
            "skill": self.skill,
            "state": self.state.value,
            "message": self.message.to_dict() if self.message else None,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "error": self.error,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AgentSkill:
    id: str
    name: str
    description: str
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


@dataclass
class AgentCard:
    """A2A Agent Card：声明「我是谁、我能干什么」。"""

    name: str
    description: str
    version: str
    skills: list[AgentSkill]
    capabilities: dict = field(default_factory=lambda: {"streaming": False, "pushNotifications": False})

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "skills": [s.to_dict() for s in self.skills],
            "capabilities": self.capabilities,
        }


def json_dump(obj: Any, **kw) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, **kw)
