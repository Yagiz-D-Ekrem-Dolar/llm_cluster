# LLM küme yöneticisi

ChatGPT benzeri hizmetinize GPU sağlar. Ana PC, switch üzerindeki Ubuntu makinelere kodu ve modelleri gönderir, inference'ı başlatır,
talebe göre yönetir ve sonucu hosting'deki sitenize verir. Sohbet, konuşma → metin ve metin → konuşma aynı yoldan servis edilir.

```
 Kullanıcı ─► Siteniz (hosting) ─► Röle ◄──── dışarı doğru bağlantı ───┐
                                                                       │
 İnternet ◄──► Superbox (modem) ◄──► Switch ◄──► Ana PC  (panel + ağ geçidi + model deposu)
                                       ├───────► laptop-1 … laptop-6 (ajan + llama.cpp / whisper.cpp / Piper)
                                       └───────► diğer Ubuntu makineler
```

* **Superbox yalnızca modemdir.** Küme makinesi değildir; panelde *Ağ* sayfasında izlenir.
* Mobil modemler çoğunlukla CGNAT arkasındadır, dışarıdan içeri port açılamaz. Bu yüzden bağlantıyı **küme başlatır**; modemde ayar gerekmez.
* Model dosyası modemden **bir kez** iner. Makinelere switch üzerinden gider; makineler birbirinden de alır.

## Kurulum

### 1. Ana PC

Windows:

```bat
start.bat
```

Ubuntu:

```bash
sudo bash deploy/install_controller.sh --admin-password 'güçlü-parola' --no-caddy
```

Panel `http://ANA-PC:8080` adresindedir, yalnızca yerel ağda kullanın. Windows güvenlik duvarı sorarsa **özel ağ** için 8080 (panel) ve 8079 (dosya dağıtımı) portlarına izin verin.

Ana PC'nin GPU'sunu da kullanmak için: *Sunucular → Ağı tara ve kur → Bu bilgisayarı ekle*.

### 2. Makineler

*Sunucular → Ağı tara ve kur*. Panel switch'i tarar, Ubuntu makineleri listeler. Seçip SSH kullanıcı/parola girin; ajan, systemd servisi, Wake-on-LAN, kapak ayarı, güç izni ve depo otomatik kurulur.
Parola saklanmaz. Makinelerde OpenSSH sunucusu açık, kullanıcıda sudo yetkisi olmalı. BIOS/UEFI'de **Wake on LAN** açık ve makine **kabloyla** bağlı olmalı.

Kurulum seçenekleri: ffmpeg (ses biçimleri), Piper (Türkçe TTS), derleme araçları, NVIDIA sürücüsü.

### 3. Yamalı llama.cpp

*Modeller → Runtime'lar → llama.cpp → Derleme ekle*:

| Yöntem | Ne olur |
|---|---|
| **Hazır derleme** (tar.gz / zip) | Platform ve hızlandırıcıyı seçin (CUDA, Vulkan, CPU). Her makineye donanımına uyan paket gider. `llama-server` paket içinde otomatik bulunur. |
| **Kaynak kod** (dosya, klasör ya da git) | Her makine kendi işlemcisi ve GPU'su için derler. Kaynak kökünde `orkestra-build.sh` varsa o çalışır, yoksa CMake kullanılır. |

Çalışan replikaları yeni sürüme geçirmek için *Çalışanları güncelle*: sırayla yeniden başlatılır. Komut satırı *Komutu düzenle* ile değiştirilir (`{model_path} {port} {ctx_total} {ngl} {parallel} {threads} {name}`).

Whisper için aynı işlemi **whisper.cpp** runtime'ında yapın. Piper köprüsü uygulamayla gelir; makinede kurulum seçeneği yeterlidir.

### 4. Modeller

*Modeller → HuggingFace'ten ekle*. Tür seçin (Sohbet, Konuşma → metin, Metin → konuşma), depoyu ve nicemlemeyi/dosyayı işaretleyin:

* Sohbet: GGUF deposu, ör. `Qwen/Qwen2.5-7B-Instruct-GGUF` → `Q4_K_M`
* Konuşma → metin: `ggerganov/whisper.cpp` → `ggml-large-v3-turbo-q5_0.bin`
* Metin → konuşma: `rhasspy/piper-voices` → `tr/tr_TR/...`

*Dağıtım* sayfası hangi modelin hangi makinede olduğunu gösterir. Sık kullanılan modellerde **en az 1 replika** verin.

### 5. Siteye bağlantı

*Site bağlantısı → Bağlantı*:

| Hosting | Yapılacak |
|---|---|
| Paylaşımlı PHP hosting | `relay.php` indirin, ör. `public_html/llm/relay.php` olarak yükleyin. Adres: `https://siteniz.com/llm/relay.php` |
| Node.js hosting (Hostinger Business/Cloud gibi) | Zip'i Node.js uygulaması olarak yayınlayın. Yoğun trafikte PHP'den iyidir. |
| VPS | `python3 relay.py --host 127.0.0.1 --port 8088`, önüne HTTPS (Caddy/nginx) koyun. |
| Cloudflare hesabı | `cloudflared` kurun, token girin. |

Adresi yazıp *Kaydet ve bağlan*, sonra *Uçtan uca sına*. Röle dosyası tünel anahtarını içerir; anahtarı değiştirirseniz yeniden yükleyin.

### 6. Site kodu

