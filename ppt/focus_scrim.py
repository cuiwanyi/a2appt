"""根据【实际生成的图片】做焦点自适应渐变遮罩（画册级逐页能力）。

与 ppt/scrim.py 的区别：
    scrim.py      : wash 方向 / 颜色由规划器写死，遮罩是一张与图片无关的纯白渐变。
    focus_scrim.py: 先真的看图 -> 算出焦点在哪一侧、边界在哪、洗白侧主色是什么，
                    再据此生成遮罩。焦点区 alpha 恒为 0（一点都不遮），
                    渐变末端纯色取自洗白侧图像采样（不是凭空白）。

对外主入口 prepare_background()：
    原图 -> 16:9 等比 cover 裁切(不拉伸) -> 焦点分析 -> 自适应渐变 PNG
    -> 返回 bg 路径 / scrim 路径 / wash / 焦点边界 / 采样色 / 分析依据
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

TARGET_RATIO = 16 / 9  # 与 PPT 画布 13.333 x 7.5 一致


# ---------------------------------------------------------------- 图片不变形
def cover_crop(src: str | Path, dst: str | Path, ratio: float = TARGET_RATIO) -> Path:
    """等比中心裁切到目标比例，绝不拉伸变形。落盘并返回路径。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        cur = w / h
        if cur > ratio:            # 太宽 -> 裁左右
            nw = int(round(h * ratio))
            x0 = (w - nw) // 2
            im = im.crop((x0, 0, x0 + nw, h))
        elif cur < ratio:          # 太高 -> 裁上下
            nh = int(round(w / ratio))
            y0 = (h - nh) // 2
            im = im.crop((0, y0, w, y0 + nh))
        im.save(dst)
    return dst


# ---------------------------------------------------------------- 焦点分析
def _detail_centroid(img: Image.Image) -> tuple[float, float]:
    """细节能量质心：灰度梯度大的地方通常是主体/焦点。返回 (cx, cy) in 0~1。"""
    small = img.convert("L").resize((96, 54))
    a = np.asarray(small).astype(np.float32)
    gx = np.abs(np.diff(a, axis=1, prepend=a[:, :1]))
    gy = np.abs(np.diff(a, axis=0, prepend=a[:1, :]))
    energy = gx + gy + 0.35 * np.abs(a - a.mean())
    energy = np.clip(energy, 0, 255) ** 1.4
    ys, xs = np.mgrid[0:energy.shape[0], 0:energy.shape[1]]
    tot = float(energy.sum()) or 1.0
    return float((energy * xs).sum() / tot / (energy.shape[1] - 1)), \
           float((energy * ys).sum() / tot / (energy.shape[0] - 1))


def _sample_side_color(img: Image.Image, wash: str) -> str:
    """从洗白侧边缘区域采平均色，让渐变末端纯色与这张图自然衔接。"""
    a = np.asarray(img.convert("RGB").resize((160, 90))).astype(np.float32)
    h, w = a.shape[:2]
    if wash == "left":
        reg = a[:, : int(w * 0.22)]
    elif wash == "right":
        reg = a[:, int(w * 0.78):]
    elif wash == "top":
        reg = a[: int(h * 0.22), :]
    elif wash == "center":
        # 中心焦点：四周都是留白，采四条边缘的平均色
        reg = np.concatenate([
            a[: int(h * 0.18), :].reshape(-1, 3),
            a[int(h * 0.82):, :].reshape(-1, 3),
            a[:, : int(w * 0.18)].reshape(-1, 3),
            a[:, int(w * 0.82):].reshape(-1, 3),
        ], axis=0)
    else:
        reg = a[int(h * 0.78):, :]
    c = reg.reshape(-1, 3).mean(axis=0)
    # 向白混 22% 保证文字可读，但仍保留图片色相（不是死白）
    c = c * 0.78 + np.array([255, 255, 255]) * 0.22
    return "#%02X%02X%02X" % tuple(int(v) for v in np.clip(c, 0, 255))


