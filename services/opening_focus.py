"""整册开篇摄影构图；角落描述主体的位置，文字仍用完整对侧半幅。"""

OPENING_LIMIT = 3
# 与逐页助手的 subject_position 取值一致，不导入 CLI。
POSITIONS = {
    "left side": ("左侧", "right"), "right side": ("右侧", "left"),
    "upper left corner": ("左上角", "right"), "upper right corner": ("右上角", "left"),
    "lower left corner": ("左下角", "right"), "lower right corner": ("右下角", "left"),
}
OPENING_BG_PREFIX = "整页摄影背景构图："


def composition(position: str) -> str:
    subject, wash = POSITIONS[position]
    blank = "左" if wash == "left" else "右"
    return f"主体集中在画面{subject}的外三分之一，边缘留裁切余量；{blank}半幅整体干净浅色、低细节，留给文字"
