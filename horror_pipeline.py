#!/usr/bin/env python3
"""
horror_pipeline.py — 2ちゃん怖い話 → 英語ホラー動画 全自動生成

フロー:
  2ちゃんスクレイプ → 英訳 → Claudeでスクリプト構造化
  → Pollinations.ai でAI画像生成(無料・APIキー不要)
  → ffmpegでズーム+ノイズ+ビネット効果をかけてMP4化(無料)
  → gTTS音声生成(無料)
  → Remotionで動画レンダリング(無料)

使い方:
  python horror_pipeline.py                        # 全自動
  python horror_pipeline.py --dry-run              # レンダリング省略テスト
  python horror_pipeline.py --story "手動テキスト"  # 直接テキスト指定
  python horror_pipeline.py --voice elevenlabs     # ElevenLabsで高品質音声
  python horror_pipeline.py --animate svd          # SVD切替（将来実装）
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# .envを両方のプロジェクトから読み込む
ROOT = Path(__file__).parent
BEAST_ROOT = ROOT.parent / "threads-auto-beast"

load_dotenv(ROOT / ".env")
load_dotenv(BEAST_ROOT / ".env", override=False)

# ── 設定 ──────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")

PUBLIC_DIR   = ROOT / "public" / "horror"
OUT_DIR      = ROOT / "out"
PROPS_FILE   = ROOT / "horror_props.json"
HISTORY_FILE = ROOT / "horror_history.json"

# Pollinations.ai（APIキー不要の無料AI画像生成）
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}?width=1920&height=1080&model=flux&nologo=true&enhance=true"

# ホラー画像プロンプトのベーススタイル（Flux向け）
HORROR_IMAGE_STYLE = (
    ", cinematic horror photography, dark atmosphere, volumetric fog, "
    "film grain, high contrast shadows, photorealistic, 8k, masterpiece"
)

# セクションタイプごとのフォールバック画像プロンプト
FALLBACK_IMAGE_PROMPTS = {
    "hook":      "dark abandoned japanese torii gate at night, eerie red lanterns, thick fog",
    "setting":   "empty dark japanese temple corridor, moonlight shadows, abandoned",
    "event":     "ghostly figure silhouette in dark forest japan, supernatural mist, horror",
    "end":       "lonely foggy road through japanese bamboo forest at night, unsettling",
}

# 2ちゃん系怖い話まとめRSS
RSS_FEEDS = [
    ("不思議.net",         "https://world-fusigi.net/feed"),
    ("オカルト板まとめ",   "https://okina-matome.com/feed"),
    ("ちろちゃんねる",     "https://chiro-chiro.net/feed"),
]

HORROR_KEYWORDS = [
    "怖い", "都市伝説", "心霊", "幽霊", "呪い", "不思議", "謎",
    "事件", "ミステリー", "オカルト", "霊", "異世界", "禁忌",
]

# ElevenLabs ホラー向けボイス
ELEVENLABS_VOICES = {
    "adam":      "pNInz6obpgDQGcFmaJgB",  # 深い男性
    "aria":      "9BWtsMINqrJLrRacOk9x",  # 女性
    "grandpa":   "NOpBlnGInO9m6vDvFkFC",  # 老人
    "charlotte": "XB0fDUnXU5powFXDhCwa",  # British女性
}
DEFAULT_ELEVENLABS_VOICE = "adam"


# ══════════════════════════════════════════════════════════════
# 1. スクレイピング
# ══════════════════════════════════════════════════════════════

def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {"urls": [], "titles": []}

def save_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

def is_duplicate(history: dict, url: str, title: str) -> bool:
    return url in history["urls"] or title in history["titles"]

def record_history(history: dict, url: str, title: str):
    if url not in history["urls"]:
        history["urls"].append(url)
    if title not in history["titles"]:
        history["titles"].append(title)
    # 直近500件のみ保持
    history["urls"]   = history["urls"][-500:]
    history["titles"] = history["titles"][-500:]

def fetch_rss_candidates(history: dict) -> list[dict]:
    """RSSから怖い話の候補を取得する。"""
    try:
        import feedparser
    except ImportError:
        print("⚠ feedparser未インストール: pip install feedparser")
        return []

    candidates = []
    for name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                link  = getattr(entry, "link",  "").strip()
                if not title or not link:
                    continue
                if not any(kw in title for kw in HORROR_KEYWORDS):
                    continue
                if is_duplicate(history, link, title):
                    continue
                candidates.append({"title": title, "url": link, "source": name})
            print(f"[Scraper] {name}: {len(feed.entries)}件取得")
            time.sleep(1)
        except Exception as e:
            print(f"[Scraper] {name} 失敗: {e}")
    return candidates

def fetch_body(url: str) -> str:
    """記事URLから本文を取得する。"""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120"}
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["nav", "footer", "script", "style", "aside", "header", "noscript"]):
            tag.decompose()
        for sel in ["article", ".entry-content", ".post-content", ".article-body", ".main-content", "#content"]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator="\n", strip=True)
                if len(text) > 200:
                    return text[:4000]
        body = soup.find("body")
        if body:
            return body.get_text(separator="\n", strip=True)[:4000]
    except Exception as e:
        print(f"[Scraper] 本文取得失敗: {e}")
    return ""

def scrape_story() -> dict | None:
    """2ちゃんから怖い話を1件取得する。"""
    print("\n[1/7] 2ちゃんネタ収集中...")
    history = load_history()
    candidates = fetch_rss_candidates(history)
    if not candidates:
        print("  ⚠ 新着なし")
        return None

    import random
    random.shuffle(candidates)
    for cand in candidates[:5]:
        body = fetch_body(cand["url"])
        if len(body) > 200:
            record_history(history, cand["url"], cand["title"])
            save_history(history)
            print(f"  ✅ 取得: {cand['title'][:50]}")
            return {**cand, "body": body}
        time.sleep(2)

    print("  ⚠ 本文取得できる記事なし")
    return None


# ══════════════════════════════════════════════════════════════
# 2. 翻訳
# ══════════════════════════════════════════════════════════════

def translate(text: str, src: str = "ja", dest: str = "en") -> str:
    """deep-translatorで翻訳する。"""
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        print("⚠ deep-translator未インストール: pip install deep-translator")
        return text

    # 4500文字ごとに分割
    chunks = [text[i:i+4500] for i in range(0, len(text), 4500)]
    parts = []
    for chunk in chunks:
        for attempt in range(3):
            try:
                result = GoogleTranslator(source=src, target=dest).translate(chunk)
                parts.append(result)
                time.sleep(0.5)
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    print(f"  ⚠ 翻訳失敗: {e}")
                    parts.append(chunk)
    return " ".join(parts)


# ══════════════════════════════════════════════════════════════
# 3. Claudeでスクリプト構造化
# ══════════════════════════════════════════════════════════════

FALLBACK_SECTION_TYPES = [
    ("HOOK",          "hook"),
    ("THE SETTING",   "setting"),
    ("THE EVENT",     "event"),
    ("WHAT REMAINED", "end"),
]

def split_into_sections_simple(title_en: str, body_en: str) -> list[dict]:
    """Claudeなしでテキストを4分割するフォールバック。"""
    sentences = re.split(r'(?<=[.!?])\s+', body_en.strip())
    sentences = [s for s in sentences if len(s) > 20]
    if len(sentences) < 4:
        sentences *= 2

    n = len(sentences)
    splits = [
        sentences[:max(1, n//6)],
        sentences[max(1, n//6):max(2, n//3)],
        sentences[max(2, n//3):max(3, 2*n//3)],
        sentences[max(3, 2*n//3):],
    ]

    sections = []
    for (caption, qtype), sents in zip(FALLBACK_SECTION_TYPES, splits):
        narration = " ".join(sents[:4])
        sections.append({
            "caption":      caption,
            "narration":    narration,
            "imagePrompt":  FALLBACK_IMAGE_PROMPTS[qtype],
        })
    return sections

def generate_script_with_claude(title_en: str, body_en: str) -> list[dict]:
    """Claude APIでホラースクリプトを構造化する。"""
    if not ANTHROPIC_API_KEY:
        print("  ℹ ANTHROPIC_API_KEY未設定 → シンプル分割を使用")
        return split_into_sections_simple(title_en, body_en)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        prompt = f"""You are a horror video script writer for English-speaking audiences on YouTube Shorts and TikTok.

