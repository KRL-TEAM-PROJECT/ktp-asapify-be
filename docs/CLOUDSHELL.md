# Deployment ASAPify melalui Cloud Shell

Target tetap: project `hack2skill-510921`, region Jakarta `asia-southeast2`, database `asapify`.
Runner mengerjakan API, worker, antrean, indeks dan recovery scheduler. Notifikasi tetap `dry_run`;
Telegram dan FCM dinonaktifkan. Pipeline satelit/AI belum diimplementasikan oleh deployment ini.

## Status percobaan 9 Oktober 2026

Deployment belum dijalankan ke GCP. Lingkungan agen tidak mempunyai Google Cloud CLI atau Application
Default Credentials, dan konsol project dua kali menampilkan `Site Unavailable`. Tidak ada resource GCP
yang diubah. Runner diperiksa secara lokal; hasil lokal bukan bukti layanan cloud sudah berjalan.

## 1. Buka Cloud Shell dan ambil source

Di Google Cloud Console, pilih `hack2skill-510921`, lalu buka Cloud Shell. Gunakan akun yang sudah
berwenang mengelola project dan setujui otorisasi Cloud Shell ketika diminta. Billing harus sudah aktif;
runner tidak membuat akun billing atau mengubah budget. Resource yang dibuat bisa menimbulkan biaya.

Clone kedua repo privat menggunakan akun GitHub yang mempunyai akses. Bila `gh` tersedia, login lewat
`gh auth login --hostname github.com --git-protocol https --web`, lalu jalankan `gh auth setup-git`.
Jangan menaruh token GitHub, kode login, password, atau service-account key ke chat maupun commit.

Jalankan dari direktori baru agar checkout pekerjaan yang sudah ada tidak tertimpa:

```bash
git clone --branch codex/notification-engine-v1 --single-branch https://github.com/KRL-TEAM-PROJECT/ktp-asapify-be.git
git clone --branch codex/notification-engine-v1 --single-branch https://github.com/KRL-TEAM-PROJECT/ktp-asapify-fe.git
cd ktp-asapify-be
```

Source aplikasi awal berasal dari BE `c0305126ba2d6072c8299fa35d7e15d2dd75e9a1` dan FE
`bed1e941c5f08198ac000de2236d8ffefb807ca9`. Runner ini ditambahkan pada commit berikutnya dalam PR backend #1.
PR tidak harus di-merge untuk menjalankan branch ini. Catat `git rev-parse HEAD` di kedua checkout sebelum deploy.

## 2. Deploy backend

```bash
python3 -m gcp.cloudshell backend
python3 -m gcp.cloudshell backend --apply
```

Tanpa `--apply`, runner hanya mencetak rencana. Dengan `--apply`, runner:

1. Memeriksa akun aktif, project, dan billing.
2. Menyiapkan resource dan service account dari `gcp/manage.py` serta `gcp/notifications.py`.
3. Menambahkan tiga indeks Firestore yang belum ada tanpa menghapus indeks lain, lalu menunggu READY.
4. Build image dengan tag waktu unik, deploy API dan worker, dan memasang recovery scheduler.
5. Memeriksa kedua service Ready dan semua flag mode uji, health API 200, akses operator tanpa token
   Firebase 401, serta akses anonim ke worker ditolak.

Backend tetap privat pada lapisan IAM sampai tahap Hosting. Pemeriksaan memakai token gcloud berumur
pendek di memori, bukan key JSON atau token yang dicetak. Akun deploy perlu izin memanggil layanan tersebut.
Resource yang berhasil dibuat tetap ada jika langkah berikutnya gagal; tidak ada rollback destruktif otomatis.
Setelah masalah diperbaiki, runner dapat dijalankan ulang. Gunakan `--version TAG_UNIK` jika ingin menetapkan tag.

## 3. Siapkan Firebase dan deploy Hosting

Tambahkan Firebase ke **project yang sama**, daftarkan Web App, aktifkan provider Google dan tambahkan
`hack2skill-510921.web.app` ke Authorized domains. Jika ada persetujuan/ketentuan layanan, pemilik akun
menyelesaikannya di konsol. Gunakan Node.js >=22.12 dan Firebase CLI resmi:

