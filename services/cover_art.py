"""封面艺术字生成（设计导向提示词模板）。

经过多轮真实生图验证的关键结论：
1. 中文短标题（2~13 字）用 gpt-image-2.6-flare 能 100% 写对——前提是提示词把
   目标汉字用「」逐字圈出，并强调「exactly these characters, complete strokes, no gibberish」。
2. 「把字写漂亮」会得到"普通字加特效"，不像艺术字；要得到设计级封面，提示词必须描述
   **整张海报的设计语言**：字形风格 + 构图 + 背景图形 + 副标题层级 + 公司信息。
3. 同提示词多次生成：字与风格稳定，构图细节有差异——所以默认出 N 个候选供人工挑。

本模块把验证过的三种风格固化成模板。make_cover.py 调用它生成候选封面。
"""
from __future__ import annotations

from typing import Literal

Style = Literal["calligraphy", "3d_gradient", "minimal_serif"]

STYLE_LABELS = {
    "calligraphy": "书法水墨海报（飞白笔锋 + 印章 + 留白）",
    "3d_gradient": "3D 渐变立体海报（几何背景 + 强构成）",
    "minimal_serif": "极简宋体海报（大留白 + 单色 + 摄影感）",
}

# 三种风格的"设计语言"骨架；{title} 会被逐字圈进「」并强调不可改写。
_STYLE_BODIES: dict[str, str] = {
    "calligraphy": (
        "Main hero title in large expressive BRUSH-CALLIGRAPHY Chinese handwriting with strong "
        "slanted strokes, flying-white ink texture and a red seal stamp: 「{title}」. "
        "Design it like a premium cultural poster: the calligraphy is the hero, with a thin elegant "
        "curved swoosh and a neat vertical typographic hierarchy block carrying 「{subtitle}」. "
        "Background: warm rice-paper texture with soft ink-wash mountains, generous negative space. "
    ),
    "3d_gradient": (
        "Main hero title in large bold CUSTOM-DESIGNED 3D Chinese lettering with slanted dynamic "
        "strokes and a vivid blue-to-violet gradient: 「{title}」. "
        "Design it like a premium tech/corporate poster: the lettering is the hero, with a small "
        "underline accent; background features abstract gradient geometric folded-paper shapes, "
        "a subtle skyline or light streaks, and small elegant text 「{subtitle}」. "
    ),
    "minimal_serif": (
        "Main hero title in large refined thin Song-style serif Chinese lettering with a fine "
        "outline: 「{title}」. "
        "Design it like a minimal editorial poster: extreme negative space, a single accent color "
        "on a warm off-white background, a delicate thin line, and a small quiet line 「{subtitle}」. "
    ),
}

_PREFIX = (
    "A complete professional Chinese cover slide design, high-end graphic design, NOT plain bold "
    "text, NOT plain text with effects. "
)
_SUFFIX = (
    "The hero Chinese title must be written 100% correct with complete strokes, exactly these "
    "characters and no others, NO gibberish, NO floating or missing strokes. "
    "Include 「{brand}」as a small company line. "
)


def cover_prompt(title: str, subtitle: str = "", brand: str = "",
                 style: Style = "3d_gradient") -> str:
    """把标题+风格转成设计导向的生图提示词。

    title 里的汉字会被逐字圈出并要求 100% 正确；subtitle/brand 作为信息层级进入画面。
    """
    style = style if style in _STYLE_BODIES else "3d_gradient"
    body = _STYLE_BODIES[style].format(title=title.strip(), subtitle=subtitle.strip() or "")
    return _PREFIX + body + _SUFFIX.format(brand=brand.strip() or "BRAND")