Given this translated Japanese horror story, structure it into exactly 4 sections for a short horror video.
Total narration should be under 220 words. Make it feel like a creepypasta narrator — mysterious, unsettling, atmospheric.

Story title: {title_en}
Story: {body_en[:2000]}

Return ONLY valid JSON (no markdown, no explanation):
[
  {{
    "caption": "HOOK",
    "narration": "1-2 shocking sentences that grab attention immediately. Start with something unsettling.",
    "imagePrompt": "dark abandoned japanese torii gate at night, red lanterns fading into fog, eerie silence"
  }},
  {{
    "caption": "THE SETTING",
    "narration": "2-3 sentences establishing the eerie atmosphere and location.",
    "imagePrompt": "empty crumbling japanese house interior, moonlight through broken shoji screens, dust and shadows"
  }},
  {{
    "caption": "THE EVENT",
    "narration": "3-4 sentences describing the terrifying event. Build tension.",
    "imagePrompt": "dark forest path japan at midnight, ghostly silhouette between trees, supernatural mist rising"
  }},
  {{
    "caption": "NO EXPLANATION",
    "narration": "1-2 sentences. Leave it unresolved. No happy ending. Something is still wrong.",
    "imagePrompt": "lonely foggy road through japanese bamboo forest, single dim streetlight, ominous atmosphere"
  }}
]

