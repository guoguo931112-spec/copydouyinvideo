#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emo-clip-cli —— 名人金句 情绪短片 自动合成

复刻"名人口播片段 + 电影感风光空镜 + 中英双语字幕 + BGM"那套抖音情绪短片模板。

链接 → 下载原片 → Whisper转写(带时间轴) → 选金句区间切片(留原声)
     → GPT中译英 → Pexels自动下风光空镜 → ffmpeg合成(口播↔空镜交替+烧双语字幕+BGM)

每步独立子命令, 可单独跑/断点续。一个"作业"= data/<job>/ 一个目录。

依赖:
  - yt-dlp (B站/YouTube下载) + ffmpeg/ffprobe   —— brew 已装
  - 抖音链接走 skill 内置 TikTokDownloader (无水印)
  - AIBH_API_KEY (api.aibh.site, OpenAI兼容): Whisper转写 + GPT翻译/关键词
  - PEXELS_API_KEY: 风光空镜素材 (默认读项目 config.json)

用法:
  python3 emo_clip.py download "<链接>" --job zcy        # 下原片
  python3 emo_clip.py transcribe --job zcy               # 转写出 sentences.json(带时间轴)
  #   看 sentences.json 选好金句的起止秒, 然后:
  python3 emo_clip.py cut --job zcy --start 12.0 --end 30.0
  python3 emo_clip.py translate --job zcy                # 中译英(双语字幕)
  python3 emo_clip.py broll --job zcy --n 6              # Pexels下空镜
  python3 emo_clip.py compose --job zcy [--bgm bgm.mp3]  # 合成出片
  python3 emo_clip.py run "<链接>" --job zcy --start 12 --end 30   # 一条龙