*Site bağlantısı → Servisler*: site sabit adlar kullanır (`sohbet`, `konusma-metin`, `metin-konusma`), arkadaki modeli istediğiniz an değiştirirsiniz; birden çok model yedek sırasıyla eklenebilir.
*Ayarlar → API anahtarları*: sitenin arka ucu için anahtar. Hazır PHP/Node örnekleri *Site entegrasyonu* sekmesindedir.

```bash
curl https://siteniz.com/llm/relay.php/v1/chat/completions \
  -H "Authorization: Bearer sk-ork-..." -H "X-User-Id: kullanici-42" -H "X-User-Plan: plus" \
  -H "X-Conversation-Id: sohbet-9" -H "Content-Type: application/json" \
  -d '{"model":"sohbet","stream":true,"messages":[{"role":"user","content":"Merhaba"}]}'
```

| Uç | Görev |
|---|---|
| `POST /v1/chat/completions` | Sohbet (akışlı) |
| `POST /v1/audio/transcriptions` | Konuşma → metin (`file`, isteğe bağlı `model`, `language`) |
| `POST /v1/audio/speech` | Metin → konuşma (`input`, parça parça ses) |
| `GET /v1/usage?user=ID` | Kullanıcının kullanımı ve kalan hakkı |
| `GET /v1/status` | Servis hazır mı, yoğun mu |

**Paketler:** `free`, `plus`, `pro` için istek/dk, eşzamanlı istek, günlük token ve yanıt uzunluğu *Paketler ve kullanım* sekmesinden ayarlanır; ücretli paketler kuyrukta önce gelir, bekleyen ücretsiz kullanıcı da zamanla öne geçer.
API anahtarı tarayıcıya verilmez. Tarayıcı doğrudan bağlanacaksa arka ucunuz imza anahtarıyla kısa ömürlü `ut1.` jetonu üretir.

## Otomatik işleyiş

| Konu | Davranış |
|---|---|
| Dağıtım | Model ana PC'ye bir kez iner, sha256 doğrulanır, makineler arası aktarılır; kopan aktarım kaldığı yerden sürer, yer azalırsa kullanılmayan model silinir. |
| Başlatma | Dosyalar hazır olunca süreç başlar; 1 token'lık deneme çıkarımı geçmeden trafik verilmez. |
| Yerleştirme | VRAM/RAM, dosyaların nerede olduğu, sıcaklık ve pil durumuna göre seçer; yer yoksa boştaki düşük öncelikli modeli kaldırır, gerekirse uyuyan makineyi uyandırır. |
| Yük dengeleme | Ölçülen hıza ve yoğunluğa göre dağıtır; aynı sohbet aynı replikaya gider; hata olursa başka replikaya geçer. |
| Güç | Boştaki makineler uyur, talepte Wake-on-LAN ile uyanır, GPU düşerse yeniden başlar. Adresi değişen makine ağda yeniden bulunur. |
| Isı | Kritik sıcaklıkta modeller taşınır, soğuyunca makine geri döner. |
| Ajan | Ana PC'deki sürüme kendini günceller; çalışan inference kesilmez. |
| Kayıt | Kullanıcı ve sohbetler saklanır, zamanlanmış olarak HuggingFace dataset'ine gider. Boşta distillation veri seti üretir. |

## Güvenlik

* Modemde port açmayın. Panel (8080), dosya portu (8079) ve ajan portu (7777) yalnızca yerel ağda kalmalı.
* Ajan token'ı o makinede program çalıştırma yetkisidir; makineler arası dosya alışverişi süreli imzalı biletle yapılır, her dosya sha256 ile doğrulanır.
* Tünel anahtarı ve API anahtarları gizlidir. Site ile röle arasında HTTPS kullanın.
* **Sohbetler kullanıcı verisidir.** HuggingFace deposunu özel tutun, anonimleştirme ve kişisel veri maskelemeyi açık bırakın, gizlilik metninizde belirtin.

## Neler sınandı

`tests/e2e_cluster.py` simülatör kullanmaz: ayrı IP'lerde gerçek ajan süreçleri, gerçek TCP aktarımları, gerçek SSH protokolü, PHP 8.3 / Node.js / Python röleleri, gerçek akış.
Taklit edilenler: HuggingFace sunucusu, `llama-server`/`whisper-server` ikilileri, Piper kütüphanesi ve Ubuntu sistem komutları (`apt-get`, `systemctl`).

**Henüz gerçek donanımda doğrulanmadı:** Ubuntu üzerinde kurulum betiğinin tamamı, Wake-on-LAN ve uyku, NVIDIA ölçümleri, gerçek llama.cpp/whisper.cpp/Piper ikilileri, Hostinger üzerindeki PHP/Node.js davranışı.
PHP rölesinde kullanıcı bağlantıyı kestiğinde üretimin durması hosting'in bağlantı kopmasını bildirmesine bağlıdır; yoğun kullanımda Node.js ya da VPS rölesi önerilir.
Distillation veri seti üretir, öğrenci modeli eğitmez. Tek modeli birden çok makineye bölme yoktur; kapasite replikayla artar.

```bash
py tests/e2e_cluster.py    # gerçek akış
py tests/e2e_real.py       # ajan güvenliği ve süreç yönetimi
py tests/run_e2e_demo.py   # panel/otomasyon senaryoları (simülatör)
```

Arayüzü donanımsız görmek için: `start_demo.bat` (sahte makinelerle çalışır).
