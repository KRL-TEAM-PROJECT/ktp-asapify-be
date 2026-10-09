# Deploy ASAPify ke Google Cloud

Panduan ini untuk project khusus ASAPify yang sudah aktif dan terhubung billing.
Tidak ada deployment cloud yang telah dijalankan dalam pembuatan perubahan ini.

Untuk menjalankan tahapan sekaligus melalui akun Cloud Shell pengguna, lihat
[runner Cloud Shell](CLOUDSHELL.md). Runner menyediakan plan, deploy backend, deploy Hosting, dan smoke checks.

## Yang disiapkan

| Resource | Konfigurasi |
|---|---|
| Cloud Run | `asapify-api`, Jakarta `asia-southeast2`, 1 vCPU, 512 MiB, min 0/max 2 |
| Firestore Native | Database bernama `asapify`, Jakarta, delete protection |
| GCS | `<PROJECT_ID>-asapify-data`, uniform IAM, public access prevention |
| Build staging/logs | `<PROJECT_ID>-asapify-builds`, privat |
| Artifact Registry | Docker repository `asapify`, Jakarta |
| Runtime account | `asapify-api`: datastore.user + firebaseauth.viewer, read-only data bucket |
| Builder account | `asapify-build`: objectAdmin hanya bucket build, writer hanya repository image |

Peran datastore.user berlaku se-project; karena itu gunakan project khusus ASAPify.
Tidak ada key JSON service account, akun Owner/Editor untuk runtime, undangan anggota, billing baru,
VM, Redis, atau cron ingest yang diaktifkan oleh skrip. Scheduler/agent menunggu pipeline yang tervalidasi.

## 1. Pilih project dan periksa biaya

Buka Cloud Shell di project yang benar. Akun setup memerlukan izin membuat resource, mengaktifkan API,
menambahkan binding IAM, dan `iam.serviceAccounts.actAs` pada kedua service account. Jalankan sebagai
admin project yang sudah berwenang; skrip tidak memberikan hak admin pada pengguna lain.

Pastikan billing aktif, lalu buat budget dan notifikasi pada ambang yang disepakati Harris.
Budget alert bukan batas pengeluaran otomatis; min 0/max 2 juga bukan jaminan biaya nol.
Belum ada estimasi biaya live: jumlah piksel, volume data satelit, dan frekuensi verifikasi belum diukur.

Di checkout branch perubahan backend:

```bash
export ASAPIFY_PROJECT_ID='hack2skill-510921'
python3 gcp/manage.py check --project "$ASAPIFY_PROJECT_ID" --apply
python3 gcp/manage.py bootstrap --project "$ASAPIFY_PROJECT_ID"
```

Perintah terakhir hanya mencetak rencana. Tinjau resource dan project; untuk membuatnya:

```bash
python3 gcp/manage.py bootstrap --project "$ASAPIFY_PROJECT_ID" --apply
python3 gcp/manage.py build --project "$ASAPIFY_PROJECT_ID" --version operator-v1 --apply
python3 gcp/manage.py deploy --project "$ASAPIFY_PROJECT_ID" --version operator-v1 --apply
```

Bootstrap membaca inventory sebelum create; kegagalan permission tidak dianggap resource hilang.
Bucket yang sudah ada tidak dihapus/dikosongkan. Bila memakai ulang bucket bernama sama,
periksa sendiri region dan public access prevention-nya. Perintah ini belum diuji di project nyata.
Gunakan versi image baru untuk setiap perubahan berikutnya agar rollback tidak ambigu.

## 2. Aktifkan Firebase Auth

Tambahkan Firebase ke **project GCP yang sama** melalui Firebase Console, buat Web App, aktifkan provider
Google, pilih support email, dan tambahkan domain Hosting ke Authorized domains. Langkah akun/persetujuan
Firebase dilakukan oleh pemilik project. Salin konfigurasi web ke `.env.local` repo FE sesuai `.env.example`.
Konfigurasi web bukan kunci Admin; jangan meletakkan token bot atau service-account key di `VITE_*`.

Setelah calon operator login Google pertama kali (akan ditolak sampai perannya diberikan), admin
memeriksa email dan UID di Firebase Authentication. Dari lingkungan Python yang terautentikasi ADC:

