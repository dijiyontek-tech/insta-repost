#!/usr/bin/env python3
"""
Kendi Instagram videolarini otomatik "remix"leyip (çevirme / hafif efekt /
üst yazı değiştirme) yeniden Reels olarak paylaşan script.

Sadece resmi Meta Graph API kullanır — kullanıcı adı/şifre ile giriş yoktur.
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

GRAPH_API_VERSION = "v21.0"
# "Instagram API with Instagram Login" akışıyla üretilen token'lar
# graph.facebook.com değil, graph.instagram.com üzerinden çalışıyor.
GRAPH_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"

IG_USER_ID = os.environ["IG_BUSINESS_ACCOUNT_ID"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "insta-repost")

STATE_PATH = "state/processed.json"
MAX_REPOSTS_PER_VIDEO = int(os.environ.get("MAX_REPOSTS_PER_VIDEO", "1"))
CAPTION_SUFFIX = os.environ.get("CAPTION_SUFFIX", "")
FONT_PATH = os.environ.get("FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")

TEXT_VARIANTS_FILE = Path(__file__).parent.parent / "assets" / "captions.txt"


def log(msg: str) -> None:
    print(f"[repost] {msg}", flush=True)


def raise_for_status_verbose(r: requests.Response) -> None:
    """r.raise_for_status() gibi ama hata gövdesini de loga yazar (Graph API/Supabase
    hata mesajları genelde JSON body içinde, aksi halde sebep görünmüyor)."""
    if not r.ok:
        log(f"HTTP {r.status_code} yanıt gövdesi: {r.text[:2000]}")
    r.raise_for_status()


# ---------- Supabase Storage yardımcıları ----------

def supabase_headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
    }


def load_state() -> dict:
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{STATE_PATH}"
    r = requests.get(url, headers=supabase_headers())
    if r.status_code == 200:
        return r.json()
    # Supabase Storage, henüz hiç yüklenmemiş bir dosya için 404 yerine
    # 400 de dönebiliyor (obje/klasör hiç oluşturulmamışsa) — ilk
    # çalıştırmada bu normal, boş state ile devam ediyoruz.
    if r.status_code in (400, 404):
        return {}
    raise_for_status_verbose(r)
    return {}


def save_state(state: dict) -> None:
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{STATE_PATH}"
    r = requests.put(
        url,
        headers={**supabase_headers(), "Content-Type": "application/json", "x-upsert": "true"},
        data=json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    raise_for_status_verbose(r)


def upload_video(local_path: Path, remote_name: str) -> str:
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/videos/{remote_name}"
    with open(local_path, "rb") as f:
        r = requests.put(
            url,
            headers={**supabase_headers(), "Content-Type": "video/mp4", "x-upsert": "true"},
            data=f,
        )
    raise_for_status_verbose(r)
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/videos/{remote_name}"


def delete_video(remote_name: str) -> None:
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/videos/{remote_name}"
    requests.delete(url, headers=supabase_headers())


def cleanup_old_videos(max_age_hours: int = 24) -> None:
    """videos/ altında max_age_hours'tan eski dosyaları siler (özellikle deneme modu artıkları için)."""
    from datetime import datetime, timezone

    url = f"{SUPABASE_URL}/storage/v1/object/list/{SUPABASE_BUCKET}"
    r = requests.post(url, headers=supabase_headers(), json={"prefix": "videos"})
    if r.status_code != 200:
        return
    now = datetime.now(timezone.utc)
    stale = []
    for item in r.json():
        created_at = item.get("created_at")
        name = item.get("name")
        if not created_at or not name:
            continue
        age_hours = (now - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds() / 3600
        if age_hours > max_age_hours:
            stale.append(f"videos/{name}")
    if stale:
        requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}",
            headers=supabase_headers(),
            json={"prefixes": stale},
        )
        log(f"{len(stale)} eski geçici video temizlendi.")


# ---------- Instagram Graph API yardımcıları ----------

def fetch_own_videos(limit: int = 50) -> list:
    """Kendi hesabındaki video/Reels medyalarını çeker (sayfalayarak)."""
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    params = {
        "fields": "id,media_type,media_product_type,media_url,caption,timestamp,permalink",
        "limit": limit,
        "access_token": IG_ACCESS_TOKEN,
    }
    items = []
    while url:
        r = requests.get(url, params=params)
        raise_for_status_verbose(r)
        data = r.json()
        items.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = None  # 'next' URL zaten tüm query'yi içeriyor
    return [
        it for it in items
        if it.get("media_type") == "VIDEO" or it.get("media_product_type") == "REELS"
    ]


def create_media_container(video_url: str, caption: str) -> str:
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    r = requests.post(url, data={
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN,
    })
    raise_for_status_verbose(r)
    return r.json()["id"]


def wait_until_ready(creation_id: str, timeout: int = 600, interval: int = 10) -> None:
    url = f"{GRAPH_BASE}/{creation_id}"
    waited = 0
    while waited < timeout:
        r = requests.get(url, params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN})
        raise_for_status_verbose(r)
        data = r.json()
        code = data.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"Instagram video işleme hatası: {data}")
        time.sleep(interval)
        waited += interval
    raise TimeoutError("Instagram video işleme zaman aşımına uğradı")