"""
import sys, os, json, re, time, argparse, subprocess, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CONFIG = os.path.join(HERE, "config.json")
# 711bigseller 中转站(OpenAI兼容): 只有 GPT-5.x chat + image, 没有whisper, 故转写走本地。
AIBH_BASE = os.environ.get("AIBH_BASE") or (json.load(open(CONFIG)).get("aibh_base") if os.path.exists(CONFIG) else None) or "https://sub.711bigseller.icu/v1"
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gpt-5.4-mini")    # 翻译/关键词
WHISPER_BIN = "whisper-cli"
WHISPER_MODEL = os.path.join(HERE, "models", "ggml-medium.bin")  # 本地转写模型(medium中文更准)
LB_TOP, LB_BOT = 90, 165          # 电影感黑边: 上/下黑边高度(下边更高放双语字幕+遮原字幕)
DOUYIN_DL = "/Users/kara/.claude/skills/good-TTvideo2text/TikTokDownloader"
W, H, FPS = 1280, 720, 30          # 输出规格(横屏电影感)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")   # 没UA会被Pexels CDN 403

# 默认电影感空镜搜索词(关键词生成失败时兜底)
DEFAULT_BROLL_KW = ["cinematic mountain sunrise", "aerial lake landscape",
                    "sunset clouds timelapse", "forest waterfall nature",
                    "snow mountain peak", "golden grassland wind"]


def cfg(key, env):
    v = os.environ.get(env)
    if v:
        return v
    if os.path.exists(CONFIG):
        return json.load(open(CONFIG)).get(key)
    return None


def jobdir(job):
    d = os.path.join(DATA, job)
    os.makedirs(d, exist_ok=True)
    return d


def sh(cmd, **kw):
    """跑命令, 失败抛错。cmd 是 list。"""
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd[:6])}...\n{r.stderr[-800:]}")
    return r.stdout


def ffprobe_dur(path):
    out = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(out.strip())


# ============== AIBH (OpenAI兼容) ==============
def _aibh_key():
    k = cfg("aibh_api_key", "AIBH_API_KEY")
    if not k:
        raise RuntimeError("缺少 AIBH_API_KEY (export 或写进 config.json), Whisper转写/翻译需要它")
    return k


def aibh_transcribe(audio_path, language="zh"):
    """Whisper 转写, 返回 verbose_json 的 segments [{start,end,text}]。multipart 手搓。"""
    key = _aibh_key()
    boundary = "----emoclip" + str(len(audio_path))
    fields = {"model": "whisper-1", "language": language, "response_format": "verbose_json"}
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    fname = os.path.basename(audio_path)
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
             "Content-Type: audio/mpeg\r\n\r\n").encode()
    body += open(audio_path, "rb").read() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(AIBH_BASE + "/audio/transcriptions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    r = json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
    return r.get("segments", [])


def aibh_chat(prompt, model=None, temperature=0.3):
    key = _aibh_key()
    body = json.dumps({"model": model or CHAT_MODEL, "temperature": temperature,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(AIBH_BASE + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json",
                                          "User-Agent": UA})   # 不带UA会被中转站WAF 403
    last = None
    for i in range(4):
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
            return r["choices"][0]["message"]["content"].strip()
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"chat 失败(4次): {last}")


# ============== 1) 下载 ==============
def is_douyin(url):
    return "douyin.com" in url or "iesdouyin" in url


def step_download(url, job):
    d = jobdir(job)
    out = os.path.join(d, "source.mp4")
    print(f"  yt-dlp 下载 -> {out}")
    # B站需 Chrome cookie + referer 否则 412; 旧版yt-dlp会412, 需较新版。抖音同理走cookie。
    ref = "https://www.douyin.com" if is_douyin(url) else "https://www.bilibili.com"
    sh(["yt-dlp", "--no-update", "--cookies-from-browser", "chrome", "--referer", ref,
        "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-o", out, url])
    dur = ffprobe_dur(out)
    print(f"  ✓ 已下载, 时长 {dur:.1f}s")
    return out


# ============== 2) 转写 ==============
def local_transcribe(wav, model=WHISPER_MODEL, lang="zh"):
    """本机 whisper.cpp 离线转写, 返回 [{start,end,zh}](秒)。中转站没whisper, 故走本地。"""
    if not os.path.exists(model):
        raise RuntimeError(f"缺少 whisper 模型: {model}\n  下载: curl -L -o {model} "
                           "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin")
    prefix = wav.rsplit(".", 1)[0] + "_asr"
    sh([WHISPER_BIN, "-m", model, "-l", lang, "-f", wav, "-oj", "-of", prefix])
    d = json.load(open(prefix + ".json"))
    out = []
    for s in d.get("transcription", []):
        o = s.get("offsets", {})
        t = s.get("text", "").strip()
        if t:
            out.append({"start": round(o.get("from", 0) / 1000, 2),
                        "end": round(o.get("to", 0) / 1000, 2), "zh": t})
    return out


def step_transcribe(job, src_name="source.mp4"):
    d = jobdir(job)
    video = os.path.join(d, src_name)
    wav = os.path.join(d, "audio.wav")
    print(f"  提取音频 {src_name} -> audio.wav (16k单声道)")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vn",
        "-ac", "1", "-ar", "16000", wav])
    print("  本地 whisper.cpp 转写中...")
    sents = local_transcribe(wav)
    out = os.path.join(d, "sentences.json")
    json.dump(sents, open(out, "w"), ensure_ascii=False, indent=1)
    print(f"  ✓ 转写 {len(sents)} 句 -> sentences.json")
    for s in sents:
        print(f"    [{s['start']:6.2f}-{s['end']:6.2f}] {s['zh']}")
    return sents


# ============== 3) 切金句区间 ==============
def step_cut(job, start, end):
    """从 source 切 [start,end] -> seg.mp4(留原声), 再【只对这段】本地whisper转写 -> seg_sentences.json(时间从0)。
    长视频不整段转写, 切完只转这段, 省时且时间轴天然归零。"""
    d = jobdir(job)
    src = os.path.join(d, "source.mp4")
    seg = os.path.join(d, "seg.mp4")
    print(f"  切片 [{start}, {end}] -> seg.mp4")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-to", str(end),
        "-i", src, "-c:v", "libx264", "-c:a", "aac", "-preset", "veryfast", seg])
    wav = os.path.join(d, "seg.wav")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", seg, "-vn", "-ac", "1", "-ar", "16000", wav])
    print("  本地 whisper 转写该段...")
    sub = local_transcribe(wav)
    json.dump(sub, open(os.path.join(d, "seg_sentences.json"), "w"), ensure_ascii=False, indent=1)
    print(f"  ✓ seg.mp4 时长 {ffprobe_dur(seg):.1f}s, 转写 {len(sub)} 句")
    for s in sub:
        print(f"    [{s['start']:6.2f}-{s['end']:6.2f}] {s['zh']}")
    return sub


# ============== 4) 中译英 ==============
def step_translate(job):
    d = jobdir(job)
    f = os.path.join(d, "seg_sentences.json")
    sents = json.load(open(f))
    lines = [s["zh"] for s in sents]
    prompt = ("把下面每行中文翻成地道、简洁、有电影感的英文字幕(一行一句, 不加引号/序号, 行数与输入一致):\n"
              + "\n".join(lines))
    en = aibh_chat(prompt)
    en_lines = [l.strip() for l in en.splitlines() if l.strip()]
    for i, s in enumerate(sents):
        s["en"] = en_lines[i] if i < len(en_lines) else ""
    json.dump(sents, open(f, "w"), ensure_ascii=False, indent=1)
    print(f"  ✓ 已翻译 {len(sents)} 句")
    for s in sents:
        print(f"    {s['zh']}  /  {s.get('en','')}")
    return sents


# ============== 5) Pexels 空镜 ==============
def _dl_retry(url, dst, tries=4):
    """带UA下载, 偶发SSL中断时重试。"""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            data = urllib.request.urlopen(req, timeout=180).read()
            open(dst, "wb").write(data)
            return True
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"下载失败({tries}次): {last}")


def _pexels_search(query, key, n=6):
    url = ("https://api.pexels.com/videos/search?orientation=landscape&size=medium&per_page="
           + str(max(8, n)) + "&query=" + urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": UA})
    r = None
    for i in range(4):                       # 搜索也抗SSL抖动
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
            break
        except Exception:
            time.sleep(1.5 * (i + 1))
    if r is None:
        return []
    picks = []
    for v in r.get("videos", []):
        # 选 >=1280 宽的 mp4, 取最接近 720p 的
        files = [f for f in v["video_files"] if f.get("width", 0) >= 1280 and f["link"].endswith((".mp4", "mp4"))]
        files = files or [f for f in v["video_files"] if f.get("width", 0) >= 960]
        if not files:
            continue
        files.sort(key=lambda f: abs(f.get("height", 720) - 720))
        picks.append({"url": files[0]["link"], "dur": v.get("duration", 0)})
        if len(picks) >= n:
            break
    return picks


def _gen_keywords(sents, n):
    """让 GPT 据金句情绪给 n 个英文电影感空镜搜索词; 失败用默认。"""
    try:
        zh = " ".join(s["zh"] for s in sents)
        prompt = (f"这是一段励志/情绪短片的旁白:「{zh}」。给我 {n} 个适合做电影感空镜B-roll的英文Pexels搜索词,"
                  "每个2-4个词, 偏壮阔自然/孤独奋斗/日出登顶/城市夜景等情绪意象, 一行一个, 不要编号。")
        out = aibh_chat(prompt)
        kw = [l.strip("-• ").strip() for l in out.splitlines() if l.strip()]
        kw = [k for k in kw if 2 <= len(k) <= 40]
        return kw[:n] if kw else DEFAULT_BROLL_KW[:n]
    except Exception as e:
        print("  (关键词生成失败, 用默认词):", e)
        return (DEFAULT_BROLL_KW * 3)[:n]


def step_broll(job, n=6):
    d = jobdir(job)
    bdir = os.path.join(d, "broll")
    os.makedirs(bdir, exist_ok=True)
    key = cfg("pexels_api_key", "PEXELS_API_KEY")
    if not key:
        raise RuntimeError("缺少 PEXELS_API_KEY (config.json 或环境变量)")
    sents = json.load(open(os.path.join(d, "seg_sentences.json"))) if os.path.exists(os.path.join(d, "seg_sentences.json")) else []
    kws = _gen_keywords(sents, n) if sents else DEFAULT_BROLL_KW[:n]
    print(f"  空镜关键词: {kws}")
    got = []
    for i, kw in enumerate(kws):
        dst = os.path.join(bdir, f"b{i:02d}.mp4")
        try:
            picks = _pexels_search(kw, key, 6)
        except Exception as e:
            print(f"    ✗ [{i}] 搜索失败 {kw}: {e}")
            continue
        ok = False
        for p in picks:                     # 多候选逐个试, 抗SSL抖动
            try:
                _dl_retry(p["url"], dst, tries=3)
                got.append(dst)
                print(f"    ✓ [{i}] {kw} -> {os.path.basename(dst)} ({p['dur']}s)")
                ok = True
                break
            except Exception:
                continue
        if not ok:
            print(f"    ✗ [{i}] {kw}: 候选全下载失败(网络)")
    print(f"  ✓ 共下 {len(got)} 段空镜")
    return got


# ============== 6) 合成 ==============
# 本机 ffmpeg 未编 libass/drawtext, 故字幕用 Pillow 渲染成透明PNG, 再用 overlay 按时间轴叠加。
FONT_ZH_CANDS = ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc",
                 "/System/Library/Fonts/STHeiti Medium.ttc"]
FONT_EN_CANDS = ["/System/Library/Fonts/Supplemental/Times New Roman.ttf",
                 "/System/Library/Fonts/Supplemental/Georgia.ttf"]


def _load_font(cands, size):
    from PIL import ImageFont
    for p in cands:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w, by_word):
    if not text:
        return []
    units = text.split(" ") if by_word else list(text)
    sep = " " if by_word else ""
    lines, cur = [], ""
    for u in units:
        t = (cur + sep + u) if cur else u
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur); cur = u
    if cur:
        lines.append(cur)
    return lines


def render_sub_png(zh, en, path):
    """整帧透明PNG: 中文(白, 稍大, 黑描边)在上, 英文(浅灰衬线)在下, 居中底部。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    fz = _load_font(FONT_ZH_CANDS, 46)
    fe = _load_font(FONT_EN_CANDS, 30)
    max_w = W - 160
    zl = _wrap(dr, zh, fz, max_w, by_word=False)
    el = _wrap(dr, en or "", fe, max_w, by_word=True)
    # 从底部往上排版
    y = H - 56
    for line in reversed(el):
        w = dr.textlength(line, font=fe)
        dr.text(((W - w) / 2, y - 34), line, font=fe, fill=(225, 225, 225, 255),
                stroke_width=2, stroke_fill=(20, 20, 20, 230))
        y -= 40
    y -= 6
    for line in reversed(zl):
        w = dr.textlength(line, font=fz)
        dr.text(((W - w) / 2, y - 52), line, font=fz, fill=(255, 255, 255, 255),
                stroke_width=3, stroke_fill=(20, 20, 20, 235))
        y -= 58
    img.save(path)
    return path


