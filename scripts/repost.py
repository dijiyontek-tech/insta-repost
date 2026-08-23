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
LOCK_PATH = "state/lock.json"
LOCK_STALE_SECONDS = 600
MAX_REPOSTS_PER_VIDEO = int(os.environ.get("MAX_REPOSTS_PER_VIDEO", "1"))
CAPTION_SUFFIX = os.environ.get("CAPTION_SUFFIX", "")
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
# "random", "most_liked" ya da "top_viewed_cycle" (izlenmesi en yüksekten
# en düşüğe doğru; bir tur tüketilince baştan başlar). Düşük izlenmeli bir
# video, deneme (trial) olarak yeniden paylaşılınca da düşük izlenecek diye
# bir kural yok, bu yüzden alt sınır uygulanmıyor — tüm videolar dahil.
MEDIA_SELECTION = os.environ.get("MEDIA_SELECTION", "random").strip().lower()
MIN_VIEW_COUNT = int(os.environ.get("MIN_VIEW_COUNT", "0"))
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


def acquire_lock(timeout_s: int = 90) -> bool:
    """Aynı anda iki çalıştırmanın (ör. zamanlanmış tetikleme + elle test)
    aynı videoyu seçip iki kez paylaşmasını önlemek için basit bir Supabase
    tabanlı kilit. Kilit dosyası POST (upload, x-upsert:false) ile
    oluşturulmaya çalışılır — dosya zaten varsa bu istek başarısız olur
    (atomik "sadece yoksa oluştur"; PUT burada İŞE YARAMIYOR çünkü
    Supabase Storage'da PUT x-upsert bayrağından bağımsız her zaman
    üzerine yazıyor, test ederek doğrulandı). Kilit LOCK_STALE_SECONDS'tan
    eskiyse (önceki çalışma çökmüş demektir) devralınır; değilse kısa bir
    süre beklenip tekrar denenir."""
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{LOCK_PATH}"
    token = f"{os.getpid()}-{random.randint(0, 1_000_000)}"
    deadline = time.time() + timeout_s
    while True:
        now = time.time()
        post = requests.post(
            url,
            headers={**supabase_headers(), "Content-Type": "application/json", "x-upsert": "false"},
            data=json.dumps({"locked_at": now, "token": token}).encode("utf-8"),
        )
        if post.status_code in (200, 201):
            # Yarış koşuluna karşı doğrulama: kilidi biz mi tutuyoruz?
            check = requests.get(url, headers=supabase_headers())
            if check.status_code == 200 and check.json().get("token") == token:
                return True
        else:
            r = requests.get(url, headers=supabase_headers())
            locked_at = 0
            if r.status_code == 200:
                try:
                    locked_at = r.json().get("locked_at", 0)
                except Exception:
                    locked_at = 0
            if now - locked_at > LOCK_STALE_SECONDS:
                requests.delete(url, headers=supabase_headers())
                continue
        if time.time() >= deadline:
            return False
        time.sleep(5)


def release_lock() -> None:
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{LOCK_PATH}"
    requests.delete(url, headers=supabase_headers())


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


def _download_bytes(url: str, referer: str = "https://www.instagram.com/", min_size: int = 100_000) -> Optional[bytes]:
    try:
        r = requests.get(url, headers={**_BROWSER_HEADERS, "Referer": referer}, timeout=60)
    except requests.RequestException:
        return None
    if not r.ok or len(r.content) < min_size:
        return None
    return r.content


