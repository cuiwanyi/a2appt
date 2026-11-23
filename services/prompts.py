"""提示词（来自已核验工作流）+ 本编排器实际消费的版式 JSON 契约。

关键设计：**原语与具体版式分离**。
这里只描述「有哪些可编辑元素可以搭」，不推荐任何特定构图。
「5 根柱子拼一张滨水城市全景」只是 mask_picture 的一种可选参数组合，
不是默认版式，也不出现在示例里 —— 否则 LLM 会把示例当模板抄。

提示词只是 A2A Task 里的 TextPart：Agent 读完后产出必须是结构化数据，
不允许把自然语言当作交付物。
"""
from __future__ import annotations

import json
from typing import Any

from services.font_guard import preferred_fonts
from services.opening_focus import OPENING_BG_PREFIX, composition

# ---------------------------------------------------------------- 版式 JSON 契约
LAYOUT_CONTRACT = r"""
【跨主题通用原则：本次需求决定设计】
- 页数、配色、逐页题材以及是否分栏，由本次 brief 和内容决定。未指定时按表达需要选择，不得把其他项目的设置当作默认模板；单页例外仅在指定页生效。
- 摄影主体的位置不按页码固定。生成时可以建议构图，但建议方位不是成图证据。排版时以实际照片中的主体和留白为准，让文字、遮罩避开主体；某一页明确指定方向，只对那一页生效。若实际照片不满足明确的构图要求，调整或更换该页照片，不要假定它已经符合。

【字段格式示例：只演示字段名与类型，不是推荐构图，禁止照抄其版式】
{
  "vi": {"primary": "#0B2A4A", "secondary": "#C9A86A", "accent": "#E8EEF5"},
  "pages": [
    {
      "page_name": "封面",
      "bg": {"type": "color", "color": "#0B2A4A", "image": null},
      "elements": [
        {"type":"text","content":"主标题","position":{"x":1.0,"y":2.0,"w":11.3,"h":1.2},
         "font":{"family":"微软雅黑","weight":"bold","color":"#FFFFFF","size":44},
         "align":"center","layer":"text","note":"主标题"},
        {"type":"shape","shape":"rect","fill":"#C9A86A","line":null,
         "position":{"x":5.9,"y":3.4,"w":1.5,"h":0.06},"layer":"decoration","note":"分隔线"},
        {"type":"picture","asset_id":"photo_1","content":"产品特写",
         "position":{"x":8.0,"y":1.2,"w":4.2,"h":5.0},"layer":"picture"},
        {"type":"decorate","primitive":"ring","color":"#C9A86A",
         "position":{"x":0.6,"y":0.5,"w":0.8,"h":0.8},"layer":"decoration","note":"边角点缀"}
      ]
    }
  ]
}

字段说明：
- position 单位英寸，画布 13.333 x 7.5；x/y/w/h 必须为正数且不得超出画布。
- type: text | shape | picture | mask_picture | decorate | branch | scrim | toc；背景由 bg 单独描述。
- shape 可用 fill_opacity 设置填充不透明度（0 到 1，默认 1）；叠在摄影背景上的浅色数据条或指标卡底框标记 role="photo_metric_card"，使用暖米色 #F7EEDC、fill_opacity=0.58（42% 透明）；数字和说明放在卡框内的独立原生文本框，字色 #20160F。不要给刊头、页脚或普通装饰框添加此标记。
- scrim（焦点保护渐变遮罩，配合 bg.image 用在文字压图的页）：
  {"type":"scrim","wash":"left|right|top|bottom|bottomleft|bottomright|topleft|topright",
   "color":"#FFFFFF","strength":0.94,"position":{"x":0,"y":0,"w":13.333,"h":7.5},"layer":"mask"}
  wash = 洗白哪一侧（文字放该侧，图片焦点在相反侧保持清晰）。
- toc（仅在本页承担目录功能时使用，不按页数或页码自动添加）：
  {"type":"toc","title":"目录","cols":2,"wash":"left|right|...（与该页 scrim 的 wash 一致）",
   "items":[{"num":"01","label":"章节名","desc":"一句话说明"}],
   "position":{"x":0.8,"y":1.2,"w":11.7,"h":5.6},"layer":"text"}
  注意：若本页同时有 scrim（摄影背景焦点在另一侧），必须给 toc 填 wash。默认收进洗白侧且用单列；
  仅当本次需求明确要求多列时保留指定 cols，在洗白侧内部排布，不压到图片焦点；目录项按实际页面数均分行列。
- layer: bg | picture | mask | decoration | overlay | text（决定从底到顶的图层顺序）。
- text 必须给 font{family,weight,color,size} 与 align(left|center|right)；禁止把文字写进图片。
- shape.shape: rect | round_rect | oval | line | triangle | chevron | hexagon |
  diamond | parallelogram | trapezoid；fill/line 为 #RRGGBB 或 null。
- picture: 独立图片图层，给 asset_id 与 content（素材需求描述）。独立品牌徽标须设 asset_kind="emblem"，content 只写本图的图案、轮廓、配色与画法，不得写页名、周边内容或整页排版。逐项配图时，每幅 picture 与对应的一个行标题 text 必填同一 item_key，各行键不同。每个 asset_id 独立于背景及其他项；content 只写该条的可见主体与画法，不要写整页构图、系列数量或其他条目。返修保留行键与素材 ID。
- decorate.primitive: ring | dashed_line | line | diagonal_bar | dot_grid；
  由原生矢量形状绘制，可改色改描边，不要生成位图。
- branch（可选原语，仅当本页内容本身就是「一个主句 + 几个分支」时用）：
  {"type":"branch","branch_type":"arrow|timeline|icon_grid|metric_grid|radial",
   "title":"中心主题（radial 用）","items":[{"num":"01","label":"分支标题","desc":"分支说明"}, ...],
   "position":{...},"layer":"decoration"}
  branch_type 选择：数值指标→metric_grid，num 保留完整数字及单位，label/desc 不带前导分隔符；并列几点→icon_grid；有先后/阶段/历程→timeline 或 arrow；
  一个中心向外展开→radial。items 3~8 项，label<=16字、desc<=80字。
  分支图示是原生矢量形状（双击可改、不依赖生图），所有分支文字由 items 提供，
  不要为分支单独配图；一页里通常只需 1 个 branch 元素表达全部分支。
- 满幅摄影叠加多幅逐项小图时，bg.subject 只描述主照片的镜头、环境、可见主体和具体动作；不要把步骤列表、器具清单、整页创意或文案塞入 bg.subject。小图各写入对应 picture.content；程序按每个文字框的实际坐标为两翼、底部结语和页码生成禁入区，主摄影中的高纹理器物不得侵入，返修时只改主摄影，不覆盖独立小图或改文案。
- 区分画布范围和照片范围：“幻灯片铺满画布／正面平视”不意味着照片铺满整页。仅在用户要求整页摄影时将主摄影放入 bg.image；指定横幅、栏、卡片等局部区域时按该区域坐标创建 picture，用 picture.content 写主体和动作，页面底色独立保留。局部渐隐、波浪与遮罩不得扩到整页。
- 摄影背景 bg.subject 必填，用具体可拍摄的主体和场景解释本页，而不是复述行业。空间页拍空间、座位与长桌，到店页拍门面或入口，不用通用产品静物代替。
- 所有颜色只能取自本次给定的 VI 主色/辅助色/强调色，外加黑 #000000 / 白 #FFFFFF。
- pages[].decorations[] 可为空数组；装饰也可以直接写进 elements[]。

mask_picture（可选原语，只有需要「图片被形状裁切」时才用）字段：
  asset_id、content（素材描述）、position（外框）、layer，
  mask: {
    "shape": "rect|round_rect|oval|triangle|chevron|hexagon|diamond",
    "layout": "single|columns|rows|grid",
    "count": 整数,            // columns/rows 的格数；single 固定 1
    "rows": 整数, "cols": 整数, // grid 的行列数
    "ratios": [0..1, ...],     // 每格高度(columns)或宽度(rows)占外框的比例，长度=count
    "gap_in": 英寸,            // 格间距
    "align": "start|center|end", // 交叉轴对齐：columns 时 end=底部对齐，rows 时 end=右对齐
    "continuous": true|false,  // true=所有格从同一张图连续裁切；false=每格独立素材
    "asset_ids": ["..."],      // continuous=false 时每格各自的 asset_id（可省略，自动生成）
    "cell_prompts": ["..."]    // continuous=false 时每格各自的画面描述（可选）
  }
  layout=single 适合圆形/异形裁一张图；columns/rows/grid + continuous=true 适合
  把一张全景拆成条带或网格；grid + continuous=false 适合多张不同产品图并排。
"""