Rules:
- imagePrompt: vivid visual scene description for AI image generation (Flux model), 10-20 words
  Focus on: location, lighting, mood, specific Japanese horror elements (shrine, torii, bamboo, etc.)
- Keep cultural context vague but mention "Japan" or "Japanese" once
- Never explain the horror — let it be ambiguous
- Tone: like the narrator of Lazy Masquerade or Be. Busta"""

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # JSON部分を抽出
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return json.loads(raw)

    except Exception as e:
        print(f"  ⚠ Claude API失敗: {e} → シンプル分割を使用")
        return split_into_sections_simple(title_en, body_en)


# ══════════════════════════════════════════════════════════════
# 4. Pollinations.ai 画像生成（APIキー不要・完全無料）
# ══════════════════════════════════════════════════════════════

def fetch_pollinations_image(image_prompt: str, out_path: Path) -> bool:
    """
    Pollinations.ai（Fluxモデル）でホラー画像を生成してダウンロードする。
    APIキー不要。1920x1080 JPEGを返す。
    """
    import urllib.parse

    full_prompt = image_prompt + HORROR_IMAGE_STYLE
    encoded    = urllib.parse.quote(full_prompt)
    url        = POLLINATIONS_URL.format(prompt=encoded)

    print(f"  🎨 AI画像生成中: {image_prompt[:60]}...")
    try:
        # Pollinationsは生成に10〜30秒かかることがある
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        if len(resp.content) < 1000:
            print(f"  ⚠ 画像が小さすぎます（{len(resp.content)}bytes）→ フォールバック")
            return False
        out_path.write_bytes(resp.content)
        print(f"  ✅ AI画像保存: {out_path.name} ({len(resp.content)//1024}KB)")
        return True
    except Exception as e:
        print(f"  ⚠ Pollinations取得失敗: {e}")
        return False

def create_fallback_image(out_path: Path, section_index: int):
    """Pillowで暗いグラデーション画像を生成する（Pollinationsが失敗した場合）。"""
    try:
        from PIL import Image, ImageFilter

        colors = [
            ((5, 2, 8),  (20, 10, 25)),   # 深紫
            ((2, 8, 5),  (10, 25, 15)),   # 深緑
            ((8, 2, 2),  (25, 10, 10)),   # 深赤
            ((2, 5, 8),  (10, 15, 25)),   # 深青
        ]
        c1, c2 = colors[section_index % len(colors)]
        img    = Image.new("RGB", (1920, 1080), c1)
        pixels = img.load()
        for y in range(1080):
            ratio = y / 1080
            r = int(c1[0] + (c2[0] - c1[0]) * ratio)
            g = int(c1[1] + (c2[1] - c1[1]) * ratio)
            b = int(c1[2] + (c2[2] - c1[2]) * ratio)
            for x in range(1920):
                pixels[x, y] = (r, g, b)
        img = img.filter(ImageFilter.GaussianBlur(radius=2))
        img.save(out_path, "JPEG", quality=85)
        print(f"  ✅ フォールバック画像生成: {out_path.name}")
    except ImportError:
        # 最小限の黒JPEGを書き込む
        out_path.write_bytes(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
            b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
            b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xc0"
            b"\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00"
            b"\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01"
            b"\x01\x00\x00?\x00\xfb\xd5\xff\xd9"
        )
        print(f"  ✅ 最小フォールバック画像: {out_path.name}")


# ══════════════════════════════════════════════════════════════
# 4b. ffmpeg で画像をホラー動画に変換
# ══════════════════════════════════════════════════════════════

def find_ffmpeg() -> str | None:
    """ffmpegの実行パスを返す。見つからなければNone。"""
    import shutil
    return shutil.which("ffmpeg")

def animate_image_with_ffmpeg(
    image_path: Path,
    output_path: Path,
    duration: float = 5.0,
) -> bool:
    """
    静止画にffmpegでホラーエフェクトをかけてMP4に変換する。

    エフェクト:
      - zoompan : ゆっくりズームイン（1.0 → 1.12、中央固定）
      - noise   : フィルムグレイン（時間経過で変化するテンポラルノイズ）
      - vignette: 画面端を暗くするビネット効果
      - eq      : 彩度を下げてコントラストを上げるカラーグレード
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("  ⚠ ffmpegが見つかりません。brew install ffmpeg でインストールしてください。")
        return False

    fps    = 30
    frames = int(duration * fps)

    # ── フィルターチェーン ──────────────────────────────────────
    # 1. zoompan: 1.0 → 1.12 へゆっくりズーム（中央でパン固定）
    #    zoom の増分 = (1.12 - 1.0) / frames = 0.12 / frames
    zoom_delta = 0.12 / frames
    zoompan = (
        f"zoompan="
        f"z='min(zoom+{zoom_delta:.6f},1.12)':"
        f"d={frames}:"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
        f"s=1920x1080"
    )
    # 2. fps を明示的に指定
    fps_filter = f"fps={fps}"
    # 3. noise: テンポラルノイズ（フレームごとに変化）
    #    c0s=輝度ノイズ強度, c1s/c2s=色差ノイズ強度, allf=t=temporal
    noise = "noise=c0s=18:c1s=6:c2s=6:allf=t"
    # 4. vignette: PI/4 ≒ 45° ≒ 中程度のビネット
    vignette = "vignette=PI/4"
    # 5. eq: 彩度0.55（くすんだ色）、コントラスト1.2（深い黒）、輝度-0.04（暗め）
    color_grade = "eq=saturation=0.55:contrast=1.2:brightness=-0.04"

    vf = ",".join([zoompan, fps_filter, noise, vignette, color_grade])
    # ──────────────────────────────────────────────────────────

    cmd = [
        ffmpeg, "-y",
        "-loop", "1",
        "-i",    str(image_path),
        "-vf",   vf,
        "-t",    str(duration),
        "-c:v",  "libx264",
        "-preset", "fast",
        "-crf",  "20",
        "-pix_fmt", "yuv420p",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            size_kb = output_path.stat().st_size // 1024
            print(f"  ✅ ffmpeg動画化: {output_path.name} ({size_kb}KB, {duration}秒)")
            return True
        else:
            print(f"  ⚠ ffmpeg失敗:\n{result.stderr[-300:]}")
            return False
    except subprocess.TimeoutExpired:
        print("  ⚠ ffmpegタイムアウト（120秒）")
        return False
    except Exception as e:
        print(f"  ⚠ ffmpeg例外: {e}")
        return False


def animate_image(
    image_path: Path,
    output_path: Path,
    duration: float = 5.0,
    backend: str = "ffmpeg",
) -> bool:
    """
    画像をアニメーション動画に変換するディスパッチャー。

    backend:
      "ffmpeg" — ローカルffmpegで処理（無料・即時）
      "svd"    — Stable Video Diffusion（将来実装）
      "runway" — Runway gen4_turbo I2V（有料）

    SVDへの切替方法:
      animate_image(..., backend="svd")
      → animate_image_with_svd() を実装して呼び出すだけでOK
    """
    if backend == "ffmpeg":
        return animate_image_with_ffmpeg(image_path, output_path, duration)

    # ── 将来のバックエンド ──
    elif backend == "svd":
        # TODO: animate_image_with_svd(image_path, output_path, duration) を実装
        print("  ⚠ SVDバックエンドは未実装です。ffmpegにフォールバックします。")
        return animate_image_with_ffmpeg(image_path, output_path, duration)

    elif backend == "runway":
        # TODO: animate_image_with_runway(image_path, output_path, duration) を実装
        print("  ⚠ Runwayバックエンドは未実装です。ffmpegにフォールバックします。")
        return animate_image_with_ffmpeg(image_path, output_path, duration)

    else:
        print(f"  ⚠ 不明なバックエンド: {backend} → ffmpegを使用")
        return animate_image_with_ffmpeg(image_path, output_path, duration)


# ══════════════════════════════════════════════════════════════
# 5. 音声生成
# ══════════════════════════════════════════════════════════════

def generate_audio_gtts(text: str, out_path: Path) -> bool:
    """gTTS（完全無料）で音声生成する。"""
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang="en", slow=False)
        tts.save(str(out_path))
        print(f"  ✅ gTTS音声: {out_path.name}")
        return True
    except ImportError:
        print("  ⚠ gTTS未インストール: pip install gtts")
        return False
    except Exception as e:
        print(f"  ⚠ gTTS失敗: {e}")
        return False

