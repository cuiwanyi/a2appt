"""骨架母版知识库读取层。

四个设计目标（对应用户的四条建议）：
  1. 提示词编排层先读目录、按页语义选母版 —— select_master / orchestration_hint
  2. 骨架母版确定性读取（按 ID，不靠记忆）—— get_master / get_template
  3. 返修问题回写母版而非只改成品 —— record_review
  4. 版本化：历史任务锁定版本可复现，一次改动不污染历史 —— pin / load_catalog(version=)

本模块只读知识库资产，不改核心生成逻辑；生成端按需调用。
知识库根目录走环境变量 A2A_KNOWLEDGE，默认 F:/a2appt-data/knowledge。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("F:/a2appt-data/knowledge")


def knowledge_root() -> Path:
    raw = os.getenv("A2A_KNOWLEDGE", "").strip()
    return Path(raw).resolve() if raw else DEFAULT_ROOT


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return version
    return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"


# ---------------------------------------------------------------- 版本化

def current_version() -> str:
    """当前目录版本。"""
    return str(_read_json(knowledge_root() / "catalog.json").get("catalog_version", ""))


def list_versions() -> list[str]:
    root = knowledge_root() / "versions"
    return sorted((p.stem for p in root.glob("*.json")),
                  key=lambda s: tuple(int(x) for x in s.split(".") if x.isdigit()))


def _snapshot(version: str) -> dict[str, Any] | None:
    p = knowledge_root() / "versions" / f"{version}.json"
    return _read_json(p) if p.is_file() else None


def pin(version: str | None = None) -> str:
    """把要消费的版本写入任务目录，历史任务据此复现。返回被锁定的版本。"""
    return version or current_version()


def load_catalog(version: str | None = None) -> dict[str, Any]:
    """加载目录。指定版本时从版本快照取完整内容，保证历史复现不被后续改动污染。"""
    root = knowledge_root()
    if version:
        snap = _snapshot(version)
        if not snap:
            raise FileNotFoundError(f"版本快照不存在: {version}")
        return deepcopy(snap["content"]["catalog"])
    return _read_json(root / "catalog.json")


def load_global_rules(version: str | None = None) -> dict[str, Any]:
    root = knowledge_root()
    if version:
        snap = _snapshot(version)
        if not snap:
            raise FileNotFoundError(f"版本快照不存在: {version}")
        return deepcopy(snap["content"]["global_rules"])
    return _read_json(root / "global_rules.json")


# ---------------------------------------------------------------- 确定性读取

def list_masters(*, status: str | None = None,
                 version: str | None = None) -> list[dict[str, Any]]:
    """列出母版精简清单。status='active' 只取可生成版式。"""
    masters = load_catalog(version)["masters"]
    if status:
        masters = [m for m in masters if m.get("status") == status]
    keys = ("id", "name", "scene", "signals", "layout_ref", "status", "version")
    return [{k: m.get(k) for k in keys} for m in masters]


def get_master(master_id: str, version: str | None = None) -> dict[str, Any]:
    """按 ID 确定性读取单个母版；找不到抛错，绝不靠语义猜测返回近似项。"""
    for m in load_catalog(version)["masters"]:
        if m["id"] == master_id:
            return deepcopy(m)
    raise KeyError(f"母版不存在: {master_id}（版本 {version or current_version()}）")


def get_template(master_id: str, version: str | None = None) -> Path:
    """返回母版骨架图绝对路径；图片缺失抛错。"""
    m = get_master(master_id, version)
    root = knowledge_root()
    path = root / m["template"]
    if not path.is_file():
        raise FileNotFoundError(f"母版骨架图缺失: {path}")
    return path


def resolve_layout(master_id: str, version: str | None = None) -> str | None:
    """母版映射到程序版式注册表；reference_only 母版返回 None，不可下发生成。"""
    m = get_master(master_id, version)
    if m.get("status") != "active":
        return None
    return m.get("layout_ref")


# ---------------------------------------------------------------- 提示词编排

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).lower()


def select_master(hint: str, *, active_only: bool = True,
                  version: str | None = None) -> dict[str, Any] | None:
    """按页语义提示选母版：优先 signal 命中，其次 scene 子串命中。

    这是给编排层用的确定性选择器；模型可直接输出 master_id，也可用本函数兜底。
    命中多个时取第一个（目录顺序即优先级），不靠模糊相似度乱选。
    """
    masters = load_catalog(version)["masters"]
    if active_only:
        masters = [m for m in masters if m.get("status") == "active"]
    h = _norm(hint)
    if not h:
        return None
    for m in masters:
        for sig in m.get("signals") or []:
            if _norm(sig) in h:
                return deepcopy(m)
    for m in masters:
        if _norm(m.get("scene", "")) in h:
            return deepcopy(m)
    return None


def select_master_for_single(content_hint: str, *,
                             version: str | None = None) -> dict[str, Any] | None:
    """单页专用选母版:只按内容语义,绝不用'第1页/最后一页'的位置默认。

    总页数=1 时不存在首页/末页概念。这里把内容语义分成四类:
      - 致谢/告别/问答 -> 20_qa
      - 目录            -> 02_contents
      - 明确声明封面    -> 01_cover
      - 其余内容        -> 走通用 select_master(内容母版)
    内容语义不足以判断时返回 None,由调用方向用户确认,不擅自当封面。
    """
    h = _norm(content_hint)
    if not h:
        return None
    # 目录优先于致谢:目录文案里可能出现"谢谢"等词,先判结构信号
    if any(_norm(s) in h for s in ("目录", "contents", "章节一览", "章节导览")):
        return get_master("02_contents", version)
    if any(_norm(s) in h for s in (
            "致谢", "感谢观看", "谢谢观看", "thanks", "明天见", "下次见",
            "再会", "敬请期待", "q&a", "qa", "问答")):
        return get_master("20_qa", version)
    if any(_norm(s) in h for s in ("这是封面", "封面页", "做一页封面", "扉页", "cover page")):
        return get_master("01_cover", version)
    # 其余按内容语义选,但排除封面(单页无声明不默认封面)
    m = select_master(content_hint, active_only=True, version=version)
    if m and m["id"] == "01_cover":
        return None  # 语义不足,不擅自当封面
    return m


def orchestration_hint(version: str | None = None, *,
                       active_only: bool = True) -> str:
    """生成给提示词编排层的一段文本：有哪些母版可选、各适合什么页。

    编排层据此为每个内容页指定 master_id 与 version，而不是自己记母版细节。
    """
    masters = list_masters(status="active" if active_only else None, version=version)
    lines = [
        "可用骨架母版（先读本清单，再为每页指定 master_id 与 version）：",
        "规则：按每页语义选，不按页码固定；layout_ref 为空的母版不可用于生成。",
    ]
    for m in masters:
        layout = m.get("layout_ref") or "无(仅参考)"
        sc = m["scene"]
        lines.append(f"- {m['id']} {m['name']}｜适合:{sc}｜版式:{layout}")

    # 只把仅参考母版列为提示，避免被误选
    refs = list_masters(status="reference_only", version=version)
    if refs:
        lines.append("仅作构图参考（程序无对应版式，不要选为生成母版）："
                     + "、".join(f"{r['id']}{r['name']}" for r in refs))
    return "\n".join(lines)


# ---------------------------------------------------------------- 返修回写

def record_review(master_id: str, issue: str, *,
                  source: str = "", version: str | None = None) -> str:
    """把本轮返修经验回写到母版：追加到 review/<id>.jsonl 与目录 review_notes，
    并 bump 修订号、生成新版本快照。返回新版本号。

    回写不改变母版集合与骨架结构，只累积经验，让下一次同类页少犯同样的错。
    """
    root = knowledge_root()
    master = get_master(master_id, version)
    new_version = _bump_patch(current_version())

    note = {
        "date": date.today().isoformat(),
        "master_id": master_id,
        "issue": issue,
        "source": source,
        "from_version": version or current_version(),
        "to_version": new_version,
    }
    review_dir = root / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    with (review_dir / f"{master_id}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(note, ensure_ascii=False) + "\n")

    entry = {"date": note["date"], "issue": issue, "source": source}
    cat = load_catalog()
    for m in cat["masters"]:
        if m["id"] == master_id:
            m.setdefault("review_notes", []).append(entry)
    cat["catalog_version"] = new_version
    cat["updated"] = note["date"]
    (root / "catalog.json").write_text(
        json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")

    # 同步派生单母版配置
    single = deepcopy(master)
    single["version"] = new_version
    single.setdefault("review_notes", []).append(entry)
    (root / "masters" / f"{master_id}.json").write_text(
        json.dumps(single, ensure_ascii=False, indent=2), encoding="utf-8")

    # 新版本快照：内嵌完整内容，旧快照保留不动
    master_content = {}
    for f in sorted((root / "masters").glob("*.json")):
        master_content[f.stem] = _read_json(f)
    import hashlib
    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    snap = {
        "version": new_version,
        "frozen_at": note["date"],
        "catalog_sha256_16": sha(root / "catalog.json"),
        "global_rules_sha256_16": sha(root / "global_rules.json"),
        "master_ids": [m["id"] for m in cat["masters"]],
        "template_sha256_16": {t.name: sha(t) for t in sorted((root / "templates").glob("*.png"))},
        "content": {
            "catalog": cat,
            "global_rules": load_global_rules(),
            "masters": master_content,
        },
    }
    (root / "versions" / f"{new_version}.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return new_version


if __name__ == "__main__":
    root = knowledge_root()
    print("知识库根:", root)
    print("当前版本:", current_version(), "| 版本快照:", list_versions())
    print(orchestration_hint())
