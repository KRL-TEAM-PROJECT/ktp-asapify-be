# Hasil pipeline → API operator

Kontrak frontend adalah `ktp-asapify-fe/src/types.ts` dan `src/contract/spec.ts`.
API ini membaca hasil yang sudah dihitung, bukan menghitung deteksi. Kode eksperimen `pipeline/`
belum menghasilkan kontrak ini. Tidak ada data observasi sintetis yang dipasang otomatis.

## Bundle yang diterima publisher

`tools/publish_snapshot.py` menerima satu berkas JSON berisi:

| Key | Bentuk |
|---|---|
| `index` | `PixelIndex`: version, origin `[94.9,6.2]`, step 0.02, rows/cols/province sejajar |
| `grid` | `GridCompact`: pixels_version, slot UTC, n_sat, status, utility base64, clusters |
| `meta` | archive_first YYYY-MM-DD, himawari_slot/gk2a_slot UTC atau null, replay boolean eksplisit |
| `night` | `NightCompact`: night, pixels_version, 54 slots/status/n_sat, clusters |
| `clusters` | Daftar `ClusterDetail`, ditambah `awas_nights` (daftar night_id) |

`grid.pixel_n_sat` adalah tambahan daftar 0/1/2 per piksel yang diperlukan untuk GeoJSON.
Format compact FE tetap bisa berjalan tanpanya. Jumlah satelit seluruh slot tidak boleh diklaim
sebagai jumlah satelit cerah untuk setiap piksel.

Semua waktu berupa string `YYYY-MM-DDTHH:MM:SSZ`, urutan UTC. Semua angka dari sumber terukur.
Status S/N/W/A; N selalu utility null (byte 255); nilai lain 0..250. PixelIndex.version harus cocok.
Slot malam mengikuti FE: 13.00–21.50 UTC, 54 slot. Slot belum dievaluasi = null.
`clusters` hanya berisi kelompok dari rule engine, dengan attributes, series, neighbours,
verification sesuai tipe FE. `decision` harus null/absent dari pipeline; keputusan hanya ditulis API.
Untuk replay, masukkan hasil backtest dan `meta.replay=true`; angka backtest bukan metrik validasi otomatis.

```bash
python tools/publish_snapshot.py /path/hasil_pipeline.json \
  --project "$ASAPIFY_PROJECT_ID" --bucket "$ASAPIFY_PROJECT_ID-asapify-data"
# Setelah review validasi, tambahkan --apply untuk menyimpan.
```

Jalankan dengan ADC yang berwenang menulis GCS dan Firestore. Akun runtime API sengaja hanya dapat
membaca bucket. Publisher ini adalah batas integrasi sementara; transformasi Parquet hasil ingest
menjadi bundle JSON harus ditambahkan oleh tim pipeline.

## Penyimpanan

- GCS `api/pixels/<sha256>.json`, `api/grid/<sha256>.json`, `api/nights/<sha256>.json`: object immutable
  berdasarkan isi. Raw/Parquet/crops boleh tetap pada prefix rancangan tim; API tidak membacanya langsung.
- Firestore `api_snapshots/<slot>`: timestamp, n_sat, archive_first, replay, himawari_slot, gk2a_slot,
  pixels_object, grid_object. Query satu field timestamp, tidak perlu composite index.
- `api_nights/<night>`: night, month, has_data, awas_clusters, object, as_of.
- `clusters/<id>`: ClusterDetail terkini + awas_nights + observation_at.
- `clusters/<id>/history/<slot>`: timestamp dan cluster lengkap untuk pembacaan `as_of`.
- `alerts/<id>`: keputusan publish dan status delivery; `audit_log/<auto-id>`: siapa memutuskan dan kapan.
- `notification_recipients`, `notification_inbox`, `notification_jobs`, `notification_devices`,
  `notification_telegram_chats`: langganan, inbox, job kirim, token push, dan tautan DM Telegram.
  Ditulis hanya oleh API/worker; lihat [NOTIFICATIONS.md](NOTIFICATIONS.md). Grup Telegram per provinsi tidak dipakai.

Publisher tidak menimpa keputusan operator dan tidak mengganti current cluster dengan snapshot lebih lama.
Riwayat harus mencakup perubahan yang ingin dilihat saat replay. Snapshot keputusan di antara dua slot,
closed state historis, version rollover peat mask, serta URL citra yang expired perlu penyempurnaan
sebelum replay dipakai sebagai audit lengkap.

Metadata publik tanpa token tidak berisi email operator atau detail kelompok. Semua endpoint operator
memerlukan token Firebase. Jangan membuka bucket data ke publik; layer provinsi/gambut yang memang
publik boleh dikemas sebagai aset FE statis setelah memeriksa lisensi sumber.