PRIMITIVE_CATALOG = """可用版式原语（按 brief 自由组合，不需要全用，也不要求都用图片）：
- text        原生文本框，双击可改字（所有文字都走这里）
- shape       原生形状：色块、斜切、圆环、异形、分割线，可改色改粗细
- picture     独立图片图层，可单独删除或替换
- mask_picture 形状内图片填充（异形/条带/网格裁切），仅在 brief 明确要求裁切效果时使用
- decorate    原生矢量装饰（细线圆环、虚线、点阵、斜条）
- branch      矢量分支图示：编号箭头/时间轴/图标网格/中心放射，用于「一个主句 + 几个分支」
- scrim       焦点保护渐变遮罩：盖在背景摄影图上，洗白文字侧、保留图片焦点侧清晰
- toc         目录页：编号+章节名+一句话说明的原生文字条目
- bg          页面背景（一整张摄影图，或 VI 纯色）

=====================================================================
【画册级中年/企业/品牌宣传硬规则】（只要整套 >= 4 页就必须遵守，优先级最高）
=====================================================================
这些是成品画册的通用设计语言，不是某种特定构图模板，务必内建到你的每一次输出：

1) 背景优先用【摄影图 + 渐变遮罩】，不要用纯色大色块顶整页：
   - 页码本身不要求摄影。明确纯文字、独立照片与信息栏的页遵守本页规则，不套摄影背景。要求「摄影背景」或「满幅摄影」的页不得用纯色底、切片或细图带替代，bg 用
     {"type":"image","image":"<asset_id>","color":"<VI浅色>"}，并在文字之下加 1 个 scrim 元素；
   - scrim 的 wash（洗白方向）必须与文字位置一致、与图片焦点相反：
     文字在左 → wash="left"（右侧保留焦点）；文字在右 → wash="right"；
     文字在上 → wash="top"；文字在下 → wash="bottom"；四角用 "bottomleft" 等。
   - scrim 字段：{"type":"scrim","wash":"left|right|top|bottom|bottomleft|bottomright|topleft|topright",
                 "color":"#FFFFFF","strength":0.9~0.97,"position":{"x":0,"y":0,"w":13.333,"h":7.5},
                 "layer":"mask"}
   - 配套的背景图素材 content 必须写清焦点方位，例如「主体/建筑/产品位于画面右侧三分之一，
     左侧大面积浅色留白，前景清晰锐利」，这样遮罩洗左侧不会毁掉右侧焦点。

2) 仅当本页承担目录功能时采用下列规则。不按总页数或第二页的位置添加目录，不替换已指定的正文页；用户明确不要目录时不得添加：
   - 多页导航目录用 1 个 toc 元素；背景与摄影范围遵守本页需求，不因目录而设置 bg.image。仅在文字压在整页摄影上时使用相应 scrim；
   - toc 字段：{"type":"toc","title":"目录","cols":2或3,"items":[
       {"num":"01","label":"章节名（<=10字）","desc":"一句话说明（<=30字）"}, ...],
       "position":{...},"layer":"text"}；
   - 多页导航的 items 对应后续真实页，不含封面和目录，顺序与后文一致。单独交付的目录样页不是整册导航，不创建虚构页面或因无后续页而删除文案；用户指定卡片式版面时，每项标题、编号及副标题用原生 text，图片独立。

3) 历程/发展史页（文案在讲阶段、年份、成长、roadmap 时）：
   - 用 1 个 branch timeline 元素；【每个节点的 desc 必须具体写清「这一阶段做了什么、
     有什么结果/数据」】，不能只填年份或空话；desc 上限放宽到 80 字；
   - 年份/阶段名放 label，具体作为放 desc；例：
       {"num":"2021","label":"实验室成立","desc":"三人小组起步，完成第一版基础模型原型"}；
   - 这样时间轴既是点缀也是完整叙述，不要只画几个空节点。

4) 并列要点页（几个原因/能力/优势）用 icon_grid 或 arrow，每个 item 同样要有实质 desc。

5) 结尾/落款页：若用横向条带图，图片框高度不得小于 2.0 英寸（宽高比 <=6:1），
   否则源图会被裁成看不清的细条；更推荐直接用整页背景图 + scrim 的画册式结尾。

=====================================================================

【内容结构信号 → 自动选版式】
- 并列几点（三个原因、六项能力、几大优势）→ branch icon_grid 或 arrow
- 几段历程（先后、阶段、发展史、roadmap）→ branch timeline，且每节点 desc 写具体
- 一个中心向外展开（以X为核心的几个方向）→ branch radial
- 只是连续叙述、没有并列项 → 普通 text 段，不要套 branch
只在结构确实成立的那一两页用 branch，其余页照常；用户若在需求里指明某页用分支，以用户为准。

常见构图方向只是举例，按 brief 的 creative / tone / industry 选择，不要硬套：
- 全幅摄影背景 + scrim：bg.image + scrim + 文字（封面/目录/结尾首选）
- 居中海报：大标题居中 + 上下装饰线 + 留白
- 对角/斜切切分：triangle / parallelogram / diagonal_bar 与文字分栏
- 左右分栏：一侧整图，一侧文字（洗白侧放文字）
- 多图网格：mask_picture layout=grid, continuous=false
- 圆形/异形裁图：mask_picture layout=single, shape=oval|hexagon
- 纯文字极简：只用 text + shape + decorate，全页零图片（仅当该页明确要极简时用）

配色只用给定 VI 色（外加黑白）。scrim 的洗白末端允许用白色。
"""

