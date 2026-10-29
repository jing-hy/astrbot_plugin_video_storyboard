# 视频分镜 · astrbot_plugin_video_storyboard

> 让 AstrBot 里的模型**真的看到**视频画面，而不是只读别人写的字幕摘要。

发一个 B 站链接或一段视频，模型会拿到一张带编号的宫格图（每格是该镜头最清晰的一帧，底部有音频包络条）和一份**每个格子的起止时间清单**。它能据此回答“第几分钟在演示什么”“这个界面上写的是什么字”，需要细看时再调 video_zoom 把某几秒放大成 4–6 格。

引擎来自 [dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard)（MIT），已内联进本插件，**不需要再装 DeepSeek Harness**。

---

## 它解决什么问题

AstrBot 生态里做“视频理解”的插件，主流做法是抓字幕 → 丢给模型总结。问题是：

| 情况 | 抽字幕的插件 | 本插件 |
|---|---|---|
| 视频没有字幕 / 没人投稿字幕 | ❌ 用不了 | ✅ 照常 |
| 字幕是机翻或口语转写 | 误差被带进结论 | 画面是证据，字幕只是辅助 |
| 纯画面信息（软件界面、PPT、演示动作、场景） | ❌ 完全丢失 | ✅ 看图 |
| 想知道“那句话在第几秒” | 靠猜 | ✅ video_locate 精确到 0.01 秒 |

---

## 四个工具

| 工具 | 作用 | 成本 |
|---|---|---|
| video_storyboard | 全片 / 一段的概览宫格 + 时间轴清单（按镜头自动切分，快剪给更多格） | 1–4 张图 |
| video_zoom | 某一段近距离重看（格子少、每格大，能看清字幕和小字），可按宫格编号寻址 | 1 张图 |
| video_probe | 只读容器信息：时长、分辨率、帧率、音轨、字幕轨 | 零图片 token |
| video_locate | 在台词转写里找一句话出现在第几秒 | 纯文本 |

典型链路：

```
video_probe      →  93.31s，25fps，有音轨无字幕轨
video_storyboard →  6 格宫格 + "cell 3 = 00:30.90-00:31.56 …"
video_zoom       →  只看 00:30-00:45，4 格，看清界面文字
```

---

## 依赖

| 依赖 | 必需 | 说明 |
|---|---|---|
| AstrBot | ✅ | >= 4.20, < 5 |
| Node.js >= 20 | ✅ | 引擎运行在 Node 上；插件配置里可填 node_path |
| ffmpeg / ffprobe | ✅ | 在 PATH，或配置里指定路径 |
| 多模态模型 | ✅ | 服务提供商的“模型能力”要勾选**图像**，否则宫格会退化成 [Image] 占位符 |
| yt-dlp | 链接才需要 | 解析 B 站/YouTube 等链接 |
| cookies.txt | B 站会员/高清才需要 | Netscape 格式 |
| ASR 服务 | 可选 | OpenAI 兼容的 /audio/transcriptions，没有就用视频自带字幕轨 |
| rapidocr | 可选 | 硬字幕校正（detail=high 时） |

---

## 安装（三种方式）

**① 自定义市场源（立刻可用）**

插件市场页 → 自定义市场源，填入：

```
https://fastly.jsdelivr.net/gh/jing-hy/astrbot_plugin_video_storyboard@main/market.json
```

（备用地址：把 fastly 换成 gcore，或用 GitHub Pages 版 `https://jing-hy.github.io/astrbot_plugin_video_storyboard/market.json`）
然后搜索「视频分镜」即可一键安装。

**② 官方插件市场**

搜索「视频分镜」或 video_storyboard。仓库已打上 `astrbot-plugin` 等 topic，官方市场（cloud.astrbot.app）同步收录后即可直接搜到。

**③ git clone**

```bash
cd <AstrBot 数据目录>/plugins
git clone https://github.com/jing-hy/astrbot_plugin_video_storyboard.git
```

重启或重载插件。日志里应出现：

```
[video-storyboard] 引擎就绪，node = C:\Program Files\nodejs\node.exe
```

---

## 配置

在 AstrBot 后台 → 插件 → 视频分镜 里配置：

| 配置项 | 默认 | 说明 |
|---|---|---|
| node_path | 空（走 PATH） | Node.js 可执行文件 |
| ffmpeg_path / ffprobe_path | 空（走 PATH） | |
| cookies_file | 空 | B 站等需要登录态的链接 |
| asr_base_url / asr_model / asr_key | 空 | 配了才会做人声转写 |
| ocr_python | 空 | 装了 rapidocr 的 python（硬字幕校正用） |
| timeout_seconds | 100 | 单次引擎超时，要小于 AstrBot 的 tool_call_timeout（默认 120） |
| max_download_mb | 300 | 消息里视频链接的下载上限 |

---

## 使用

**自动触发**（推荐）：把链接直接发进聊天，说“看看这个视频讲了什么”，模型会自己调工具。

**命令**（不需要多模态模型，直接把宫格发到群里）：

```
/分镜 https://www.bilibili.com/video/BV1xx411c7mD 9
/分镜 D:\videos\demo.mp4 6
```

第二个数字是格数（1–16），不填则自动。

---

## 工作原理

```
AstrBot 模型  ──tool call──▶  main.py
                                │  写 spec.json
                                ▼
                       node engine/runner.mjs
                                │  ffmpeg/ffprobe/yt-dlp
                                ▼
                     PNG 宫格 + 文本清单
                                │
        mcp.CallToolResult(ImageContent + TextContent)
                                ▼
              AstrBot tool_image_cache → 回灌模型（data URL）
```

---

## 限制

1. **图片有成本**：一张 4 格宫格约 340KB，多格图更贵。默认只出 1 张，细节用 video_zoom。
2. **工具超时**：AstrBot 默认 tool_call_timeout=120s。长片请先 video_probe 看时长，再分窗 start/end。
3. **串行处理**：插件内用锁串行调用引擎，多人同时丢视频会排队（避免 CPU 打满）。
4. **链接安全**：仅用于分析用户主动给出的视频链接；抓取走 yt-dlp 与引擎内置的 SSRF 防护，请勿在公开大群开放任意链接。

---

## 致谢与许可

- 引擎：[dsh-video-storyboard](https://github.com/SAOS-3624/dsh-video-storyboard)（MIT, Copyright © SAOS-3624），engine/ 目录为其源码内联。
- 本插件 AstrBot 侧代码：MIT，Copyright © 2026 jing-hy。