def _mux_captured_groups(groups: list) -> Optional[bytes]:
    """Tarayıcıdan yakalanan bir veya birden fazla parça grubunu (video-only,
    audio-only veya zaten muxlanmış tek grup) tek bir oynatılabilir mp4'e
    dönüştürür. Instagram bazı videolarda ses ve görüntüyü ayrı
    SourceBuffer'lara (dolayısıyla ayrı fragmented mp4 akışlarına) veriyor;
    bunları ffmpeg ile birleştirmemiz gerekiyor."""
    if not groups:
        return None
    if len(groups) == 1:
        return groups[0]["body"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        inputs = []
        for i, g in enumerate(groups):
            p = tmp_path / f"part{i}.mp4"
            p.write_bytes(g["body"])
            inputs.append(p)

        video_idx = audio_idx = None
        for i, g in enumerate(groups):
            mime = g["mime"].lower()
            if "audio" in mime and audio_idx is None:
                audio_idx = i
            elif audio_idx != i and video_idx is None:
                video_idx = i
        if video_idx is None:
            video_idx = max(range(len(groups)), key=lambda i: groups[i]["body"].__len__())
        if audio_idx is None or audio_idx == video_idx:
            return groups[video_idx]["body"]

        out_path = tmp_path / "muxed.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(inputs[video_idx]),
            "-i", str(inputs[audio_idx]),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c", "copy", "-shortest",
            str(out_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as exc:
            log(f"  tarayıcı teşhis: ses/görüntü birleştirme (mux) hatası: {exc}")
            return groups[video_idx]["body"]
        if r.returncode != 0 or not out_path.exists():
            log(f"  tarayıcı teşhis: mux başarısız, sadece video akışı kullanılacak. ffmpeg: {r.stderr[-500:]}")
            return groups[video_idx]["body"]
        log("  tarayıcı teşhis: ses ve görüntü akışları başarıyla birleştirildi (mux).")
        return out_path.read_bytes()


def _select_best_dash_representations(html: str) -> Optional[dict]:
    """Sayfa HTML'ine JS tarafından gömülen DASH manifestini (video_dash_manifest)
    ayrıştırıp en yüksek bant genişlikli video ve ses temsillerinin BaseURL'lerini
    döndürür. Bu URL'ler kendinden imzalı (self-signed) olduğundan normal bir
    requests.get() ile, tarayıcı oturumu/çerez gerekmeden indirilebiliyor —
    doğrulandı: 540x960 yerine gerçek 1080x1920'yi bu şekilde alabiliyoruz."""
    key = '"video_dash_manifest":"'
    idx = html.find(key)
    if idx == -1:
        return None
    start = idx + len(key)
    end = start
    while True:
        end = html.find('"', end + 1)
        if end == -1:
            return None
        if html[end - 1] != "\\":
            break
    raw = html[start:end]
    raw = (
        raw.replace("\\u003C", "<")
        .replace("\\u003E", ">")
        .replace("\\/", "/")
        .replace("\\n", "\n")
        .replace('\\"', '"')
    )
    # Instagram bazı videolarda 1440x2560, hatta daha yükseğini de sunuyor —
    # Reels için zaten fazlasıyla yeterli olan 1080x1920'nin üzerine çıkmak
    # hem gereksiz hem de işlenmiş dosyayı Supabase'in obje boyutu limitini
    # aşacak kadar büyütebiliyor (denendi: "Payload too large" hatası). Bu
    # yüzden bu tavanın altındaki EN İYİ kaliteyi seçiyoruz.
    MAX_VIDEO_HEIGHT = 1920

    reps = re.findall(r"<Representation\b([^>]*)>(.*?)</Representation>", raw, re.DOTALL)
    best_video = None
    best_video_capped = None
    best_audio = None
    for attrs, body in reps:
        base_url_m = re.search(r"<BaseURL>([^<]+)</BaseURL>", body)
        if not base_url_m:
            continue
        url = base_url_m.group(1).replace("&amp;", "&")
        mime_m = re.search(r'mimeType="([^"]*)"', attrs)
        mime = mime_m.group(1) if mime_m else ""
        bandwidth_m = re.search(r'\bbandwidth="(\d+)"', attrs)
        bandwidth = int(bandwidth_m.group(1)) if bandwidth_m else 0
        # Not: "width" niteliğini regex'le ararken "bandwidth" içindeki
        # "width" alt dizesiyle karışmaması için harf öncesi olmadığını
        # kontrol ediyoruz.
        width_m = re.search(r'(?<![a-zA-Z])width="(\d+)"', attrs)
        height_m = re.search(r'\bheight="(\d+)"', attrs)
        entry = {
            "url": url,
            "bandwidth": bandwidth,
            "width": int(width_m.group(1)) if width_m else None,
            "height": int(height_m.group(1)) if height_m else None,
        }
        if "video" in mime:
            if not best_video or bandwidth > best_video["bandwidth"]:
                best_video = entry
            if (entry["height"] or 0) <= MAX_VIDEO_HEIGHT and (
                not best_video_capped or bandwidth > best_video_capped["bandwidth"]
            ):
                best_video_capped = entry
        elif "audio" in mime and (not best_audio or bandwidth > best_audio["bandwidth"]):
            best_audio = entry
    chosen_video = best_video_capped or best_video
    if not chosen_video:
        return None
    return {"video": chosen_video, "audio": best_audio}


def _fetch_video_bytes_via_browser(permalink: str) -> Optional[bytes]:
    """media_url'in Graph API'de olmadığı videolar için: gerçek bir
    (headless) tarayıcı ile sayfayı yükleyip JS tarafından gömülen DASH
    manifestini okur ve sunulan kalite seviyeleri arasından EN YÜKSEK bant
    genişlikli video + ses temsillerini seçip doğrudan indirir.

    Not: bu veri sadece gerçek bir tarayıcıda JS çalıştırılınca oluşuyor —
    düz bir requests.get() ile sayfa HTML'inde bulunmuyor (test edildi).
    Ama Instagram'ın kendi oynatıcısının seçtiği/oynattığı akışı yakalamaya
    çalışmak (önceki yöntem) EN DÜŞÜK kaliteyi veriyordu, çünkü adaptif
    bitrate algoritması bağlantıyı test etmek için başlangıçta bilerek en
    düşük kaliteyi seçiyor (ör. 540x960). Bunun yerine manifesti kendimiz
    okuyup en iyi kaliteyi (ör. 1080x1920) seçiyoruz — hem daha hızlı hem
    çok daha kaliteli."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("UYARI: playwright kurulu değil, tarayıcı tabanlı indirme atlanıyor.")
        return None

    html = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=_BROWSER_HEADERS["User-Agent"])
            try:
                page.goto(permalink, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                html = page.content()
            except Exception as exc:
                log(f"UYARI: tarayıcı ile sayfa yükleme hatası: {exc}")
            browser.close()
    except Exception as exc:
        log(f"UYARI: tarayıcı başlatma hatası: {exc}")
        return None

    if not html:
        return None

    reps = _select_best_dash_representations(html)
    if not reps:
        log("  tarayıcı teşhis: sayfada DASH manifesti bulunamadı.")
        return None

    video_rep = reps["video"]
    video_body = _download_bytes(video_rep["url"], min_size=20_000)
    if not video_body:
        log("  tarayıcı teşhis: en yüksek kaliteli video akışı indirilemedi.")
        return None
    log(
        f"  tarayıcı teşhis: video akışı indirildi "
        f"({video_rep.get('width')}x{video_rep.get('height')}, bw={video_rep['bandwidth']}), "
        f"boyut {len(video_body)} bayt"
    )

    groups = [{"mime": "video/mp4", "body": video_body}]
    audio_rep = reps.get("audio")
    if audio_rep:
        audio_body = _download_bytes(audio_rep["url"], min_size=1_000)
        if audio_body:
            log(f"  tarayıcı teşhis: ses akışı indirildi, boyut {len(audio_body)} bayt")
            groups.append({"mime": "audio/mp4", "body": audio_body})
        else:
            log("  tarayıcı teşhis: ses akışı indirilemedi, sessiz video kullanılacak.")

    return _mux_captured_groups(groups)


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
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
    ]
    # Artık kaynağı gerçek yüksek çözünürlükte (1080x1920'ye kadar) indirdiğimiz
    # için uzun videolarda sabit CRF çıktısı Supabase'in obje boyutu limitini
    # ("Payload too large") aşabiliyor. Videonun süresine göre bir üst bitrate
    # sınırı (VBV) uygulayıp toplam boyutu güvenli bir tavanın altında tutuyoruz
    # — kısa videolarda bu tavana hiç dokunulmuyor, kalite CRF'ten geliyor.
    duration = _probe_duration_seconds(src)
    audio_bitrate = 192_000
    if duration and duration > 0:
        max_upload_bytes = 45 * 1024 * 1024
        target_total_bps = (max_upload_bytes * 8) / duration
        video_bitrate_cap = max(int(target_total_bps - audio_bitrate), 800_000)
        cmd += ["-maxrate", str(video_bitrate_cap), "-bufsize", str(video_bitrate_cap * 2)]
    cmd += [
        "-c:a", "aac", "-b:a", "192k",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def _probe_duration_seconds(path: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


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
    # Zamanlanmış (cron) ve elle tetiklenen çalıştırmalar aynı ana denk
    # gelirse, ikisi de aynı "henüz kullanılmamış" videoyu seçip iki kez
    # paylaşabiliyordu (state okuma/yazma arasında yarış durumu). Bunu
    # önlemek için tüm state okuma/seçme/yazma süresince basit bir kilit
    # tutuyoruz.
    # ÖNEMLİ: bu süre LOCK_STALE_SECONDS'tan (600s) uzun olmalı — kısaysa,
    # gerçekten sıkışmış (ör. runner çökmesiyle serbest bırakılmamış) bir
    # kilit hiçbir zaman "bayat" eşiğine ulaşmadan bu bekleme süresi
    # dolup vazgeçiliyor, yani otomatik kendini onarma hiç devreye girmiyor.
    if not acquire_lock(timeout_s=LOCK_STALE_SECONDS + 60):
        log("UYARI: başka bir çalıştırma zaten sürüyor (kilit alınamadı), bu çalıştırma atlanıyor.")
        return
    try:
        _run()
    finally:
        release_lock()


def _run() -> None:
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

    # Sıradaki en uygun videodan başlayıp, indirilip İŞLENEBİLEN bir video
    # bulana kadar dener (media_url yoksa yedek yöntemler çalışır, indirilen
    # dosya ffmpeg ile açılamazsa da sıradaki video denenir — tek bir video
    # yüzünden çalıştırma boşa gitmez).
    candidate = None
    public_url = None
    remote_name = None
    for v in ordered:
        data = fetch_video_bytes(v)
        if not data:
            log(f"UYARI: {v['id']} indirilemedi, sıradaki video deneniyor.")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source.mp4"
            dst = Path(tmp) / "processed.mp4"
            src.write_bytes(data)
            try:
                process_video(src, dst)
            except subprocess.CalledProcessError as exc:
                log(f"UYARI: {v['id']} işlenemedi (ffmpeg hatası: {exc}), sıradaki video deneniyor.")
                continue
            remote_name = f"{v['id']}-{int(time.time())}.mp4"
            try:
                public_url = upload_video(dst, remote_name)
            except requests.HTTPError as exc:
                log(f"UYARI: {v['id']} Supabase'e yüklenemedi ({exc}), sıradaki video deneniyor.")
                continue
            log(f"Video Supabase'e yüklendi: {public_url}")
        candidate = v
        break

    if not candidate:
        log("Uygun videolardan hiçbiri indirilip işlenemedi.")
        return

    log(f"Seçilen video: {candidate['id']} ({candidate.get('permalink')})")

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