# ---------------------------------------------------------------- 步骤 1 规划
PLANNER_SYSTEM = """你是广告宣传PPT的版式规划师。根据品牌需求输出结构化版式JSON，不要画图，不要生成PPT，不要输出解释文字。

硬性规则：
1. 先判断 brief 的 creative / tone / industry 需要哪些版式原语，再决定构图；
   下方契约里的 JSON 只演示字段格式，**不是推荐版式，禁止照抄**。
2. brief 没有明确要求「图片被形状裁切 / 异形裁图 / 条带拼接」时，不要用 mask_picture；
   纯文字版式（text + shape + decorate，零图片）完全合法，别为了凑元素硬加图片。
3. 图层必须分开标注：text、shape、picture、mask_picture、decorate、bg。
4. 每个元素给出：类型、内容、坐标(x,y,w,h，单位英寸)、字体/颜色/字号、素材需求描述。
5. 所有标题先规划为可编辑原生 text（包括封面）；禁止以图片代替标题或把艺术字写入素材需求。仅用户明确点名的标题效果由后续专用步骤处理。其他文字也登记为独立 text。
6. 配色只允许从品牌VI色里取（外加黑白），列出主色/辅助色/强调色。
7. 只输出一个 JSON 对象（不要 markdown 代码块，不要注释），结构严格遵循下方契约。

输出JSON字段：vi、pages[].page_name、pages[].bg、pages[].elements[]、pages[].decorations[]（可为空）。

""" + PRIMITIVE_CATALOG + "\n" + LAYOUT_CONTRACT


