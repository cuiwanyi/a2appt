"""A2A 进程内总线：注册、发现、派发、状态流追踪。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .types import AgentCard, Part, Task, TaskState, _new_id, _now, user_message


class A2ABus:
    """把 A2A 的「按 skill 发现 Agent -> 委派 Task -> 收 Artifact」压到单进程内。

    - 每个 Agent 用 AgentCard 注册，Orchestrator 只认 skill id，不认具体类；
    - 所有状态迁移写成 TaskStatusUpdateEvent 落到 trace.jsonl，便于复盘返修链路；
    - 换成 a2a-sdk 时，只需把 send() 换成 HTTP client，上层编排不动。
    """

    def __init__(self, context_id: str | None = None, trace_file: str | Path | None = None):
        self.context_id = context_id or _new_id("ctx")
        self._agents: dict[str, Any] = {}
        self._skill_index: dict[str, list[str]] = {}
        self.events: list[dict] = []
        self.trace_file = Path(trace_file) if trace_file else None
        if self.trace_file:
            self.trace_file.parent.mkdir(parents=True, exist_ok=True)
            self.trace_file.write_text("", encoding="utf-8")

    # ---------- discovery ----------
    def register(self, agent: Any) -> None:
        card: AgentCard = agent.card
        self._agents[card.name] = agent
        for skill in card.skills:
            self._skill_index.setdefault(skill.id, []).append(card.name)
        self.emit("agent.registered", agent=card.name, skills=[s.id for s in card.skills])

    def cards(self) -> list[dict]:
        return [a.card.to_dict() for a in self._agents.values()]

    def discover(self, skill: str) -> list[AgentCard]:
        """A2A agent discovery：按 skill id 找能接活的 Agent。"""
        return [self._agents[n].card for n in self._skill_index.get(skill, [])]

    # ---------- task ----------
    def new_task(self, skill: str, *parts: Part, **metadata) -> Task:
        return Task(
            skill=skill,
            context_id=self.context_id,
            message=user_message(*parts) if parts else None,
            metadata=metadata,
        )

    def send(self, skill: str, *parts: Part, target: str | None = None, **metadata) -> Task:
        task = self.new_task(skill, *parts, **metadata)
        candidates = [target] if target else self._skill_index.get(skill, [])
        if not candidates:
            task.state = TaskState.FAILED
            task.error = f"no agent registered for skill '{skill}'"
            self.emit("task.failed", task_id=task.id, skill=skill, error=task.error)
            raise RuntimeError(task.error)

        agent_name = candidates[0]
        agent = self._agents[agent_name]
        self._transition(task, TaskState.SUBMITTED, agent=agent_name)
        self._transition(task, TaskState.WORKING, agent=agent_name)
        try:
            agent.handle(task)
        except Exception as exc:  # noqa: BLE001 - 统一收口为 A2A FAILED 状态
            task.state = TaskState.FAILED
            task.error = f"{type(exc).__name__}: {exc}"
            self._transition(task, TaskState.FAILED, agent=agent_name, error=task.error)
            raise
        else:
            self._transition(task, TaskState.COMPLETED, agent=agent_name,
                             artifacts=[a.name for a in task.artifacts])
        return task

    # ---------- tracing ----------
    def _transition(self, task: Task, state: TaskState, **extra) -> None:
        task.state = state
        task.updated_at = _now()
        self.emit("task.status", task_id=task.id, skill=task.skill, state=state.value, **extra)

    def emit(self, event: str, **payload) -> None:
        record = {"ts": _now(), "event": event, "context_id": self.context_id, **payload}
        self.events.append(record)
        if self.trace_file:
            with self.trace_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def write_trace(self, path: str | Path, cards: bool = True) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"context_id": self.context_id, "events": self.events}
        if cards:
            payload["agent_cards"] = self.cards()
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
