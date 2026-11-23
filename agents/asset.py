"""Agent 2 / 步骤 2：素材生成（无文字主视觉 + 装饰 SVG 参考文件）。"""
from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import copy, deepcopy
from pathlib import Path

from a2a.base import BaseAgent
from a2a.types import AgentSkill, Task, data_part, file_part, json_dump, text_part
import config
from ppt.decorations import svg_source_for_page
from ppt.schema import asset_requests
from ppt.slicing import mask_geometry, prepare_mask_slices, src_rect_for
from services.asset_candidates import similarity_groups, write_gallery
from services.llm import LLMClient
from services.mock_data import mock_image
from services.prompts import asset_prompt


class AssetAgent(BaseAgent):
    NAME = "asset"
    VERSION = "1.0.0"
    DESCRIPTION = "按版式 JSON 的 asset_id 逐个生成无文字主视觉/背景图，并落地装饰 SVG 参考文件"
    SKILLS = [
        AgentSkill(
            id="generate_assets",
            name="素材生成",
            description="调绘图模型出主视觉；装饰用原生矢量形状而不是位图；图与 JSON 元素一一对应",
            input_schema={"layout": "object"},
            output_schema={"assets": "map<asset_id, path>", "manifest": "02_assets/manifest.json"},
        ),
    ]

    def _can_parallelize(self, reqs, n_cand):
        if (os.getenv('A2A_IMAGE_WORKERS', '1').strip() != '2' or n_cand != 1
                or len(reqs) < 2 or self.ctx.assets or self.ctx.rework_hints or self.ctx.review
                or type(self.ctx.llm) is not LLMClient):
            return False
        # 未知请求、含路径的 ID、大小写重名或非全新素材目录，均不假定输出独立。
        directory = self.ctx.root / '02_assets'
        if directory.exists() and any(directory.iterdir()):
            return False
        ids = [req['asset_id'] for req in reqs]
        return (all(re.fullmatch(r'[\w-]+', aid) and not Path(aid + '.png').is_reserved() for aid in ids)
                and len({aid.casefold() for aid in ids}) == len(ids)
                and all(r['kind'] in {'bg', 'photo', 'storefront', 'item', 'hero', 'cell'} for r in reqs))

    def _images(self, reqs, n_cand):
        parallel = self._can_parallelize(reqs, n_cand)
        manifest = []
        with ExitStack() as stack:
            if parallel:
                # requests.Session 不能跨线程共享；保留端点、模型和会话配置。
                peer = copy(self.ctx.llm)
                try:
                    peer.session = stack.enter_context(deepcopy(self.ctx.llm.session))
                except Exception as exc:
                    parallel = False
                    self.log('无法隔离生图会话，改为串行', error=str(exc))
                else:
                    pool = stack.enter_context(ThreadPoolExecutor(max_workers=2))
            index = 0
            while index < len(reqs):
                batch = reqs[index:index + (2 if parallel else 1)]
                pending = []
                for slot, req in enumerate(batch):
                    aid = req['asset_id']
                    prompt = asset_prompt(req['prompt'], req['kind'], tone=req.get('tone'), industry=req.get('industry'))
                    out = self.ctx.path('02_assets', f'{aid}.png')
                    future = None
                    if len(batch) == 2:
                        self._log_image(req, 0, n_cand)
                        client = peer if slot else self.ctx.llm
                        future = pool.submit(client.image, prompt, out, size=req['size'])
                    pending.append((req, prompt, out, future))
                for req, prompt, out, future in pending:
                    entry, limited = self._finish_image(req, prompt, out, n_cand, future)
                    manifest.append(entry)
                    if parallel and limited:
                        parallel = False
                        self.log('生图触发限流，剩余素材改为串行', asset_id=req['asset_id'])
                index += len(batch)
        return manifest

    def _log_image(self, req, k, n_cand):
        self.log('生图', asset_id=req['asset_id'], candidate=f'{k + 1}/{n_cand}',
                 model=getattr(self.ctx.llm, 'image_model', '?'), size=req['size'])

    def _finish_image(self, req, prompt, out, n_cand, future):
        aid = req['asset_id']
        cand_dir = self.ctx.path('02_assets', 'candidates', aid)
        cand_paths = []
        sources = {}
        limited = False
        for k in range(n_cand):
            target = out if n_cand == 1 else cand_dir / f'c{k + 1}.png'
            if future is None:
                self._log_image(req, k, n_cand)
            try:
                if future is None:
                    self.ctx.llm.image(prompt, target, size=req['size'])
                else:
                    future.result()
                sources[target.name] = 'mock' if self.ctx.mock else 'llm'
            except Exception as exc:  # noqa: BLE001
                if req.get('item_target') and not self.ctx.mock:
                    raise RuntimeError(f'{aid} 单条插图生成失败，不能用占位图代替') from exc
                limited = (getattr(getattr(exc, 'response', None), 'status_code', None) == 429
                           or bool(re.search(r'\b429\b|rate[_ -]?limit|too many requests', str(exc), re.I)))
                self.log('生图失败，降级本地占位图', asset_id=aid, candidate=k + 1, error=str(exc))
                mock_image(target, size=(1536, 1024), seed=len(aid) + 3 + k)
                sources[target.name] = 'local-fallback'
            cand_paths.append(str(target))

        if n_cand > 1:
            # 默认采用第 1 张，其余候选留待人工挑选。
            if Path(cand_paths[0]).exists():
                self.ctx.path('02_assets').mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(cand_paths[0], out)
            groups = similarity_groups(cand_paths)
            if groups:
                self.log('候选相似度告警', asset_id=aid, groups=groups)
        self.ctx.assets[aid] = str(out)
        entry = {'asset_id': aid, 'path': str(out), 'prompt': prompt,
                 'kind': req['kind'], 'source': sources[Path(cand_paths[0]).name],
                 'page': req['page'], 'theme': req['theme'],
                 'sha256': hashlib.sha256(out.read_bytes()).hexdigest()}
        if 'item_target' in req:
            entry['item_target'] = req['item_target']
        if req['kind'] == 'bg':
            bg = self.ctx.layout['pages'][req['page'] - 1]['bg']
            bg['asset_source'] = entry['source']
            bg['asset_sha256'] = entry['sha256']
        if n_cand > 1:
            entry.update(candidates=cand_paths, candidate_sources=sources, similar_groups=groups)
        return entry, limited

    def handle(self, task: Task) -> None:
        reqs = asset_requests(self.ctx.layout or {}, self.ctx.brief)
        # 候选数：>1 时开启 best-of-N + 人工挑图检查点；mock 模式固定 1（可复现、不耗时）
        n_cand = 1 if self.ctx.mock else config.IMAGE_CANDIDATES
        manifest = self._images(reqs, n_cand)

        if n_cand > 1:
            gallery = write_gallery(self.ctx.root, manifest)
            self.log("挑图检查点就绪", gallery=str(gallery),
                     picks=str(self.ctx.path("02_assets", "picks.json")))

        # 形状图片填充的预裁切片：避免依赖渲染器对 a:srcRect 的支持（LibreOffice 实测会忽略）。
        # 连续模式共用一张全景，独立模式每格用自己的素材；几何按版式规格通用计算。
        slices_info = []
        for page in (self.ctx.layout or {}).get("pages", []):
            for el in page["elements"]:
                if el["type"] != "mask_picture":
                    continue
                paths = prepare_mask_slices(el, self.ctx.assets,
                                            self.ctx.path("02_assets", "slices"))
                aid = el["asset_id"]
                for i, sp in enumerate(paths):
                    if sp:
                        self.ctx.assets[f"{aid}__{i}"] = sp
                slices_info.append({
                    "asset_id": aid,
                    "layout": el["mask"]["layout"],
                    "continuous": el["mask"]["continuous"],
                    "count": len(paths),
                    "slices": [p for p in paths if p],
                    "geometry": mask_geometry(el),
                    "src_rect_fallback": src_rect_for(el),
                })
                self.log("蒙版预裁完成", asset_id=aid, layout=el["mask"]["layout"],
                         cells=len(paths))

        # 装饰 SVG 参考文件（Builder 实际画的是原生矢量形状）
        svg_files = []
        for i, page in enumerate((self.ctx.layout or {}).get("pages", [])):
            svg = svg_source_for_page(page)
            if "<circle" in svg or "<line" in svg or "<rect" in svg:
                p = self.ctx.path("02_assets", f"decorations_p{i + 1}.svg")
                p.write_text(svg, encoding="utf-8")
                svg_files.append(str(p))

        mpath = self.ctx.path("02_assets", "manifest.json")
        payload = {"images": manifest, "mask_slices": slices_info, "decoration_svg": svg_files}
        mpath.write_text(json_dump(payload), encoding="utf-8")
        self.log("素材就绪", images=len(manifest), svg=len(svg_files))
        self.artifact(
            task, "assets",
            data_part(payload),
            file_part(mpath, "application/json"),
            text_part(f"images={len(manifest)}, slices={sum(s['count'] for s in slices_info)}, svg={len(svg_files)}"),
            stage=2, kind="file-set",
        )
