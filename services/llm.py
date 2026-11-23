"""OpenAI 兼容客户端：chat / chat_json / image，外加离线 MockLLM。"""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

import requests
from PIL import Image

import config
from services.mock_data import build_mock_layout, mock_image


def _extract_json(text: str) -> Any:
    """LLM 常会带 ```json 围栏或前后解释，做稳健提取。"""
    s = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if fenced:
        s = fenced.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start : end + 1])
        raise ValueError("模型输出中没有可解析的 JSON 对象")


class LLMClient:
    """真实端点：http://8.216.45.190:8011/ （OpenAI 兼容 /v1）。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        llm_model: str | None = None,
        image_model: str | None = None,
        timeout: int | None = None,
    ):
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.API_KEY
        self.llm_model = config.resolve_model(llm_model or config.LLM_MODEL)
        self.image_model = config.resolve_model(image_model or config.IMAGE_MODEL)
        self.timeout = timeout or config.REQUEST_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    # ---------------- chat ----------------
    def chat(self, system: str, user: str, max_tokens: int = 6000) -> str:
        payload = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        resp = self.session.post(
            f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout
        )
        if resp.status_code == 400:
            # 个别推理模型不接受 system role，降级为单条 user 消息
            payload = {
                "model": self.llm_model,
                "messages": [{"role": "user", "content": system + "\n\n" + user}],
                "max_tokens": max_tokens,
            }
            resp = self.session.post(
                f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout
            )
        if resp.status_code != 200:
            raise RuntimeError(f"chat HTTP {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""

    def chat_json(self, system: str, user: str, max_tokens: int = 6000) -> dict:
        text = self.chat(system, user, max_tokens=max_tokens)
        try:
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError):
            repair = self.chat(
                system,
                user + "\n\n上一次输出不是合法 JSON，请只输出一个 JSON 对象，不要任何其它文字。",
                max_tokens=max_tokens,
            )
            return _extract_json(repair)

    # ---------------- image ----------------
    def image(self, prompt: str, out_path: str | Path, size: str = "1536x1024") -> Path:
        payload = {
            "model": self.image_model,
            "prompt": prompt,
            "size": size,
            "n": 1,
        }
        resp = self.session.post(
            f"{self.base_url}/v1/images/generations", json=payload, timeout=self.timeout
        )
        if resp.status_code != 200:
            raise RuntimeError(f"image HTTP {resp.status_code}: {resp.text[:400]}")
        item = resp.json()["data"][0]
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if item.get("b64_json"):
            p.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            r = self.session.get(item["url"], timeout=self.timeout)
            r.raise_for_status()
            p.write_bytes(r.content)
        else:
            raise RuntimeError("生图响应既没有 b64_json 也没有 url")
        # 确认能被 Pillow 解码，避免坏图进 PPT
        with Image.open(p) as im:
            im.verify()
        return p

    # ---------------- vision ----------------
    def chat_vision(self, system: str, user: str, image_paths: list[str | Path],
                    max_tokens: int = 2500) -> str:
        """多模态评审：把真实逐页截图作为 image_url 传给模型。"""
        content: list[dict] = [{"type": "text", "text": user}]
        for p in image_paths:
            with Image.open(p) as im:
                im = im.convert("RGB")
                im.thumbnail((1000, 1000))
                buf = io.BytesIO()
                im.save(buf, "PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            content.append(
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}
            )
        payload = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "max_tokens": max_tokens,
        }
        resp = self.session.post(
            f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout
        )
        if resp.status_code != 200:
            raise RuntimeError(f"vision HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json()["choices"][0]["message"]["content"] or ""

    def chat_vision_json(self, system: str, user: str, image_paths: list[str | Path],
                         max_tokens: int = 2500) -> dict:
        text = self.chat_vision(system, user, image_paths, max_tokens=max_tokens)
        try:
            return _extract_json(text)
        except (ValueError, json.JSONDecodeError):
            repair = self.chat_vision(
                system, user + "\n\n请只输出一个 JSON 对象。", image_paths, max_tokens=max_tokens
            )
            return _extract_json(repair)


class MockLLM:
    """离线占位：与 LLMClient 同接口，PlannerAgent / AssetAgent 无需分支。"""

    def __init__(self, brief: dict | None = None):
        self.brief = brief or {}
        self.llm_model = "mock-layout"
        self.image_model = "mock-image"

    def chat(self, system: str, user: str, max_tokens: int = 6000) -> str:
        return json.dumps(build_mock_layout(self.brief), ensure_ascii=False)

    def chat_json(self, system: str, user: str, max_tokens: int = 6000) -> dict:
        return build_mock_layout(self.brief)

    def image(self, prompt: str, out_path: str | Path, size: str = "1536x1024") -> Path:
        w, h = (int(v) for v in size.split("x"))
        # 用内容哈希而不是长度当 seed：多格素材的描述往往长度相近，长度取模会撞色
        import zlib

        seed = zlib.crc32(prompt.encode("utf-8")) % 997 + 1
        return mock_image(out_path, size=(w, h), seed=seed)

    def chat_vision(self, system: str, user: str, image_paths: list[str | Path],
                    max_tokens: int = 2500) -> str:
        return json.dumps({
            "page_index": 1,
            "checks": [{"rule": r, "ok": True, "detail": "mock 模式未做视觉评审"}
                       for r in ("text_readable", "no_overlap", "hierarchy",
                                 "decoration_not_dominant", "vi_compliance",
                                 "layering", "safe_margin")],
            "overall": "pass", "issues": [], "rework_hints": [],
        }, ensure_ascii=False)

    def chat_vision_json(self, system: str, user: str, image_paths: list[str | Path],
                         max_tokens: int = 2500) -> dict:
        return json.loads(self.chat_vision(system, user, image_paths, max_tokens))
