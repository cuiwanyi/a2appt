"""全局配置。

优先级：系统环境变量 > 项目根目录 .env > 代码默认值。
模型别名在这里做「用户写法 -> 端点真实 ID」的解析，避免 404。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from run_paths import RUNS

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

BASE_URL = os.getenv("A2A_BASE_URL", "http://8.216.45.190:8011/").rstrip("/")
API_KEY = os.getenv("A2A_API_KEY", "")

# 端点 /v1/models 实测结果（2026-11-19）：
#   语言模型 gpt-6.2-astra
#   生图模型 gpt-image-2.6-flare / gpt-image-2.6-sunburst
# 用户给的别名 gpt6.2Astra / gpt2.6img 直接调用会返回 model_not_found。
LLM_MODEL = os.getenv("A2A_LLM_MODEL", "gpt-6.2-astra")
IMAGE_MODEL = os.getenv("A2A_IMAGE_MODEL", "gpt-image-2.6-flare")

MODEL_ALIASES = {
    "gpt6.2astra": "gpt-6.2-astra",
    "gpt-6.2astra": "gpt-6.2-astra",
    "gpt2.6img": "gpt-image-2.6-flare",
    "gpt-image2.6": "gpt-image-2.6-flare",
    "gpt2.6img-sunburst": "gpt-image-2.6-sunburst",
}


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get(name.strip().lower().replace("_", ""), name)


MOCK = os.getenv("A2A_MOCK", "0").lower() in {"1", "true", "yes", "on"}
REQUEST_TIMEOUT = int(os.getenv("A2A_TIMEOUT", "180"))
MAX_REWORK = int(os.getenv("A2A_MAX_REWORK", "3"))
# 进程内 HTTP A2A 的 message/send 读超时：一次 Task 里可能串多张生图 + 渲染，
# 必须比最慢的一步更长，否则多图网格这类版式会把委派请求撑超时。
HTTP_TIMEOUT = int(os.getenv("A2A_HTTP_TIMEOUT", "600"))
# 真实截图 + 多模态视觉评审（步骤 4b）
VISION_REVIEW = os.getenv("A2A_VISION_REVIEW", "1").lower() in {"1", "true", "yes", "on"}
# 视觉评审的问题是否升级为阻塞项（默认只报 warning，避免主观误判触发无限返修）
VISION_BLOCKING = os.getenv("A2A_VISION_BLOCKING", "0").lower() in {"1", "true", "yes", "on"}
# 真实截图的渲染引擎：libreoffice（默认，已验证可用）/ wps / auto
# wps 走 WPS 自带 kwpsconvert.exe ppt2pdf，需要本机 WPS 已登录且有转换权限；
# 失败会自动回退 LibreOffice，并在自检报告里写明原因。
RENDERER = os.getenv("A2A_RENDERER", "libreoffice").strip().lower()
# 生图候选数（best-of-N）：默认 1 保持原有链路不变；>1 时每个 asset 生成 N 张候选，
# 落盘到 02_assets/candidates/ 并生成联系表，默认采用第 1 张，用户可用 pick_assets.py 改选。
IMAGE_CANDIDATES = max(1, int(os.getenv("A2A_IMAGE_CANDIDATES", "1")))
# 封面主标题艺术字：默认开启，作为生成流程的最后一步自动套用（只把标题换成艺术字图，
# 其余封面元素保持原生可编辑）。关掉则交付纯原生文本框版本。
COVER_TITLE_ART = os.getenv("A2A_COVER_TITLE_ART", "1").lower() in {"1", "true", "yes", "on"}
COVER_TITLE_ART_N = max(1, int(os.getenv("A2A_COVER_ART_N", "2")))  # 艺术字候选数（默认2，取第1张）

WORKSPACE = RUNS
SLIDE_W_IN = 13.333
SLIDE_H_IN = 7.5

# 画布安全边距（英寸），人工收口时也要守这个值
SAFE_MARGIN_IN = 0.45