def generate_audio_elevenlabs(text: str, out_path: Path, voice_key: str = DEFAULT_ELEVENLABS_VOICE) -> bool:
    """ElevenLabs（月10,000文字まで無料）で音声生成する。"""
    if not ELEVENLABS_API_KEY:
        return False
    voice_id = ELEVENLABS_VOICES.get(voice_key, ELEVENLABS_VOICES["adam"])
    try:
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={"text": text, "model_id": "eleven_v3", "output_format": "mp3_44100_128"},
            timeout=30,
        )
        if resp.status_code == 200:
            out_path.write_bytes(resp.content)
            print(f"  ✅ ElevenLabs音声: {out_path.name}")
            return True
        else:
            err = resp.json().get("detail", {})
            print(f"  ⚠ ElevenLabs失敗 ({resp.status_code}): {err}")
            return False
    except Exception as e:
        print(f"  ⚠ ElevenLabs失敗: {e}")
        return False

def get_audio_duration(path: Path) -> float:
    """MP3の再生時間を秒で返す。"""
    try:
        from mutagen.mp3 import MP3
        return MP3(str(path)).info.length
    except Exception:
        # フォールバック: ファイルサイズから推定（128kbps MP3）
        size = path.stat().st_size
        return size / 16000.0  # 128kbps = 16000 bytes/sec


