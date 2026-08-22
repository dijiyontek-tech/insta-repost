# Instagram Reels Remix & Repost

Kendi Reels'lerini her gün otomatik olarak alır, hafif değişikliklerle
(çevirme, renk/kontrast varyasyonu, hafif keskinlik/vinyet, hız değişimi)
yeniden işler ve resmi **Meta Graph API** ile tekrar paylaşır. Kullanıcı adı/şifre ile giriş
yoktur — yalnızca Instagram Business hesabına bağlı bir erişim token'ı
kullanılır, bu yüzden hesap askıya alınma riski şifre-tabanlı bot
yöntemlerine göre çok daha düşüktür.

## Nasıl çalışır

1. GitHub Actions her gün belirlediğin saatte tetiklenir.
2. Script, Graph API üzerinden kendi hesabındaki videoları listeler.
3. Daha önce (limitten az) remix'lenmemiş bir video seçer, indirir.
4. `ffmpeg` ile çevirir + renk/kontrast/keskinlik/vinyet oynatır + hafif
   hız değişimi uygular. Bir önceki çalıştırmanın çevirme yönünü art arda
   tekrar etmez.
5. İşlenmiş videoyu geçici olarak Supabase Storage'a yükler (Instagram'ın
   videoyu çekebilmesi için herkese açık bir URL gerekiyor).
6. Graph API ile Reels olarak yayınlar, sonra geçici dosyayı Supabase'ten
   siler.
7. Hangi orijinal videonun kaç kez remix'lendiğini `state/processed.json`
   içinde (Supabase Storage'ta) tutar, böylece aynı video art arda
   kullanılmaz.

## Kurulum

### 1. Instagram / Meta tarafı

1. Instagram hesabının **Business veya Creator** olduğunu ve bir
   **Facebook Sayfası'na bağlı** olduğunu doğrula (Instagram ayarları >
   Hesap türü ve araçlar).
2. [developers.facebook.com](https://developers.facebook.com/) üzerinden
   yeni bir uygulama oluştur (tür: **Business**).
3. Uygulamaya **Instagram Graph API** ürününü ekle.
4. Uygulama ayarlarından Facebook Sayfanı ve ona bağlı Instagram hesabını
   bağla.
5. **App roles > Roles** kısmından kendi Instagram/Facebook hesabını
   "Instagram Tester" olarak ekle ve Instagram uygulamasından gelen daveti
   kabul et (kendi hesabın için app review beklemeden çalışması için bu
   yeterli).
6. **Graph API Explorer**'da uygulamanı seçip şu izinlerle bir kullanıcı
   token'ı üret: `instagram_basic`, `instagram_content_publish`,
   `pages_show_list`, `pages_read_engagement`.
7. Bu kısa ömürlü token'ı, aynı Explorer'daki "Access Token Debugger" ile
   **uzun ömürlü (60 gün) token**'a çevir. Tam otomasyon için 60 günde bir
   yenilemen gerekecek — istersen ileride bunu da otomatikleştirebiliriz
   (System User token ile süresiz erişim mümkün, Business Manager
   gerektirir).
8. `IG_BUSINESS_ACCOUNT_ID`'ni bulmak için:
   `GET /me/accounts` → sayfanı bul → o sayfa için
   `GET /{page-id}?fields=instagram_business_account` → dönen ID budur.

### 2. Supabase tarafı

Mevcut Swapla Supabase projesini kullanmak yerine **ayrı, yeni bir
Supabase projesi** açmanı öneririm — bu tamamen farklı bir iş, aynı
projede karışmasın.

1. [supabase.com](https://supabase.com) üzerinde yeni proje oluştur.
2. **Storage** kısmından `insta-repost` adında **public** bir bucket aç.
3. **Project Settings > API** kısmından `URL` ve `service_role` key'ini al
   (service_role key'i asla client tarafında/mobil uygulamada kullanma —
   bu sadece bu otomasyon script'i için).

### 3. GitHub tarafı

1. Bu klasörü bir GitHub reposuna push et (istersen bu adımı benimle
   birlikte, onayınla yaparız).
2. Repo **Settings > Secrets and variables > Actions** kısmından şu
   secret'ları ekle:
   - `IG_BUSINESS_ACCOUNT_ID`
   - `IG_ACCESS_TOKEN`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
3. İstersen `CAPTION_SUFFIX` adında bir **variable** ekle (ör. "Sipariş
   için DM 📩") — her paylaşımın altına otomatik eklenir.
4. `.github/workflows/repost.yml` içindeki `cron` satırını istediğin
   saate göre ayarla (saat UTC cinsinden).

### 4. Test — deneme modu

Push'tan sonra Actions sekmesinden workflow'u **"Run workflow"** ile elle
tetikleyebilirsin. Elle tetiklemede karşına çıkan **"Deneme modu"**
kutucuğu varsayılan olarak **işaretli** gelir: bu modda video işlenip
Supabase'e yüklenir ama Instagram'a paylaşılmaz. Çalıştırma bitince
run sayfasının en altındaki **Summary**'de video linkini görüp
izleyebilirsin. Beğendiysen "Run workflow"u tekrar açıp bu sefer
**Deneme modu kutucuğunu kapatarak** çalıştır — bu sefer gerçekten
Instagram'a paylaşılır.

Zamanlanmış (cron) çalıştırmalar her zaman gerçek paylaşım yapar, deneme
modunda çalışmaz — deneme modu yalnızca elle tetiklemede kullanılır.

## Ayarlanabilir şeyler

- `MAX_REPOSTS_PER_VIDEO`: aynı videonun en fazla kaç kez remix'lenip
  paylaşılacağı (varsayılan 1).
- `scripts/repost.py` içindeki `process_video`: çevirme olasılığı,
  renk/kontrast/saturasyon aralıkları, zoom oranı — hepsi orada.

## Riskler / bilinmesi gerekenler

- Instagram günlük paylaşım sayısına limit koyuyor (hesaba göre
  genelde 25-50 arası) — günde 1 paylaşımla bu asla sorun olmaz.
- Uzun ömürlü token'lar ~60 günde bir sona eriyor; workflow o noktada
  401 hatasıyla başarısız olur, token'ı yenilemen gerekir.
- Aynı içeriği ufak değişikliklerle geri paylaşmak Instagram'ın "recycled
  content" tespiti nedeniyle bazı videolarda daha düşük dağıtım
  görebilir — bu senin zaten manuel yaptığın ve işe yaradığını söylediğin
  bir strateji, script sadece elle yaptığını otomatikleştiriyor.
- `media_url` alanı Graph API tarafında videonun orijinal CDN linkini
  döner; bazı hesap/izin kombinasyonlarında bu alan boş dönebilir — ilk
  testte bunu doğrula, boş dönerse bana söyle, alternatif bir indirme
  yolu (ör. `permalink` üzerinden oEmbed) ekleriz.
