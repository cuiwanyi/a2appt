"""Agent 基类与共享运行上下文。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import AgentCard, AgentSkill, Artifact, Part, Task, data_part, file_part, text_part


@dataclass
class RunContext:
    """一次编排运行的共享状态。

    A2A 规范里跨 Agent 传递的是 Artifact（不是共享内存）；这里用 RunContext
    模拟「Artifact 存储」，每个 Agent 仍然只能通过 Task 的 Artifact 声明产出，
    Orchestrator 负责把 Artifact 落到磁盘并把引用传给下一个 Task。
    """

    run_id: str
    root: Path
    brief: dict
    llm: Any
    mock: bool = False
    layout: dict | None = None
    assets: dict[str, str] = field(default_factory=dict)  # asset_id -> 绝对路径
    deck_path: str | None = None
    review: dict | None = None
    rework_hints: list[str] = field(default_factory=list)

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


class BaseAgent(ABC):
    """所有 Agent 的公共壳：声明 Card、收 Task、挂 Artifact。"""

    NAME = "base"
    VERSION = "1.0.0"
    DESCRIPTION = ""
    SKILLS: list[AgentSkill] = []

    def __init__(self, ctx: RunContext, bus: Any):
        self.ctx = ctx
        self.bus = bus

    @property
    def card(self) -> AgentCard:
        return AgentCard(
            name=self.NAME,
            description=self.DESCRIPTION,
            version=self.VERSION,
            skills=self.SKILLS,
        )

    def log(self, message: str, **extra) -> None:
        self.bus.emit("agent.log", agent=self.NAME, message=message, **extra)

    def artifact(self, task: Task, name: str, *parts: Part, **metadata) -> Artifact:
        return task.add_artifact(name, *parts, agent=self.NAME, **metadata)

    def text_artifact(self, task: Task, name: str, value: str, **metadata) -> Artifact:
        return self.artifact(task, name, text_part(value), **metadata)

    def data_artifact(self, task: Task, name: str, value: Any, **metadata) -> Artifact:
        return self.artifact(task, name, data_part(value), **metadata)

    def file_artifact(self, task: Task, name: str, path: str | Path, mime: str, **metadata) -> Artifact:
        return self.artifact(task, name, file_part(path, mime), **metadata)

    @abstractmethod
    def handle(self, task: Task) -> None:
        """处理 A2A Task；产出必须挂在 task.artifacts 上。"""
