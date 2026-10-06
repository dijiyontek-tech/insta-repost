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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

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
# Günlük sert tavan — bu sayıya ulaşınca kalan çalıştırmalar indirme/işleme
# yapmadan sessizce atlanır (elle tetiklemeler dahil).
DAILY_PUBLISH_LIMIT = int(os.environ.get("DAILY_PUBLISH_LIMIT", "6"))


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


# ---------- Video kütüphanesi (GitHub Releases) ----------
#
# Videoların ORİJİNAL (işlenmemiş) halleri, bu reponun "video-library"
# adlı release'ine dosya (asset) olarak KALICI şekilde konur. Paylaşım
# zamanı gelince video buradan alınıp renklendirilerek paylaşılır;
# Instagram'dan tekrar tekrar indirilmez. (Önceden Supabase'te sources/
# klasöründe tutuluyordu ama ücretsiz planın 1 GB sınırı yüksek kaliteli
# tüm kütüphaneye yetmiyor; GitHub release dosyalarında pratik bir toplam
# boyut sınırı yok.)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
LIBRARY_TAG = "video-library"
_GH_API = "https://api.github.com"

# Supabase'teki eski kütüphane klasörü — yalnızca taşıma sonrası temizlik için.
SOURCE_PREFIX = "sources"

_library_cache: Optional[dict] = None  # {video_id: {"id": asset_id, "size": bayt}}
_release_id: Optional[int] = None


def _gh_headers(**extra) -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        **extra,
    }


def _library_release_id() -> int:
    global _release_id
    if _release_id is None:
        r = requests.get(f"{_GH_API}/repos/{GITHUB_REPOSITORY}/releases/tags/{LIBRARY_TAG}", headers=_gh_headers())
        if r.status_code == 404:
            r = requests.post(
                f"{_GH_API}/repos/{GITHUB_REPOSITORY}/releases",
                headers=_gh_headers(),
                json={
                    "tag_name": LIBRARY_TAG,
                    "name": "Video kütüphanesi",
                    "body": "Otomasyonun kullandığı orijinal videolar (en yüksek kalite). Elle düzenlemeyin.",
                    "prerelease": True,
                },
            )
        raise_for_status_verbose(r)
        _release_id = r.json()["id"]
    return _release_id


def library_list() -> dict:
    """Kütüphanedeki videoları {video_id: {"id": asset_id, "size": bayt}} olarak döndürür."""
    global _library_cache
    if _library_cache is None:
        found = {}
        page = 1
        while True:
            r = requests.get(
                f"{_GH_API}/repos/{GITHUB_REPOSITORY}/releases/{_library_release_id()}/assets",
                headers=_gh_headers(),
                params={"per_page": 100, "page": page},
            )
            raise_for_status_verbose(r)
            batch = r.json()
            for a in batch:
                if a["name"].endswith(".mp4"):
                    found[a["name"][:-4]] = {"id": a["id"], "size": a["size"]}
            if len(batch) < 100:
                break
            page += 1
        _library_cache = found
    return _library_cache


def library_get(video_id: str) -> Optional[bytes]:
    asset = library_list().get(video_id)
    if not asset:
        return None
    # İndirme isteği imzalı bir depolama URL'sine yönlendiriliyor; requests
    # başka bir alan adına yönlendirmede Authorization başlığını zaten
    # göndermiyor, bu doğru davranış.
    r = requests.get(
        f"{_GH_API}/repos/{GITHUB_REPOSITORY}/releases/assets/{asset['id']}",
        headers=_gh_headers(Accept="application/octet-stream"),
        timeout=120,
    )
    if r.status_code == 200 and len(r.content) > 100_000:
        return r.content
    log(f"UYARI: {video_id} kütüphaneden indirilemedi (HTTP {r.status_code}).")
    return None


