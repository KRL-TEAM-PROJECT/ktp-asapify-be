# ASAPify Notification Engine v1

Implementasi keputusan chat 9 Oktober 2026:
https://chatgpt.com/s/t_6ac82245a3988191a78613a3f6fc0751

Tujuan: beberapa pilihan kanal, persetujuan per wilayah, pesan ringkas, konfirmasi penerimaan,
antrean yang dapat dipulihkan, dan jejak pengiriman. Google Chat tidak digunakan.
Status paket: kode dan pengujian lokal; **belum deploy atau mengirim ke penerima nyata**.

## Batas tahap ini

| Komponen | Implementasi |
|---|---|
| Cloud Run API, worker privat, Firestore | Kode API, transaksi publikasi dan outbox, skrip setup |
| Cloud Tasks, Cloud Scheduler | Tugas HTTP OIDC, nama deterministik, pemulihan outbox tiap menit |
| Inbox | Firebase login penerima; wilayah, persetujuan, 50 pesan terbaru, konfirmasi idempoten |
| Telegram | Linking DM dengan kode sekali pakai; webhook secret; `sendMessage` teks ringan |
| FCM dan Web Push | Satu transport FCM; satu perangkat per akun; hanya simulasi/non-darurat |
| WhatsApp, SMS, Gmail | Pilihan tersimpan dan kontrak routing; adapter **belum diimplementasikan** |
| LoRa / LoRaWAN, Bluetooth Mesh | Pilihan/desain saja; belum ada driver, gateway, atau perangkat |
| Offline | Frontend menyimpan shell publik dan inbox yang dipilih pengguna; tanpa pesan baru saat terputus |
| Penggunaan publik massal | Belum; pilot maksimal 40 pelanggan aktif per provinsi |

Batas 40 sengaja menahan satu transaksi di bawah batas operasi Firestore. Melampaui batas menghasilkan
`FANOUT_LIMIT` sebelum keputusan disimpan; tidak diam-diam mengabaikan pelanggan. Untuk skala publik,
pindahkan fanout ke cursor/pagination dan event outbox terpisah, lalu uji beban sebelum membuka pendaftaran luas.

**Koreksi rancangan FCM:** dokumentasi Google melarang FCM untuk emergency/high-risk use cases.
Karena itu semua publikasi AWAS operasional diberi `purpose=emergency` oleh server dan diblokir untuk
`fcm`/`web_push` pada tiga lapisan: planning, claiming, adapter. Mengubah caption atau prioritas tidak
mengubah klasifikasi. Backtest mendapat `replay=true` dari manifest pipeline dan menjadi `exercise`;
penerima harus menyetujui simulasi. Jangan mengubah replay untuk menyamarkan peringatan nyata sebagai informasi.

## Alur dan jaminan

1. Operator memilih Publikasikan setelah verifikasi. Transaksi Firestore membaca pelanggan yang setuju,
   lalu menyimpan keputusan, audit, alert, inbox dan semua job kanal sekaligus. Tidak ada provider call di transaksi.
2. API mencoba enqueue. Gagal enqueue tidak menghilangkan keputusan; respons `queue.awaiting_recovery`.
   Scheduler memindai outbox `pending` dan mencoba lagi. Nama task menggunakan hash dan generation.
3. Cloud Tasks memanggil worker privat menggunakan OIDC service account yang tepat. Header nama task
   sendiri tidak memberi akses. UID, chat ID dan token perangkat tidak dimasukkan ke task.
4. Transaksi worker mengklaim satu job, memeriksa persetujuan terkini, wilayah, expiry, dan acknowledgement.
   Permintaan ulang/concurrent yang melihat `sending` atau hasil terminal tidak mengirim ulang.
5. Respons provider diterima = `accepted`, bukan `delivered`/`read`. Tombol Konfirmasi Diterima mengubah
   `acknowledged_at` di inbox milik akun tersebut. Membuka link saja tidak membuat acknowledgement.
6. Timeout atau proses mati setelah klaim dapat berarti provider sudah menerima. Status menjadi `unknown`;
   tidak ada retry otomatis ke provider. Scheduler menandai `sending` > 5 menit sebagai unknown.
   Ini menghindari resend buta, **bukan jaminan exactly-once**. Rekonsiliasi unknown dilakukan operator/admin.
