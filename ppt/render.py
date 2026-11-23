"""逐页真实截图：PPTX -> (内部临时 PDF) -> PNG，并抽取文本层证据。

注意：这里的 PDF **不是交付物**，只在系统临时目录里存在一瞬间，用完即删。
交付物始终只有 python-pptx 直接生成的 deliverable.pptx，与 LibreOffice / WPS 无关。
它们只是被借来把 PPTX 渲染成图片，好让第 4 步自检能「看到」页面。

渲染引擎由 config.RENDERER 选择：
  libreoffice（默认）：C:\\Program Files\\LibreOffice\\program\\soffice.exe headless，本机实测可用。
  wps / auto        ：WPS 自带 kwpsconvert.exe ppt2pdf，产出更贴近国内交付的原厂渲染。
                      WPS 装在用户自定义路径（目录名含全角字符），且 ppt2pdf 要求
                      登录 WPS 账号并具备转换权限；失败会自动回退 LibreOffice。

WPS 的 COM ProgID（KWPP.Application / PowerPoint.Application）在本机也已注册，
但自动化实例化属于需要用户本机授权的操作，本项目不依赖它。

PDF 文本层是「文字没被烤进图片」的硬证据：能从 PDF 里抽出的文字，
必然是 PPTX 里的原生文本对象，而不是位图里的像素。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import config

SOFFICE_PATHS = [
    "C:/Program Files/LibreOffice/program/soffice.exe",
    "C:/Program Files (x86)/LibreOffice/program/soffice.exe",
    "C:/Program Files/LibreOffice/program/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/opt/libreoffice/program/soffice",
]

# WPS 常被装到自定义目录，且目录名可能是全角字符，不能只查 C 盘默认路径。
WPS_BASES = [
    "F:/ｓｏｆｔ/wps/WPS Office",
    "C:/Program Files/WPS Office",
    "C:/Program Files (x86)/WPS Office",
    "C:/Program Files (x86)/Kingsoft/WPS Office",
    str(Path.home() / "AppData/Local/Kingsoft/WPS Office"),
]


def soffice_executable() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for p in SOFFICE_PATHS:
        if Path(p).exists():
            return p
    return None


def wps_converter_executable() -> str | None:
    """找 kwpsconvert.exe（wpscli ppt2pdf 的入口）。"""
    for base in WPS_BASES:
        b = Path(base)
        if not b.exists():
            continue
        hits = sorted(b.glob("*/office6/kwpsconvert.exe")) + sorted(
            b.glob("office6/kwpsconvert.exe")
        )
        if hits:
            return hits[-1].as_posix()
    return None


def wps_diagnostics() -> dict[str, Any]:
    """把 WPS 的可用状态写进自检报告，避免「装了却没被发现」这种误判。"""
    converter = wps_converter_executable()
    com_ids = []
    try:
        import winreg

        for key in (r"KWPP.Application\CLSID", r"PowerPoint.Application\CLSID"):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key):
                    com_ids.append(key.split("\\")[0])
            except OSError:
                pass
    except ImportError:
        pass
    return {
        "converter": converter,
        "converter_found": bool(converter),
        "com_progids": com_ids,
        "cli_requires": "WPS 账号登录 + 转换权限（未登录返回 exit 100，权限不足返回 101）",
        "configured": config.RENDERER,
    }


def wps_to_pdf(pptx: Path, out_pdf: Path, exe: str, timeout: int) -> tuple[bool, str]:
    """用 WPS 演示原厂引擎导出 PDF。输出目录独立，避免 WPS 自动改名产生 file(1).pdf。"""
    tmp = Path(tempfile.mkdtemp(prefix="_wps_", dir=str(out_pdf.parent)))
    try:
        target = tmp / (pptx.stem + ".pdf")
        cmd = [
            exe, "ppt2pdf", str(pptx), "-o", str(target),
            "--json", "--timeout", str(timeout),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"启动失败: {exc}"
        if proc.returncode != 0 or not target.exists():
            msg = (
                proc.stdout.decode("utf-8", "ignore")
                + proc.stderr.decode("utf-8", "ignore")
            ).strip()[:260]
            return False, f"exit={proc.returncode} {msg}"
        shutil.copy2(target, out_pdf)
        return True, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _libreoffice_pdf(pptx: Path, out_pdf: Path, work_dir: Path, timeout: int) -> tuple[bool, str]:
    """headless soffice 转 PDF。每次用独立临时目录，避开 profile 锁与旧 PDF。"""
    exe = soffice_executable()
    if not exe:
        return False, "未检测到 LibreOffice / PowerPoint"
    for stale in list(work_dir.glob("_lo_*")):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    tmp = Path(tempfile.mkdtemp(prefix="_lo_", dir=str(work_dir)))
    try:
        profile = tmp / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        profile_uri = "file:///" + profile.as_posix().lstrip("/")
        cmd = [
            exe, "--headless", "--norestore",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to", "pdf", "--outdir", str(tmp), str(pptx),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"LibreOffice 启动失败: {exc}"
        tmp_pdf = tmp / (pptx.stem + ".pdf")
        if not tmp_pdf.exists():
            return False, (
                f"LibreOffice 未产出 PDF (exit={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'ignore')[:200]}"
            )
        shutil.copy2(tmp_pdf, out_pdf)  # 覆盖上一轮 PDF，保证截图与本次 PPTX 一致
        return True, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def detect_renderer() -> str | None:
    if soffice_executable():
        return "libreoffice"
    # 注意：WPS 也会注册 PowerPoint.Application，不能仅凭该键判断装了 MS PowerPoint。
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"PowerPoint.Application\CLSID"):
            try:
                import win32com.client  # noqa: F401

                return "powerpoint-com"
            except ImportError:
                pass
    except (OSError, ImportError):
        pass
    return None


def try_render(pptx_path: str | Path, out_dir: str | Path, timeout: int = 180, dpi: int = 110) -> dict[str, Any]:
    """产出逐页 PNG 与文本层证据，供自检使用。

    PDF 只是「PPTX -> 逐页截图」的内部中间件：放在系统临时目录里用完即删，
    **不会写进交付目录，也不是交付物**。交付物始终只有 deliverable.pptx。
    """
    pptx = Path(pptx_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    renderer: str | None = None
    diag = wps_diagnostics()
    pdf_note = "PDF 仅为逐页截图与文本层校验的内部中间件，已在临时目录用完即删；交付物只有 deliverable.pptx"

    with tempfile.TemporaryDirectory(prefix="a2a_render_") as td:
        work = Path(td)
        pdf = work / (pptx.stem + ".pdf")

        wps_exe = diag["converter"]
        if config.RENDERER in {"wps", "auto"} and wps_exe:
            ok, reason = wps_to_pdf(pptx, pdf, wps_exe, timeout)
            if ok:
                renderer = "wps"
                notes.append("WPS 演示原厂渲染")
            else:
                notes.append(f"WPS 渲染不可用（{reason}），已回退 LibreOffice")

        if renderer is None:
            ok, reason = _libreoffice_pdf(pptx, pdf, work, timeout)
            if not ok:
                return {
                    "status": "skipped",
                    "reason": reason + "；跳过逐页截图，仅做 OOXML 结构自检",
                    "images": [], "page_texts": [],
                    "renderer": None, "pdf": None, "pdf_note": pdf_note,
                    "wps": diag, "fallback_notes": notes,
                }
            renderer = "libreoffice"

        try:
            import pymupdf
        except ImportError:
            return {
                "status": "partial", "renderer": renderer, "pdf": None,
                "pdf_note": pdf_note, "images": [], "page_texts": [],
                "reason": "PDF 已生成，但未装 PyMuPDF", "wps": diag,
                "fallback_notes": notes,
            }

        doc = pymupdf.open(str(pdf))
        images, page_texts = [], []
        for i, page in enumerate(doc):
            png = out / f"slide_{i + 1}_real.png"
            page.get_pixmap(dpi=dpi).save(str(png))
            images.append(str(png))
            page_texts.append(page.get_text())
        size = {"w": doc[0].rect.width, "h": doc[0].rect.height} if len(doc) else {}
        doc.close()

    return {
        "status": "ok",
        "renderer": renderer,
        "pdf": None,
        "pdf_note": pdf_note,
        "images": images,
        "page_texts": page_texts,
        "page_count": len(images),
        "page_size_pt": size,
        "dpi": dpi,
        "wps": diag,
        "fallback_notes": notes,
    }