# ══════════════════════════════════════════════════════════════
# 6. project.json生成
# ══════════════════════════════════════════════════════════════

def build_project_json(
    title_en: str,
    sections_data: list[dict],
    video_paths: list[Path],      # ffmpeg / SVD で生成したMP4
    audio_paths: list[Path],
    image_paths: list[Path] | None = None,  # 動画化失敗時のフォールバック用
) -> dict:
    """
    Remotion用のproject定義JSONを組み立てる。

    各セクションはffmpegで動画化されたMP4を `video` フィールドに設定する。
    動画化が失敗している場合は `photos` モードにフォールバックする。
    """
    sections = []
    for i, (sec, vid_path, aud_path) in enumerate(zip(sections_data, video_paths, audio_paths)):
        dur     = get_audio_duration(aud_path) if aud_path.exists() else 8.0
        aud_rel = str(aud_path.relative_to(ROOT / "public"))

        # MP4が正常に生成されているか確認
        video_ok = vid_path.exists() and vid_path.stat().st_size > 1000

        if video_ok:
            vid_rel = str(vid_path.relative_to(ROOT / "public"))
            section = {
                "caption":   sec["caption"],
                "video":     vid_rel,          # ← MP4動画を使用
                "audio":     aud_rel,
                "narration": sec["narration"],
                "durSec":    round(dur, 1),
            }
        else:
            # 動画化失敗 → 静止画フォールバック
            img_path = (image_paths or [])[i] if image_paths and i < len(image_paths) else None
            img_rel  = str(img_path.relative_to(ROOT / "public")) if img_path and img_path.exists() else ""
            section  = {
                "caption":   sec["caption"],
                "photos":    [img_rel] if img_rel else [],
                "audio":     aud_rel,
                "narration": sec["narration"],
                "durSec":    round(dur, 1),
            }
            print(f"  ⚠ セクション{i+1}: 動画化失敗 → 静止画モードで代替")

        sections.append(section)

    return {
        "project": {
            "title":       title_en,
            "subtitle":    "From the darkest corners of the Japanese internet",
            "theme":       "dark",
            "textMode":    "subtitle",
            "endingText":  "Something is still out there.",
            "sections":    sections,
        }
    }


