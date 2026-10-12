# ASAPify backend

API operator untuk frontend `KRL-TEAM-PROJECT/ktp-asapify-fe`, ditujukan ke Cloud Run Jakarta.
Menambahkan API ini tidak berarti sistem deteksi satelit sudah siap operasional.

## Notification Engine v1 — 9 Oktober 2026

Menerapkan keputusan multi-kanal: inbox + persetujuan wilayah, Telegram teks, push simulasi/non-darurat,
Cloud Tasks OIDC, Firestore outbox, acknowledgement dan recovery. Mode bawaan dry_run.
Lihat [rincian implementasi dan aktivasi](docs/NOTIFICATIONS.md). WhatsApp, SMS, Gmail, LoRa dan Bluetooth
masih tahap berikutnya; tidak ditampilkan sebagai pengiriman berhasil. Google Chat tidak digunakan.

## Status integrasi 12 Oktober 2026

| Komponen | Status pada perubahan ini |
|---|---|
| FastAPI `/api/v1`, health, metadata, grid, kalender, kelompok | Diimplementasikan; baca hasil pipeline dari GCS/Firestore |
| Autentikasi | Firebase ID token, email terverifikasi, custom claim `role=operator`; revocation diperiksa |
| Keputusan operator | Transaksi Firestore: keputusan, reservasi alert, audit; keputusan kedua ditolak |
| Telegram | Adapter teks dalam Notification Engine; linking DM, dry_run default, status accepted/unknown |
| Cloud Run + GCS + Firestore + Artifact Registry | Live di `hack2skill-510921` sejak 9 Okt; image `main` di-deploy ulang 12 Okt (lihat [CLOUDSHELL.md](docs/CLOUDSHELL.md)). Rules Firestore belum di-release; bucket data masih kosong |
| Pipeline Himawari/GK2A, baseline, MAUT, kalibrasi | Kode `pipeline/` lama tetap eksperimen; belum terhubung ke API baru |
| Agen ADK/Gemini | Scaffold lama tetap; `/verify` mengembalikan 503 yang jelas, bukan hasil AI palsu |
| Daftar/penutupan alert | Belum diimplementasikan; endpoint 503, belum dipakai UI saat ini |
| Uji cloud dengan Firebase/Firestore/Telegram nyata | Login Google, preferensi dan inbox terbukti di live; 401/403 dan worker privat terverifikasi. Publish, Telegram dan alur operator belum diuji (belum ada data, bot belum dipasang) |

Kontrak FE 6 Oktober mencakup **Sumatra + Kalimantan**, origin grid `[94.9, 6.2]`, langkah 0,02°,
54 slot malam (20.00–04.50 WIB). Ini lebih baru daripada WORKLOG 29 September yang hanya Kalimantan
dan memiliki perbedaan redaksi akhir jam malam. Perubahan ini mengikuti kontrak FE untuk pertukaran data;
keputusan scope detektor tetap perlu dicatat bersama tim.

## Lokal

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
uvicorn main:app --host 127.0.0.1 --port 8080
```

`GET /api/v1/health` berjalan tanpa cloud. Endpoint operator menolak request tanpa token.
Tidak ada login bypass, seed demo otomatis, maupun data sintetis di layanan ini.
Data belum tersedia menghasilkan `DATA_NOT_READY`, bukan status `SAFE`.

Gunakan [panduan GCP](docs/GCP.md) untuk deployment dan [kontrak penyimpanan](docs/DATA_CONTRACT.md)
untuk menghubungkan pekerjaan tim observasi/agen. `requirements.txt` lama tetap untuk eksperimen pipeline;
image API memakai `requirements-api.txt` agar dependency geospasial tidak membebani layanan operator.