```bash
npm install -g firebase-tools
firebase login --no-localhost
```

Jika CLI sudah terautentikasi atau menggunakan ADC, login ulang tidak diperlukan. Ikuti login di browser;
jangan membagikan nilai autentikasi. Isi `ktp-asapify-fe/.env.production.local` dari Web App tersebut:

```dotenv
VITE_API_MODE=live
VITE_API_BASE=
VITE_FIREBASE_API_KEY=SALIN_DARI_WEB_APP
VITE_FIREBASE_AUTH_DOMAIN=hack2skill-510921.firebaseapp.com
VITE_FIREBASE_PROJECT_ID=hack2skill-510921
VITE_FIREBASE_APP_ID=SALIN_DARI_WEB_APP
VITE_FIREBASE_MESSAGING_SENDER_ID=950889990251
VITE_FIREBASE_VAPID_KEY=
VITE_PROVINCES_LAYER_URL=/mock/provinces.geojson
```

Gunakan `authDomain` yang benar-benar diberikan konfigurasi Firebase jika berbeda. VAPID boleh kosong
selama push masih nonaktif. Konfigurasi web adalah identifier publik, bukan kredensial Admin.
Simpan konfigurasi lokal ini di luar git. Lihat juga `docs/GCP.md` di repo frontend.

Dari root repo backend:

```bash
python3 -m gcp.cloudshell hosting --frontend ../ktp-asapify-fe
python3 -m gcp.cloudshell hosting --frontend ../ktp-asapify-fe --apply
```

Tahap ini memeriksa konfigurasi, membangun frontend dalam mode live, memeriksa backend, memasang rules
deny-all hanya pada database `asapify`, memberi `allUsers` **run.invoker hanya pada API**, lalu deploy Hosting.
Firebase ID token dan role tetap diperiksa aplikasi; worker tetap privat. Bila organisasi melarang public
invoker, berhenti dan minta admin menentukan desain akses; runner tidak mengubah kebijakan organisasi.

Jangan gunakan tahap ini pada database `asapify` yang dipakai aplikasi lain dengan rules berbeda. Hosting
mengganti konten situs default project dengan ASAPify; pastikan itu memang situs tujuan. Periksa rencana dahulu.

Setelah berhasil, runner memeriksa HTTP halaman utama, `/notifikasi`, health lewat rewrite, dan penolakan
akses operator anonim. URL yang diharapkan setelah sukses: `https://hack2skill-510921.web.app/notifikasi`.
URL ini belum dinyatakan live sebelum hasil deployment dan pemeriksaan tersebut lulus.

## 4. Verifikasi akun dan data

Login Google sebagai penerima, buka `/notifikasi`, lalu uji penyimpanan preferensi dan inbox. Admin perlu
memeriksa UID/email di Firebase Authentication sebelum memakai `tools/set_operator.py` untuk memberi
role operator. Runner tidak otomatis memberi role kepada akun pengguna mana pun.

Untuk mengulang pemeriksaan backend saja:

```bash
python3 -m gcp.cloudshell verify --apply
```

HTTP checks tidak membuktikan login, transaksi Firestore, pengiriman provider, atau pipeline satelit bekerja.
Tanpa data pipeline, `DATA_NOT_READY` adalah kondisi yang diharapkan. Lanjutkan pengujian integrasi di
`docs/NOTIFICATIONS.md` sebelum mengaktifkan kanal nyata. Catat URL dan pesan error pertama untuk diagnosis;
jangan salin token atau log berisi rahasia.

Referensi: [Cloud Run + Firebase Hosting](https://firebase.google.com/docs/hosting/cloud-run),
[Firebase CLI](https://firebase.google.com/docs/cli),
[Firestore composite indexes](https://docs.cloud.google.com/sdk/gcloud/reference/firestore/indexes/composite/create),
[Cloud Run developer authentication](https://docs.cloud.google.com/run/docs/authenticating/developers).