# ══════════════════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="2ちゃん怖い話 → 英語ホラー動画 全自動生成")
    p.add_argument("--dry-run",  action="store_true", help="レンダリングを省略（テスト用）")
    p.add_argument("--story",    default="",          help="日本語ストーリーを直接指定")
    p.add_argument("--title",    default="",          help="タイトルを直接指定（--storyと併用）")
    p.add_argument("--voice",    default="gtts",      help="音声エンジン: gtts (無料) | elevenlabs")
    p.add_argument("--el-voice", default=DEFAULT_ELEVENLABS_VOICE,
                   help=f"ElevenLabsボイス: {', '.join(ELEVENLABS_VOICES.keys())}")
    p.add_argument("--animate",  default="ffmpeg",
                   help="動画化バックエンド: ffmpeg (無料) | svd (将来実装) | runway (有料)")
    p.add_argument("--clip-duration", type=float, default=5.0,
                   help="各クリップの秒数（デフォルト: 5.0秒）")
    return p.parse_args()

def find_npm_bin() -> str:
    """npmのパスを探す。nvm環境に対応。"""
    import shutil
    # shutil.whichで見つかればそれを使う
    npm = shutil.which("npm")
    if npm:
        return str(Path(npm).parent)
    # nvm の典型的なパスを試す
    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.exists():
        versions = sorted(nvm_dir.iterdir(), reverse=True)
        for v in versions:
            bin_path = v / "bin"
            if (bin_path / "npm").exists():
                return str(bin_path)
    return ""

def get_env_with_node() -> dict:
    """Node.jsのbinをPATHに追加した環境変数を返す。"""
    env = os.environ.copy()
    node_bin = find_npm_bin()
    if node_bin:
        env["PATH"] = node_bin + ":" + env.get("PATH", "")
    return env

def check_node_modules():
    """node_modulesがなければnpm installを実行する。"""
    if not (ROOT / "node_modules").exists():
        print("\n[Setup] node_modules未インストール → npm install 実行中...")
        result = subprocess.run(
            ["npm", "install"], cwd=ROOT,
            capture_output=True, text=True,
            env=get_env_with_node(),
        )
        if result.returncode != 0:
            print(f"❌ npm install 失敗:\n{result.stderr}")
            sys.exit(1)
        print("  ✅ npm install 完了")

