"""内容入口：把你写的自然语言需求变成 PPT。

用法：
    python make_ppt.py                          # 读项目根目录 my_request.txt
    python make_ppt.py --text "帮我做一页咖啡品牌的宣传PPT，..."  # 直接给一句话
    python make_ppt.py --file 我的需求.txt       # 指定需求文件
    python make_ppt.py --brief examples/brief_centered.json  # 已经是结构化 brief，跳过整理
    python make_ppt.py --mock                   # 离线快速试跑（不调 API）

流程始终全自动：需求整理 -> 版式规划 -> 素材生成 -> 构建 PPTX -> 真实截图自检 -> 返修。
本脚本只负责「把你的话整理成 brief」，后面的流水线由 Orchestrator 负责，不需要你选步骤。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import config
from a2a.orchestrator import Orchestrator
from services.llm import LLMClient, MockLLM

ROOT = Path(__file__).resolve().parent
DEFAULT_REQUEST = ROOT / "my_request.txt"

BRIEF_SYSTEM = """你是广告需求整理助手。把用户用自然语言描述的 PPT 需求，整理成一份结构化 brief JSON，交给下游版式规划 Agent 使用。

只输出一个 JSON 对象，不要解释文字、不要 Markdown 围栏。

字段：
{
  "brand": "品牌或项目名",
  "industry": "行业",
  "vi": {"primary": "#RRGGBB", "secondary": "#RRGGBB", "accent": "#RRGGBB"},
  "creative": "这一页的版式方向，1~2 句中文，写清构图：文字放哪、图片放哪、要不要形状裁图、要不要装饰",
  "copy": {"title": "主标题", "subtitle": "副标题", "body": "正文", "cta": "行动号召或落款"},
  "tone": "调性",
  "pages": 1,
  "page_specs": [
    {"name": "封面", "creative": "这一页的构图方向", "copy": {"title": "…", "subtitle": "…", "body": "…", "cta": "…"}, "style": "视觉关键词"}
  ]
}

规则：
1. 用户明确给出的品牌名、文案、色值、版式要求必须原样保留，不要改写、不要替用户编造新卖点。
2. 用户没提的字段才补合理默认值；补的内容要贴合主题，禁止出现「XX品牌」「请输入」这类占位符。
3. vi 必须是合法 #RRGGBB。用户没给色值时，按行业和调性选一组克制且对比清晰的配色。
4. creative 是关键：忠实转写用户想要的构图。**不要默认使用柱状蒙版 / 多柱裁切**，也不要预设任何固定构图；纯文字、居中海报、全幅背景、圆形裁图、多图网格、斜切分栏都同样是合法选择。用户没说版式时，按调性自选一个最稳妥的。
5. 文案简洁：标题不超过 16 字，正文不超过 60 字。所有文字都会放进 PPT 原生文本框，不要要求把文字画进图片。
6. pages 默认 1，除非用户明确要几页，最多 8 页。
7. **多页（pages>1）时必须输出 page_specs 数组**：长度严格等于 pages，按页顺序排列。每页对象含 name（页名）、creative（该页具体构图，写清文字/图片/形状怎么放、要不要形状裁图）、copy（该页独立的 title/subtitle/body/cta）、style（视觉关键词）。用户逐页写的要求必须原样落进对应页的 creative 和 copy；用户没逐页写时按内容安排。同类门店、产品、季度或连续对比页优先复用同一骨架与字段位置，不为求变化强制换版；若用户要求逐页变化，再按要求设计。标题默认使用可编辑原生文字；不要替用户增加艺术字要求。
8. 单页（pages=1）时**不要**输出 page_specs 字段。
9. **画册级结构硬规则（pages>=4 时必须遵守）**：
   - 第 1 页封面：用整幅摄影背景图 + 渐变遮罩洗白标题侧（caption 里写明「摄影背景+渐变遮罩，标题在左/右侧留白」），不要用纯色大色块；
   - 不因页数或页码自动添加目录，不把已有正文顺延或替换成目录。用户要求导航，或未给逐页提纲且内容确有章节导航需要时，才安排目录。若要求无目录则不安排。有目录时用 toc，条目只对应目录之后的真实章节；指定位置时按用户位置。
   - 凡是讲【发展历程/成长阶段/roadmap/年份】的页：creative 写明「时间轴 timeline，每个节点要写清这一阶段做了什么、有什么结果」，copy.body 逐条给出「年份：阶段名——具体做了什么、结果/数据」；
   - 结尾页若用横向图片条带，写明「条带图高度≥2英寸，不要压成细条」，否则改用整页摄影背景+渐变遮罩；
   - 文字压在摄影图上的页都要写明「渐变遮罩洗白文字侧、图片焦点在另一侧保持清晰」。