def _toc_constraint(brief: dict) -> str:
    if brief.get('toc_required') is False:
        return '全册明确不设目录，不得用目录替换任何正文页。'
    if brief.get('toc_required') is True:
        return '全册需要目录；遵循指定页序，只在负责该页的批次生成一次。'
    return '不按页数增设目录；优先保持已指定的逐页提纲。'


def planner_user(brief: dict[str, Any]) -> str:
    recipe = brief.get("layout_recipe")
    recipe_line = ("指定版式方向：" + str(recipe) + "\n") if recipe else ""
    return (
        "品牌：" + str(brief.get("brand", "")) + "\n"
        "行业：" + str(brief.get("industry", "")) + "\n"
        "VI色值：" + json.dumps(brief.get("vi", {}), ensure_ascii=False) + "\n"
        "这一页主题与创意版式：" + str(brief.get("creative", "")) + "\n"
        + recipe_line + _toc_constraint(brief) + "\n"
        + "文案：" + json.dumps(brief.get("copy", {}), ensure_ascii=False) + "\n"
        "调性：" + str(brief.get("tone", "")) + "\n"
        "页数：" + str(brief.get("pages", 1)) + "\n"
        "可用字体（font.family 只能从这里选，避免 PowerPoint 静默替换导致排版跑偏）："
        + "、".join(preferred_fonts()) + "\n\n"
        "请只输出版式 JSON。"
    )