def library_put(video_id: str, data: bytes) -> bool:
    lib = library_list()
    if video_id in lib:
        requests.delete(
            f"{_GH_API}/repos/{GITHUB_REPOSITORY}/releases/assets/{lib[video_id]['id']}",
            headers=_gh_headers(),
        )
        lib.pop(video_id, None)
    r = requests.post(
        f"https://uploads.github.com/repos/{GITHUB_REPOSITORY}/releases/{_library_release_id()}/assets",
        headers=_gh_headers(**{"Content-Type": "video/mp4"}),
        params={"name": f"{video_id}.mp4"},
        data=data,
        timeout=300,
    )
    if not r.ok:
        log(f"UYARI: {video_id} kütüphaneye yüklenemedi (HTTP {r.status_code}: {r.text[:200]}).")
        return False
    lib[video_id] = {"id": r.json()["id"], "size": len(data)}
    return True


def fetch_video_bytes_cached(candidate: dict) -> Optional[bytes]:
    """Video kütüphanede varsa Instagram'a hiç gitmeden oradan döndürür; yoksa
    (ör. hesaba sonradan eklenmiş yeni bir video) en yüksek kalitede bir kez
    indirip kütüphaneye ekler."""
    video_id = candidate["id"]
    cached = library_get(video_id)
    if cached:
        log(f"  {video_id}: kütüphanedeki video kullanıldı (Instagram'dan indirilmedi).")
        return cached
    data = fetch_video_bytes_best(candidate)
    if data:
        library_put(video_id, data)
    return data


def _cleanup_legacy_supabase_sources(migrated: set) -> None:
    """Kütüphaneye taşınmış videoların Supabase sources/ klasöründeki eski
    (çoğu düşük kaliteli) kopyalarını siler — Supabase'in 1 GB'lık alanı
    geçici paylaşım dosyaları için boş kalsın."""
    url = f"{SUPABASE_URL}/storage/v1/object/list/{SUPABASE_BUCKET}"
    r = requests.post(url, headers=supabase_headers(), json={"prefix": SOURCE_PREFIX, "limit": 1000})
    if r.status_code != 200:
        return
    stale = [
        f"{SOURCE_PREFIX}/{it['name']}"
        for it in r.json()
        if (it.get("name") or "").endswith(".mp4") and it["name"][:-4] in migrated
    ]
    for i in range(0, len(stale), 100):
        requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}",
            headers=supabase_headers(),
            json={"prefixes": stale[i:i + 100]},
        )
    if stale:
        log(f"Supabase'teki {len(stale)} eski kütüphane kopyası silindi (artık GitHub'da).")


def sync_library(videos: list, state: dict) -> None:
    """Hesaptaki TÜM orijinal videoları EN YÜKSEK kalitede bir kez indirip
    kütüphaneye koyar. Zaten kütüphanede olanlara dokunmaz."""
    posted_ids = set(state.get("_posted_ids", []))
    lib = library_list()
    originals = [v for v in videos if v["id"] not in posted_ids]
    todo = [v for v in originals if v["id"] not in lib]
    log(
        f"Kütüphane: {len(originals)} orijinal videonun {len(originals) - len(todo)} tanesi zaten "
        f"kütüphanede, {len(todo)} tanesi en yüksek kalitede indirilecek."
    )
    failed = []
    for i, v in enumerate(todo, 1):
        data, info = _fetch_best_with_info(v)
        if not data or not library_put(v["id"], data):
            failed.append(v["id"])
        else:
            log(
                f"  [{i}/{len(todo)}] {v['id']} kütüphaneye eklendi "
                f"({_fmt_info(info)}, {len(data) / 1e6:.1f} MB)."
            )
        # Instagram'a art arda seri istek atmamak için kısa, rastgele ara.
        time.sleep(random.uniform(3, 8))
    total_mb = sum(a["size"] for a in lib.values()) / 1e6
    log(
        f"Kütüphane güncel: {len(lib)} video, toplam {total_mb:.0f} MB. İndirilemeyen: {len(failed)}"
        f"{' (' + ', '.join(failed) + ')' if failed else ''}."
    )
    _cleanup_legacy_supabase_sources(set(lib))


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
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                if "Executable doesn't exist" not in str(exc):
                    raise
                # Tarayıcı artık her çalıştırmada peşin kurulmuyor (Actions
                # dakikası harcıyordu; videolar zaten kütüphanede) — yalnızca
                # gerçekten gerektiğinde, o an kuruluyor.
                log("Headless tarayıcı kurulu değil, şimdi kuruluyor...")
                subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
                    check=True,
                )
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