7. TTL pengiriman v1 = satu jam dari publikasi; TTL push di perangkat = 5 menit. Inbox tetap menyimpan riwayat.
   SMS/WhatsApp untuk emergency direncanakan paralel, tanpa menunggu 120 detik. Untuk exercise/informasi,
   rencana eskalasi tertunda 120 detik. Saat ini adapter tersebut menghasilkan `blocked/ADAPTER_PENDING`.

Ada perlombaan yang tidak dapat ditarik kembali: pencabutan persetujuan/ack setelah worker mulai mengirim
mungkin tidak menghentikan request yang sedang berjalan. Semua job berikutnya memeriksa ulang kondisi.

Status kanal: `pending`, `sending`, `accepted`, `failed`, `unknown`, `blocked`, `cancelled`, `simulated`.
`simulated` berarti provider tidak dihubungi. Hanya job `pending` dijadwalkan; SMS/WhatsApp aktif nol,
sehingga tidak ada biaya pesan berbayar pada v1. Jangan membuka adapter berbayar tanpa quota harian atomik,
rate limit per penerima, template resmi, persetujuan dan uji delivery receipt.

## Project yang dipilih

Screenshot GCP pengguna tanggal 9 Oktober 2026 menunjukkan nama `hack2skill`, Project ID
`hack2skill-510921`, nomor project `950889990251`. Profil non-rahasia ada di `gcp/project.json`.
Region Jakarta adalah rancangan tim, bukan informasi yang dibuktikan oleh screenshot.
Screenshot belum membuktikan billing, Firebase Auth, izin deploy atau layanan sudah aktif.

## Konfigurasi dan deployment

Jalankan setup dasar dari [GCP.md](GCP.md), termasuk database `asapify` dan Firebase Auth Google.
Gunakan project khusus yang telah disetujui pemiliknya dan billing aktif. Token/secret tetap di Secret Manager.
Skrip ini hanya mencetak rencana tanpa `--apply`.

```bash
export ASAPIFY_PROJECT_ID='hack2skill-510921'
python3 gcp/manage.py bootstrap --project "$ASAPIFY_PROJECT_ID"
python3 -m gcp.notifications bootstrap --project "$ASAPIFY_PROJECT_ID"
python3 gcp/manage.py bootstrap --project "$ASAPIFY_PROJECT_ID" --apply
python3 -m gcp.notifications bootstrap --project "$ASAPIFY_PROJECT_ID" --apply
python3 gcp/manage.py build --project "$ASAPIFY_PROJECT_ID" --version notification-v1 --apply
python3 gcp/manage.py deploy --project "$ASAPIFY_PROJECT_ID" --version notification-v1 --apply
python3 -m gcp.notifications deploy --project "$ASAPIFY_PROJECT_ID" --version notification-v1 \
  --public-origin "https://${ASAPIFY_PROJECT_ID}.web.app"
python3 -m gcp.notifications deploy --project "$ASAPIFY_PROJECT_ID" --version notification-v1 \
  --public-origin "https://${ASAPIFY_PROJECT_ID}.web.app" --apply
```

Script notification deploy selalu mengembalikan `NOTIFICATION_MODE=dry_run`, `FCM_ENABLED=false`.
Pasang mode live hanya setelah pengujian penerima demo yang disepakati. Worker tetap IAM privat.
Account `asapify-task` mendapat `run.invoker` hanya pada worker; tidak mendapat akses database.
API dan worker diberi `cloudtasks.enqueuer` serta hak actAs hanya account task. Worker mendapatkan
`datastore.user` dan `firebasecloudmessaging.admin`; tidak ada Owner/Editor maupun key service account.
Default Google Cloud Tasks / Scheduler service agents harus tetap memiliki role bawaan untuk menerbitkan OIDC.

Deploy index dan rules dari folder `gcp` setelah memeriksa rules database yang ada:

```bash
cd gcp
npx firebase-tools deploy --only firestore --config firebase.notifications.json --project "$ASAPIFY_PROJECT_ID"
```

Rules terlampir menolak semua akses browser langsung ke Firestore; server menggunakan IAM. **Jangan
mengganti rules database aplikasi lain.** Konfigurasi ini menarget database khusus `asapify`.
Tunggu index READY sebelum uji publikasi/inbox. Semua field konfigurasi tercantum pada `.env.example`.

### Telegram

- Buat bot demo; simpan token sebagai secret `telegram-bot-token`, akses worker saja.
- Buat secret webhook acak `telegram-webhook-secret`, akses API saja.
- Set bot webhook ke `https://<API_OR_HOSTING>/api/v1/notifications/telegram-webhook`,
  dengan `secret_token` yang sama dan `allowed_updates=["message"]` melalui API resmi Telegram.
