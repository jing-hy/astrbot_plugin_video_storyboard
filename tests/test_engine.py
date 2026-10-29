"""引擎链路自检：用 AstrBot 自带的 python 跑一遍，不启动完整 AstrBot。"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

import main  # noqa: E402


def make_plugin():
    plugin = main.VideoStoryboardPlugin.__new__(main.VideoStoryboardPlugin)
    plugin.config = {"timeout_seconds": 100}
    plugin._node = shutil.which("node")
    plugin._lock = asyncio.Lock()
    plugin._work_root = Path(tempfile.gettempdir()) / "astrbot-video-storyboard"
    plugin._work_root.mkdir(parents=True, exist_ok=True)
    return plugin


async def run(sample: str):
    plugin = make_plugin()
    workdir = plugin._work_root / "selftest"
    result = await plugin._run_engine(
        "storyboard", {"input": sample, "cells": 4, "transcript": "off"}, workdir
    )
    print("images bytes:", [i["bytes"] for i in result["images"]])
    print("text chars:", len(result["text"]))
    tool_result = plugin._to_tool_result(result)
    print("CallToolResult:", type(tool_result).__name__,
          [type(c).__name__ for c in tool_result.content])
    probe = await plugin._run_engine("probe", {"input": sample}, workdir)
    print("probe:", probe["text"][:120].replace("\n", " | "))


if __name__ == "__main__":
    sample = sys.argv[1] if len(sys.argv) > 1 else ""
    if not sample or not Path(sample).exists():
        print("usage: python tests/test_engine.py <video-path>")
        raise SystemExit(2)
    asyncio.run(run(sample))