def main():
    args = parse_args()

    print("\n" + "═" * 60)
    print("🎬 ホラー動画自動生成パイプライン")
    print("═" * 60)

    # ─ Step 1: ストーリー取得 ─
    if args.story:
        print("\n[1/7] 手動ストーリーを使用")
        story = {
            "title":  args.title or "A Strange Story from Japan",
            "body":   args.story,
            "url":    "",
            "source": "manual",
        }
    else:
        story = scrape_story()
        if not story:
            print("❌ ストーリー取得失敗。--story オプションで直接指定してください。")
            sys.exit(1)

    title_ja = story["title"]
    body_ja  = story["body"][:3000]

    # ─ Step 2: 英訳 ─
    print("\n[2/7] 英訳中...")
    title_en = translate(title_ja)
    body_en  = translate(body_ja)
    print(f"  タイトル: {title_en[:60]}")
    print(f"  本文: {len(body_en)}文字")

    # ─ Step 3: スクリプト構造化 ─
    print("\n[3/7] ホラースクリプト生成中...")
    sections_data = generate_script_with_claude(title_en, body_en)
    print(f"  ✅ {len(sections_data)}セクション生成")
    for s in sections_data:
        print(f"    [{s['caption']}] {s['narration'][:60]}...")

    # ─ 出力ディレクトリ準備 ─
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = PUBLIC_DIR / timestamp
    session_dir.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)

    # ─ Step 4: AI画像生成（Pollinations.ai） ─
    print("\n[4/7] AI画像生成中（Pollinations.ai・Fluxモデル）...")
    print("  ℹ APIキー不要。1枚あたり10〜30秒かかります。")

    image_paths = []
    for i, sec in enumerate(sections_data):
        img_path   = session_dir / f"img_{i+1:02d}.jpg"
        img_prompt = sec.get("imagePrompt", "dark abandoned japanese shrine")
        ok = fetch_pollinations_image(img_prompt, img_path)
        if not ok:
            print(f"  → フォールバック画像を生成")
            create_fallback_image(img_path, i)
        image_paths.append(img_path)
        time.sleep(4)  # Pollinationsのレート制限対策（429回避）

    # ─ Step 5: ffmpegで動画化 ─
    print(f"\n[5/7] 画像をMP4動画に変換中（backend={args.animate}）...")
    if not find_ffmpeg():
        print("  ⚠ ffmpegが見つかりません")
        print("    インストール: brew install ffmpeg")

    video_paths = []
    for i, img_path in enumerate(image_paths):
        vid_path = session_dir / f"clip_{i+1:02d}.mp4"
        ok = animate_image(
            image_path=img_path,
            output_path=vid_path,
            duration=args.clip_duration,
            backend=args.animate,
        )
        if not ok:
            print(f"  ⚠ 動画化失敗 → 静止画モードで代替（セクション{i+1}）")
        video_paths.append(vid_path)

    # ─ Step 6: 音声生成 ─
    print(f"\n[6/7] 音声生成中 ({args.voice})...")
    audio_paths = []
    for i, sec in enumerate(sections_data):
        aud_path  = session_dir / f"nar_{i+1:02d}.mp3"
        narration = sec["narration"]

        ok = False
        if args.voice == "elevenlabs" and ELEVENLABS_API_KEY:
            ok = generate_audio_elevenlabs(narration, aud_path, args.el_voice)
        if not ok:
            ok = generate_audio_gtts(narration, aud_path)
        if not ok:
            print(f"  ❌ 音声生成失敗: セクション{i+1}")
            aud_path.write_bytes(b"")

        audio_paths.append(aud_path)
        time.sleep(0.5)

    # ─ Step 7: project.json生成 ─
    print("\n[7/7] project.json生成中...")
    project = build_project_json(
        title_en, sections_data, video_paths, audio_paths, image_paths
    )
    PROPS_FILE.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✅ {PROPS_FILE}")

    # ─ プレビュー ─
    print("\n" + "─" * 60)
    print("📋 生成内容プレビュー:")
    print(f"  タイトル: {project['project']['title']}")
    total_dur = sum(s["durSec"] for s in project["project"]["sections"])
    print(f"  総尺: {total_dur:.1f}秒")
    for s in project["project"]["sections"]:
        mode = "video" if "video" in s else "photo"
        print(f"  [{s['caption']}] {s['durSec']}秒 [{mode}] | {s['narration'][:45]}...")
    print("─" * 60)

    if args.dry_run:
        print("\n✅ DRY_RUN完了（レンダリングはスキップ）")
        print(f"手動レンダリング: npx remotion render AIVideo out/horror_{timestamp}.mp4 --props=horror_props.json")
        return

    # ─ Remotionレンダリング ─
    check_node_modules()
    out_file = OUT_DIR / f"horror_{timestamp}.mp4"
    print(f"\n🎬 Remotionレンダリング開始...")
    print(f"  出力: {out_file}")

    result = subprocess.run(
        ["npx", "remotion", "render", "AIVideo", str(out_file), f"--props={PROPS_FILE}"],
        cwd=ROOT,
        text=True,
        env=get_env_with_node(),
    )

    if result.returncode == 0:
        print(f"\n✅ 動画生成完了!")
        print(f"  📁 {out_file}")
        size_mb = out_file.stat().st_size / 1024 / 1024
        print(f"  サイズ: {size_mb:.1f}MB")
    else:
        print(f"\n❌ レンダリング失敗 (終了コード: {result.returncode})")
        print(f"  手動で確認: npx remotion studio")


if __name__ == "__main__":
    main()