def build_sub_pngs(sents, outdir):
    """每句一张透明PNG, 返回 [(png, start, end), ...]。"""
    os.makedirs(outdir, exist_ok=True)
    items = []
    for i, s in enumerate(sents):
        p = os.path.join(outdir, f"sub{i:03d}.png")
        render_sub_png(s["zh"], s.get("en", ""), p)
        items.append((p, s["start"], s["end"]))
    return items


def build_letterbox_png(path):
    """电影感黑边: 上/下不透明黑条(中间透明), 全程叠加遮住源片底部原字幕+顶部部分台标。"""
    from PIL import Image
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (W, LB_TOP), (0, 0, 0, 255)), (0, 0))
    img.paste(Image.new("RGBA", (W, LB_BOT), (0, 0, 0, 255)), (0, H - LB_BOT))
    img.save(path)
    return path


def _scene_plan(sents, broll_files, segdur):
    """排镜: 第1句和最后1句给人像(露脸建立信任), 中间每句轮换一段空镜。无空镜则全人像。"""
    scenes = []
    if not sents:
        scenes.append({"start": 0, "end": segdur, "src": "face"})
        return scenes
    bi = 0
    n = len(sents)
    for i, s in enumerate(sents):
        st = s["start"] if i > 0 else 0
        en = s["end"] if i < n - 1 else segdur
        face = (i == 0) or (i == n - 1) or (broll_files == []) or (i % 4 == 2)
        if face:
            scenes.append({"start": st, "end": en, "src": "face"})
        else:
            scenes.append({"start": st, "end": en, "src": broll_files[bi % len(broll_files)]})
            bi += 1
    return scenes