- Set `TELEGRAM_BOT_USERNAME` di API. UI menghasilkan deep link /start sekali pakai selama 10 menit.
- Penerima login, pilih wilayah/Telegram, setujui langganan, buka bot dan tekan Start.
  Linking hanya menerima chat privat dari webhook; browser tidak bisa mengisi tujuan chat orang lain.
- Untuk demo live, set `TELEGRAM_ENABLED=true` dan `NOTIFICATION_MODE=live` pada API + worker,
  setelah memasang secret token di worker. Foto tidak wajib sehingga pesan tidak menunggu unggahan citra.
- Telegram grup provinsi dari rancangan lama tidak diaktifkan otomatis. Tahap ini menggunakan DM
  per penerima agar persetujuan dan acknowledgement bisa diatribusikan dengan benar. Group targets
  memerlukan registrasi admin dan persetujuan grup terpisah; belum diimplementasikan.

### Push browser / FCM

Di Firebase Console, pastikan FCM API aktif, salin Web Push VAPID public key dan messaging sender ID
ke konfigurasi publik frontend. Konfigurasi web bukan service-account key. Browser memerlukan HTTPS,
service worker, dan izin notifikasi. Klik Daftarkan push browser, pilih Web Push, dan simpan persetujuan
serta izin simulasi. Menentukan FCM dan Web Push bersamaan tetap hanya menghasilkan satu task per token.
Set `FCM_ENABLED=true` pada API + worker hanya untuk uji simulasi/non-darurat. Kode tetap memblokir emergency.

### Pengujian cloud yang masih diperlukan

- Firebase token nyata: penerima dapat membuka inbox sendiri, tetapi operator endpoint tetap ditolak.
- Firestore transaksi konkuren dan index: dua operator menerbitkan cluster sama hanya sekali.
- Dua Cloud Tasks job yang sama: satu klaim; matikan worker setelah klaim untuk memeriksa unknown.
- Cabut persetujuan/ack sebelum job due, pastikan pembatalan; uji expiry dan 40/41 pelanggan.
- Telegram ke akun demo terverifikasi dan push simulasi ke browser yang mengizinkan. Catat waktu
  published_at, started_at, finished_at, acknowledged_at; jangan menyebut accepted sebagai latency ke ponsel.
- Uji ponsel di daerah sasaran; SMS/WA belum bisa dibandingkan sebelum adapter/provider tersedia.

## API tambahan

| Endpoint | Akses / fungsi |
|---|---|
| GET `/api/v1/notifications/settings` | Penerima; preferensi tanpa token/alamat chat rahasia |
| PUT `/api/v1/notifications/preferences` | Penerima; provinces, channels, consent, exercises |
| POST / DELETE `/api/v1/notifications/device` | Penerima; daftar / lepas satu perangkat push |
| POST `/api/v1/notifications/telegram-link` | Penerima; kode deep link sekali pakai |
| POST `/api/v1/notifications/telegram-webhook` | Webhook secret; hanya binding, tanpa pengiriman balasan |
| GET `/api/v1/notifications/inbox` | 50 pesan terakhir milik akun |
| POST `/api/v1/notifications/inbox/{id}/ack` | Acknowledgement idempoten; ID milik akun lain -> 404 |
| GET `/api/v1/operator/alerts/{id}/deliveries` | Operator; status, error, timestamps, acknowledgement |
| POST `/_internal/notifications/dispatch` | OIDC worker, body job_id |
| POST `/_internal/notifications/sweep` | OIDC Scheduler; pemulihan antrean/unknown |

## Referensi resmi

- https://firebase.google.com/docs/cloud-messaging/customize-messages/setting-message-priority (life critical use cases)
- https://firebase.google.com/docs/cloud-messaging/js/client
- https://docs.cloud.google.com/tasks/docs/creating-http-target-tasks
- https://docs.cloud.google.com/tasks/docs/samples/cloud-tasks-create-http-task-with-token
- https://core.telegram.org/bots/api#sendmessage
- https://core.telegram.org/bots/api#setwebhook

Pipeline satelit dan agen Gemini tetap pekerjaan terpisah dari notification engine. Paket ini belum
merupakan EWS operasional dan belum membuktikan latency, jangkauan, atau delivery rate di lapangan.
