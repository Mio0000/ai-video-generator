#!/usr/bin/env python3
"""
horror_pipeline.py — 2ちゃん怖い話 → 英語ホラー動画 全自動生成

フロー:
  2ちゃんスクレイプ → 英訳 → Claudeでスクリプト構造化
  → Pexels画像取得(無料) → gTTS音声生成(無料)
  → Remotionで動画レンダリング(無料)

使い方:
  python horror_pipeline.py                        # 全自動
  python horror_pipeline.py --dry-run              # レンダリング省略テスト
  python horror_pipeline.py --story "手動テキスト"  # 直接テキスト指定
  python horror_pipeline.py --voice elevenlabs     # ElevenLabsで高品質音声
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
PEXELS_API_KEY      = os.getenv("PEXELS_API_KEY", "")
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")

PUBLIC_DIR  = ROOT / "public" / "horror"
OUT_DIR     = ROOT / "out"
PROPS_FILE  = ROOT / "horror_props.json"
HISTORY_FILE = ROOT / "horror_history.json"

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

# セクションタイプごとのPexels検索クエリ候補
PEXELS_QUERIES = {
    "hook":      ["dark abandoned shrine japan", "horror foggy night japan", "eerie dark forest japan"],
    "setting":   ["abandoned japanese house dark", "dark corridor empty", "foggy japanese shrine"],
    "event":     ["dark shadow silhouette horror", "supernatural horror darkness", "ghost shadow dark"],
    "aftermath": ["lonely dark road japan", "dark abandoned room horror", "foggy empty street night"],
    "end":       ["dark torii gate fog", "japanese shrine dark mystery", "horror dark japan night"],
}

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
    print("\n[1/6] 2ちゃんネタ収集中...")
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
    ("HOOK",      "hook"),
    ("THE SETTING", "setting"),
    ("THE EVENT", "event"),
    ("WHAT REMAINED", "end"),
]

def split_into_sections_simple(title_en: str, body_en: str) -> list[dict]:
    """Claudeなしでテキストを4分割するフォールバック。"""
    sentences = re.split(r'(?<=[.!?])\s+', body_en.strip())
    sentences = [s for s in sentences if len(s) > 20]
    if len(sentences) < 4:
        sentences *= 2  # 短すぎる場合は繰り返す

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
        import random
        queries = PEXELS_QUERIES[qtype]
        sections.append({
            "caption":    caption,
            "narration":  narration,
            "imageQuery": random.choice(queries),
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
    "imageQuery": "dark foggy japanese shrine night"
  }},
  {{
    "caption": "THE SETTING",
    "narration": "2-3 sentences establishing the eerie atmosphere and location.",
    "imageQuery": "abandoned japanese house dark corridor"
  }},
  {{
    "caption": "THE EVENT",
    "narration": "3-4 sentences describing the terrifying event. Build tension.",
    "imageQuery": "dark shadow silhouette horror"
  }},
  {{
    "caption": "NO EXPLANATION",
    "narration": "1-2 sentences. Leave it unresolved. No happy ending. Something is still wrong.",
    "imageQuery": "lonely dark road fog japan"
  }}
]

Rules:
- imageQuery: simple Pexels search terms, 3-5 words, dark/horror themed
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
# 4. Pexels画像取得
# ══════════════════════════════════════════════════════════════

def fetch_pexels_image(query: str, out_path: Path, index: int) -> bool:
    """Pexels APIで画像を検索してダウンロードする。"""
    if not PEXELS_API_KEY:
        return False
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": 5, "orientation": "landscape"},
            timeout=10,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if not photos:
            return False
        # インデックスで違う写真を選ぶ（同じ写真を使い回さない）
        photo = photos[index % len(photos)]
        img_url = photo["src"]["large2x"]

        img_resp = requests.get(img_url, timeout=20)
        img_resp.raise_for_status()
        out_path.write_bytes(img_resp.content)
        print(f"  ✅ 画像保存: {out_path.name} ({query})")
        time.sleep(0.5)
        return True
    except Exception as e:
        print(f"  ⚠ Pexels取得失敗 ({query}): {e}")
        return False

def create_fallback_image(out_path: Path, section_index: int):
    """Pillowで暗いグラデーション画像を生成する（Pexelsキーなし時のフォールバック）。"""
    try:
        from PIL import Image, ImageFilter
        import random

        # 暗い色のグラデーション（セクションごとに色味を変える）
        colors = [
            ((5, 2, 8), (20, 10, 25)),    # 深紫
            ((2, 8, 5), (10, 25, 15)),    # 深緑
            ((8, 2, 2), (25, 10, 10)),    # 深赤
            ((2, 5, 8), (10, 15, 25)),    # 深青
        ]
        c1, c2 = colors[section_index % len(colors)]

        img = Image.new("RGB", (1920, 1080), c1)
        # 簡易グラデーション（上から下）
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
        # Pillowもない場合は最小JPEGを書き込む（真っ黒）
        # 1x1の真っ黒なJPEG（最小バイナリ）を1920x1080にリサイズしたもの
        _write_minimal_dark_image(out_path)

def _write_minimal_dark_image(out_path: Path):
    """最小限の暗い画像を書き込む（依存なし）。"""
    # 最小JPEGヘッダ（1x1黒ピクセル）
    minimal_jpeg = bytes([
        0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
        0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
        0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
        0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
        0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
        0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
        0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
        0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
        0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
        0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
        0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
        0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
        0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
        0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
        0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
        0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
        0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
        0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
        0x00, 0x00, 0x3F, 0x00, 0xFB, 0xD5, 0xFF, 0xD9,
    ])
    out_path.write_bytes(minimal_jpeg)


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
    image_paths: list[Path],
    audio_paths: list[Path],
) -> dict:
    """Remotion用のproject定義JSONを組み立てる。"""
    sections = []
    for i, (sec, img_path, aud_path) in enumerate(zip(sections_data, image_paths, audio_paths)):
        dur = get_audio_duration(aud_path) if aud_path.exists() else 8.0
        # publicフォルダからの相対パス
        img_rel = str(img_path.relative_to(ROOT / "public"))
        aud_rel = str(aud_path.relative_to(ROOT / "public"))

        sections.append({
            "caption":   sec["caption"],
            "photos":    [img_rel],
            "audio":     aud_rel,
            "narration": sec["narration"],
            "durSec":    round(dur, 1),
        })

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
        print("\n[1/6] 手動ストーリーを使用")
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
    print("\n[2/6] 英訳中...")
    title_en = translate(title_ja)
    body_en  = translate(body_ja)
    print(f"  タイトル: {title_en[:60]}")
    print(f"  本文: {len(body_en)}文字")

    # ─ Step 3: スクリプト構造化 ─
    print("\n[3/6] ホラースクリプト生成中...")
    sections_data = generate_script_with_claude(title_en, body_en)
    print(f"  ✅ {len(sections_data)}セクション生成")
    for s in sections_data:
        print(f"    [{s['caption']}] {s['narration'][:60]}...")

    # ─ 出力ディレクトリ準備 ─
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = PUBLIC_DIR / timestamp
    session_dir.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)

    # ─ Step 4: 画像取得 ─
    print("\n[4/6] ホラー画像取得中...")
    if not PEXELS_API_KEY:
        print("  ℹ PEXELS_API_KEY未設定 → フォールバック画像を生成")
        print("    (無料キー取得: https://www.pexels.com/api/)")

    image_paths = []
    for i, sec in enumerate(sections_data):
        img_path = session_dir / f"img_{i+1:02d}.jpg"
        query = sec.get("imageQuery", "dark horror")
        ok = fetch_pexels_image(query, img_path, i)
        if not ok:
            create_fallback_image(img_path, i)
        image_paths.append(img_path)

    # ─ Step 5: 音声生成 ─
    print(f"\n[5/6] 音声生成中 ({args.voice})...")
    audio_paths = []
    for i, sec in enumerate(sections_data):
        aud_path = session_dir / f"nar_{i+1:02d}.mp3"
        narration = sec["narration"]

        ok = False
        if args.voice == "elevenlabs" and ELEVENLABS_API_KEY:
            ok = generate_audio_elevenlabs(narration, aud_path, args.el_voice)
        if not ok:
            ok = generate_audio_gtts(narration, aud_path)
        if not ok:
            print(f"  ❌ 音声生成失敗: セクション{i+1}")
            # ダミー用の空MP3を作成
            aud_path.write_bytes(b"")

        audio_paths.append(aud_path)
        time.sleep(0.5)

    # ─ Step 6: project.json生成 ─
    print("\n[6/6] project.json生成中...")
    project = build_project_json(title_en, sections_data, image_paths, audio_paths)
    PROPS_FILE.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✅ {PROPS_FILE}")

    # ─ プレビュー ─
    print("\n" + "─" * 60)
    print("📋 生成内容プレビュー:")
    print(f"  タイトル: {project['project']['title']}")
    total_dur = sum(s["durSec"] for s in project["project"]["sections"])
    print(f"  総尺: {total_dur:.1f}秒")
    for s in project["project"]["sections"]:
        print(f"  [{s['caption']}] {s['durSec']}秒 | {s['narration'][:50]}...")
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