def _render_scene(scene, seg, idx, outdir):
    """渲染单场景为统一规格的无声片段。face=从seg切对应时段; 否则空镜循环裁到时长。"""
    dur = round(scene["end"] - scene["start"], 3)
    out = os.path.join(outdir, f"s{idx:03d}.mp4")
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},format=yuv420p"
    if scene["src"] == "face":
        sh(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(scene["start"]), "-t", str(dur),
            "-i", seg, "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", out])
    else:
        sh(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", scene["src"],
            "-t", str(dur), "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", out])
    return out


def step_compose(job, bgm=None, bgm_vol=0.18, letterbox=True):
    d = jobdir(job)
    seg = os.path.join(d, "seg.mp4")
    if not os.path.exists(seg):
        raise RuntimeError("没有 seg.mp4, 先 cut")
    sents = json.load(open(os.path.join(d, "seg_sentences.json")))
    segdur = ffprobe_dur(seg)
    broll = sorted([os.path.join(d, "broll", f) for f in os.listdir(os.path.join(d, "broll"))]) \
        if os.path.isdir(os.path.join(d, "broll")) else []
    print(f"  seg {segdur:.1f}s, {len(sents)}句, {len(broll)}段空镜")
    # 排镜 + 逐场景渲染
    sdir = os.path.join(d, "scenes")
    os.makedirs(sdir, exist_ok=True)
    scenes = _scene_plan(sents, broll, segdur)
    files = []
    for i, sc in enumerate(scenes):
        files.append(_render_scene(sc, seg, i, sdir))
        tag = "脸" if sc["src"] == "face" else os.path.basename(sc["src"])
        print(f"    场景{i}: [{sc['start']:.1f}-{sc['end']:.1f}] {tag}")
    # 拼视频轨
    concat_txt = os.path.join(d, "concat.txt")
    open(concat_txt, "w").write("\n".join(f"file '{f}'" for f in files))
    vtrack = os.path.join(d, "vtrack.mp4")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", concat_txt, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", vtrack])
    # 字幕: 每句渲染透明PNG, 用 overlay+enable 按时间轴叠加(本机ffmpeg无libass/drawtext)
    subs = build_sub_pngs(sents, os.path.join(d, "subs"))
    lb = build_letterbox_png(os.path.join(d, "letterbox.png")) if letterbox else None
    print(f"  字幕 {len(subs)} 张PNG{' + 电影感黑边' if lb else ''}, overlay 叠加...")
    final = os.path.join(d, "final.mp4")
    inputs = ["-i", vtrack, "-i", seg]
    bgm_abs = os.path.abspath(bgm) if bgm else None
    if bgm_abs and os.path.exists(bgm_abs):
        inputs += ["-i", bgm_abs]
    img_start = len(inputs) // 2          # 图片输入索引起点(黑边+字幕)
    if lb:
        inputs += ["-i", lb]
    for p, _, _ in subs:
        inputs += ["-i", p]
    # 链式 overlay: 先全程叠黑边(遮原字幕/台标), 再每张字幕在其时间窗显示
    chain, cur = [], "0:v"
    idx = img_start
    if lb:
        chain.append(f"[{cur}][{idx}:v]overlay=0:0[vlb]"); cur = "vlb"; idx += 1
    for k, (_, st, en) in enumerate(subs):
        nxt = f"v{k}"
        chain.append(f"[{cur}][{idx + k}:v]overlay=0:0:enable='between(t,{st},{en})'[{nxt}]")
        cur = nxt
    fc = ";".join(chain) if chain else "[0:v]null[vout]"
    vlabel = cur if chain else "vout"
    if bgm_abs and os.path.exists(bgm_abs):
        fc += (f";[1:a]volume=1.0[a0];[2:a]volume={bgm_vol},aloop=loop=-1:size=2e9[a1];"
               "[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        amap = "[aout]"
    else:
        amap = "1:a"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + \
          ["-filter_complex", fc, "-map", f"[{vlabel}]", "-map", amap,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac",
           "-shortest", final]
    print("  合成中(叠字幕+混音)...")
    sh(cmd)
    print(f"  ✅ 出片: {final}  ({ffprobe_dur(final):.1f}s)")
    return final


# ============== 编排 ==============
def step_run(url, job, start, end, n=6, bgm=None):
    step_download(url, job)
    step_cut(job, start, end)        # 切段并只转写该段(长视频不整段转写)
    step_translate(job)
    step_broll(job, n)
    step_compose(job, bgm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["download", "transcribe", "cut", "translate", "broll", "compose", "run"])
    ap.add_argument("url", nargs="?", default=None)
    ap.add_argument("--job", required=True)
    ap.add_argument("--start", type=float)
    ap.add_argument("--end", type=float)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--bgm", default=None)
    a = ap.parse_args()
    if a.step == "download":
        step_download(a.url, a.job)
    elif a.step == "transcribe":
        step_transcribe(a.job)
    elif a.step == "cut":
        step_cut(a.job, a.start, a.end)
    elif a.step == "translate":
        step_translate(a.job)
    elif a.step == "broll":
        step_broll(a.job, a.n)
    elif a.step == "compose":
        step_compose(a.job, a.bgm)
    elif a.step == "run":
        step_run(a.url, a.job, a.start, a.end, a.n, a.bgm)


if __name__ == "__main__":
    main()