REWORK_SYSTEM = """你是版式返修师。上一版 PPTX 自检发现下列问题，只修这些问题，不要重做整套。

规则：
- 只调整被点名的元素参数（坐标、字号、颜色、蒙版比例等），保持其余元素不变。
- 不要因为返修就改变整体版式方向、增减原语种类。
- 所有文字仍是独立 text 元素；配色仍只取 VI 色（外加黑白）。
- 输出完整版式 JSON（不是补丁），结构遵循下方契约，不要解释文字。
""" + LAYOUT_CONTRACT


def rework_user(layout: dict, issues: list[str]) -> str:
    return (
        "当前版式 JSON：\n" + json.dumps(layout, ensure_ascii=False) + "\n\n"
        "自检不合格项：\n- " + "\n- ".join(issues) + "\n\n"
        "保留各页原背景与前景素材 ID。bg.subject_position 只是生图意图，返修请按实际截图中的主体和留白排版，不强行压进预设半幅。"
        "请输出修正后的完整版式 JSON。"
    )


# ---------------------------------------------------------------- 多页分批规划
MULTI_PAGE_RULES = """
【多页分批规划】本次只规划下面列出的几页，其余页由别的批次负责，不要替它们生成内容。
- 输出的 pages 数组长度必须严格等于本次要求的页数，顺序一一对应，page_name 用给定页名。
- 按各页 creative / style 构图；同类内容或横向比较页优先复用骨架，标题、图片、指标的位置保持一致。仅在内容结构不同或用户明确要求变化时换构图，不因版式重复而返修。
- 每页至少 3 个元素：主标题 text、副标题或正文 text、以及视觉元素
  （picture / mask_picture / shape / decorate / branch / scrim 均可）。明确写「纯文字 / 零图片 / 无图」的页
  只用 text + shape + decorate，禁止塞图片。
- 用 branch 表达分支的本页：主标题 text + 1 个 branch 元素即可（branch 自带全部分支文字与矢量图形），
  不要再给每个分支塞图片，也不要重复把分支文字抄成 text。历程 branch timeline 的每个 item
  必须写具体 desc（做了什么/结果数据），不要只填年份。
- 文字压在背景摄影图上的页：bg 用 image + 1 个 scrim 元素（wash 与文字侧一致），文字放洗白侧。
- 目录页用 1 个 toc 元素（items 带 label 和 desc），不要手搓一堆 text。
- 每页 copy 原样使用该页给的 title/subtitle/body/cta，全部登记为独立 text 元素，禁止写进图片。
- 坐标在 13.333 x 7.5 英寸画布内，留 0.45 英寸安全边距；文字与图片不要互相压住。
- 结尾页若用横向条带图，图框高度 >=2.0 英寸（宽高比<=6:1），不要压成细条。
- 只输出一个 JSON 对象：{"vi": {...}, "pages": [...]}，不要解释文字。
"""

PAGE_BATCH_SYSTEM = PLANNER_SYSTEM + "\n" + MULTI_PAGE_RULES


