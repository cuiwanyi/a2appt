"""焦点保护型渐变遮罩（产品化自 test_focus_scrim.py）。

背景是一整张摄影图；本模块在【文字所在的那一侧】盖一层 local 生成的
白色渐变 PNG（alpha 从文字侧高 → 焦点侧 0），文字落在被洗白的浅色区，
图片焦点一侧保持清晰不被遮罩。渐变末端是纯白，用户在需求里已确认允许。

纯本地 Pillow 生成，不调生图模型；遮罩色可指定（默认白，也可用 VI 浅色）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

W, H = 1536, 864  # 遮罩分辨率（16:9，与画布一致）

# wash 表示「洗白哪一侧」——文字就放这一侧；另一侧是图片焦点，保持清晰。
_WASH_RAMP = {
    "left": lambda nx, ny: (0.72 - nx) / 0.62,
    "right": lambda nx, ny: (nx - 0.28) / 0.62,
    "top": lambda nx, ny: (0.70 - ny) / 0.62,
    "bottom": lambda nx, ny: (ny - 0.30) / 0.62,
    "bottomleft": lambda nx, ny: ((0.72 - nx) + (ny - 0.28)) / 1.30,
    "topleft": lambda nx, ny: ((0.72 - nx) + (0.70 - ny)) / 1.30,
    "bottomright": lambda nx, ny: ((nx - 0.28) + (ny - 0.30)) / 1.30,
    "topright": lambda nx, ny: ((nx - 0.28) + (0.70 - ny)) / 1.30,
}


def make_scrim(wash: str, color: str = "#FFFFFF", strength: float = 0.96,
               out_path: str | Path | None = None) -> Image.Image:
    """生成一张焦点保护渐变遮罩 PNG。

    wash: 洗白哪一侧（left/right/top/bottom/及四角组合），文字放该侧；
    color: 渐变末端纯色（默认白）；strength: 最大不透明度 0~1；
    out_path: 给了就落盘，返回 PIL.Image。
    """
    ramp_fn = _WASH_RAMP.get(str(wash).lower(), _WASH_RAMP["left"])
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    ramp = np.clip(ramp_fn(xx / W, yy / H), 0, 1)
    alpha = np.uint8(ramp ** 1.15 * 255 * max(0.0, min(1.0, strength)))

    hx = str(color).lstrip("#")
    try:
        r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 255, 255, 255
    arr = np.zeros((H, W, 4), np.uint8)
    arr[:, :, 0], arr[:, :, 1], arr[:, :, 2] = r, g, b
    arr[:, :, 3] = alpha
    im = Image.fromarray(arr, "RGBA")
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        im.save(p)
    return im
