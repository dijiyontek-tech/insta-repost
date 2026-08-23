#!/usr/bin/env python3
"""
Kendi Instagram videolarini otomatik "remix"leyip (renk/kontrast/hız
varyasyonu) yeniden Reels olarak paylaşan script.

Paylaşım tamamen resmi Meta Graph API ile yapılır — kullanıcı adı/şifre ile
giriş yoktur. Kaynak videoyu indirmek için önce Graph API'nin verdiği
media_url kullanılır; API bazı (özellikle çok yüksek performanslı) videolar
için bu linki hiç vermiyor, o durumda videonun herkese açık sayfasından
(permalink) dosya linkini bulan bir yedek yönteme düşülür.
"""
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
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
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
# "random", "most_liked" ya da "top_viewed_cycle" (izlenmesi en yüksekten
# en düşüğe doğru, MIN_VIEW_COUNT eşiğinin altına inmeden döner; eşiğin
# altındaki tüm videolar tüketilince baştan başlar).
MEDIA_SELECTION = os.environ.get("MEDIA_SELECTION", "random").strip().lower()
MIN_VIEW_COUNT = int(os.environ.get("MIN_VIEW_COUNT", "10000"))
TRIAL_REEL = os.environ.get("TRIAL_REEL", "false").strip().lower() in ("1", "true", "yes")
# Instagram'ın kendi günlük paylaşım limitine (~25) yaklaşınca kalan
# çalıştırmalar indirme/işleme yapmadan sessizce atlanır.
DAILY_PUBLISH_LIMIT = int(os.environ.get("DAILY_PUBLISH_LIMIT", "20"))


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
        "fields": "id,media_type,media_product_type,media_url,caption,timestamp,permalink,like_count",
        "limit": limit,
        "access_token": IG_ACCESS_TOKEN,
    }
    items = []
    pages = 0
    while url:
        r = requests.get(url, params=params)
        raise_for_status_verbose(r)
        data = r.json()
        items.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = None  # 'next' URL zaten tüm query'yi içeriyor
        pages += 1
    is_video = lambda it: it.get("media_type") == "VIDEO" or it.get("media_product_type") == "REELS"
    # media_url eksik olanları da havuza dahil ediyoruz — indirme anında
    # fetch_video_bytes() permalink üzerinden yedek yollarla indirmeyi dener.
    videos = [it for it in items if is_video(it) and it.get("permalink")]

    no_url = [it for it in videos if not it.get("media_url")]
    other_types = sorted({it.get("media_type", "?") for it in items if not is_video(it)})
    log(
        f"Toplam {len(items)} medya ({pages} sayfa) — {len(videos)} video/Reels bulundu, "
        f"{len(no_url)} tanesinde media_url eksik (indirme anında yedek yöntem denenecek), "
        f"diğer türler: {other_types or 'yok'}."
    )
    target_id = os.environ.get("TARGET_MEDIA_ID", "").strip()
    if target_id:
        found = next((it for it in items if it["id"] == target_id), None)
        log(f"HEDEF {target_id} ham liste verisi: {json.dumps(found, ensure_ascii=False) if found else 'LİSTEDE YOK'}")
        views = fetch_view_count(target_id)
        log(f"HEDEF {target_id} izlenme sayısı: {views}")

    return videos


def fetch_view_count(media_id: str) -> int:
    """Insights API'den gerçek izlenme sayısını (views) çeker. Bu ayrı bir
    izin (instagram_business_manage_insights) gerektirir. Hata olursa 0 döner
    (o video sıralamada en sona düşer, script çökmez) ama hatayı loglar."""
    url = f"{GRAPH_BASE}/{media_id}/insights"
    r = requests.get(url, params={"metric": "views", "access_token": IG_ACCESS_TOKEN})
    if not r.ok:
        log(f"UYARI: {media_id} için izlenme alınamadı — HTTP {r.status_code}: {r.text[:300]}")
        return 0
    for metric in r.json().get("data", []):
        if metric.get("name") == "views":
            values = metric.get("values", [])
            if values:
                return values[0].get("value", 0)
    return 0


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _fetch_download_url_from_html(permalink: str) -> Optional[str]:
    """Hızlı yol: sayfanın ham HTML'inde video linki gömülü mü diye bakar.
    Instagram'ın çoğu sayfası artık JS ile dolduğu için genelde boş döner,
    ama ücretsiz ve hızlı olduğu için önce bu denenir."""
    try:
        r = requests.get(permalink, headers=_BROWSER_HEADERS, timeout=20)
    except requests.RequestException:
        return None
    if not r.ok:
        return None
    html = r.text
    match = re.search(r'<meta property="og:video(?::secure_url)?" content="([^"]+)"', html)
    if match:
        return match.group(1).replace("&amp;", "&")
    match = re.search(r'"video_url":"([^"]+?)"', html)
    if match:
        try:
            return match.group(1).encode().decode("unicode_escape")
        except UnicodeDecodeError:
            return match.group(1)
    return None