"""

DEFAULT_VI = {"primary": "#1E2A32", "secondary": "#D8B98C", "accent": "#F2F0EA"}
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def read_text(path: Path) -> str:
    """兼容记事本可能另存的 UTF-8 BOM / GBK。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore").strip()


def normalize_brief(raw: dict, source_text: str = "") -> dict:
    """LLM 输出兜底：保证下游 planner 拿到的字段一定齐全且类型正确。"""
    vi = raw.get("vi") if isinstance(raw.get("vi"), dict) else {}
    vi = {
        k: (v if isinstance(v, str) and HEX_RE.match(v) else DEFAULT_VI[k])
        for k, v in ((k, vi.get(k)) for k in DEFAULT_VI)
    }
    copy = raw.get("copy") if isinstance(raw.get("copy"), dict) else {}
    lines = [ln.strip() for ln in source_text.splitlines() if ln.strip()]

    def s(key: str, fallback: str) -> str:
        v = raw.get(key)
        return str(v).strip() if isinstance(v, str) and v.strip() else fallback

    def cs(key: str, fallback: str) -> str:
        v = copy.get(key)
        return str(v).strip() if isinstance(v, str) and v.strip() else fallback

    title = cs("title", (lines[0][:16] if lines else "主题呈现"))
    body = cs("body", " ".join(lines[1:])[:60] if len(lines) > 1 else "以克制的设计语言，呈现内容本身。")
    pages = raw.get("pages", 1)
    try:
        pages = max(1, min(8, int(pages)))
    except (TypeError, ValueError):
        pages = 1

    # 各页拥有独立的文案；同类页可以共享骨架。
    specs: list[dict] = []
    raw_specs = raw.get("page_specs")
    if isinstance(raw_specs, list):
        for i, sp in enumerate(raw_specs[:8]):
            if not isinstance(sp, dict):
                continue
            sc = sp.get("copy") if isinstance(sp.get("copy"), dict) else {}
            specs.append({
                "name": str(sp.get("name") or f"第 {i + 1} 页").strip(),
                "creative": str(sp.get("creative") or "").strip(),
                "style": str(sp.get("style") or "").strip(),
                **{k: sp[k] for k in ("layout", "exclude", "toc_required", "layout_recipe") if k in sp},
                "copy": {k: str(sc.get(k) or "").strip()
                         for k in ("title", "subtitle", "body", "cta")},
            })
    # 补齐缺页不引入新的页面角色，也不为凑页数轮换骨架。
    if pages > 1 and len(specs) < pages:
        for i in range(len(specs), pages):
            specs.append({
                "name": f"第 {i + 1} 页",
                "creative": "遵照全册设计要求，按本页内容安排构图，同类内容可复用骨架。",
                "style": "沿用全局调性，便于跨页阅读与比较",
                "copy": {
                    "title": lines[i][:16] if i < len(lines) else title,
                    "subtitle": "",
                    "body": body,
                    "cta": "",
                },
            })
    if specs:
        pages = len(specs)

    # copy 除了四个文本字段，还可能是结构化数据（指标、图表等）。
    # 这些字段要原样保留，否则规划器拿不到数值，页面只剩指标名、数值全丢。
    copy_out: dict[str, Any] = {
        "title": title,
        "subtitle": cs("subtitle", ""),
        "body": body,
        "cta": cs("cta", ""),
    }
    for key, value in copy.items():
        if key not in copy_out:
            copy_out[key] = value

    brief = {
        "brand": s("brand", "未命名品牌"),
        "industry": s("industry", "品牌传播"),
        "vi": vi,
        "creative": s("creative", "居中海报：标题居中，上下细线装饰，配一张与主题相关的独立图片；不要求形状裁图。"),
        "copy": copy_out,
        "tone": s("tone", "简约 / 克制"),
        "pages": pages,
    }
    if specs:
        brief["page_specs"] = specs
    # 保留用户的目录约束供审核；正文/标题里的“目录”不是授权。
    from ppt.toc_policy import toc_intent
    if source_text and not source_text.lstrip().startswith("{"):
        intent = toc_intent({"prompt": source_text})
    else:
        intent = toc_intent(raw)
    if intent is not None:
        brief["toc_required"] = intent
    # 用户直接给了完整 brief 时，保留他想强制指定的版式配方
    if isinstance(raw.get("layout_recipe"), str) and raw["layout_recipe"].strip():
        brief["layout_recipe"] = raw["layout_recipe"].strip()
    return brief