def publish_media(creation_id: str) -> str:
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media_publish"
    r = requests.post(url, data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN})
    raise_for_status_verbose(r)
    return r.json()["id"]


# ---------- Video işleme ----------

def pick_text_variant() -> Optional[str]:
    if not TEXT_VARIANTS_FILE.exists():
        return None
    lines = [l.strip() for l in TEXT_VARIANTS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    return random.choice(lines) if lines else None


def process_video(src: Path, dst: Path) -> None:
    """Videoyu çevirir, renk/kontrast varyasyonu + keskinlik + vinyet uygular,
    hafif hız değişimi yapar ve videonun başında birkaç saniye üst yazı gösterir."""
    filters = []

    if random.random() < 0.6:
        filters.append("hflip")

    filters.append("crop=iw*0.97:ih*0.97")
    filters.append("scale=1080:1920")

    brightness = round(random.uniform(-0.04, 0.04), 3)
    contrast = round(random.uniform(0.92, 1.12), 3)
    saturation = round(random.uniform(0.9, 1.15), 3)
    filters.append(f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}")

    # Hafif keskinlik ve kenar kararması (vinyet) — telefon kamera uygulamalarının
    # varsayılan "pop" efektine benzer, düz ffmpeg çıktısını daha az "ham" gösterir.
    filters.append("unsharp=5:5:0.8:5:5:0.0")
    filters.append("vignette=PI/5")

    text = pick_text_variant()
    if text:
        escaped = text.replace(":", "\\:").replace("'", "\\'")
        filters.append(
            f"drawtext=fontfile={FONT_PATH}:"
            f"text='{escaped}':fontcolor=white:fontsize=68:"
            "box=1:boxcolor=black@0.5:boxborderw=24:"
            "shadowcolor=black@0.6:shadowx=2:shadowy=2:"
            "x=(w-text_w)/2:y=h*0.08:"
            "enable='lt(t,4)'"
        )

    filter_chain = ",".join(filters)

    # Hafif hız değişimi (video + ses birlikte) — hem görsel imzayı biraz daha
    # değiştirir hem de videoya hafif dinamizm katar.
    speed = round(random.uniform(0.97, 1.04), 3)

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"{filter_chain},setpts={1 / speed:.4f}*PTS",
        "-af", f"atempo={speed}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


# ---------- Ana akış ----------

def main() -> None:
    cleanup_old_videos()
    state = load_state()
    videos = fetch_own_videos()
    if not videos:
        log("Hesapta video bulunamadı.")
        return

    random.shuffle(videos)
    candidate = None
    for v in videos:
        count = state.get(v["id"], {}).get("repost_count", 0)
        if count < MAX_REPOSTS_PER_VIDEO:
            candidate = v
            break

    if not candidate:
        log("Tüm videolar limit sayısı kadar remix'lenmiş. MAX_REPOSTS_PER_VIDEO'yu artırmayı düşünebilirsin.")
        return

    log(f"Seçilen video: {candidate['id']} ({candidate.get('permalink')})")

    remote_name = f"{candidate['id']}-{int(time.time())}.mp4"

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "source.mp4"
        dst = Path(tmp) / "processed.mp4"

        r = requests.get(candidate["media_url"], stream=True)
        raise_for_status_verbose(r)
        with open(src, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)

        process_video(src, dst)
        public_url = upload_video(dst, remote_name)
        log(f"Video Supabase'e yüklendi: {public_url}")

    caption = (candidate.get("caption") or "").strip()
    if CAPTION_SUFFIX:
        caption = f"{caption}\n\n{CAPTION_SUFFIX}" if caption else CAPTION_SUFFIX

    if DRY_RUN:
        log("DENEME MODU: Instagram'a paylaşılmadı. Videoyu şu linkten izleyebilirsin:")
        log(public_url)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(
                    "## 🎬 Deneme modu — işlenmiş video\n\n"
                    f"[Videoyu izle]({public_url})\n\n"
                    f"Video 24 saatten eskiyince bir sonraki çalıştırmada otomatik silinir. Beğendiysen "
                    "\"Run workflow\" ile bu sefer **Deneme modu**'nu kapatıp gerçek "
                    "paylaşımı tetikleyebilirsin.\n"
                )
        return

    try:
        creation_id = create_media_container(public_url, caption)
        log(f"Container oluşturuldu: {creation_id}")
        wait_until_ready(creation_id)
        media_id = publish_media(creation_id)
        log(f"Yayınlandı! Yeni medya ID: {media_id}")
    finally:
        delete_video(remote_name)
        log("Geçici video Supabase'ten silindi.")

    entry = state.get(candidate["id"], {"repost_count": 0})
    entry["repost_count"] = entry.get("repost_count", 0) + 1
    entry["last_reposted_at"] = int(time.time())
    state[candidate["id"]] = entry
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - GitHub Actions'ta hatayı görünür kılmak için
        log(f"HATA: {exc}")
        sys.exit(1)