def _download_bytes(url: str, referer: str = "https://www.instagram.com/") -> Optional[bytes]:
    try:
        r = requests.get(url, headers={**_BROWSER_HEADERS, "Referer": referer}, timeout=60)
    except requests.RequestException:
        return None
    if not r.ok or len(r.content) < 100_000:
        return None
    return r.content


def _fetch_video_bytes_via_browser(permalink: str) -> Optional[bytes]:
    """Yavaş ama daha güvenilir yol: gerçek bir (headless) tarayıcı ile
    sayfayı işletir, videonun gerçek dosya isteğini ağ trafiğinden yakalar
    ve dosyayı AYNI tarayıcı oturumunun (çerezleri dahil) isteğiyle indirir
    — Instagram'ın CDN'i çerezsiz/oturumsuz isteklere boş/hata sayfası
    döndürüyor, bu yüzden ayrı bir requests.get() işe yaramıyor."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("UYARI: playwright kurulu değil, tarayıcı tabanlı indirme atlanıyor.")
        return None

    # Instagram videoyu blob: URL üzerinden oynatıyor. Sayfa içinden bu
    # blob: URL'i fetch() ile çekmeye çalışmak Instagram'ın CSP'si tarafından
    # engelleniyor ("TypeError: Failed to fetch"). Ham ağ segmentlerini
    # birleştirmeye çalışmak da işe yaramıyor çünkü tek bir segment isteği
    # (moof/mdat) başlangıç (init/moov) atomunu içermiyor ("could not find
    # corresponding trex"). Çözüm: `URL.createObjectURL` ve
    # `SourceBuffer.appendBuffer`'ı sayfa yüklenmeden ÖNCE yamalayıp asıl
    # Blob/MediaSource nesnesini veya MSE'ye eklenen ham parçaları
    # yakalıyoruz — bunlar ağ isteği değil, bellek-içi işlemler olduğu için
    # CSP'den etkilenmiyor.
    js_init = """
    (() => {
        if (window.__igPatched) return;
        window.__igPatched = true;
        window.__igBlobMap = new Map();
        window.__igChunks = [];
        const origCreate = URL.createObjectURL.bind(URL);
        URL.createObjectURL = function(obj) {
            const url = origCreate(obj);
            window.__igBlobMap.set(url, obj);
            return url;
        };
        if (window.SourceBuffer) {
            const origAppend = SourceBuffer.prototype.appendBuffer;
            SourceBuffer.prototype.appendBuffer = function(data) {
                try {
                    let ab = null;
                    if (data instanceof ArrayBuffer) ab = data.slice(0);
                    else if (ArrayBuffer.isView(data)) {
                        ab = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
                    }
                    if (ab) window.__igChunks.push(ab);
                } catch (e) {}
                return origAppend.apply(this, arguments);
            };
        }
    })();
    """

    js_extract = """
    async () => {
        const video = document.querySelector('video');
        if (!video) return {error: 'no-video-element'};
        await new Promise(resolve => {
            if (video.readyState >= 2) return resolve();
            video.addEventListener('loadeddata', resolve, {once: true});
            setTimeout(resolve, 8000);
        });
        try { video.muted = true; await video.play(); } catch (e) {}
        const dur = video.duration;
        if (isFinite(dur) && dur > 0) {
            for (const frac of [0.3, 0.6, 0.9, 0.99]) {
                try {
                    video.currentTime = dur * frac;
                    await new Promise(r => setTimeout(r, 800));
                } catch (e) {}
            }
        }
        await new Promise(r => setTimeout(r, 1500));

        const toB64 = (bytes) => {
            let binary = '';
            const chunkSize = 0x8000;
            for (let i = 0; i < bytes.length; i += chunkSize) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
            }
            return btoa(binary);
        };

        const src = video.currentSrc || video.src;
        const obj = src ? window.__igBlobMap.get(src) : null;
        if (obj && typeof obj.arrayBuffer === 'function') {
            try {
                const buf = await obj.arrayBuffer();
                const bytes = new Uint8Array(buf);
                return {src, method: 'blob', size: bytes.length, b64: toB64(bytes)};
            } catch (e) {
                return {src, error: 'blob-read-failed: ' + String(e)};
            }
        }

        const chunks = window.__igChunks || [];
        if (chunks.length) {
            let total = 0;
            for (const c of chunks) total += c.byteLength;
            const merged = new Uint8Array(total);
            let offset = 0;
            for (const c of chunks) { merged.set(new Uint8Array(c), offset); offset += c.byteLength; }
            return {src, method: 'sourcebuffer', size: merged.length, b64: toB64(merged)};
        }

        return {src, error: 'no-blob-and-no-sourcebuffer-chunks'};
    }
    """

    video_bytes = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=_BROWSER_HEADERS["User-Agent"])
            page.add_init_script(js_init)
            try:
                page.goto(permalink, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                result = page.evaluate(js_extract)
                if not result:
                    log("  tarayıcı teşhis: beklenmeyen boş sonuç.")
                elif result.get("error"):
                    log(f"  tarayıcı teşhis: {result.get('src', '?')[:80]} -> {result['error']}")
                elif result.get("b64"):
                    import base64

                    body = base64.b64decode(result["b64"])
                    log(
                        f"  tarayıcı teşhis: [{result['method']}] {result['src'][:100]}, boyut {len(body)} bayt"
                    )
                    if len(body) >= 100_000:
                        video_bytes = body
                    else:
                        log(f"  tarayıcı teşhis: içerik başı: {body[:200]!r}")
            except Exception as exc:
                log(f"UYARI: tarayıcı ile video indirme hatası: {exc}")
            browser.close()
    except Exception as exc:
        log(f"UYARI: tarayıcı başlatma hatası: {exc}")
        return None
    return video_bytes


def fetch_video_bytes(candidate: dict) -> Optional[bytes]:
    """Kaynak videonun dosya içeriğini (bytes) döndürür. Önce Graph API'nin
    verdiği media_url'i, sonra sayfanın ham HTML'ini, en son (en yavaş ama
    en güvenilir) headless tarayıcıyı dener. Bazı çok yüksek performanslı
    videolar için Graph API media_url hiç vermiyor, bu yüzden bu zincir var."""
    if candidate.get("media_url"):
        b = _download_bytes(candidate["media_url"])
        if b:
            return b

    permalink = candidate.get("permalink")
    if not permalink:
        return None

    url = _fetch_download_url_from_html(permalink)
    if url:
        b = _download_bytes(url)
        if b:
            return b

    b = _fetch_video_bytes_via_browser(permalink)
    if b:
        return b

    log(f"UYARI: {candidate['id']} için hiçbir yöntemle video indirilemedi.")
    return None


def create_media_container(video_url: str, caption: str, trial: bool = False) -> str:
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    data = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": IG_ACCESS_TOKEN,
    }
    if trial:
        # Instagram uygulamasındaki "Deneme" özelliğinin API karşılığı:
        # Reels ilk başta yalnızca takipçi olmayanlara gösterilir, profilde
        # görünmez. MANUAL: sen manuel olarak "herkese aç" demeden yaygınlaşmaz.
        data["trial_params"] = json.dumps({"graduation_strategy": "MANUAL"})
    r = requests.post(url, data=data)
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

def process_video(src: Path, dst: Path) -> None:
    """Videoyu renk/kontrast varyasyonu + hafif keskinlik/vinyet ile işler ve
    hafif hız değişimi uygular. Üst yazı yok.

    Not: Yatay çevirme (hflip) kasıtlı olarak kullanılmıyor — kaynak videonun
    içine gömülü yazılar varsa çevirmede ters/okunmaz hale geliyordu."""
    filters = []

    crop_pct = round(random.uniform(0.94, 0.98), 3)
    filters.append(f"crop=iw*{crop_pct}:ih*{crop_pct}")
    # Kaynak videonun çözünürlüğünü büyütmüyoruz (upscale kalite kaybına yol
    # açıyordu) — sadece x264'ün gerektirdiği gibi çift sayıya yuvarlıyoruz.
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    brightness = round(random.uniform(-0.04, 0.04), 3)
    contrast = round(random.uniform(0.92, 1.12), 3)
    saturation = round(random.uniform(0.9, 1.15), 3)
    filters.append(f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}")

    # Hafif keskinlik ve çok hafif kenar kararması (vinyet) — belirgin/dikkat
    # çekici olmayacak kadar hafif tutuluyor.
    unsharp_amount = round(random.uniform(0.3, 0.7), 2)
    filters.append(f"unsharp=5:5:{unsharp_amount}:5:5:0.0")
    vignette_angle = round(random.uniform(math.pi / 12, math.pi / 8), 3)
    filters.append(f"vignette={vignette_angle}")

    filter_chain = ",".join(filters)

    # Hafif hız değişimi (video + ses birlikte) — hem görsel imzayı biraz daha
    # değiştirir hem de videoya hafif dinamizm katar.
    speed = round(random.uniform(0.97, 1.04), 3)

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"{filter_chain},setpts={1 / speed:.4f}*PTS",
        "-af", f"atempo={speed}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-c:a", "aac", "-b:a", "192k",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


VIEW_COUNT_CACHE_TTL = 6 * 3600  # saniye


def get_view_counts(state: dict, videos: list) -> dict:
    """Her video için gerçek izlenme sayısını döndürür. state'te 6 saatten
    taze bir değer varsa tekrar API çağrısı yapmadan onu kullanır — 121 video
    için her 30 dakikalık çalıştırmada 121 ekstra çağrı yapmamak için."""
    cache = state.setdefault("_view_counts", {})
    now = int(time.time())
    counts = {}
    for v in videos:
        vid = v["id"]
        cached = cache.get(vid)
        if cached and now - cached.get("fetched_at", 0) < VIEW_COUNT_CACHE_TTL:
            counts[vid] = cached["views"]
        else:
            views = fetch_view_count(vid)
            cache[vid] = {"views": views, "fetched_at": now}
            counts[vid] = views
    return counts


# ---------- Ana akış ----------

def main() -> None:
    cleanup_old_videos()
    state = load_state()

    if os.environ.get("RESET_CYCLE", "false").strip().lower() in ("1", "true", "yes"):
        # Bakım komutu: geçmiş test karmaşasını (farklı modların birbirine
        # karışmasından kalan last_cycle_used/_cycle izlerini) temizler,
        # top_viewed_cycle'ı sıfırdan, temiz bir turla başlatır.
        for entry in state.values():
            if isinstance(entry, dict):
                entry.pop("last_cycle_used", None)
        state.pop("_cycle", None)
        state.pop("_history", None)
        save_state(state)
        log("Tur takibi sıfırlandı, bir sonraki çalıştırma en yüksek izlenmeliden başlayacak.")
        return

    today = date.today().isoformat()
    daily = state.get("_daily", {})
    if daily.get("date") != today:
        daily = {"date": today, "count": 0}
    if not DRY_RUN and daily["count"] >= DAILY_PUBLISH_LIMIT:
        log(f"Bugün için günlük paylaşım limiti ({DAILY_PUBLISH_LIMIT}) zaten doldu, atlanıyor.")
        return

    videos = fetch_own_videos()

    # Kendi attığımız (remix'lenmiş) videoları asla yeniden kaynak olarak
    # seçme — hem daha önce paylaştığımız medya ID'lerini hem de sabit
    # caption'ımızla eşleşen videoları eliyoruz (ikisi de kendi paylaşımımız
    # olduğunu gösterir).
    posted_ids = set(state.get("_posted_ids", []))
    videos = [
        v for v in videos
        if v["id"] not in posted_ids
        and not (CAPTION_SUFFIX and (v.get("caption") or "").strip() == CAPTION_SUFFIX.strip())
    ]

    if not videos:
        log("Hesapta video bulunamadı.")
        return

    cycle = state.get("_cycle", 0)

    if MEDIA_SELECTION == "top_viewed_cycle":
        # İzlenmesi en yüksekten en düşüğe doğru sırayla paylaşır (eşiğin
        # altındakiler hiç dahil edilmez). Bu turda kullanılmamış en yüksek
        # izlenmeli video seçilir; hiçbiri kalmadıysa yeni bir tur başlatıp
        # baştan (en yüksek izlenmeliden) devam eder.
        view_counts = get_view_counts(state, videos)
        for v in videos:
            v["view_count"] = view_counts.get(v["id"], 0)
        by_views = sorted(videos, key=lambda v: v.get("view_count", 0), reverse=True)
        top15 = "\n".join(
            f"  {v.get('view_count')} — {v['id']} — {v.get('permalink')}" for v in by_views[:15]
        )
        log(f"En yüksek 15 izlenme değeri:\n{top15}")
        pool = [v for v in videos if (v.get("view_count") or 0) >= MIN_VIEW_COUNT]
        pool.sort(key=lambda v: v.get("view_count", 0), reverse=True)

        def cycle_used_at(v):
            entry = state.get(v["id"], {})
            if "last_cycle_used" in entry:
                return entry["last_cycle_used"]
            if entry.get("repost_count", 0) > 0:
                # Bu video başka bir modla (ör. most_liked) zaten paylaşılmış —
                # bu turda tekrar seçilmesin, bir sonraki turda uygun olsun.
                return 0
            return -1

        ordered = [v for v in pool if cycle_used_at(v) < cycle]
        if not ordered and pool:
            cycle += 1
            state["_cycle"] = cycle
            ordered = pool
    elif MEDIA_SELECTION == "most_liked":
        ordered = [v for v in videos if state.get(v["id"], {}).get("repost_count", 0) < MAX_REPOSTS_PER_VIDEO]
        ordered.sort(key=lambda v: v.get("like_count", 0), reverse=True)
    else:
        ordered = [v for v in videos if state.get(v["id"], {}).get("repost_count", 0) < MAX_REPOSTS_PER_VIDEO]
        random.shuffle(ordered)

    if not ordered:
        if MEDIA_SELECTION == "top_viewed_cycle":
            log(f"{MIN_VIEW_COUNT} üzeri izlenmeye sahip video bulunamadı.")
        else:
            log("Tüm videolar limit sayısı kadar remix'lenmiş. MAX_REPOSTS_PER_VIDEO'yu artırmayı düşünebilirsin.")
        return

    # Sıradaki en uygun videodan başlayıp, indirilebilen bir video bulana
    # kadar dener (media_url yoksa yedek yöntemler çalışır, onlar da
    # başarısız olursa sıradaki video denenir — tek bir video yüzünden
    # çalıştırma boşa gitmez).
    candidate = None
    video_bytes = None
    for v in ordered:
        data = fetch_video_bytes(v)
        if data:
            candidate = v
            video_bytes = data
            break
        log(f"UYARI: {v['id']} indirilemedi, sıradaki video deneniyor.")

    if not candidate:
        log("Uygun videolardan hiçbiri indirilemedi.")
        return

    log(f"Seçilen video: {candidate['id']} ({candidate.get('permalink')})")

    remote_name = f"{candidate['id']}-{int(time.time())}.mp4"

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "source.mp4"
        dst = Path(tmp) / "processed.mp4"

        src.write_bytes(video_bytes)

        process_video(src, dst)
        public_url = upload_video(dst, remote_name)
        log(f"Video Supabase'e yüklendi: {public_url}")

    # CAPTION_SUFFIX ayarlıysa paylaşımın tam metni olarak kullanılır (orijinal
    # caption'ın yerine geçer); ayarlı değilse orijinal caption aynen kullanılır.
    caption = CAPTION_SUFFIX or (candidate.get("caption") or "").strip()

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
        creation_id = create_media_container(public_url, caption, trial=TRIAL_REEL)
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
    if MEDIA_SELECTION == "top_viewed_cycle":
        entry["last_cycle_used"] = cycle
    state[candidate["id"]] = entry
    daily["count"] += 1
    state["_daily"] = daily
    posted_ids.add(media_id)
    state["_posted_ids"] = list(posted_ids)
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - GitHub Actions'ta hatayı görünür kılmak için
        log(f"HATA: {exc}")
        sys.exit(1)