```bash
gcloud auth application-default login
python tools/set_operator.py --project "$ASAPIFY_PROJECT_ID" --uid 'UID_YANG_SUDAH_DIVERIFIKASI'
python tools/set_operator.py --project "$ASAPIFY_PROJECT_ID" --uid 'UID_YANG_SUDAH_DIVERIFIKASI' --apply
```

Tidak ada email dari chat yang diberi akses otomatis. Operator keluar lalu masuk lagi agar claim diperbarui.

## 3. Periksa API sebelum menghubungkan Hosting

Deploy awal menggunakan IAM privat. Gunakan proxy gcloud, yang memasukkan identitas cloud secara otomatis:

```bash
gcloud run services proxy asapify-api --project "$ASAPIFY_PROJECT_ID" --region asia-southeast2 --port 8080
```

Dari terminal lain: `curl -i http://127.0.0.1:8080/api/v1/health` harus 200;
`curl -i http://127.0.0.1:8080/api/v1/operator/pixels` harus 401.
Token Firebase yang sah tetapi tidak punya role operator harus 403. Token expired/revoked harus 401.
Periksa akses sah dengan token operator dari aplikasi; jangan kirim token ke chat atau commit repo.

Untuk rewrite Firebase Hosting, Cloud Run perlu menerima request pada lapisan transport tanpa IAM.
Endpoint operator **tetap** memverifikasi token Firebase dan peran di server:

```bash
python3 gcp/manage.py expose-api --project "$ASAPIFY_PROJECT_ID"
python3 gcp/manage.py expose-api --project "$ASAPIFY_PROJECT_ID" --apply
```

Perintah tersebut memberi `allUsers` hanya `roles/run.invoker`, bukan akses database/storage.
Jika kebijakan organisasi melarang public invoker, jangan melemahkan kebijakan: gunakan desain akses
yang disetujui admin. Deployment Hosting dijelaskan di `docs/GCP.md` repo FE.

## 4. Sambungkan data dan notifikasi

Gunakan `tools/publish_snapshot.py` dengan hasil nyata sesuai DATA_CONTRACT.md.
Manifest diterbitkan setelah objek dan detail selesai. `replay` pada manifest diwariskan ke cluster,
sehingga backtest tidak dipublikasikan sebagai peringatan nyata.

Telegram inline lama diganti outbox multi-kanal. Ikuti [NOTIFICATIONS.md](NOTIFICATIONS.md) untuk worker,
Cloud Tasks, consent penerima, Telegram DM, push simulasi, indeks Firestore, dan pengujian cloud.
Jangan memakai instruksi tg_chats/sendPhoto lama untuk jalur v1 baru.

## Pekerjaan lanjutan yang masih wajib

1. Samakan keputusan scope Sumatra/Kalimantan dan jam malam dengan tim; catat pada WORKLOG.
2. Lengkapi ingest GK2A/Himawari, peat mask, baseline NASA POWER, MAUT/neighbour support, dan kalibrasi.
3. Lengkapi agen ADK/Gemini dengan bukti VIIRS/citra dan batas tool; hapus ketergantungan jalur lama pada ToPeCAl.
4. Lengkapi penutupan alert, pemulihan outbox, historical decision snapshots, dan pembaruan signed URL citra.
5. Uji token nyata, transaksi Firestore konkuren, egress Telegram ke penerima demo, build container, dan Firebase rewrite.
6. Baru kemudian tambahkan Cloud Scheduler untuk job yang sudah dapat dijalankan dan idempoten.

Jangan menyebut hasil tahap ini sebagai EWS operasional atau demo end-to-end satelit/AI yang telah tervalidasi.

## Referensi resmi yang diperiksa

- https://docs.cloud.google.com/run/docs/deploying
- https://docs.cloud.google.com/sdk/gcloud/reference/builds/submit
- https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts
- https://firebase.google.com/docs/hosting/cloud-run
- https://firebase.google.com/docs/auth/admin/verify-id-tokens
- https://firebase.google.com/docs/auth/admin/custom-claims
- https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule
