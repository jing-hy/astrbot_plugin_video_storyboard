"""
AstrBot 视频分镜插件（video_storyboard / video_zoom / video_probe / video_locate）

把一段视频变成一张带编号的宫格图 + 一份可引用的时间轴清单，让模型真的"看到"画面，
而不是只读别人写的摘要。引擎复用 dsh-video-storyboard（MIT，SAOS-3624）的纯 Node 管线，
本文件只做 AstrBot 侧的协议翻译：

    @filter.llm_tool  ->  写 spec.json  ->  node engine/runner.mjs  ->  PNG + 清单
                       ->  mcp.types.CallToolResult(ImageContent + TextContent)

图片会进 AstrBot 的 tool_image_cache，以 data URL 回灌给模型（要求该 provider 勾了「图像」能力）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # AstrBot 4.x 自带 mcp，正常都能导入
    import mcp.types as mcp_types
except Exception:  # pragma: no cover
    mcp_types = None

PLUGIN_DIR = Path(__file__).resolve().parent
ENGINE_DIR = PLUGIN_DIR / "engine"
RUNNER = ENGINE_DIR / "runner.mjs"

URL_RE = re.compile(r"https?://[^\s<>\"'）)]+", re.IGNORECASE)
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".flv", ".avi", ".m4v", ".ts", ".m3u8"}


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


@register(
    "astrbot_plugin_video_storyboard",
    "jing-hy",
    "把视频变成可寻址的编号宫格 + 时间轴清单，让多模态模型直接看画面（分镜/近看/探元数据/按台词定位）",
    "1.0.0",
    "https://github.com/jing-hy/astrbot_plugin_video_storyboard",
)
class VideoStoryboardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._node: str | None = None
        self._lock = asyncio.Lock()
        self._work_root = Path(tempfile.gettempdir()) / "astrbot-video-storyboard"
        self._work_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        self._node = self._resolve_binary("node_path", "node")
        if not self._node:
            logger.error(
                "[video-storyboard] 找不到 Node.js。请安装 Node.js >= 20，"
                "或在插件配置里填写 node_path（例如 C:\\Program Files\\nodejs\\node.exe）。"
            )
        else:
            logger.info(f"[video-storyboard] 引擎就绪，node = {self._node}")

    async def terminate(self) -> None:
        # 清理一周前的临时产物，避免 temp 越堆越大
        cutoff = time.time() - 7 * 86400
        try:
            for child in self._work_root.iterdir():
                try:
                    if child.stat().st_mtime < cutoff:
                        shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            pass

    # ------------------------------------------------------------------ 工具

    @filter.llm_tool(name="video_storyboard")
    async def video_storyboard(
        self,
        event: AstrMessageEvent,
        input: str = "",
        start: float = 0,
        end: float = 0,
        cells: int = 0,
        sheets: int = 0,
        detail: str = "med",
        transcript: str = "auto",
        transcript_file: str = "",
        text_only: bool = False,
        source_hash: bool = False,
    ) -> Any:
        """把视频变成一张带编号的宫格图 + 每个格子的时间轴清单，让模型直接看到画面而不是只读摘要。

用什么场景：用户发来视频链接（B 站/YouTube 等）或本地视频，问"讲了什么/第几秒发生了什么/截图里是什么/这个教程怎么做"。
返回：1-4 张宫格图（每格左上角有编号，底部有音频包络条）和一份文本清单，清单里写明每个格子的起止时间，可以据此再调 video_zoom 近距离重看。

Args:
    input(string): 视频的本地绝对路径，或 http(s) 链接（B 站等支持）。留空则自动使用当前消息里附带的视频、文件或链接。
    start(number): 只看这一段，起点（秒），默认 0。
    end(number): 终点（秒），默认 0 表示到结尾。
    cells(number): 每张图几格，1-16，默认 0 表示按内容自动决定（快切素材会给更多格）。
    sheets(number): 出几张图，1-4，默认 0 表示自动。每多一张图就多花一份图像 token。
    detail(string): 清单里每格保留多少台词：low=60 字，med=120 字，high=400 字（可逐句引用）。默认 med。
    transcript(string): auto=有字幕轨就用，没有就调 ASR；off=完全跳过语音。
    transcript_file(string): 已有字幕文件路径（SRT，或 [{start,end,text}] / B 站 {body:[...]} 形式的 JSON），给了就不抽字幕也不跑 ASR。
    text_only(boolean): 只返回文本清单，不生成图片。用于先便宜地分页浏览长片。
    source_hash(boolean): 额外计算源文件 SHA-256（会完整读一遍文件，大文件慎用）。
        """
        args = {
            "input": input.strip() if input else "",
            "from": float(start or 0),
            "to": float(end or 0),
            "cells": int(cells or 0),
            "sheets": int(sheets or 0),
            "detail": detail if detail in {"low", "med", "high"} else "med",
            "transcript": transcript if transcript in {"auto", "off"} else "auto",
            "textOnly": bool(text_only),
            "sourceHash": bool(source_hash),
        }
        if transcript_file:
            args["transcriptFile"] = transcript_file.strip()
        return await self._call_engine(event, "storyboard", args)

    @filter.llm_tool(name="video_zoom")
    async def video_zoom(
        self,
        event: AstrMessageEvent,
        input: str = "",
        start: float = 0,
        end: float = 0,
        cell_id: int = 0,
        cells: int = 6,
        min_gap: float = 1.2,
    ) -> Any:
        """把视频的一小段用更少的格子、更大的尺寸重看一遍，看清字幕、人脸、界面文字等细节。

什么时候用：video_storyboard 给过清单之后，想看清其中某一格（传 cell_id，编号来自宫格左上角），或者想细看某几秒（传 start/end）。
注意：窗口要短（几秒到一分钟），太长就没有细节增益了。

Args:
    input(string): 与 video_storyboard 用同一个视频（本地路径或链接）。留空则自动取当前消息里的视频。
    start(number): 窗口起点（秒）。用 cell_id 时不需要传。
    end(number): 窗口终点（秒）。用 cell_id 时不需要传。
    cell_id(number): 上一次 video_storyboard 里某一格的编号（从 1 开始），自动换算成该格的起止时间。
    cells(number): 这段出几格，1-9，默认 6。格子越少每格越大。
    min_gap(number): 采样最小间隔（秒），默认 1.2。看快剪素材可以调到 0.4。
        """
        args = {
            "input": input.strip() if input else "",
            "from": float(start) if start else 0,
            "to": float(end) if end else 0,
            "cells": int(cells or 6),
            "minGap": float(min_gap or 1.2),
            "adaptive": False,
        }
        if cell_id:
            args["cellId"] = int(cell_id)
        return await self._call_engine(event, "zoom", args)

    @filter.llm_tool(name="video_probe")
    async def video_probe(self, event: AstrMessageEvent, input: str = "") -> Any:
        """只读视频的容器信息（时长、分辨率、帧率、有没有音轨、有没有字幕轨），不出图也不解码，几乎零成本。

什么时候用：动手分析前先确认"这视频多长、该按什么粒度读"；或者用户只问视频参数。

Args:
    input(string): 本地绝对路径或 http(s) 链接。留空则自动取当前消息里的视频。
        """
        return await self._call_engine(event, "probe", {"input": (input or "").strip()})

    @filter.llm_tool(name="video_locate")
    async def video_locate(
        self,
        event: AstrMessageEvent,
        query: str,
        input: str = "",
        start: float = 0,
        end: float = 0,
        max: int = 5,
    ) -> Any:
        """在视频的台词转写里找一句话出现在第几秒，纯文本、不出图、不花图像 token。

什么时候用：记得大概说了什么但不知道时间点（"他说到某句话的时候是第几分钟"）。找到的时间段可以直接喂给 video_zoom。

Args:
    query(string): 要找的台词或关键词，中文英文都行。
    input(string): 本地路径或链接。留空则自动取当前消息里的视频。
    start(number): 只在这之后找（秒）。
    end(number): 只在这之前找（秒）。
    max(number): 最多返回几条，1-20，默认 5。
        """
        args = {
            "input": (input or "").strip(),
            "query": query,
            "from": float(start) if start else 0,
            "to": float(end) if end else 0,
            "max": int(max or 5),
        }
        return await self._call_engine(event, "locate", args)

    # ------------------------------------------------------------------ 命令（不走 LLM）

    @filter.command("分镜")
    async def cmd_storyboard(self, event: AstrMessageEvent):
        """分镜 <视频链接或本地路径> [格数]：直接生成宫格图发到当前会话（不需要多模态模型）"""
        text = (event.message_str or "").strip()
        parts = text.split()
        target = ""
        cells = 0
        for token in parts[1:]:
            if token.isdigit() and not target:
                cells = int(token)
            elif not target and (URL_RE.match(token) or Path(token).exists()):
                target = token
        if not target:
            matched = URL_RE.search(event.message_str or "")
            target = matched.group(0) if matched else ""
        if not target:
            yield event.plain_result(
                "用法：/分镜 <视频链接或本地绝对路径> [格数]\n例如：/分镜 https://www.bilibili.com/video/BVxxxx 9"
            )
            return

        tempdir = await asyncio.to_thread(Path, self._work_root / uuid.uuid4().hex)
        try:
            result = await self._run_engine("storyboard", {"input": target, "cells": cells, "transcript": "off"}, tempdir)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[video-storyboard] /分镜 失败: {exc}")
            yield event.plain_result(f"分镜失败：{exc}")
            return
        images = result.get("images") or []
        for image in images:
            try:
                payload = base64.b64decode(image["data"])
            except Exception:
                continue
            yield event.image_result(payload)
        if not images:
            yield event.plain_result(result.get("text", "")[:1500])

    # ------------------------------------------------------------------ 引擎调用

    async def _call_engine(self, event: AstrMessageEvent, tool: str, args: dict[str, Any]) -> Any:
        args = dict(args)
        try:
            args["input"] = await self._resolve_input(event, args.get("input") or "")
        except Exception as exc:  # noqa: BLE001
            return self._error_result(f"无法确定视频输入：{exc}")

        if not args["input"]:
            return self._error_result(
                "没有找到可分析的视频：请在 input 里给本地绝对路径或 http(s) 链接，"
                "或者让用户把视频/链接直接发到当前消息里。"
            )

        workdir = self._work_root / uuid.uuid4().hex
        try:
            result = await self._run_engine(tool, args, workdir)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[video-storyboard] {tool} 失败: {exc}")
            return self._error_result(str(exc))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        return self._to_tool_result(result)

    async def _run_engine(self, tool: str, args: dict[str, Any], workdir: Path) -> dict[str, Any]:
        node = self._node or self._resolve_binary("node_path", "node")
        if not node:
            raise RuntimeError("找不到 Node.js（>=20）。请安装 Node.js，或在插件配置里填写 node_path。")
        if not RUNNER.exists():
            raise RuntimeError(f"引擎脚本缺失：{RUNNER}")

        await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
        spec = {
            "tool": tool,
            "args": args,
            "outDir": str(workdir),
            "prefix": f"{tool}-{int(time.time())}",
            "ffmpegPath": self._resolve_binary("ffmpeg_path", "ffmpeg") or "",
            "ffprobePath": self._resolve_binary("ffprobe_path", "ffprobe") or "",
        }
        spec_path = workdir / "spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

        env = os.environ.copy()
        env.setdefault("DSH_VIDEO_CACHE_DIR", str(self._work_root / "cache"))
        cookies = str(self.config.get("cookies_file", "") or "").strip()
        if cookies:
            env["DSH_VIDEO_COOKIES"] = cookies
        asr_base = str(self.config.get("asr_base_url", "") or "").strip()
        if asr_base:
            env["DSH_ASR_BASE_URL"] = asr_base
        asr_model = str(self.config.get("asr_model", "") or "").strip()
        if asr_model:
            env["DSH_ASR_MODEL"] = asr_model
        asr_key = str(self.config.get("asr_key", "") or "").strip()
        if asr_key:
            env["DSH_ASR_KEY"] = asr_key

        timeout = float(self.config.get("timeout_seconds", 100) or 100)
        async with self._lock:
            proc = await asyncio.create_subprocess_exec(
                node,
                str(RUNNER),
                str(spec_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(ENGINE_DIR),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(
                    f"引擎超时（{timeout:.0f}s）。可以调大插件配置 timeout_seconds，"
                    "或先用 video_storyboard 的 start/end 只看一小段。"
                ) from None

        text_out = stdout.decode("utf-8", "replace").strip()
        err_out = stderr.decode("utf-8", "replace").strip()
        if err_out:
            logger.debug(f"[video-storyboard] engine stderr: {err_out[-1500:]}")
        if not text_out:
            raise RuntimeError(f"引擎没有输出（exit={proc.returncode}）：{err_out[-500:]}")

        try:
            payload = json.loads(text_out)
        except json.JSONDecodeError:
            raise RuntimeError(f"引擎输出不是 JSON：{text_out[:300]}") from None
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "引擎返回失败")

        images = []
        for item in payload.get("images") or []:
            try:
                data = Path(item["path"]).read_bytes()
            except OSError:
                continue
            images.append({"data": base64.b64encode(data).decode("ascii"), "bytes": len(data)})
        return {"text": payload.get("text") or "", "images": images}

    def _to_tool_result(self, result: dict[str, Any]) -> Any:
        text = result.get("text") or ""
        images = result.get("images") or []
        if mcp_types is None or not images:
            return text
        content: list[Any] = []
        for image in images:
            content.append(
                mcp_types.ImageContent(type="image", data=image["data"], mimeType="image/png")
            )
        if text:
            content.append(mcp_types.TextContent(type="text", text=text))
        return mcp_types.CallToolResult(content=content, isError=False)

    def _error_result(self, message: str) -> Any:
        if mcp_types is None:
            return f"error: {message}"
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=f"error: {message}")],
            isError=True,
        )

    # ------------------------------------------------------------------ 辅助

    def _resolve_binary(self, config_key: str, name: str) -> str | None:
        configured = str(self.config.get(config_key, "") or "").strip().strip('"')
        if configured:
            candidate = Path(configured)
            if candidate.exists():
                return str(candidate)
            found = shutil.which(configured)
            if found:
                return found
            logger.warning(f"[video-storyboard] 配置的 {config_key}={configured} 不可用，回退到 PATH 查找 {name}")
        return shutil.which(name)

    async def _resolve_input(self, event: AstrMessageEvent, input_value: str) -> str:
        """把 input 落成"本地路径或 URL"。

        - 显式给了路径/链接：原样返回（本地路径会验证存在）
        - 留空：先扫当前消息里的视频/文件组件，再扫消息文本里的链接
        """
        if input_value:
            stripped = input_value.strip()
            if stripped.startswith("http://") or stripped.startswith("https://"):
                return stripped
            if Path(stripped).exists():
                return str(Path(stripped).resolve())
            # 模型有时把消息里的链接抄成 Markdown
            matched = URL_RE.search(stripped)
            if matched:
                return matched.group(0)
            raise FileNotFoundError(f"路径或链接不可用：{stripped}")

        # 1) 消息组件里的视频/文件
        try:
            components = event.get_messages() or []
        except Exception:
            components = []
        for component in components:
            kind = type(component).__name__.lower()
            if kind not in {"video", "file"}:
                continue
            source = (
                getattr(component, "file", None)
                or getattr(component, "url", None)
                or getattr(component, "path", None)
            )
            if not source:
                continue
            source = str(source)
            if source.startswith(("http://", "https://")):
                return await self._download(source)
            if Path(source).exists():
                return str(Path(source).resolve())

        # 2) 消息文本里的链接
        matched = URL_RE.search(event.message_str or "")
        if matched:
            return matched.group(0)
        return ""

    async def _download(self, url: str) -> str:
        """把消息里的视频 URL 落盘（平台通常给的是带时效的 CDN 链接）。"""
        import aiohttp

        target_dir = self._work_root / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(url.split("?")[0]).suffix or ".mp4"
        target = target_dir / f"{uuid.uuid4().hex}{suffix}"
        max_mb = float(self.config.get("max_download_mb", 300) or 300)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as response:
                response.raise_for_status()
                written = 0
                with target.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(1 << 20):
                        written += len(chunk)
                        if written > max_mb * 1024 * 1024:
                            raise RuntimeError(f"视频超过 {max_mb:.0f}MB 上限（插件配置 max_download_mb 可改）")
                        handle.write(chunk)
        return str(target)
