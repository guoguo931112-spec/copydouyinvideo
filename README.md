# emo-clip-cli —— 名人金句 情绪短片 自动合成

复刻抖音上「名人口播片段 + 电影感风光空镜 + 中英双语字幕 + 情绪BGM」那套模板
（参考原片：张朝阳「视频自媒体是年轻人的创业机会」#拍出电影感 #情绪短片）。

```
链接 → 下原片 → Whisper转写(带时间轴) → 选金句区间切片(留原声)
     → GPT中译英 → Pexels自动下风光空镜 → ffmpeg合成(口播↔空镜交替+烧双语字幕+BGM)
```

## 依赖与配置

| 能力 | 用什么 | 配置 |
|---|---|---|
| 下载 B站/YouTube | `yt-dlp` (brew已装) | — |
| 下载抖音(无水印) | skill 内置 TikTokDownloader | — |
| 剪辑/合成 | `ffmpeg`/`ffprobe` (brew已装) | 本机 ffmpeg **未编 libass/drawtext**，故字幕走 **Pillow渲染透明PNG + overlay** |
| 转写(语音→带时间轴文字) | **本地 whisper.cpp**(`whisper-cli` + `models/ggml-small.bin`) | 中转站没whisper, 故离线转写 |
| 翻译 + 空镜关键词 | `sub.711bigseller.icu/v1`(OpenAI兼容, GPT-5.x) | `AIBH_API_KEY`(config.json), 模型 `gpt-5.4-mini` |
| 风光空镜素材 | Pexels 视频API | `PEXELS_API_KEY`（已写入 config.json） |
| 字幕渲染 | Pillow | `pip3 install --break-system-packages Pillow` |

- **Pexels CDN 和 711 中转站 都需 User-Agent**，否则 403；脚本已带 UA + SSL中断重试 + 多候选。
- 转写模型: `curl -L -o models/ggml-small.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin`（要更准换 medium）。
- `config.json`（已 gitignore）存 `pexels_api_key` / `aibh_api_key` / `aibh_base`。

## 用法

```bash
# 一条龙
python3 emo_clip.py run "<视频链接>" --job zcy --start 12 --end 30 --n 6 [--bgm bgm.mp3]

# 或分步（推荐：先转写看文案再定金句区间）
python3 emo_clip.py download "<链接>" --job zcy
python3 emo_clip.py transcribe --job zcy          # 出 sentences.json(带时间轴), 人工挑金句段
python3 emo_clip.py cut --job zcy --start 12 --end 30   # 切片留原声 + 重定基字幕
python3 emo_clip.py translate --job zcy            # 中译英
python3 emo_clip.py broll --job zcy --n 6          # Pexels下空镜(关键词据金句情绪自动生成)
python3 emo_clip.py compose --job zcy [--bgm x.mp3]   # 合成出片 -> data/zcy/final.mp4
```

## 排镜逻辑（_scene_plan）
按句切镜：第1句和最后1句**露脸**（建立"是谁在说"），中间每句轮换一段空镜，
每隔几句再回脸一次。音频自始至终是原声口播，空镜段=原声当旁白（无需对口型）。
想全程空镜/调节奏改 `_scene_plan` 即可。

## 输出规格
1280×720 横屏（电影感），H.264 + AAC。字幕：中文白字(46px,黑描边)在上，
英文衬线浅灰(30px)在下，居中底部。样式改 `render_sub_png`。

## 素材来源（去哪找名人采访）
- **B站**：人名+`访谈/演讲/金句`；《十三邀》《一席》混沌/鲁豫有约/央视《对话》；`yt-dlp` 下
- **YouTube**：TED、发布会演讲、名人访谈
- **抖音同类号**：扒别人剪好的金句段（内置下载器下无水印）
- **搜狐视频**：张朝阳专属
> ⚠️ 二创混剪在版权灰区：尽量取无水印源、自己加字幕；带原台标易被判搬运限流。

## 已验证
- ✅ 本地whisper转写(带时间轴)、切片留原声、711/GPT-5.4-mini中译英、GPT空镜关键词、
  Pexels空镜下载、排镜、逐场景渲染、Pillow双语字幕overlay、留原声合成 —— 全链路出片已验证
- ⏳ 唯一没实测的是 `download`(yt-dlp 拉真实B站/YT链接), 标准工具, 接口已写好
- 坑：用「已带字幕水印的成片」当源会字幕重影+留台标；用干净源片(B站/YT)无此问题
