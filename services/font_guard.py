"""字体可用性守卫：LLM 爱写「思源宋体」「阿里巴巴普惠体」这类本机没装的字体。

缺字体时 PowerPoint 会静默替换，字宽一变，广告级排版就跑偏了。
这里在版式 JSON 规范化阶段把字体映射到本机真实存在的字体，并在报告里留痕。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# 常见字体别名 -> Windows 字体文件名（按衬线/无衬线分组）
_CJK_SERIF = ["simsun.ttc", "SourceHanSerifSC-Regular.otf", "NotoSerifCJKsc-Regular.otc"]
_CJK_SANS = ["msyh.ttc", "simhei.ttf", "Deng.ttf", "SourceHanSansSC-Regular.otf", "NotoSansCJKsc-Regular.otc"]
_LATIN = ["arial.ttf", "calibri.ttf", "times.ttf"]

_SERIF_HINTS = ("宋", "serif", "song", "source han serif", "思源宋", "明体", "mincho")
_SANS_HINTS = ("雅黑", "黑体", "sans", "hei", "微软", "等线", "苹方", "pingfang", "思源黑")

FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
]


@lru_cache(maxsize=1)
def available_fonts() -> dict[str, str]:
    """返回 {文件名小写: 绝对路径}，只扫描一次。"""
    found: dict[str, str] = {}
    for d in FONT_DIRS:
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() in {".ttf", ".ttc", ".otf", ".otc"}:
                found.setdefault(p.name.lower(), str(p))
    return found


def _installed(names: list[str]) -> tuple[str, str] | None:
    fonts = available_fonts()
    for n in names:
        if n.lower() in fonts:
            # 返回「字体家族名」，不是文件名
            family = {
                "simsun.ttc": "宋体", "msyh.ttc": "微软雅黑", "simhei.ttf": "黑体",
                "deng.ttf": "等线", "arial.ttf": "Arial", "calibri.ttf": "Calibri",
                "times.ttf": "Times New Roman",
            }.get(n.lower(), Path(fonts[n.lower()]).stem)
            return n.lower(), family
    return None


def _is_serif(name: str) -> bool:
    low = name.lower()
    return any(h.lower() in low for h in _SERIF_HINTS)


def _is_sans(name: str) -> bool:
    low = name.lower()
    return any(h.lower() in low for h in _SANS_HINTS)


def preferred_fonts() -> list[str]:
    """本机实际可用的中文/西文字体家族名，写进规划提示词从源头避免替换。"""
    fonts = available_fonts()
    candidates = [
        ("msyh.ttc", "微软雅黑"), ("simsun.ttc", "宋体"),
        ("simhei.ttf", "黑体"), ("deng.ttf", "等线"),
        ("arial.ttf", "Arial"), ("times.ttf", "Times New Roman"),
    ]
    return [family for file, family in candidates if file in fonts]


def resolve_font(name: str) -> tuple[str, str | None]:
    """返回 (实际使用的字体名, 替换说明或 None)。"""
    requested = (name or "微软雅黑").strip()
    fonts = available_fonts()
    low = requested.lower()

    # 直接命中：按常见家族名映射到文件
    direct = {
        "微软雅黑": "msyh.ttc", "microsoft yahei": "msyh.ttc", "msyh": "msyh.ttc",
        "宋体": "simsun.ttc", "simsun": "simsun.ttc", "黑体": "simhei.ttf",
        "simhei": "simhei.ttf", "等线": "deng.ttf", "dengxian": "deng.ttf",
        "arial": "arial.ttf", "calibri": "calibri.ttf",
        "times new roman": "times.ttf",
    }
    alias = direct.get(low)
    if alias and alias in fonts:
        return requested, None

    # 文件名形式的家族名也认
    if low in fonts:
        return requested, None

    # 缺失：按衬线倾向挑一个本机有的
    candidates = _CJK_SERIF if _is_serif(requested) else _CJK_SANS
    hit = _installed(candidates)
    if hit is None:
        hit = _installed(_CJK_SANS + _CJK_SERIF + _LATIN)
    if hit is None:
        return "微软雅黑", f"'{requested}' 未安装且未找到可用中文字体，回退为 微软雅黑"
    _, family = hit
    return family, f"'{requested}' 本机未安装，回退为 '{family}'"


if __name__ == "__main__":
    print("可用字体数:", len(available_fonts()))
    for n in ["微软雅黑", "思源宋体", "Source Han Serif SC", "阿里巴巴普惠体", "宋体", "Arial"]:
        print(f"  {n!r:28} -> {resolve_font(n)}")