def analyze_focus(img_path: str | Path, llm=None, hint_side: str | None = None,
                  focus_lock: dict | None = None, *, horizontal_only: bool = False) -> dict:
    """真的看图定焦点：优先多模态模型，失败/异常时用图像细节质心兜底。

    focus_lock 可选：已验收页把 wash/boundary 锁进内容数据，避免视觉模型
    边界在 0.60/0.62 之类来回波动导致重跑画面漂移；颜色仍从这张图采样。
    hint_side=None 表示自由构图，焦点完全以实际图像为准。
    """
    img = Image.open(img_path).convert("RGB")
    cx, cy = _detail_centroid(img)
    basis = f"图像细节质心 cx={cx:.2f}, cy={cy:.2f}"

    if focus_lock:
        side = str(focus_lock.get("focus_side", "right")).lower()
        wash = str(focus_lock.get("wash", "")).lower() or {
            "right": "left", "left": "right", "bottom": "top", "top": "bottom"
        }[side]
        boundary = max(0.40, min(0.80, float(focus_lock.get("boundary", 0.60))))
        return {
            "focus_side": side, "wash": wash, "boundary": round(boundary, 3),
            "color": _sample_side_color(img, wash),
            "centroid": [round(cx, 3), round(cy, 3)],
            "consistent_with_prompt": None,
            "locked": True,
            "basis": basis + "；按给定构图锁定焦点侧和边界",
        }

    vfocus = None
    if llm is not None and not horizontal_only:
        try:
            r = llm.chat_vision_json(
                "你是版面分析师。只输出一个 JSON 对象。",
                "看这张图，判断画面主体/视觉焦点落在哪里。必须依据图中主体的"
                "真实位置判断，不要默认右侧。只输出："
                "{\"focus_side\":\"left|right|top|bottom|center\","
                "\"boundary\":0.0~1.0,\"reason\":\"一句话\"}。"
                "若主体明显居于画面中央、四周都是留白，就判 center。"
                "boundary 指：从与焦点相反的那一侧（将来放文字的一侧）算起，"
                "到主体开始清晰出现的分界位置占比；center 时给 0.42。",
                [str(img_path)], max_tokens=300,
            )
            if str(r.get("focus_side", "")).lower() in {
                    "left", "right", "top", "bottom", "center"}:
                vfocus = r
                basis += f"；视觉模型判定 {r['focus_side']} / {r.get('reason', '')}"
        except Exception as exc:  # noqa: BLE001
            basis += f"；视觉模型不可用({type(exc).__name__})，改用图像质心"

    if horizontal_only:
        # 没有可信构图记录的旧图：角落也按横向质心投影，保留完整目录高度。
        side = "left" if cx < 0.5 else "right"
        boundary = 0.5
        basis += "；按原图横向质心选择对侧半区"
    elif vfocus:
        side = str(vfocus["focus_side"]).lower()
        try:
            boundary = float(vfocus.get("boundary", 0.60))
            boundary = max(0.40, min(0.80, boundary))
        except (TypeError, ValueError):
            boundary = 0.60
    else:
        # 质心兜底：接近画面正中判 center，否则哪个方向偏得多就判哪一侧
        dx, dy = cx - 0.5, cy - 0.5
        if abs(dx) < 0.08 and abs(dy) < 0.08:
            side = "center"
        elif abs(dx) >= abs(dy):
            side = "right" if dx >= 0 else "left"
        else:
            side = "bottom" if dy >= 0 else "top"
        boundary = 0.42 if side == "center" else 0.58

    if side == "center":
        boundary = 0.42
    wash = {"right": "left", "left": "right", "bottom": "top",
            "top": "bottom", "center": "center"}[side]
    color = _sample_side_color(img, wash)

    return {
        "focus_side": side,
        "wash": wash,
        "boundary": round(boundary, 3),
        "color": color,
        "centroid": [round(cx, 3), round(cy, 3)],
        "consistent_with_prompt": None if not hint_side else (side == hint_side),
        "basis": basis,
    }


# ---------------------------------------------------------------- 自适应遮罩
def make_adaptive_scrim(focus: dict, out_path: str | Path,
                        size: tuple[int, int] = (1536, 864),
                        strength: float = 0.97) -> Path:
    """按焦点边界生成渐变 PNG：焦点区 alpha 恒 0，洗白侧渐变到纯色。

    boundary 是「从洗白侧算起焦点开始的位置」，例如 wash=left/boundary=0.66
    表示 x<0.66 是文字区，x>=0.66 是焦点区完全不遮。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    W, H = size
    wash = focus["wash"]
    b = float(focus["boundary"])
    band = 0.30  # 渐变过渡带宽度（占比）

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    nx, ny = xx / W, yy / H
    if wash == "center":
        # 中心焦点：中心主体完全不遮，向四周边缘渐变到采样纯色
        rx = (nx - 0.5) / 0.52
        ry = (ny - 0.5) / 0.52
        radial = np.sqrt(rx * rx + ry * ry)
        ramp = np.clip((radial - 0.25) / 0.35, 0, 1) ** 0.90
    else:
        if wash == "left":
            u = nx
        elif wash == "right":
            u = 1 - nx
        elif wash == "top":
            u = ny
        else:
            u = 1 - ny
        # u 小=离洗白侧边缘近(全遮)，u 接近 b=焦点边界(不遮)
        ramp = np.clip((b - u) / band, 0, 1) ** 1.05
    alpha = np.uint8(ramp * 255 * max(0.0, min(1.0, strength)))

    hx = str(focus["color"]).lstrip("#")
    r, g, bl = (int(hx[i:i + 2], 16) for i in (0, 2, 4))
    arr = np.zeros((H, W, 4), np.uint8)
    arr[:, :, 0], arr[:, :, 1], arr[:, :, 2] = r, g, bl
    arr[:, :, 3] = alpha
    Image.fromarray(arr, "RGBA").save(out_path)
    return out_path


def prepare_background(raw_src: str | Path, out_dir: str | Path,
                       name: str, llm=None, hint_side: str | None = None,
                       focus_lock: dict | None = None, *, horizontal_only: bool = False) -> dict:
    """主入口：原图 -> 16:9 不拉伸裁切 -> 真看图定焦点(四向) -> 自适应遮罩 PNG。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bg = cover_crop(raw_src, out / f"{name}_bg16x9.png")
    focus = analyze_focus(bg, llm=llm, hint_side=hint_side, focus_lock=focus_lock,
                          horizontal_only=horizontal_only)
    scrim = make_adaptive_scrim(focus, out / f"{name}_scrim.png")
    focus["bg_path"] = str(bg)
    focus["scrim_path"] = str(scrim)
    (out / f"{name}_focus.json").write_text(
        json.dumps(focus, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return focus