def rule_based_brief(text: str) -> dict:
    """离线 mock：不调 LLM，也能把一句话变成可用 brief。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return normalize_brief(
        {
            "brand": "我的品牌",
            "industry": "品牌传播",
            "vi": dict(DEFAULT_VI),
            "creative": "居中海报：标题居中、上下细线装饰，底部一条横向独立图片带。",
            "copy": {
                "title": lines[0][:16] if lines else "主题呈现",
                "subtitle": lines[1][:24] if len(lines) > 1 else "",
                "body": " ".join(lines[2:])[:60] if len(lines) > 2 else "以克制的设计语言，呈现内容本身。",
                "cta": "了解更多",
            },
            "tone": "简约 / 克制",
            "pages": 1,
            "layout_recipe": "centered",
        },
        text,
    )


def make_brief(text: str, client: LLMClient | None) -> tuple[dict, bool]:
    """返回 (brief, 是否由 LLM 整理)。"""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            raw = json.loads(stripped)
            if isinstance(raw, dict) and ("copy" in raw or "brand" in raw):
                return normalize_brief(raw, stripped), False
        except json.JSONDecodeError:
            pass
    if client is None:
        return rule_based_brief(text), False
    try:
        raw = client.chat_json(BRIEF_SYSTEM, text)
        return normalize_brief(raw, text), True
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 需求整理调用失败（{exc}），改用本地规则解析，仍会继续生成。", file=sys.stderr)
        return rule_based_brief(text), False


def show_brief(brief: dict, by_llm: bool) -> None:
    print("\n===== 我理解到的需求 =====")
    print("整理方式:", "LLM 智能整理" if by_llm else "本地规则解析 / 直接读取 JSON")
    print("品牌:", brief["brand"], "| 行业:", brief["industry"], "| 调性:", brief["tone"])
    print("配色:", " / ".join(brief["vi"].values()))
    print("版式:", brief["creative"])
    for k, label in (("title", "主标题"), ("subtitle", "副标题"), ("body", "正文"), ("cta", "落款")):
        v = brief["copy"].get(k, "")
        if v:
            print(f"{label}: {v}")
    print("页数:", brief["pages"])
    if brief.get("page_specs"):
        print("逐页:", " | ".join(f"{i + 1}.{s['name']}" for i, s in enumerate(brief["page_specs"])))


def main() -> int:
    ap = argparse.ArgumentParser(description="按你的内容生成可编辑 PPT（整条 A2A 流水线自动跑完）")
    ap.add_argument("--text", type=str, default="", help="直接写一句需求")
    ap.add_argument("--file", type=str, default="", help="需求文本文件路径，默认 my_request.txt")
    ap.add_argument("--brief", type=str, default="", help="已有结构化 brief JSON，跳过需求整理")
    ap.add_argument("--mock", action="store_true", help="离线模式，不调用 LLM / 生图")
    ap.add_argument("--transport", choices=["inproc", "http"], default="inproc")
    ap.add_argument("--max-rework", type=int, default=None)
    ap.add_argument('--name', help='任务文件夹名称；默认用画册标题，同名自动分版')
    args = ap.parse_args()

    mock = args.mock or config.MOCK
    client = None if mock else LLMClient()

    if args.brief:
        p = Path(args.brief)
        if not p.exists():
            print(f"brief 不存在: {p}")
            return 2
        request_text = read_text(p)
        brief = normalize_brief(json.loads(request_text), "")
        by_llm = False
    else:
        if args.text:
            request_text = args.text.strip()
        else:
            p = Path(args.file) if args.file else DEFAULT_REQUEST
            if not p.exists():
                print(f"没找到需求文件: {p}")
                print("请编辑 my_request.txt 写清楚你要什么，或用 --text \"你的需求\"。")
                return 2
            request_text = read_text(p)
        # 忽略 my_request.txt 里以 # 开头的说明行
        request_text = "\n".join(
            ln for ln in request_text.splitlines() if not ln.lstrip().startswith("#")
        ).strip()
        if not request_text:
            print("需求是空的。请在 my_request.txt 里写清楚你要的 PPT 内容。")
            return 2
        brief, by_llm = make_brief(request_text, client)

    show_brief(brief, by_llm)
    llm = client or MockLLM(brief)
    orch = Orchestrator(brief, llm=llm, mock=mock, max_rework=args.max_rework,
                        transport=args.transport, name=args.name, user_request=request_text)
    (orch.root / "00_request.txt").write_text(request_text, encoding="utf-8")
    print(f'微调后重跑：--brief "{orch.root / "brief.json"}"')
    print(f"[A2A] run_id={orch.run_id}  mode={'mock' if mock else 'live'}  root={orch.root}")
    print("[A2A] 自动执行：需求整理 -> 版式规划 -> 素材生成 -> 构建PPTX -> 真实截图自检 -> 返修\n")
    summary = orch.run()

    print("\n===== 生成结果 =====")
    print("状态:", "PASS" if summary["ok"] else "FAIL", "| 构建次数:", summary["attempts"],
          "| 已执行返修:", summary["rework_count"])
    print("★ 交付 PPTX:", summary["deliverable"])
    if not summary['ok'] and summary.get('candidate'):
        print('  未通过的候选稿:', summary['candidate'])
    if summary.get('stop_reason'):
        print('  提前停止:', summary['stop_reason']['detail'])
    shot_dir = Path(summary["root"]) / "04_screenshots"
    shots = sorted(shot_dir.glob("slide_*_real.png"))
    print(f"  真实截图: {len(shots)} 张 -> {shot_dir}")
    print("  版式 JSON:", summary["layout"])
    print("  自检报告:", summary["review"])
    print("  需求存档:", orch.root / "00_request.txt")
    print("自检摘要:", json.dumps(summary["review_summary"], ensure_ascii=False))
    if summary.get('repair_ledger'):
        print('  全册返修清单:', summary['repair_ledger'])
    for row in summary.get('visual_review_rounds', []):
        print(f"  第{row['attempt']}轮视觉评审：新审 {row['reviewed_pages']} 页，复用 {row['reused_pages']} 页，模型调用 {row['model_calls']} 次，缺证 {row['unverified_pages']} 页")
    if summary["blocking_issues"]:
        print("阻塞问题:")
        for i in summary["blocking_issues"]:
            print("  -", i)
    if summary["error"]:
        print("错误:", summary["error"])
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
