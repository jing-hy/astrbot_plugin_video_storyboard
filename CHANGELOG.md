# Changelog

## 1.0.0 - 2026-10-30

首个版本。

### 新增
- `video_storyboard`：把视频切成镜头，每格取最清晰的一帧拼成带编号的宫格，并附每格起止时间清单。
- `video_zoom`：按宫格编号或时间窗近距离重看，看清字幕、人脸、界面文字。
- `video_probe`：只读容器信息（时长/分辨率/帧率/音轨/字幕轨），不出图。
- `video_locate`：在台词转写里定位一句话出现在第几秒。
- `/分镜` 命令：不需要多模态模型，直接把宫格发到当前会话。
- 支持本地文件与 http(s) 链接（B 站等，走 yt-dlp），支持 cookies、ASR、硬字幕 OCR 校正。

### 实现
- 引擎内联 dsh-video-storyboard（MIT），AstrBot 侧只做协议翻译：写 spec.json → 调 node → PNG 转 mcp.ImageContent。
- 图片经 AstrBot 的 tool_image_cache 以 data URL 回灌模型，能在对话里直接看图。
