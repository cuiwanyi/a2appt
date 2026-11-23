"""A2A 编排 CLI。

    F:\\python_env\\a2a_env\\Scripts\\python.exe run.py --brief examples/brief_centered.json
    F:\\python_env\\a2a_env\\Scripts\\python.exe run.py --mock --recipe grid
    F:\\python_env\\a2a_env\\Scripts\\python.exe run.py --check-models

版式由 brief 决定，流水线不预设任何构图；--recipe 只是给离线 mock 指定配方用的快捷开关。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import config
from a2a.orchestrator import Orchestrator
from services.llm import LLMClient, MockLLM

DEFAULT_BRIEF = {
    "brand": "序 SEQUENCE",
    "industry": "生活方式 / 家居",
    "vi": {"primary": "#1E2A32", "secondary": "#D8B98C", "accent": "#F2F0EA"},
    "creative": "居中海报：大标题居中、上下细线装饰，底部一条横向独立图片带；不要求柱状蒙版。",
    "copy": {
        "title": "留白之间 自有秩序",
        "subtitle": "2026 春夏系列 · 空间与器物",
        "body": "把复杂留给工艺，把简单留给日常。每一处比例都经过反复推敲，让物件回到它该在的位置。",
        "cta": "探索系列  ·  预约到店",
    },
    "tone": "轻奢 / 克制",
    "pages": 1,
}


def check_models() -> None:
    import requests

    client = LLMClient()
    r = client.session.get(f"{client.base_url}/v1/models", timeout=30)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    print(f"端点: {client.base_url}  共 {len(ids)} 个模型")
    for i in ids:
        mark = "  <== 当前使用" if i in {client.llm_model, client.image_model} else ""
        print("  " + i + mark)
    missing = [m for m in (client.llm_model, client.image_model) if m not in ids]
    print("配置中的模型:", client.llm_model, "/", client.image_model)
    print("校验:", "全部存在" if not missing else f"不存在 {missing}")


def main() -> int:
    ap = argparse.ArgumentParser(description="A2A 编排：广告级可编辑 PPTX 生成流水线")
    ap.add_argument("--brief", type=str, default="", help="品牌需求 JSON 路径")
    ap.add_argument("--mock", action="store_true", help="离线模式：不调用 LLM / 生图")
    ap.add_argument("--max-rework", type=int, default=None, help="最大返修轮数")
    ap.add_argument("--transport", choices=["inproc", "http"], default="inproc",
                    help="inproc=进程内 A2A 总线；http=真实 HTTP AgentCard 发现 + JSON-RPC 委派")
    ap.add_argument("--recipe", type=str, default="",
                    help="离线 mock 的版式配方：centered/pure_type/full_bleed/single_oval/grid/columns/rows/diagonal")
    ap.add_argument("--check-models", action="store_true", help="只核对端点模型 ID 后退出")
    args = ap.parse_args()

    if args.check_models:
        check_models()
        return 0

    brief = DEFAULT_BRIEF
    if args.brief:
        p = Path(args.brief)
        if not p.exists():
            print(f"brief 不存在: {p}")
            return 2
        brief = json.loads(p.read_text(encoding="utf-8"))
    if args.recipe:
        brief = {**brief, "layout_recipe": args.recipe}

    mock = args.mock or config.MOCK
    llm = MockLLM(brief) if mock else LLMClient()
    orch = Orchestrator(brief, llm=llm, mock=mock, max_rework=args.max_rework,
                        transport=args.transport)
    print(f"[A2A] run_id={orch.run_id}  mode={'mock' if mock else 'live'}  "
          f"transport={args.transport}  root={orch.root}")
    if orch.endpoints:
        print("      HTTP A2A endpoints:")
        for url, name in orch.endpoints.items():
            print(f"        {name:10s} {url}/.well-known/agent-card.json")
    summary = orch.run()

    print("\n===== 编排结果 =====")
    print("状态:", "PASS" if summary["ok"] else "FAIL", "| 返修轮次:", summary["attempts"])
    print("交付 PPTX:", summary["deliverable"])
    print("版式 JSON:", summary["layout"])
    print("素材目录:", summary["assets_dir"])
    print("自检报告:", summary["review"])
    if summary.get("layout_preview"):
        print("版式预览(近似):", summary["layout_preview"])
    print("自检摘要:", json.dumps(summary["review_summary"], ensure_ascii=False))
    print("可编辑底线:", json.dumps(summary["editable_baseline"], ensure_ascii=False))
    if summary["blocking_issues"]:
        print("阻塞问题:")
        for i in summary["blocking_issues"]:
            print("  -", i)
    if summary["warnings"]:
        print("警告数:", len(summary["warnings"]), "（详见 04_review.json）")
    if summary["error"]:
        print("错误:", summary["error"], "| 堆栈见", Path(summary["root"]) / "error.txt")
    print("A2A 事件数:", summary["task_events"], "| trace:", Path(summary["root"]) / "trace.jsonl")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