def page_batch_user(brief: dict, batch: list[dict], start_index: int, total: int,
                    opening_positions: tuple[str, ...] = ()) -> str:
    """把逐页规格拼成一次小批量规划请求，避免 8 页一次性输出被截断或千篇一律。"""
    lines = [
        "品牌：" + str(brief.get("brand", "")),
        "行业：" + str(brief.get("industry", "")),
        "VI色值：" + json.dumps(brief.get("vi", {}), ensure_ascii=False),
        "全局调性：" + str(brief.get("tone", "")),
        "全册创意与适用范围（逐页例外优先）：" + str(brief.get("creative", "")),
        _toc_constraint(brief),
        f"整套共 {total} 页；本次只规划第 {start_index + 1} ~ {start_index + len(batch)} 页，"
        f"共 {len(batch)} 页。",
        "可用字体（font.family 只能从这里选）：" + "、".join(preferred_fonts()),
        "",
    ]
    for i, sp in enumerate(batch):
        copy = sp.get("copy") or {}
        lines += [
            f"【第 {start_index + i + 1} 页】页名：{sp.get('name', '')}",
            "  构图要求：" + (sp.get("creative") or "按调性自选一个稳妥构图"),
            "  视觉关键词：" + (sp.get("style") or ""),
            "  本页文案：" + json.dumps(copy, ensure_ascii=False),
            "",
        ]
        if start_index + i < len(opening_positions):
            pos = opening_positions[start_index + i]
            lines.append(f"  仅供拍摄参考（本页明确的场景要求优先）：{composition(pos)}。"
                         "不要因预设先缩窄文字、目录或改变阅读顺序；确定成图后才定文字安全区。")
    lines.append("请只输出本次这批页的版式 JSON。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 步骤 2 素材
ASSET_BASE_RULES = (
    "构图干净、主体明确、光线自然，色调与品牌 VI 协调；"
    "画面严禁出现任何文字、字母、数字、logo、水印、边框、UI 元素。"
)
ASSET_KIND_HINTS = {
    "hero": "作为被形状裁切的主视觉，画面要有延展性与连续性，主体不要贴在边缘",
    "bg": (
        "作为整页摄影背景图：主体必须体现本页的具体事件、物件或场所；"
        "其余区域低细节供后续叠字；主体细节锐利、不要虚化、不要贴边；"
        "整幅画面连续完整无文字（焦点方位与留白方位以素材描述里写明的为准）"
    ),
    "photo": "作为独立图片图层，主体居中，适合按给定矩形裁切",
    "cell": "作为多图网格中的独立一格，主体居中、单张即可独立成立",
}
ASSET_HERO_SUFFIX = ASSET_KIND_HINTS["hero"] + "；" + ASSET_BASE_RULES
ASSET_DECORATION_HINT = (
    "透明背景，矢量装饰图案，细线几何圆环/虚线/斜切条，单色取自品牌辅助色，"
    "简约，仅做边角点缀，无阴影，无文字。"
)


def asset_prompt(description: str, kind: str = "photo",
                 tone: str | None = None, industry: str | None = None) -> str:
    """素材提示词与版式无关，只按「元素用途」和 brief 调性拼接。"""
    if kind == 'emblem':
        target, _ = json.JSONDecoder().raw_decode(description)
        return (f"仅绘制一个独立品牌徽标。图案及风格：{target['design']}。"
                '徽标居中、轮廓完整清晰，独立图形之外留空。不做页面缩略图、版面、拼贴或场景摄影；'
                '不要文字、数字、水印或其他标志。')
    if kind == 'storefront':
        # 只投射照片本身的拍摄特征；即便描述含店名，或返修意见引用错字，也不复述它们。
        shot, _ = json.JSONDecoder().raw_decode(description)
        details = shot['framing']
        return ('街道对面的独立门面纪实照片，完整拍到门头至地面，建筑垂直线端正。'
                + (f'取景特征：{details}。' if details else '')
                + '门楣保持素面，玻璃、雨篷和墙面干净；不设可辨识招牌、店名、标识或仿字。'
                + ASSET_BASE_RULES)
    if kind == 'bg' and description.startswith('{'):
        # 与条目请求相同：只消费首个结构化目标，返修原因和全局文案不参与生图。
        target, _ = json.JSONDecoder().raw_decode(description)
        return (f"满幅连续摄影，唯一主场景：{target['subject']}。{target['framing']}"
                f'焦点清晰，不画步骤插图、不拼贴。{ASSET_BASE_RULES}')
    if kind == 'item':
        # 只消费结构化单条目标。编排器追加的自由文本返修原因留作审计，不作为画面指令。
        target, end = json.JSONDecoder().raw_decode(description)
        retry = '重新构图，使指定主体占据主要画面，不以小卡片或陪衬呈现。' if description[end:].strip() else ''
        return (f"唯一画面目标：{target['subject']}。对应条目（仅供识别，不入画）：{target['row']}。"
                f'只画这一条的独立插图，主体居中，不做拼贴、组图或整页场景。{retry}{ASSET_BASE_RULES}')
    base = (description or "品牌主题视觉").strip()
    if kind == "decoration":
        return base + "。" + ASSET_DECORATION_HINT
    head = [base]
    if tone:
        head.append("调性：" + tone)
    if industry:
        head.append("行业：" + industry)
    hint = ASSET_KIND_HINTS.get(kind, ASSET_KIND_HINTS["photo"])
    if kind == "bg" and base.startswith(OPENING_BG_PREFIX):
        hint = "连续完整的宽幅摄影画面，主体细节清晰锐利，不做拼贴或分栏"
    tail = [hint, ASSET_BASE_RULES]
    return "，".join(head) + "。" + "；".join(tail) + "。"


# ---------------------------------------------------------------- 步骤 3 构建
BUILDER_SYSTEM = """【输出原生可编辑 .pptx，全程禁止把文字烤进图片】
1. 文字：全部用独立原生文本框，双击可改；标题与正文字重、字号拉开层次。
2. 基础图形：色块、斜切块、圆环、分割线、圆角容器、异形，用PPT原生形状，可改颜色和粗细。
3. 形状图片填充（mask_picture，可选原语）：先建无描边形状，再对形状做图片填充，
   图片只在形状内部显示；单形状/条带/网格由 mask.layout 决定，不要直接并排插整张图。
   版式 JSON 里没有 mask_picture 时，这一步根本不执行。
4. 独立图片用 picture 图层；装饰用原生矢量形状，可单独删除/替换/改色。
5. 图层顺序（从底到顶）由 layer 字段决定：背景 → 图片/蒙版 → 装饰 → 遮罩 → 文字。
6. 自检：文字是否独立可选中；形状是否可选中；图片是否分层；有没有文字落在图片内部；配色是否符合VI。
7. 先确认版式JSON无误，再逐页生成；哪一页失败只返修那一页，不要整套重做。

实现备注（重要）：python-pptx 与 pptx-mcp-server 都没有 shape.fill.picture 接口，
形状图片填充必须用 OOXML a:blipFill + r:embed 图片关系实现，本项目已封装在 ppt/builder.py。
"""


# ---------------------------------------------------------------- 步骤 4 自检
REVIEWER_SYSTEM = """打开生成的pptx逐页检查，按以下规则输出报告：
- 文字能否单独选中编辑；
- 图片区域是否真的显示了图片；有形状图片填充的元素则检查是否是形状内填充（而非并排原图）；
- 有无元素重叠、文字被图压住、对比度不够；
- 装饰是否抢主视觉；配色是否超出VI；
- 背景/主视觉/装饰/文字是否分层。
列出不合格页面和具体问题，回到步骤1调整对应参数后重新生成该页。

注意：检查项必须按该页实际用到的原语来定。纯文字版式没有图片，就不要要求出现图片；
没有形状图片填充，就不要检查蒙版。

本编排器用 LibreOffice 真实渲染 PPTX -> PDF -> 逐页 PNG 再交给多模态模型评审；
同时用 PyMuPDF 抽取 PDF 文本层，与版式 JSON 的文字逐条比对，
作为「文字没有被烤进图片」的硬证据。
"""


# ---------------------------------------------------------------- 步骤 4b 视觉评审（多模态）
VISION_REVIEW_BASE = """你是广告PPT视觉评审员。你会收到一张由 LibreOffice 真实渲染的 PPT 页面截图，以及该页的版式 JSON 摘要。
只看图说话，按下方「本页适用规则」逐条检查，只输出一个 JSON 对象，不要解释文字、不要 markdown 围栏。

输出格式：
{"page_index":1,"checks":[{"rule":"规则名","ok":true,"detail":"..."}],
 "overall":"pass|needs_rework","issues":["..."],"rework_hints":["具体到坐标/字号/颜色的修改建议"]}

判分原则：
- 只检查本页确实存在的元素与原语；不要因为页面缺少某种构图（例如没有柱状蒙版、没有图片）而判为不合格。
- 截图分辨率有限，不要把压缩噪点当成缺陷；只报确实影响交付的问题。
"""
# 兼容旧导入名：通用基础规则（不含任何特定版式假设）
VISION_REVIEW_SYSTEM = VISION_REVIEW_BASE + """
本页适用规则：
- text_readable：文字清晰可读，没有被遮挡、没有意外换行挤在一起、对比度足够
- no_overlap：元素之间没有意外重叠，文字没有被图片压住
- hierarchy：标题与正文的字号/字重层次拉得开
- decoration_not_dominant：装饰没有抢主视觉
- vi_compliance：配色没有超出给定的 VI 主色/辅助色/强调色（黑白除外）
- layering：背景 / 主视觉 / 装饰 / 文字分层清晰
- safe_margin：文字离页面边缘有足够留白
"""


def vision_review_system(page: dict) -> str:
    """按本页实际用到的原语动态生成检查规则，避免拿一种版式去套所有页面。"""
    rules = [
        ("text_readable", "文字清晰可读，没有被遮挡、没有意外换行挤在一起、对比度足够"),
        ("text_visibility", "每个文字元素从首字到末字都完整可见：没有一部分落在同色或低对比的色块上而隐形，斜切/异形色块边缘不要切掉首尾字"),
        ("no_overlap", "元素之间没有意外重叠，文字没有被图片压住"),
        ("hierarchy", "标题与正文的字号/字重层次拉得开"),
        ("decoration_not_dominant", "装饰没有抢主视觉"),
        ("vi_compliance", "配色没有超出给定的 VI 主色/辅助色/强调色（黑白除外）"),
        ("layering", "背景 / 主视觉 / 装饰 / 文字分层清晰"),
        ("safe_margin", "文字离页面边缘有足够留白"),
    ]
    elements = page.get("elements", [])
    has_mask = any(e["type"] == "mask_picture" for e in elements)
    has_scrim = any(e["type"] == "scrim" for e in elements)
    has_toc = any(e["type"] == "toc" for e in elements)
    has_image = page.get("bg", {}).get("type") == "image" or any(
        e["type"] in {"picture", "mask_picture"} for e in elements
    )
    if has_image:
        rules.append(("image_filled", "图片区域确实显示了图像内容，不是空白、纯色块或错位的整张原图"))
    if has_scrim:
        rules.append(("scrim_focus_protected", "渐变遮罩只洗白文字所在的浅色侧，图片主体/焦点在另一侧保持清晰锐利，没有被洗白盖住"))
    if has_toc:
        rules.append(("toc_readable", "目录的每个编号、章节名和说明都清晰可读，层级分明，没有与图片焦点重叠"))
    if has_mask:
        rules.append(("shape_picture_fill", "图片显示在形状轮廓内部（形状裁切），而不是旁边并排的一张原图"))
        continuous = [
            e for e in elements
            if e["type"] == "mask_picture" and e["mask"].get("continuous")
            and e["mask"].get("layout") in {"columns", "rows", "grid"}
        ]
        if continuous:
            rules.append(("mask_continuity", "同属一张连续全景的各格画面衔接自然，没有整图重复或明显错位"))

    lines = [VISION_REVIEW_BASE, "本页适用规则："]
    lines += [f"- {name}：{desc}" for name, desc in rules]
    if not has_mask:
        lines.append("本页没有形状图片填充元素：不要检查 shape_picture_fill / mask_continuity，"
                     "也不要因为缺少柱状或网格蒙版而判为不合格。")
    if not has_image:
        lines.append("本页是纯文字/纯形状版式：不要检查 image_filled，零图片是合法设计。")
    lines.append("checks 数组必须且只能包含上面列出的规则名。")
    return "\n".join(lines)


def toc_acceptance(page: dict) -> str:
    for el in page.get('elements', []):
        items = el.get('items', [])
        if el.get('type') == 'toc' and items and all(isinstance(it.get('target_page'), int) for it in items):
            entries = [(it['num'], it['label'], it['target_page']) for it in items]
            return ('目录页数与编号只按已核对的实际正文清单验收（项号、标题、目标物理页）：'
                    + json.dumps(entries, ensure_ascii=False) + '。忽略与此冲突的示例项数、项号；'
                    '分组范围须对应现有项；仍按原要求核对列数、可读性和版式。')
    return ''


def vision_review_user(page_index: int, vi: dict, page: dict) -> str:
    summary = [
        {"type": e["type"], "content": e["content"][:40], "position": e["position"],
         "note": e["note"][:60]}
        for e in page.get("elements", [])
    ]
    types = sorted({e["type"] for e in page.get("elements", [])})
    return (
        f"第 {page_index} 页：{page.get('page_name', '')}\n"
        f"VI 色：{vi}\n"
        f"背景：{page.get('bg', {})}\n"
        f"本页实际原语：{types}\n"
        f"元素摘要：{json.dumps(summary, ensure_ascii=False)}\n\n"
        + toc_acceptance(page) + "\n请逐条核对本页适用规则，报告所有可辨认的独立问题，不只报最严重的一处；给出位置、受影响元素及截图证据。只输出 JSON。"
    )
