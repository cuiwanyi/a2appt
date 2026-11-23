"""画册级逐页制作：【统一引擎 + 内容信号路由】。

架构（用户要求）：所有页面走完全相同的代码；版式由【这一页的内容】决定，
不是由页码决定，也没有 `if page_no == 2` 这种按页特判。

    PAGE_CONTENT  = 纯内容数据（提示词、文案、章节/分点/里程碑等结构化内容）
    route_layout  = 内容信号 -> 版式名（pct/layout_router.py）
    build_registry= 版式名 -> 版式实现（pct/page_layouts.py）
    build_page    = 唯一引擎：生图 -> 焦点自适应遮罩 -> 查表取版式 -> 组装 -> 渲染

换内容就换版式：
    python make_page.py --route              # 打印所有页的内容->版式路由
    python make_page.py --probe "这页分四点讲，每点配图"  # 任意一段话试路由
    python make_page.py --page 2 --layout points_with_images   # 显式覆盖
    python make_page.py --page 2 --exclude 目录                # 排除某版式

每页硬规范（引擎统一保证，与版式无关）：
  1. 整页摄影图，16:9 等比裁切，绝不拉伸压缩
  2. 焦点自适应渐变遮罩：真的看图 -> 定焦点侧/边界/采样色 -> 焦点区 alpha=0
  3. 内容落在洗白侧（渐变到纯色），图片焦点保持清晰
  4. 除封面艺术字图外，全部原生可编辑
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from services.llm import LLMClient
from services.cover_title_art import matte_and_recolor, title_prompt
from ppt.focus_scrim import prepare_background, make_adaptive_scrim
from ppt.content_zone import content_zone, layout_width, fit_to_zone
from ppt.layout_router import route_layout, build_registry
from ppt.slicing import prepare_mask_slices
from ppt.schema import normalize_layout
from ppt.builder import PPTBuilder
from ppt.tools import PPTToolkit
from ppt.inspector import inspect
from ppt.render import try_render

from run_paths import RUNS

WORK = RUNS / "_coffee_v2"

VI = {"primary": "#3B2A21", "secondary": "#B07D4B", "accent": "#EDE3D3"}
INK = "#3B2A21"
INK_SOFT = "#6B5744"
PALETTE = {"primary": VI["primary"], "secondary": "#A8763E",
           "soft": INK_SOFT, "faint": "#C9B49A"}

# ---------------------------------------------------------------- 背景图提示词
OPENING_POSITIONS = ("left side", "right side", "upper left corner", "upper right corner",
                     "lower left corner", "lower right corner")
OPENING_BG = (
    "One continuous 16:9 editorial photograph for a specialty coffee brochure. "
    "Scene: {scene}. Cluster the entire subject in the {position}, within the outer third "
    "and inset from the edges for cropping. Use deep espresso-brown low-key lighting "
    "on the subject, soft window rim light, shallow depth of field and subtle film grain. "
    "Keep the entire {wash} half empty: a bright warm ivory wall, low detail, smooth "
    "tonal transition. STRICTLY NO text, letters, numbers, labels, menu boards, logos, "
    "watermarks, UI, people, hands, borders, collage or split panels."
)

# 没写 bg_prompt 的页，用这套统一模板 + 该页的 visual 主体描述
BG_TEMPLATE = (
    "A high-end editorial photograph for a specialty coffee brand brochure, 16:9 wide "
    "horizontal composition, MOODY LOW-KEY DARK BROWN on the right fading to warm ivory "
    "cream on the left. On the RIGHT THIRD: {subject}, deep espresso-brown and umber tones, "
    "soft warm window light, the subject clearly visible in dark brown low-key lighting. "
    "The LEFT TWO THIRDS must be a BRIGHT, CLEAN, near-solid warm ivory / cream wall with "
    "deliberately empty negative space for typography, almost no texture or objects. "
    "One smooth tonal gradient from light cream on the LEFT to deep dark brown on the RIGHT, "
    "with the real photographic subject living inside the dark brown right side. "
    "Shallow depth of field, cinematic, quiet contemplative mood, natural film grain. "
    "STRICTLY NO text, NO letters, NO logo, NO watermark, NO people, NO hands, "
    "NO border, NO collage, NO split panels, NO pure flat color blocks. "
    "One single continuous photograph."
)

# 四类焦点构图（用户要求：给模型「有焦点」的语言，焦点可以是一边 / 一角 /
# 半包围 / 中心，由该页主题选一类；绝不写 left/right/top/bottom 具体方位）。
# 具体落在哪一边、哪一个角由模型按主题自行决定，生成后再真看图定焦点。
FOCUS_STYLES = {
    "side": "(A) OFF TO ONE SIDE — keep the subject clearly toward ONE side of the frame, "
            "with the opposite side open and calm",
    "corner": "(B) IN ONE CORNER — cluster the subject into a SINGLE corner of the frame "
              "and leave the remaining three quarters quiet and open",
    "half": "(C) HALF-EMBRACED — let an L-shaped / diagonal / crescent band of open "
            "negative space partly wrap around the subject",
    "center": "(D) CENTERED — place the subject near the MIDDLE of the frame with "
              "generous calm open space all around it",
}
DEFAULT_FOCUS_STYLE = FOCUS_STYLES["side"]

# 自由构图层：写清主题主体 + 焦点构图类型，但不指定任何具体方位。
BG_FREE_TEMPLATE = (
    "A high-end editorial photograph for a specialty coffee brand brochure, "
    "16:9 wide horizontal composition. Subject and story of this page: {subject}. "
    "FOCAL POINT: the frame MUST contain ONE clear, sharp visual subject. The relationship "
    "between that subject and the quiet negative space must follow ONE of these four "
    "editorial compositions: (A) subject off to one side; (B) subject clustered in one "
    "corner; (C) an L-shaped / diagonal band of open space half-embracing the subject; "
    "(D) subject centered with open space all around. "
    "FOR THIS PAGE USE THIS ONE: {focus_style}. "
    "Commit to that arrangement deliberately — do NOT default to putting the subject on "
    "the right, and do not spread objects evenly across the frame. "
    "IMPORTANT FOR EDITORIAL LAYOUT: reserve a GENEROUS area (roughly 40-50 percent of "
    "the frame) of soft low-detail negative space — a plain plaster wall, defocused "
    "background, open sky, gentle bokeh or soft shadow — so typography has a clean place "
    "to live. Let this quiet area fall wherever it naturally occurs; the subject must NOT "
    "fill the entire frame edge to edge, and the subject itself stays sharp and unobscured. "
    "Use warm ivory cream and deep espresso "
    "brown tones, soft natural window light, gentle shadows, shallow depth of field, "
    "cinematic quiet contemplative mood, premium commercial photography, subtle film grain. "
    "STRICTLY NO text, NO letters, NO logo, NO watermark, NO UI, NO hands, "
    "NO border, NO collage, NO split panels, NO pure flat color blocks. "
    "One single continuous photograph."
)

CREATIVE_TOC = {
    "title": "目录",
    "eyebrow": "CONTENTS  ·  SANYU COFFEE  ·  2026",
    "manifesto": ["一杯，慢下来的时间。", "山屿咖啡 · 产品宣传画册"],
    "cols": 2,
    "items": [
        {"num": "01", "label": "品牌缘起",
         "desc": "从城南巷 17 号的一间小馆开始，\n2016 年至今只做一件事：把咖啡煮慢。"},
        {"num": "02", "label": "杯子里的内容",
         "desc": "四款招牌，从花香浅焙到深烘可可，\n每一支豆都标明产区、海拔与处理法。"},
        {"num": "03", "label": "一杯的路径",
         "desc": "十年间从街角小店走到产区直采，\n把供应链缩短成一张可追溯的地图。"},
        {"num": "04", "label": "从豆到杯",
         "desc": "生豆筛选、小批量烘焙、养豆与研磨，\n每一批都留有曲线与杯测记录。"},
        {"num": "05", "label": "坐下来的理由",
         "desc": "十八个座位、一面临窗长桌，\n光线随时间在木桌上缓慢移动。"},
        {"num": "06", "label": "来坐坐",
         "desc": "门店地址、营业时间，\n以及每月一支豆的订阅计划。"},
        {"num": "07", "label": "山屿的人",
         "desc": "吧台后的烘焙师与咖啡师，\n以及他们各自偏爱的那一支豆。"},
        {"num": "08", "label": "明天见",
         "desc": "留一封给下一次到访的短笺，\n和一只还没盛满的杯子。"},
    ],
    "groups": ["品牌与产品  01 — 04", "空间与人  05 — 08"],
}

# ---------------------------------------------------------------- 纯内容数据
# 注意：这里【没有 mode / 版式字段】。版式全部由内容信号自动推断。
# 每一条都是「这一页讲了什么」，不是「这一页长什么样」。
PAGE_CONTENT = {
    1: {
        "name": "封面",
        "opening_scene": "a single ceramic pour-over dripper with coffee grounds and a thin "
                         "wisp of steam on dark walnut, no extra cups",
        "copy": {"title": "山屿咖啡"},
        "art_color": INK,
        "elements": [
            {"type": "text", "content": "SANYU COFFEE  ·  SINCE 2016",
             "position": {"x": 0.85, "y": 0.72, "w": 6.5, "h": 0.32},
             "font": {"family": "微软雅黑", "size": 11, "color": INK_SOFT, "weight": "bold"},
             "align": "left", "layer": "text"},
            {"type": "shape", "shape": "rect", "fill": VI["secondary"],
             "position": {"x": 0.85, "y": 1.22, "w": 5.9, "h": 0.018},
             "layer": "decoration"},
            {"type": "text", "content": "SANYU COFFEE  ·  一 杯 一 会",
             "position": {"x": 0.85, "y": 3.98, "w": 6.4, "h": 0.32},
             "font": {"family": "微软雅黑", "size": 11.5, "color": INK_SOFT, "weight": "normal"},
             "align": "left", "layer": "text"},
            {"type": "text", "content": "一杯，慢下来的时间。",
             "position": {"x": 0.85, "y": 4.42, "w": 6.4, "h": 0.55},
             "font": {"family": "宋体", "size": 23, "color": INK, "weight": "bold"},
             "align": "left", "layer": "text"},
            {"type": "text",
             "content": "从产区的一支豆子，到手里这一杯。\n烘焙、研磨与等待，我们都留给时间。",
             "position": {"x": 0.85, "y": 5.12, "w": 6.5, "h": 0.9},
             "font": {"family": "微软雅黑", "size": 13, "color": INK_SOFT, "weight": "normal"},
             "align": "left", "layer": "text"},
            {"type": "shape", "shape": "rect", "fill": VI["secondary"],
             "position": {"x": 0.85, "y": 6.18, "w": 5.9, "h": 0.018},
             "layer": "decoration"},
            {"type": "text", "content": "城南巷 17 号  ·  每日 09:00–19:00",
             "position": {"x": 0.85, "y": 6.36, "w": 6.4, "h": 0.34},
             "font": {"family": "微软雅黑", "size": 11.5, "color": INK_SOFT, "weight": "normal"},
             "align": "left", "layer": "text"},
            {"type": "text", "content": "01  /  08",
             "position": {"x": 0.85, "y": 6.86, "w": 2.0, "h": 0.3},
             "font": {"family": "微软雅黑", "size": 10, "color": INK_SOFT, "weight": "normal"},
             "align": "left", "layer": "text"},
        ],
    },
    2: {
        "name": "目录",
        "prompt": "目录 CONTENTS 章节导览，列出全册八个章节与一句话说明",
        "opening_scene": "a quiet coffee bar shelf with a few ceramic cups and an unlabelled "
                         "glass jar of roasted coffee beans",
        "creative_toc": CREATIVE_TOC,
    },
    3: {
        "name": "杯子里的内容",
        "prompt": "四款招牌分几点来讲，每一点都要配一张独立插图",
        "opening_scene": "four distinct signature coffee cups grouped together on a walnut "
                         "counter, no tasting cards or packaging",
        "copy": {"title": "杯子里的内容", "subtitle": "FOUR SIGNATURE CUPS  ·  SEASON 2026"},
        "points": [
            {"label": "晨光 · 耶加雪菲", "desc": "水洗处理，茉莉与柑橘，浅焙保留花香与干净尾韵。",
             "image_prompt": "A bright airy close-up of a LIGHT ROAST pour-over in a clear glass "
                             "server, pale amber liquid, with fresh white jasmine blossoms and a "
                             "few slices of bergamot orange on a white linen cloth, cool morning "
                             "window light, high-key ivory and soft gold palette, clean minimal "
                             "composition, no text, no letters, no watermark, one photograph."},
            {"label": "山雾 · 哥伦比亚", "desc": "厌氧日晒，熟果与红糖，中焙带出圆润甜感。",
             "image_prompt": "A moody misty Andean coffee farm scene with ripe red cherries and "
                             "dark brown cane sugar cubes beside a rustic ceramic cup of medium "
                             "roast coffee, soft fog drifting between green coffee branches, "
                             "warm terracotta and deep red-brown tones, no text, no letters, "
                             "no watermark, one photograph."},
            {"label": "晚风 · 曼特宁", "desc": "湿刨法，草本与黑巧，深焙厚实低酸。",
             "image_prompt": "A dark low-key still life of a heavy black stoneware cup filled with "
                             "very dark roast coffee, broken dark chocolate pieces and a sprig of "
                             "dried herbs on a nearly black walnut table, single warm side light "
                             "from the right, deep espresso brown and charcoal palette, dramatic "
                             "quiet evening mood, no text, no letters, no watermark, one photograph."},
            {"label": "常日 · 拼配", "desc": "三支豆按季节调整比例，做奶咖也立得住。",
             "image_prompt": "A cheerful everyday flat white with simple rosetta latte art in a "
                             "plain white ceramic cup on a light oak table, three small saucers "
                             "of different green coffee beans neatly arranged behind it, bright "
                             "soft daylight, cream and warm beige palette, calm daily ritual mood, "
                             "no text, no letters, no watermark, one photograph."},
        ],
    },
    4: {
        "name": "一杯的路径",
        "prompt": "品牌十年发展历程时间轴 2016—2026，每个阶段都要写清做了什么、结果如何",
        "visual": "a roasting workshop with a small drum roaster and burlap sacks",
        "free_composition": True,
        "focus_composition": "corner",
        "copy": {"title": "一杯的路径", "subtitle": "TEN YEARS  ·  2016 — 2026"},
        "milestones": [
            {"num": "2016", "label": "街角小店", "desc": "城南巷 17 号开出 18 个座位的第一家店，手冲只用一支豆"},
            {"num": "2018", "label": "自烘焙", "desc": "后场添置 3kg 烘豆机，开始自行控制烘焙曲线与养豆周期"},
            {"num": "2020", "label": "产区直采", "desc": "与埃塞、哥伦比亚三个合作社建立直采，采购链路可追溯"},
            {"num": "2022", "label": "中央工坊", "desc": "建成中央烘焙工坊，日产能提升到 200 公斤并开放杯测课"},
            {"num": "2024", "label": "订阅计划", "desc": "每月一支当季豆寄送到家，累计订阅会员超过 4,200 位"},
            {"num": "2026", "label": "第二间店", "desc": "河岸新店开业，把长桌、窗光与慢下来的时间一起带过去"},
        ],
    },
    5: {
        "name": "从豆到杯",
        "prompt": "纯文字极简页，讲一颗豆子从筛选到出杯的完整过程",
        "visual": "green coffee beans being sorted by hand on a wooden tray",
        "free_composition": True,
        "focus_composition": "center",
        "copy": {
            "title": "从豆到杯", "subtitle": "BEAN TO CUP  ·  EVERY STEP RECORDED",
            "body": "生豆到港后先做三次手选，剔除瑕疵豆；每批只烘 3 公斤，"
                    "曲线按当日湿度微调。养豆 7 天后开袋杯测，达标才进磨。\n"
                    "研磨粗细、水温与注水节奏都写进吧台卡，"
                    "让每一杯都能被复现，也让每一次偏差都能被找回。",
            "cta": "每一批，都留得下曲线与杯测记录。",
        },
    },
    6: {
        "name": "坐下来的理由",
        "prompt": "空间一瞥，用图片拼接的方式把门店长桌、窗光与吧台连续拼在一起",
        "visual": "a long communal table by a tall window in a quiet cafe",
        "free_composition": True,
        "focus_composition": "half",
        "copy": {"title": "坐下来的理由", "subtitle": "OUR SPACES  ·  A CONTINUOUS VIEW"},
        "collage": {"count": 4, "gap_in": 0.22},
    },
    7: {
        "name": "来坐坐",
        "prompt": "门店与订阅的关键指标对开数据页，列出经营数据与订阅方式",
        "visual": "a cafe storefront with warm light and a notice board",
        "free_composition": True,
        "focus_composition": "side",
        "copy": {"title": "来坐坐", "subtitle": "VISIT & SUBSCRIBE",
                "body": "城南巷 17 号，每日 09:00–19:00。每月一支当季豆，寄到你桌上。"},
        "spread": {"stats": [
            {"value": "18", "label": "个座位 · 一面临窗长桌"},
            {"value": "4,200", "label": "位订阅会员 · 每月一支当季豆"},
            {"value": "3 kg", "label": "单批烘焙量 · 曲线逐批记录"},
            {"value": "10", "label": "年 · 从街角小店走到产区直采"},
        ]},
    },
    8: {
        "name": "明天见",
        "prompt": "最后一页致谢，谢谢你把时间留给自己，也留给我们",
        "visual": "an empty ceramic cup on a windowsill in soft evening light",
        "free_composition": True,
        "focus_composition": "half",
        "copy": {"title": "明天见。", "subtitle": "SEE YOU TOMORROW",
                "body": "谢谢你把这段时间留给自己，也留给我们。\n下一杯，我们仍在城南巷 17 号。",
                "cta": "城南巷 17 号  ·  每日 09:00–19:00"},
    },
}

TOTAL_PAGES = len(PAGE_CONTENT)


# ---------------------------------------------------------------- 工具
def gen_image(llm, prompt: str, out: Path, label: str, tries: int = 5) -> None:
    """生图带退避重试：端点偶发 403/502 是瞬时故障。"""
    for attempt in range(tries):
        try:
            llm.image(prompt, out, size="1536x1024")
            if out.exists() and out.stat().st_size > 100_000:
                return
            raise RuntimeError(f"产物过小({out.stat().st_size if out.exists() else 0}B)，疑似降级图")
        except Exception as exc:  # noqa: BLE001
            wait = 2 + attempt * 3
            print(f"  [{label}] 生图失败({type(exc).__name__}: {str(exc)[:60]})，"
                  f"{wait}s 后重试 {attempt + 2}/{tries}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"{label} 生图 {tries} 次仍失败")


def _brighten_if_dark(hex_color: str, floor: int = 205) -> str:
    hx = hex_color.lstrip("#")
    a = np.array([int(hx[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)
    lum = float(0.299 * a[0] + 0.587 * a[1] + 0.114 * a[2])
    if lum < floor:
        k = min(1.0, (floor - lum) / (255 - lum + 1e-6))
        a = a + (255 - a) * k
    return "#%02X%02X%02X" % tuple(int(v) for v in np.clip(a, 0, 255))


def _mirror_elements(els: list[dict]) -> list[dict]:
    """焦点在左 -> 洗白在右：整组元素镜像，保证永远落在洗白侧。"""
    out = []
    for e in els:
        p = dict(e["position"])
        p["x"] = round(13.333 - p["x"] - p["w"], 3)
        n = {**e, "position": p}
        if n.get("align") == "left":
            n["align"] = "right"
        elif n.get("align") == "right":
            n["align"] = "left"
        out.append(n)
    return out


# ---------------------------------------------------------------- 唯一引擎
def build_page(page_no: int, reuse: bool = False, *,
               layout_override: str | None = None,
               exclude: list[str] | None = None,
               tag: str | None = None,
               content_override: dict | None = None,
               cover_art_style: str | None = None) -> dict:
    spec = copy.deepcopy(PAGE_CONTENT[page_no])
    if content_override:
        # 整体替换，不能 update：否则旧页残留的 chapters/creative_toc 等结构化字段
        # 会继续命中旧版式，出现「内容换了版式没换」
        spec = copy.deepcopy(content_override)
    if layout_override:
        spec["layout"] = layout_override
    if exclude:
        spec["exclude"] = list(spec.get("exclude") or []) + list(exclude)

    # ---- 0. 内容信号路由（唯一决定版式的地方，与页码无关）----
    route = route_layout(spec, page_no, TOTAL_PAGES, trace=True)
    registry = build_registry()
    plan = registry.get(route["layout"]) or registry["editorial"]
    print(f"[page {page_no}] 内容 -> 版式: {route['layout']} "
          f"({plan['label']}) | 理由: {route['reason']}", flush=True)

    pdir = WORK / (f"page_{page_no:02d}_{tag}" if tag else f"page_{page_no:02d}")
    assets_dir = pdir / "assets"
    pdir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    # 变体页复用原页已生成的背景/艺术字素材，避免重复生图
    if tag and reuse:
        import shutil
        src = WORK / f"page_{page_no:02d}" / "assets"
        if src.exists():
            for f in src.glob("*.png"):
                if f.name.startswith(('art_raw', 'title_art')):
                    continue
                dst = assets_dir / f.name
                if not dst.exists():
                    shutil.copy2(f, dst)
                    if f.name == "bg_raw.png":
                        record = src / f"p{page_no:02d}_focus.json"
                        if record.is_file():
                            shutil.copy2(record, assets_dir / record.name)

    llm = LLMClient()
    raw_bg = assets_dir / "bg_raw.png"
    # 模板和路由只决定页型，不能授权；显式 CLI/API 参数才启用。
    art_size = None
    if config.COVER_TITLE_ART and route['layout'] == 'cover' and cover_art_style:
        title = spec.get('copy', {}).get('title') or spec['name']
        prompt = title_prompt(title, cover_art_style)
        color = spec.get('art_color', INK)
        key = hashlib.sha256((prompt + color).encode()).hexdigest()[:16]
        art_raw = assets_dir / f'art_raw_{key}.png'
        art_rgba = assets_dir / f'title_art_{key}.png'
        if not (reuse and art_rgba.exists() and art_rgba.stat().st_size > 20_000):
            print(f"[page {page_no}] 先做艺术字「{title}」 ...", flush=True)
            gen_image(llm, prompt, art_raw, "艺术字")
            matte_and_recolor(art_raw, art_rgba, color, style=cover_art_style)
        with Image.open(art_rgba) as im:
            art_size = im.size

    # ---- 1. 整页摄影图（自由构图页只写主题，不写焦点方位）----
    subject = spec.get("visual") or "a quiet specialty coffee bar corner"
    opening_scene = PAGE_CONTENT[page_no].get("opening_scene")
    reused_bg = reuse and raw_bg.is_file() and raw_bg.stat().st_size > 100_000
    record_path = assets_dir / f"p{page_no:02d}_focus.json"
    position = None
    lock = spec.get("focus_lock")
    if opening_scene:
        lock = None
        if reused_bg:
            # 只有记录与原图指纹一致才沿用抽样，否则走本地图像分析，不重抽。
            digest = hashlib.sha256(raw_bg.read_bytes()).hexdigest()
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = None
            if isinstance(record, dict) and record.get("raw_sha256") == digest:
                position = record.get("subject_position")
                if position not in OPENING_POSITIONS:
                    position = None
        else:
            position = random.choice(OPENING_POSITIONS)
        if position:
            side = "left" if "left" in position else "right"
            wash = "right" if side == "left" else "left"
            lock = {"focus_side": side, "wash": wash, "boundary": 0.5}
            bg_prompt = OPENING_BG.format(scene=opening_scene, position=position, wash=wash)
    elif spec.get("bg_prompt"):
        bg_prompt = spec["bg_prompt"]
    elif spec.get("free_composition"):
        bg_prompt = BG_FREE_TEMPLATE.format(
            subject=subject,
            focus_style=FOCUS_STYLES.get(spec.get("focus_composition"),
                                        DEFAULT_FOCUS_STYLE))
    else:
        bg_prompt = BG_TEMPLATE.format(subject=subject)
    if not reused_bg:
        print(f"[page {page_no}] 生成背景摄影图 ...", flush=True)
        gen_image(llm, bg_prompt, raw_bg, "背景图")
    with Image.open(raw_bg) as im:
        print(f"[page {page_no}] 原图尺寸={im.size}（16:9 等比裁切，不拉伸）", flush=True)

    # ---- 2. 真看图定焦点（四向）+ 按该焦点生成渐变遮罩 ----
    focus = prepare_background(raw_bg, assets_dir, f"p{page_no:02d}",
                               llm=llm, hint_side=spec.get("hint_side"),
                               focus_lock=lock, horizontal_only=bool(opening_scene))
    if opening_scene:
        focus["raw_sha256"] = digest if reused_bg else hashlib.sha256(raw_bg.read_bytes()).hexdigest()
        focus["subject_position"] = position or f"{focus['focus_side']} side"
    focus["color"] = _brighten_if_dark(focus["color"])
    make_adaptive_scrim(focus, assets_dir / f"p{page_no:02d}_scrim.png")
    focus["color_final"] = focus["color"]
    record_path.write_text(
        json.dumps(focus, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[page {page_no}] 焦点: {focus['focus_side']} / wash={focus['wash']} / "
          f"boundary={focus['boundary']} / {focus['color']}", flush=True)

    # ---- 2.5 内容安全区 = 非焦点区（左右型竖长区 / 上下型横宽区）----
    zone = content_zone(focus)
    wash = focus["wash"]
    wash_w = layout_width(zone)
    print(f"[page {page_no}] 内容区: axis={zone['axis']} "
          f"box=({zone['x']}, {zone['y']}, {zone['w']}, {zone['h']})", flush=True)
    assets: dict[str, str] = {"bg": focus["bg_path"], "scrim": focus["scrim_path"]}
    if art_size:
        assets["title_art"] = str(art_rgba)

    # 版式要额外素材时，统一走这个回调（不区分是哪个版式）
    def gen_asset(prompt: str, name: str) -> str:
        p = assets_dir / f"{name}.png"
        if not (reuse and p.exists() and p.stat().st_size > 100_000):
            print(f"[page {page_no}] 生成配图 {name} ...", flush=True)
            gen_image(llm, prompt, p, name)
        assets[name] = str(p)
        return name

    ctx = {
        "spec": spec, "page_no": page_no, "total": TOTAL_PAGES,
        "wash": wash, "wash_w": wash_w, "focus": focus, "zone": zone,
        "assets": assets, "assets_dir": assets_dir, "gen_asset": gen_asset,
        "palette": PALETTE, "INK": INK, "INK_SOFT": INK_SOFT, "VI": VI,
        "art_pos": None,
    }

    # ---- 3. 艺术字位置（图已在第 0.5 步生成；此处只按焦点后的安全区定位）----
    if art_size:
        aw, ah = art_size
        x0 = 0.82
        max_w = max(4.5, min(8.0, wash_w - x0 - 0.30))
        w = min(max_w, 13.333 * 0.60)
        h = min(w * ah / aw, 2.35)
        w = h * aw / ah
        ctx["art_pos"] = {"x": round(x0, 3), "y": 1.48,
                          "w": round(w, 3), "h": round(h, 3)}

    # ---- 4. 查表取版式内容（所有页完全相同的调用）----
    els = [{"type": "picture", "asset_id": "scrim",
            "position": {"x": 0, "y": 0, "w": 13.333, "h": 7.5},
            "layer": "mask"}]
    els.extend(plan["elements"](ctx))
    if zone["axis"] == "h" and wash == "right":
        els = _mirror_elements(els)
        print(f"[page {page_no}] 焦点在左，内容已镜像到右侧洗白区", flush=True)
    # 内容统一放进「非焦点区」：上下型横向铺满、纵向收进上/下半
    els = fit_to_zone(els, zone)

    raw = {"vi": VI, "pages": [{
        "page_name": spec["name"],
        "bg": {"type": "image", "image": "bg", "color": focus["color_final"]},
        "elements": els,
    }]}
    layout = normalize_layout(raw, brief={"vi": VI})
    if opening_scene:
        # normalize_layout 会为避开孤行而加宽文本框；收口不能越过背景安全区。
        for el in layout["pages"][0]["elements"]:
            if el["type"] == "text":
                pos = el["position"]
                pos["w"] = min(pos["w"], round(zone["x"] + zone["w"] - pos["x"], 3))

    # 拼接类版式需要预裁切片（LibreOffice 忽略 srcRect）
    slices_dir = assets_dir / "slices"
    for page in layout["pages"]:
        for el in page["elements"]:
            if el["type"] == "mask_picture":
                paths = prepare_mask_slices(el, assets, slices_dir)
                for i, sp in enumerate(paths):
                    if sp:
                        assets[f"{el['asset_id']}__{i}"] = sp

    (pdir / "01_layout.json").write_text(
        json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 5. 统一构建 + 版式自带的原生绘制（仍然只是查表调用）----
    b = PPTBuilder()
    b._vi_primary, b._vi_secondary = VI["primary"], VI["secondary"]
    for page in layout["pages"]:
        slide = b.prs.slides.add_slide(b.blank_layout)
        b.set_page_context(page)
        b.set_background(slide, page["bg"], assets)
        for el in sorted(page["elements"], key=lambda e: e.get("z", 0)):
            tk = PPTToolkit(b, slide, assets)
            tk.call_for_element(el)
        plan["draw"](slide, ctx)
    out = pdir / f"page_{page_no:02d}.pptx"
    b.prs.save(str(out))

    rep = inspect(str(out), layout)
    print(f"[page {page_no}] 自检: {rep['summary']} | 阻塞={rep['issues'][:3]}", flush=True)

    shots = pdir / "shots"
    rv = try_render(str(out), shots)
    print(f"[page {page_no}] 渲染: {rv.get('status')} -> {shots}", flush=True)

    result = {
        "page": page_no, "pptx": str(out),
        "shot": (rv.get("images") or [None])[0],
        "route": route, "layout": route["layout"],
        "focus": focus, "inspect_ok": rep["ok"], "render": rv.get("status"),
    }
    (pdir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ---------------------------------------------------------------- CLI
def cmd_route() -> None:
    reg = build_registry()
    print(f"共 {TOTAL_PAGES} 页内容 -> 版式路由（页码只提供首页/末页默认值）\n")
    for n in sorted(PAGE_CONTENT):
        r = route_layout(PAGE_CONTENT[n], n, TOTAL_PAGES)
        label = reg.get(r["layout"], {}).get("label", r["layout"])
        print(f"  第{n}页 {PAGE_CONTENT[n]['name']:<10} -> {r['layout']:<20} "
              f"{label}\n         理由: {r['reason']}")
        if r["evidence"]:
            print(f"         命中信号: {r['evidence'][0]}")


def cmd_probe(text: str) -> None:
    for n in (2, 3, 5):
        r = route_layout({"name": "测试页", "prompt": text}, n, TOTAL_PAGES, trace=True)
        print(f"  放在第{n}页: {r['layout']:<20} 理由: {r['reason']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", type=int)
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument('--cover-art', choices=['lettering', 'calligraphy', 'metallic'],
                    help='明确将封面主标题做成艺术字图片（默认保留原生文本）')
    ap.add_argument("--layout", help="显式指定版式（覆盖内容路由）")
    ap.add_argument("--exclude", action="append", help="排除某版式，可多次")
    ap.add_argument("--tag", help="独立产物目录后缀，不覆盖已验收的页")
    ap.add_argument("--content-from", type=int,
                    help="借用另一页的内容放到本页位置，验证版式跟着内容走")
    ap.add_argument("--route", action="store_true", help="只打印内容->版式路由表")
    ap.add_argument("--probe", help="用一段任意文字试路由，不生成任何文件")
    args = ap.parse_args()

    if args.route:
        cmd_route()
        return 0
    if args.probe:
        cmd_probe(args.probe)
        return 0
    page_no = args.page or 1
    if page_no not in PAGE_CONTENT:
        print(f"没有第 {page_no} 页的内容（当前 1..{TOTAL_PAGES}）")
        return 2
    borrow = None
    if args.content_from:
        borrow = copy.deepcopy(PAGE_CONTENT[args.content_from])
        # 只借内容，摄影场景和构图仍属当前页。
        for key in ("opening_scene", "visual", "free_composition", "focus_composition",
                    "bg_prompt", "hint_side", "focus_lock"):
            borrow.pop(key, None)
            if key in PAGE_CONTENT[page_no]:
                borrow[key] = PAGE_CONTENT[page_no][key]
        borrow["_borrowed_from"] = args.content_from
    r = build_page(page_no, reuse=args.reuse,
                   layout_override=args.layout, exclude=args.exclude,
                   tag=args.tag, content_override=borrow, cover_art_style=args.cover_art)
    print("\n★ 版式:", r["layout"], "|", r["route"]["reason"])
    print("★ 单页成品:", r["pptx"])
    print("★ 渲染截图:", r["shot"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