def _probe_info(src: str) -> Optional[dict]:
    """Dosya yolu ya da URL için {"w", "h", "dur"} döndürür."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", src],
            capture_output=True, text=True, timeout=60,
        )
        j = json.loads(r.stdout)
        st = j["streams"][0]
        return {"w": int(st["width"]), "h": int(st["height"]), "dur": float(j["format"]["duration"])}
    except Exception:
        return None


def _probe_bytes_info(data: bytes) -> Optional[dict]:
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(data)
        f.flush()
        return _probe_info(f.name)


def _fmt_info(info: Optional[dict]) -> str:
    return f"{info['w']}x{info['h']}, {info['dur']:.1f} sn" if info else "?"


def _fetch_best_with_info(candidate: dict) -> tuple:
    """Videonun Instagram'da OYNATILAN sürümünün en yüksek kalitesini
    (bytes, info) olarak döndürür.

    İki kaynak var:
    - Graph API'nin media_url'i: çoğu videoda düşük/orta kalite (360p/720p)
      veriyor; üstelik bazı videolarda yüklenen HAM dosyayı veriyor — ör.
      Instagram'da sonu kırpılmış bir videonun kırpılmamış hali (sonunda
      CapCut kapanışı duran sürüm).
    - Sayfadaki DASH manifesti: Instagram'ın gerçekten oynattığı sürüm,
      tüm kalite seviyeleriyle (1080x1920'ye kadar en iyisini seçiyoruz).

    Süreleri 1 saniyeden fazla farklıysa media_url'deki kırpılmamış hal
    demektir, DASH sürümü kullanılır. Süreler aynıysa çözünürlüğü yüksek
    olan seçilir."""
    mu = dash = None
    if candidate.get("media_url"):
        b = _download_bytes(candidate["media_url"])
        if b:
            mu = (b, _probe_bytes_info(b))
    if candidate.get("permalink"):
        b = _fetch_video_bytes_via_browser(candidate["permalink"])
        if b:
            dash = (b, _probe_bytes_info(b))

    if mu and dash and mu[1] and dash[1]:
        if abs(mu[1]["dur"] - dash[1]["dur"]) > 1.0:
            log(
                f"  {candidate['id']}: media_url sürümü ({_fmt_info(mu[1])}) ile Instagram'da oynatılan "
                f"sürüm ({_fmt_info(dash[1])}) farklı uzunlukta — oynatılan sürüm kullanılıyor."
            )
            return dash
        return max((mu, dash), key=lambda o: (o[1]["w"] * o[1]["h"], len(o[0])))
    if dash:
        return dash
    if mu:
        return mu
    b = fetch_video_bytes(candidate)  # son çare: eski zincir (HTML yolu dahil)
    return (b, _probe_bytes_info(b)) if b else (None, None)


def fetch_video_bytes_best(candidate: dict) -> Optional[bytes]:
    return _fetch_best_with_info(candidate)[0]


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

def _distinct_enough(params: dict, last: Optional[dict]) -> bool:
    """Aynı video ikinci/üçüncü kez remix'lenirken bir önceki sefere göre
    gözle görülür şekilde farklı çıksın diye — sürekli rastgele sayılarla
    zaten pratikte birebir aynı çıkması imkansıza yakın, ama yine de en az
    bir parametrenin belirgin biçimde farklı olduğunu garanti ediyoruz."""
    if not last:
        return True
    return (
        abs(params["crop_pct"] - last.get("crop_pct", 0)) > 0.01
        or abs(params["brightness"] - last.get("brightness", 0)) > 0.015
        or abs(params["contrast"] - last.get("contrast", 1)) > 0.04
        or abs(params["saturation"] - last.get("saturation", 1)) > 0.04
        or abs(params["speed"] - last.get("speed", 1)) > 0.01
    )


def process_video(src: Path, dst: Path, last_params: Optional[dict] = None) -> dict:
    """Videoyu renk/kontrast varyasyonu + hafif keskinlik/vinyet ile işler ve
    hafif hız değişimi uygular. Üst yazı yok.

    Not: Yatay çevirme (hflip) kasıtlı olarak kullanılmıyor — kaynak videonun
    içine gömülü yazılar varsa çevirmede ters/okunmaz hale geliyordu.

    last_params verilirse (aynı video daha önce remix'lenmişse), üretilen
    parametrelerin ondan belirgin şekilde farklı olması garanti edilir —
    aynı kaynak video yeniden kullanıldığında (kaynak artık önbellekten
    geliyor) her seferinde görünürde de farklı bir remix çıksın diye.
    Kullanılan parametreler, çağıran tarafından state'e kaydedilmek üzere
    döndürülür."""
    for _ in range(8):
        crop_pct = round(random.uniform(0.94, 0.98), 3)
        brightness = round(random.uniform(-0.04, 0.04), 3)
        contrast = round(random.uniform(0.92, 1.12), 3)
        saturation = round(random.uniform(0.9, 1.15), 3)
        unsharp_amount = round(random.uniform(0.3, 0.7), 2)
        vignette_angle = round(random.uniform(math.pi / 12, math.pi / 8), 3)
        speed = round(random.uniform(0.97, 1.04), 3)
        params = {
            "crop_pct": crop_pct,
            "brightness": brightness,
            "contrast": contrast,
            "saturation": saturation,
            "unsharp_amount": unsharp_amount,
            "vignette_angle": vignette_angle,
            "speed": speed,
        }
        if _distinct_enough(params, last_params):
            break

    filters = []
    filters.append(f"crop=iw*{crop_pct}:ih*{crop_pct}")
    # Kaynak videonun çözünürlüğünü büyütmüyoruz (upscale kalite kaybına yol
    # açıyordu) — sadece x264'ün gerektirdiği gibi çift sayıya yuvarlıyoruz.
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    filters.append(f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}")

    # Hafif keskinlik ve çok hafif kenar kararması (vinyet) — belirgin/dikkat
    # çekici olmayacak kadar hafif tutuluyor.
    filters.append(f"unsharp=5:5:{unsharp_amount}:5:5:0.0")
    filters.append(f"vignette={vignette_angle}")

    filter_chain = ",".join(filters)

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
    return params


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

# GitHub Actions'ın kendi dokümantasyonu, yoğunluk zamanlarında zamanlanmış
# (schedule) tetiklemelerin SAATLERCE gecikebildiğini (hatta düşürülebildiğini)
# belirtiyor — gerçekten de gece 01:00-05:00 TSİ gibi saatlerde tetiklenen
# çalıştırmalar gözlemlendi. Cron dakikasını kaydırmak yeterli olmadığı için,
# script'in kendisine bir güvenlik ağı ekliyoruz: GitHub HANGİ SAATTE
# tetiklerse tetiklesin, zamanlanmış bir çalıştırma bu pencerenin dışındaysa
# paylaşım yapmayı reddediyor. Elle tetiklemeler (workflow_dispatch, test
# amaçlı) bu kısıtlamaya tabi değil.
POSTING_WINDOW_START = (8, 30)   # TSİ (Europe/Istanbul)
POSTING_WINDOW_END = (21, 0)     # TSİ


def _within_posting_window() -> bool:
    now = datetime.now(ZoneInfo("Europe/Istanbul"))
    start = now.replace(hour=POSTING_WINDOW_START[0], minute=POSTING_WINDOW_START[1], second=0, microsecond=0)
    end = now.replace(hour=POSTING_WINDOW_END[0], minute=POSTING_WINDOW_END[1], second=0, microsecond=0)
    return start <= now <= end


# 25/gün civarı paylaşım Instagram'dan "otomatik davranış" uyarısı almamıza
# yol açtı; artık günde yalnızca 4-6 paylaşım yapılıyor.
DAILY_TARGET_RANGE = (4, 6)
# Günün paylaşım saatleri bu aralığın TAMAMINA rastgele dağıtılıyor —
# "ilk post hep sabah, son post hep akşam" gibi bir kalıp yok. Bitiş, sert
# pencere sonundan (POSTING_WINDOW_END) biraz önce tutuluyor ki yoklama
# gecikmesi + jitter yüzünden son slot pencere dışına kaçmasın.
SLOT_RANGE = ((9, 0), (20, 40))
# Aynı gün içindeki iki paylaşım arasında en az bu kadar süre olur (planlanan
# saatler arasında; dış tetikleyici gecikse bile art arda paylaşım yapılmaz).
MIN_GAP_MINUTES = 100
# Bir önceki günün paylaşım saatlerine bu kadar dakikadan yakın saat
# seçilmez — ardışık günlerde "hep aynı saatlerde paylaşım" oluşmasın diye.
AVOID_PREV_DAY_MINUTES = 25


def _tr_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Istanbul"))


def _minute_of_day(ts: int) -> int:
    t = datetime.fromtimestamp(ts, ZoneInfo("Europe/Istanbul"))
    return t.hour * 60 + t.minute


def _plan_daily_slots(target: int, prev_slots: Optional[list] = None) -> list:
    """Günün paylaşım saatlerini (epoch saniye) günün ilk yoklamasında bir kez
    rastgele belirler: SLOT_RANGE'in tamamına dağılır, aralarında en az
    MIN_GAP_MINUTES olur ve bir önceki günün saatlerinin
    ±AVOID_PREV_DAY_MINUTES yakınına denk gelmez."""
    now = _tr_now()
    lo, hi = SLOT_RANGE
    range_start = now.replace(hour=lo[0], minute=lo[1], second=0, microsecond=0)
    range_end = now.replace(hour=hi[0], minute=hi[1], second=0, microsecond=0)
    span = (range_end - range_start).total_seconds()
    min_gap = MIN_GAP_MINUTES * 60
    prev_minutes = [_minute_of_day(t) for t in (prev_slots or [])]

    def ok(offsets: list, check_prev: bool) -> bool:
        if any(b - a < min_gap for a, b in zip(offsets, offsets[1:])):
            return False
        if check_prev:
            for o in offsets:
                m = _minute_of_day(int(range_start.timestamp() + o))
                if any(abs(m - pm) < AVOID_PREV_DAY_MINUTES for pm in prev_minutes):
                    return False
        return True

    # Önce dünün saatlerinden kaçınarak dene; (pratikte olmaz ama) bulamazsa
    # yalnızca aralık kuralıyla yetin.
    for check_prev in (True, False):
        for _ in range(5000):
            offsets = sorted(random.uniform(0, span) for _ in range(target))
            if ok(offsets, check_prev):
                return [int(range_start.timestamp() + o) for o in offsets]
    seg = span / target
    offsets = [seg * i + random.uniform(0, seg * 0.4) for i in range(target)]
    return [int(range_start.timestamp() + o) for o in offsets]


def _fmt_slots(slots: list) -> str:
    tz = ZoneInfo("Europe/Istanbul")
    return ", ".join(datetime.fromtimestamp(t, tz).strftime("%H:%M") for t in slots)


def _decide_auto_batch_size(daily: dict) -> int:
    """Dış zamanlayıcı sık aralıklarla (ör. 15-30 dakikada bir) "yoklama"
    yapıyor. Günün ilk yoklamasında o günün paylaşım sayısı (4-6) ve
    saatleri rastgele planlanıyor; sonraki her yoklamada, saati gelmiş ama
    henüz paylaşılmamış bir slot varsa TEK video paylaşılıyor. Böylece her
    gün farklı saatlerde, aralıklı ve az sayıda paylaşım yapılıyor."""
    if "slots" not in daily:
        daily["target"] = random.randint(*DAILY_TARGET_RANGE)
        daily["slots"] = _plan_daily_slots(daily["target"], daily.get("prev_slots"))
        log(f"Bugünün paylaşım planı ({daily['target']} video): {_fmt_slots(daily['slots'])} TSİ")

    # Dış zamanlayıcı hatalı/geç tetiklerse bile gece paylaşım olmasın diye
    # SERT kontrol — pencere dışındaysa direkt 0.
    if not _within_posting_window():
        return 0

    now_ts = time.time()
    due = sum(1 for t in daily["slots"] if t <= now_ts)
    if daily["count"] >= due:
        upcoming = [t for t in daily["slots"] if t > now_ts]
        if upcoming:
            log(f"Sıradaki planlı paylaşım: {_fmt_slots(upcoming[:1])} TSİ")
        return 0

    # Yoklamalar bir süre atlanıp birden fazla slot birikmiş olsa bile art
    # arda paylaşım yapmıyoruz; son paylaşımdan bu yana en az MIN_GAP_MINUTES
    # geçmediyse bekleniyor (biriken slot sonraki yoklamalarda eritilir).
    last = daily.get("last_post_at")
    if last and now_ts - last < MIN_GAP_MINUTES * 60:
        return 0

    return 1


JITTER_MAX_SECONDS = 240  # 4 dakika


def _load_daily(state: dict) -> dict:
    daily = state.get("_daily", {})
    # Runner UTC'de çalışıyor; gün sınırı Türkiye saatine göre olmalı.
    today = _tr_now().date().isoformat()
    if daily.get("date") != today:
        # Dünün saatlerini sakla ki bugünün planı onlara yakın düşmesin.
        daily = {"date": today, "count": 0, "prev_slots": daily.get("slots", [])}
    return daily


def _gate() -> bool:
    """Ucuz ön kontrol (workflow'un "gate" işi): şu an paylaşım yapılacak mı?

    Dış zamanlayıcı 15 dakikada bir tetikliyor ama günde yalnızca 4-6
    tetiklemede gerçekten paylaşım yapılıyor. Eskiden her tetiklemede ffmpeg/
    bağımlılık kurulumu + rastgele bekleme yapılıyordu (~6 dk/çalıştırma) ve
    private repo'nun aylık 2000 dakikalık ücretsiz Actions kotası 5 günde
    bitiyordu. Artık karar bu hafif adımda veriliyor; ağır kurulum yalnızca
    gerçekten paylaşım yapılacaksa çalışıyor."""
    if os.environ.get("POSTS_PER_RUN", "1").strip().lower() != "auto":
        return True  # elle tetikleme / bakım komutları her zaman çalışır
    if not acquire_lock(timeout_s=45):
        log("Başka bir çalıştırma sürüyor (kilit alınamadı), bu yoklama atlanıyor.")
        return False
    try:
        state = load_state()
        daily = _load_daily(state)
        n = _decide_auto_batch_size(daily)
        state["_daily"] = daily  # günün planı ilk yoklamada burada yazılır
        save_state(state)
    finally:
        release_lock()
    log("Planlı paylaşım saati geldi, paylaşım işi başlatılıyor." if n else "Bu yoklamada paylaşım yok.")
    return n > 0


def main() -> None:
    if os.environ.get("GATE_ONLY", "false").strip().lower() in ("1", "true", "yes"):
        post = _gate()
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"post={'true' if post else 'false'}\n")
        return

    # Dış zamanlayıcı sabit bir kadansla (15 dakikada bir) tetikliyor; gerçek
    # paylaşım anları bu sabit ızgaraya (:03/:18/:33/:48) denk gelmesin diye
    # paylaşımdan önce kısa, rastgele bir bekleme uyguluyoruz. (Bekleme de
    # Actions dakikası harcadığı için kısa tutuluyor.)
    if os.environ.get("POSTS_PER_RUN", "").strip().lower() == "auto":
        jitter = random.uniform(0, JITTER_MAX_SECONDS)
        log(f"Tetikleme kadansını bulanıklaştırmak için {jitter:.0f} saniye bekleniyor.")
        time.sleep(jitter)

    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and not _within_posting_window():
        now_tr = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%H:%M")
        log(
            f"UYARI: zamanlanmış tetikleme beklenen saat aralığının "
            f"({POSTING_WINDOW_START[0]:02d}:{POSTING_WINDOW_START[1]:02d}-"
            f"{POSTING_WINDOW_END[0]:02d}:{POSTING_WINDOW_END[1]:02d} TSİ) dışında geldi "
            f"(şu an {now_tr} TSİ, muhtemelen GitHub'ın gecikmesi) — bu çalıştırma atlanıyor."
        )
        return

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

    posted_ids = set(state.get("_posted_ids", []))

    if os.environ.get("SYNC_LIBRARY", "false").strip().lower() in ("1", "true", "yes"):
        # Bakım komutu: tüm videoları kütüphaneye indirir, paylaşım yapmaz.
        sync_library(fetch_own_videos(), state)
        return

    daily = _load_daily(state)

    # POSTS_PER_RUN="auto" (dış zamanlayıcının kullandığı mod): script her
    # "yoklama" çağrısında KENDİSİ karar veriyor — şimdi paylaşım yapsın mı,
    # yapacaksa kaç video? Günde 4-6 video, her gün rastgele planlanan farklı
    # saatlerde paylaşılıyor (bkz. _decide_auto_batch_size). Sabit bir sayı verilirse (ör. testte
    # "3") o sayı olduğu gibi kullanılır.
    posts_per_run_raw = os.environ.get("POSTS_PER_RUN", "1").strip().lower()
    if posts_per_run_raw == "auto":
        posts_per_run = _decide_auto_batch_size(daily)
        if posts_per_run == 0:
            log("Bu yoklamada paylaşım yapılmayacak (planlı saat henüz gelmedi).")
            state["_daily"] = daily  # _decide_auto_batch_size günün planını state'e yazmış olabilir
            save_state(state)
            return
        log(f"Bu yoklamada {posts_per_run} video paylaşılacak (otomatik tempo).")
    else:
        posts_per_run = max(1, int(posts_per_run_raw))

    videos_all = fetch_own_videos()
    if not videos_all:
        log("Hesapta video bulunamadı.")
        return

    # Tur (cycle) matematiğinde bir uç durum vardı: bir batch içinde o anki
    # turda kalan son video paylaşılınca, hemen ardından tur ilerleyip AYNI
    # videoyu (yeni turda "henüz kullanılmadı" sayılarak) bu kez tekrar
    # seçebiliyordu — yani aynı video aynı çalıştırmada arka arkaya iki kez
    # paylaşılabiliyordu. Bu setle, bu çalıştırmada zaten paylaşılmış bir
    # orijinali kesin olarak bir daha seçmiyoruz (tur mantığından bağımsız).
    already_posted_originals = set()

    posted_this_run = 0
    for i in range(posts_per_run):
        if not DRY_RUN and daily["count"] >= DAILY_PUBLISH_LIMIT:
            log(f"Bugün için günlük paylaşım limiti ({DAILY_PUBLISH_LIMIT}) zaten doldu, atlanıyor.")
            break
        if posts_per_run > 1:
            log(f"--- Bu çalıştırmada {i + 1}/{posts_per_run}. video ---")
        ok = _post_one(state, videos_all, posted_ids, daily, already_posted_originals)
        if not ok:
            break
        posted_this_run += 1
        if DRY_RUN:
            # Deneme modu sadece önizleme amaçlı, tek video yeterli —
            # state'e hiçbir şey yazılmadığı için döngü tekrar aynı videoyu
            # seçerdi.
            break

    if posts_per_run > 1:
        log(f"Bu çalıştırmada toplam {posted_this_run} video paylaşıldı.")


def _post_one(state: dict, videos_all: list, posted_ids: set, daily: dict, already_posted_originals: set) -> bool:
    """Tek bir video seçer, indirir, işler ve (deneme modu değilse) paylaşır.
    Başarılı olursa state'i günceller ve True, uygun/indirilebilir video
    kalmadıysa False döner."""
    # Kendi attığımız (remix'lenmiş) videoları asla yeniden kaynak olarak
    # seçme — hem daha önce paylaştığımız medya ID'lerini hem de sabit
    # caption'ımızla eşleşen videoları eliyoruz (ikisi de kendi paylaşımımız
    # olduğunu gösterir). already_posted_originals, bu ÇALIŞTIRMADA zaten
    # paylaşılmış orijinalleri (tur matematiğinden bağımsız olarak) eler.
    videos = [
        v for v in videos_all
        if v["id"] not in posted_ids
        and v["id"] not in already_posted_originals
        and not (CAPTION_SUFFIX and (v.get("caption") or "").strip() == CAPTION_SUFFIX.strip())
    ]

    if not videos:
        log("Paylaşılacak uygun video kalmadı.")
        return False

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
        return False

    # Sıradaki en uygun videodan başlayıp, indirilip İŞLENEBİLEN bir video
    # bulana kadar dener (media_url yoksa yedek yöntemler çalışır, indirilen
    # dosya ffmpeg ile açılamazsa da sıradaki video denenir — tek bir video
    # yüzünden çalıştırma boşa gitmez).
    candidate = None
    public_url = None
    remote_name = None
    chosen_edit_params = None
    def _mark_attempted(v):
        # Bir video hiçbir yöntemle indirilemiyorsa (ör. kalıcı olarak
        # erişilemez/silinmiş), state'te hiçbir zaman "kullanıldı"
        # işaretlenmediği için tur (cycle) ilerledikçe TEK kalan aday haline
        # gelip her batch'i tek başına tıkıyordu ("zehirli video"). Başarısız
        # denemeleri de bu turda "denendi" olarak işaretleyip turun ilerlemesini
        # sağlıyoruz — video bir sonraki turda tekrar denenebilir.
        # DENEME MODUNDA state'e hiçbir şey yazılmaz (tekrar tekrar güvenle
        # test edilebilsin diye) — bu işaretleme de o kurala uyuyor.
        if MEDIA_SELECTION == "top_viewed_cycle" and not DRY_RUN:
            entry = state.get(v["id"], {"repost_count": 0})
            entry["last_cycle_used"] = cycle
            state[v["id"]] = entry

    for v in ordered:
        data = fetch_video_bytes_cached(v)
        if not data:
            log(f"UYARI: {v['id']} indirilemedi, sıradaki video deneniyor.")
            _mark_attempted(v)
            continue

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source.mp4"
            dst = Path(tmp) / "processed.mp4"
            src.write_bytes(data)
            last_params = state.get(v["id"], {}).get("last_edit_params")
            try:
                chosen_edit_params = process_video(src, dst, last_params)
            except subprocess.CalledProcessError as exc:
                log(f"UYARI: {v['id']} işlenemedi (ffmpeg hatası: {exc}), sıradaki video deneniyor.")
                _mark_attempted(v)
                continue
            remote_name = f"{v['id']}-{int(time.time())}.mp4"
            try:
                public_url = upload_video(dst, remote_name)
            except requests.HTTPError as exc:
                log(f"UYARI: {v['id']} Supabase'e yüklenemedi ({exc}), sıradaki video deneniyor.")
                _mark_attempted(v)
                continue
            log(f"Video Supabase'e yüklendi: {public_url}")
        candidate = v
        break

    if not candidate:
        log("Uygun videolardan hiçbiri indirilip işlenemedi.")
        if not DRY_RUN:
            # Başarısız denemelerin "bu turda denendi" işaretini kalıcı hale
            # getiriyoruz — yoksa bir sonraki çalıştırmada aynı indirilemeyen
            # video yine tek aday olarak kalıp turu tıkamaya devam eder.
            save_state(state)
        return False

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
        return True

    try:
        try:
            creation_id = create_media_container(public_url, caption, trial=TRIAL_REEL)
            log(f"Container oluşturuldu: {creation_id}")
            wait_until_ready(creation_id)
            media_id = publish_media(creation_id)
            log(f"Yayınlandı! Yeni medya ID: {media_id}")
        except (requests.HTTPError, RuntimeError, TimeoutError) as exc:
            # Instagram'ın kendi günlük gerçek paylaşım limiti (25'ten önce
            # de gelebiliyor, ör. "Application request limit reached")
            # doldurduğunda çalıştırmayı hatayla çökertmek yerine bu
            # batch'i temiz şekilde burada durduruyoruz.
            log(f"UYARI: Instagram paylaşımı reddetti ({exc}), bu batch sonlandırılıyor.")
            return False
    finally:
        delete_video(remote_name)
        log("Geçici video Supabase'ten silindi.")

    entry = state.get(candidate["id"], {"repost_count": 0})
    entry["repost_count"] = entry.get("repost_count", 0) + 1
    entry["last_reposted_at"] = int(time.time())
    if chosen_edit_params:
        entry["last_edit_params"] = chosen_edit_params
    if MEDIA_SELECTION == "top_viewed_cycle":
        entry["last_cycle_used"] = cycle
    state[candidate["id"]] = entry
    daily["count"] += 1
    daily["last_post_at"] = int(time.time())
    state["_daily"] = daily
    posted_ids.add(media_id)
    state["_posted_ids"] = list(posted_ids)
    already_posted_originals.add(candidate["id"])
    save_state(state)
    return True


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - GitHub Actions'ta hatayı görünür kılmak için
        log(f"HATA: {exc}")
        sys.exit(1)
