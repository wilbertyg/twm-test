# twm-test

Sistem penjadwalan tugas sederhana dengan kuota per user. Setiap tugas dijadwalkan pada jam tertentu (`HH:MM`) setiap hari, dijalankan oleh *executor* sesuai jenis aksinya, dan dibatasi oleh kuota milik user pemilik tugas.

## Fitur

- Tugas harian berbasis jam (`HH:MM`), dijamin hanya berjalan **sekali per hari**.
- **Async**: executor berupa coroutine, dan tugas yang jatuh tempo pada tick yang sama berjalan bersamaan tanpa bisa melewati kuota.
- Kuota eksekusi per user, di-reset otomatis setiap hari (kebijakan reset bisa diganti).
- **Konfigurasi lewat file JSON**: user, tugas, interval polling, level log, dan prefix aman untuk `delete` (lihat [Konfigurasi](#konfigurasi)).
- **Logging** di seluruh siklus hidup: perubahan user/tugas, eksekusi, kuota habis, error, dan request HTTP.
- REST API dengan FastAPI, lengkap dengan scheduler yang berjalan di background.
- Desain OOP berbasis abstraksi: aksi, kebijakan reset kuota, jam, dan penyimpanan bisa diganti lewat subclass tanpa mengubah kode inti (lihat [Arsitektur](#arsitektur)).
- Validasi di awal: user, aksi, atau jam yang tidak valid ditolak saat tugas ditambahkan.
- Kegagalan satu tugas tidak menghentikan scheduler dan tidak mengurangi kuota.
- Pengaman untuk aksi `delete`: hanya boleh di bawah prefix yang diizinkan (default `/tmp/`).

## Persyaratan

- [uv](https://docs.astral.sh/uv/) untuk manajemen interpreter dan dependency
- Python 3.12 (dipin di `.python-version`; uv mengunduhnya otomatis jika belum ada)
- Modul inti `main.py` hanya memakai standard library; `api.py` memakai `fastapi[standard]`; `pytest` hanya dependency development

## Setup

```bash
uv sync
```

Perintah ini membuat `.venv`, memasang Python 3.12 bila perlu, dan menginstal semua dependency (`fastapi[standard]`, `pytest`) sesuai `uv.lock`.

## Menjalankan

```bash
uv run main.py
```

Scheduler akan berjalan terus, memeriksa tugas setiap `poll_interval` detik (default 30). Tekan `Ctrl+C` untuk berhenti.

Contoh output dengan `alice` berkuota 1 dan ketiga tugas dijadwalkan di menit yang sama. Tugas ketiga milik `alice` ditolak karena kuotanya sudah dipakai:

```
2026-11-02 16:26:55,968 INFO __main__: User alice added (quota 1)
2026-11-02 16:26:55,968 INFO __main__: User bob added (quota 5)
2026-11-02 16:26:55,972 INFO __main__: Task 1 added: sync /data/x for alice at 16:26
2026-11-02 16:26:55,973 INFO __main__: Task 2 added: backup /srv/y for bob at 16:26
2026-11-02 16:26:55,973 INFO __main__: Task 3 added: delete /tmp/z for alice at 16:26
2026-11-02 16:26:55,974 INFO __main__: Scheduler started with 3 task(s), polling every 2s
2026-11-02 16:26:55,974 INFO __main__: Syncing /data/x for alice
2026-11-02 16:26:55,974 INFO __main__: Task 1 (sync on /data/x) for alice done
2026-11-02 16:26:55,974 INFO __main__: Backing up /srv/y for bob
2026-11-02 16:26:55,974 INFO __main__: Task 2 (backup on /srv/y) for bob done
2026-11-02 16:26:55,974 WARNING __main__: alice has reached their quota (1)
```

Nama logger adalah `__main__` saat `main.py` dijalankan langsung, dan `main` / `api` saat dijalankan lewat FastAPI.

> Executor bawaan saat ini hanya **mensimulasikan** aksi (menulis ke log). Implementasi nyata ditaruh di method `async execute()` masing-masing executor.

## Konfigurasi

`main.py` dan `api.py` membaca `config.json` di direktori kerja (atau file yang ditunjuk env `SCHEDULER_CONFIG`). Jika `config.json` tidak ada, nilai default dipakai. Key yang tidak dikenal ditolak; key yang tidak diisi memakai default.

```json
{
  "log_level": "INFO",
  "poll_interval": 30,
  "delete_allowed_prefixes": ["/tmp/"],
  "users": [{"name": "alice", "quota": 3}],
  "tasks": [{"user": "alice", "at": "12:00", "action": "sync", "target": "/data/x"}]
}
```

| Key | Default | Keterangan |
|---|---|---|
| `log_level` | `"INFO"` | Level logging: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (tidak case-sensitive) |
| `poll_interval` | `30.0` | Detik antar pengecekan tugas (sebaiknya < 60); `0`/`null` mematikan loop background di API |
| `delete_allowed_prefixes` | `["/tmp/"]` | Prefix target yang boleh dihapus oleh aksi `delete` |
| `users` | `alice` (3), `bob` (5) | User awal beserta kuotanya: `{"name", "quota"}` |
| `tasks` | 3 tugas jam `12:00` | Tugas awal: `{"user", "at": "HH:MM", "action", "target"}` |

```bash
SCHEDULER_CONFIG=prod.json uv run main.py
```

Aturan:

- Urutan pencarian file: argumen `load_config(path)` → env `SCHEDULER_CONFIG` → `./config.json`. Hanya `./config.json` yang boleh tidak ada; file yang ditunjuk secara eksplisit tetapi tidak ada menghasilkan `FileNotFoundError`.
- Top level harus object JSON. Key yang tidak dikenal (misalnya salah ketik `"poll"`) menghasilkan `ValueError`.
- Key yang diisi **menggantikan seluruh** nilai default-nya (tidak di-merge). Contoh: mengisi `users` tanpa `alice` dan `bob` berarti `tasks` juga harus diisi, karena tugas default merujuk ke kedua user itu.
- Isi konfigurasi (user ganda, kuota negatif, aksi tidak dikenal, format jam salah, tugas untuk user yang tidak ada) divalidasi di `build_scheduler()`, sehingga program gagal saat start, bukan di tengah jalan.
- Aksi yang bisa dipakai dari file: `sync`, `backup`, `delete`. Aksi lain perlu didaftarkan lewat kode (lihat [Ekstensi](#ekstensi)).

## Logging

Log ditulis ke stderr dengan format `waktu LEVEL modul: pesan`. Yang dicatat: user/tugas ditambah/dihapus, reset kuota, tiap tugas selesai/gagal (dengan traceback), kuota habis, start/stop scheduler, dan setiap request HTTP (`POST /run -> 200 (7.4 ms)`). Set `"log_level": "DEBUG"` untuk melihat jumlah tugas jatuh tempo di setiap tick.

## Penggunaan

```python
import asyncio

from main import ExecutorRegistry, Scheduler, UserManagement, build_scheduler, load_config

scheduler = build_scheduler(load_config())   # dari config.json, atau susun manual:

users = UserManagement()
users.add_user("alice", quota=3)
users.add_user("bob", quota=5)

scheduler = Scheduler(users=users, executors=ExecutorRegistry.with_defaults())
scheduler.add_task("alice", "12:00", "sync", "/data/x")
scheduler.add_task("bob", "12:00", "backup", "/srv/y")
scheduler.add_task("alice", "12:00", "delete", "/tmp/z")

asyncio.run(scheduler.run_forever())   # loop terus-menerus
# atau
asyncio.run(scheduler.run_pending())   # jalankan sekali untuk tugas yang jatuh tempo sekarang
```

`run_pending()` dan `run_forever()` adalah coroutine. Tugas yang jatuh tempo pada tick yang sama dijalankan **bersamaan** (`asyncio.gather`). Kuota dipesan sebelum executor berjalan dan dikembalikan jika gagal, sehingga tugas paralel tidak bisa melewati kuota.

## HTTP API (FastAPI)

`api.py` membungkus scheduler dalam REST API. Saat aplikasi start, scheduler juga berjalan di background setiap `poll_interval` detik, jadi tidak perlu menjalankan `main.py` terpisah. Konfigurasi dibaca dengan aturan yang sama seperti `main.py`, langsung saat modul `api` di-import.

```bash
uv run fastapi dev api.py                             # development (auto-reload) di http://127.0.0.1:8000
uv run fastapi run api.py                             # production (listen di 0.0.0.0:8000)
SCHEDULER_CONFIG=prod.json uv run fastapi run api.py  # dengan file konfigurasi lain
```

Dokumentasi interaktif (Swagger UI) tersedia di `http://127.0.0.1:8000/docs`.

### Endpoint

| Method | Path | Keterangan | Status sukses |
|---|---|---|---|
| `GET` | `/health` | Health check | 200 |
| `GET` | `/actions` | Daftar aksi yang terdaftar | 200 |
| `GET` | `/users` | Daftar user beserta kuota | 200 |
| `POST` | `/users` | Tambah user `{"name", "quota"}` | 201 |
| `GET` | `/users/{name}` | Detail user | 200 |
| `DELETE` | `/users/{name}` | Hapus user (ditolak jika masih punya tugas) | 204 |
| `POST` | `/users/reset-quotas` | Reset kuota semua user | 200 |
| `GET` | `/tasks?user=` | Daftar tugas, opsional filter per user | 200 |
| `POST` | `/tasks` | Tambah tugas `{"user", "at", "action", "target"}` | 201 |
| `GET` | `/tasks/{id}` | Detail tugas | 200 |
| `DELETE` | `/tasks/{id}` | Hapus tugas | 204 |
| `POST` | `/run` | Jalankan tugas yang jatuh tempo sekarang, atau pada `{"now": "..."}` | 200 |

### Kode error

| Status | Kapan |
|---|---|
| 404 | User atau tugas tidak ditemukan |
| 409 | User sudah ada, atau user yang dihapus masih punya tugas |
| 422 | Body tidak valid, aksi tidak dikenal, atau format jam salah |

Format body error: `{"detail": "<pesan>"}`.

### Contoh

```bash
curl -X POST localhost:8000/users -H "content-type: application/json" \
     -d '{"name": "carol", "quota": 2}'
# {"name":"carol","quota":2,"executed":0,"remaining":2}

curl -X POST localhost:8000/tasks -H "content-type: application/json" \
     -d '{"user": "carol", "at": "09:15", "action": "backup", "target": "/home/carol"}'
# {"id":4,"user":"carol","at":"09:15","action":"backup","target":"/home/carol","last_run":null}

# Simulasikan jam 12:00 untuk menjalankan tugas default
curl -X POST localhost:8000/run -H "content-type: application/json" \
     -d '{"now": "2026-11-02T12:00:00"}'
```

### Pemakaian dari kode

`create_app()` menerima scheduler sendiri, misalnya untuk testing atau konfigurasi custom:

```python
from api import create_app
from main import ExecutorRegistry, Scheduler, UserManagement

scheduler = Scheduler(users=UserManagement(), executors=ExecutorRegistry.with_defaults())
app = create_app(scheduler, poll_interval=10)    # poll_interval=None atau 0 mematikan loop background
```

Semua endpoint `async`. Akses ke scheduler diserialisasi dengan `asyncio.Lock` (`SchedulerService`), sehingga request API dan loop background tidak saling bentrok. Akibatnya, selama satu tick berjalan, request lain menunggu sampai tick selesai.

## Arsitektur

```
api.py   FastAPI app ── SchedulerService [asyncio.Lock] ── loop background
            │
main.py  load_config() ──▶ build_scheduler(config)
            │
         Scheduler (orkestrasi, async)
          ├── UserManagement ──────── UserRepository   ◁── InMemoryUserRepository
          │      └── User
          ├── TaskManagement ──────── TaskRepository   ◁── InMemoryTaskRepository
          │      └── Task
          ├── ExecutorRegistry ────── TaskExecutor     ◁── SyncExecutor / BackupExecutor / DeleteExecutor
          ├── QuotaResetPolicy ◁── DailyReset (default) / NeverReset
          └── Clock            ◁── SystemClock (default) / FixedClock

◁── = "turunan dari" (implementasi konkret sebuah abstract base class)
```

### Prinsip OOP yang diterapkan

| Prinsip | Penerapan |
|---|---|
| **Abstraction** | `Clock`, `UserRepository`, `TaskRepository`, `TaskExecutor`, dan `QuotaResetPolicy` adalah abstract base class (ABC). Class yang memakainya hanya tahu kontraknya, bukan implementasinya. |
| **Encapsulation** | State internal bersifat privat dan hanya berubah lewat method yang menjaga aturan: `User.executed` hanya lewat `consume()`/`refund()`/`reset()`, `Task.last_run` hanya lewat `mark_handled()`, penyimpanan repository dan tabel executor tidak bisa diakses langsung. Komponen `Scheduler` (`users`, `executors`, `tasks`) adalah property read-only. |
| **Inheritance** | Implementasi konkret mewarisi ABC-nya, misalnya `DeleteExecutor(TaskExecutor)` dan `DailyReset(QuotaResetPolicy)`. Error membentuk hierarki di bawah `SchedulerError`. |
| **Polymorphism** | `Scheduler` memanggil `await executor.execute(task)`, `policy.should_reset(now)`, dan `clock.now()` tanpa peduli class konkretnya. `UserManagement` dan `TaskManagement` mendukung `for ... in`, `len()`, dan `in`. |

### SOLID

| Prinsip | Penerapan |
|---|---|
| **S**ingle Responsibility | Penyimpanan (repository), aturan bisnis (`UserManagement`, `TaskManagement`), eksekusi (executor), kebijakan kuota (policy), orkestrasi (`Scheduler`), dan konfigurasi (`load_config`, `build_scheduler`) dipisah. `api.py` hanya menerjemahkan HTTP ke domain. |
| **O**pen/Closed | Aksi, kebijakan reset, jam, dan penyimpanan baru ditambahkan dengan membuat subclass, tanpa mengubah `Scheduler`. |
| **L**iskov Substitution | Setiap implementasi bisa menggantikan base class-nya; test memakai `FixedClock`, repository spy, dan executor palsu di tempat yang sama dengan implementasi asli. |
| **I**nterface Segregation | Setiap ABC kecil dan fokus: `Clock` 1 method, `QuotaResetPolicy` 1 method, `TaskExecutor` 1 method. |
| **D**ependency Inversion | `Scheduler`, `UserManagement`, dan `TaskManagement` menerima dependensinya lewat constructor (dependency injection) dan bergantung pada abstraksi. Default-nya implementasi in-memory/sistem. |

### Alur `await run_pending(now)`

1. `now` default ke `clock.now()`.
2. Jika `reset_policy.should_reset(now)` bernilai `True` (default: hari pertama ganti tanggal), semua kuota user di-reset.
3. Kumpulkan semua tugas yang `is_due(now)` (menit cocok dan belum ditangani hari ini), lalu jalankan **bersamaan** dengan `asyncio.gather`. Untuk setiap tugas:
   1. Tandai tugas sebagai sudah ditangani hari ini (`mark_handled`), sehingga tidak dijalankan ulang di hari yang sama, apa pun hasilnya.
   2. Jika kuota user sudah habis, tulis peringatan dan lewati.
   3. Pesan satu kuota (`record_execution`) **sebelum** `await`, supaya tugas milik user yang sama yang berjalan bersamaan tidak bisa melewati kuota.
   4. `await` executor sesuai `task.action`. Jika gagal (termasuk dibatalkan), kuota dikembalikan (`refund_execution`) dan error dicatat ke log.
4. Kembalikan daftar tugas yang berhasil, sesuai urutan tugas.

## Referensi API

### Model

#### `User` (dataclass)

| Anggota | Keterangan |
|---|---|
| `name: str` | Nama user (tidak boleh kosong) |
| `quota: int` | Batas jumlah eksekusi (harus `>= 0`) |
| `executed` | Jumlah eksekusi terpakai (property read-only) |
| `remaining` | Sisa kuota (property read-only) |
| `has_quota()` | `True` jika masih ada sisa kuota |
| `consume()` | Pakai satu kuota; `QuotaReachedError` jika habis |
| `refund()` | Kembalikan satu kuota (tidak pernah di bawah `0`) |
| `reset()` | Set `executed` kembali ke `0` |

#### `Task` (dataclass)

| Anggota | Keterangan |
|---|---|
| `user`, `time`, `action`, `target` | Pemilik, jam harian (`datetime.time`), nama aksi, dan target |
| `id: int` | ID unik, diberikan oleh `TaskManagement` (tidak dipakai ulang) |
| `last_run` | Tanggal terakhir ditangani (property read-only) |
| `is_due(now)` | `True` jika jam dan menit cocok dan belum ditangani hari ini |
| `mark_handled(day)` | Tandai sudah ditangani pada tanggal `day` |
| `Task.parse_time(at)` | Static method: parse `"HH:MM"` secara ketat; `ValueError` jika format salah |

### Repository

`UserRepository` dan `TaskRepository` adalah ABC untuk penyimpanan. Implementasi bawaannya `InMemoryUserRepository` dan `InMemoryTaskRepository`.

| `UserRepository` | `TaskRepository` | Keterangan |
|---|---|---|
| `get(name)` | `get(task_id)` | Ambil objek, atau `None` |
| `save(user)` | `save(task)` | Insert atau update |
| `delete(name)` | `delete(task_id)` | Hapus (no-op jika tidak ada) |
| `all()` | `all()` | Salinan daftar semua objek |
| | `next_id()` | Reservasi ID baru |

### Service

#### `UserManagement(repository=None)`

| Anggota | Keterangan |
|---|---|
| `add_user(name, quota)` | Tambah user; `UserAlreadyExistsError` jika nama sudah ada |
| `get_user(name)` | Ambil user; `UserNotFoundError` jika tidak ada |
| `remove_user(name)` | Hapus user; `UserNotFoundError` jika tidak ada |
| `record_execution(user)` | Pakai satu kuota lalu simpan ke repository |
| `refund_execution(user)` | Kembalikan satu kuota lalu simpan ke repository |
| `reset_quotas()` | Reset kuota semua user |
| `iter()`, `len()`, `in` | Iterasi user, jumlah user, cek nama user |

#### `TaskManagement(repository=None)`

| Anggota | Keterangan |
|---|---|
| `create(user, at, action, target)` | Buat tugas dengan ID baru (`at` berupa `datetime.time`) |
| `get(task_id)` / `remove(task_id)` | Ambil / hapus; `TaskNotFoundError` jika tidak ada |
| `for_user(name)` | Daftar tugas milik user |
| `mark_handled(task, day)` | Tandai tugas sudah ditangani lalu simpan |
| `iter()`, `len()` | Iterasi tugas, jumlah tugas |

### Executor

#### `TaskExecutor` (ABC)

| Anggota | Keterangan |
|---|---|
| `action: ClassVar[str]` | Nama aksi yang ditangani (wajib, tidak boleh kosong) |
| `async execute(task)` | Jalankan tugas; lempar exception untuk menandakan gagal |

| Executor bawaan | `action` | Keterangan |
|---|---|---|
| `SyncExecutor` | `sync` | Simulasi sinkronisasi |
| `BackupExecutor` | `backup` | Simulasi backup |
| `DeleteExecutor(allowed_prefixes=("/tmp/",))` | `delete` | Simulasi penghapusan; menolak target di luar `allowed_prefixes` dengan `PermissionError`. Diisi dari `delete_allowed_prefixes` di konfigurasi |

#### `ExecutorRegistry(executors=())`

| Anggota | Keterangan |
|---|---|
| `with_defaults()` | Classmethod: registry berisi ketiga executor bawaan |
| `register(executor)` | Daftarkan executor (menimpa `action` yang sama); `ValueError` jika `action` kosong |
| `get(action)` | Ambil executor; `UnknownActionError` jika belum terdaftar |
| `actions` | Tuple nama aksi terurut (property) |
| `in` | Cek apakah aksi terdaftar |

### Policy & Clock

| Class | Keterangan |
|---|---|
| `QuotaResetPolicy` (ABC) | `should_reset(now) -> bool`, dipanggil setiap tick scheduler |
| `DailyReset` | Default. Reset di tick pertama setiap hari baru (bukan di tick paling awal; jam mundur diabaikan) |
| `NeverReset` | Kuota tidak pernah di-reset otomatis |
| `Clock` (ABC) | `now() -> datetime` |
| `SystemClock` | Default. Jam lokal mesin |
| `FixedClock(now)` | Jam manual dengan `set(now)` dan `advance(delta)`; untuk test dan simulasi |

### `Scheduler`

```python
Scheduler(users, executors, *, tasks=None, reset_policy=None, clock=None)
```

| Anggota | Keterangan |
|---|---|
| `users`, `executors`, `tasks` | Komponen scheduler (property read-only) |
| `add_task(user, at, action, target)` | Tambah tugas harian; `at` berformat `"HH:MM"`. Memvalidasi user, aksi, dan jam |
| `get_task(task_id)` / `remove_task(task_id)` | Ambil / hapus tugas; `TaskNotFoundError` jika tidak ada |
| `remove_user(name)` | Hapus user; `UserHasTasksError` jika user masih punya tugas |
| `await run_pending(now=None)` | Jalankan tugas yang jatuh tempo secara bersamaan; `now` default ke `clock.now()` |
| `await run_forever(interval=30.0)` | Loop `run_pending()` setiap `interval` detik sampai dibatalkan / `Ctrl+C` |

#### Fungsi modul

| Fungsi | Keterangan |
|---|---|
| `load_config(path=None)` | Default + isi file JSON (`path`, `$SCHEDULER_CONFIG`, atau `./config.json`) |
| `build_scheduler(config=None)` | Buat `Scheduler` dari dict konfigurasi (default jika `None`) |
| `setup_logging(level="INFO")` | Konfigurasi logging root |
| `main()` | Entry point `uv run main.py`: `load_config` → `setup_logging` → `build_scheduler` → `run_forever` |

Konstanta: `DEFAULT_CONFIG` (nilai default), `CONFIG_ENV` (`"SCHEDULER_CONFIG"`), `DEFAULT_CONFIG_FILE` (`"config.json"`).

### Exception

Semua turunan dari `SchedulerError`:

| Exception | Kapan dilempar |
|---|---|
| `UserNotFoundError` | User tidak terdaftar |
| `UserAlreadyExistsError` | Nama user sudah dipakai (juga turunan `ValueError`) |
| `UserHasTasksError` | Menghapus user yang masih punya tugas |
| `TaskNotFoundError` | ID tugas tidak ditemukan |
| `QuotaReachedError` | `User.consume()` dipanggil saat kuota habis |
| `UnknownActionError` | Aksi tidak punya executor terdaftar |

`load_config()` melempar `ValueError` (key tidak dikenal, atau top level bukan object), `json.JSONDecodeError` (JSON rusak), atau `FileNotFoundError` (file eksplisit tidak ada).

## Ekstensi

Semua titik ekstensi berupa subclass dari ABC, lalu di-inject ke `Scheduler` atau registry. Tidak ada kode inti yang perlu diubah.

### Aksi baru

```python
import asyncio

from main import ExecutorRegistry, Scheduler, Task, TaskExecutor, UserManagement


class NotifyExecutor(TaskExecutor):
    action = "notify"

    async def execute(self, task: Task) -> None:
        await asyncio.sleep(0)  # mis. panggil HTTP client async di sini
        print(f"Notifying {task.user}: {task.target}")


users = UserManagement()
users.add_user("bob", quota=5)

registry = ExecutorRegistry.with_defaults()
registry.register(NotifyExecutor())

scheduler = Scheduler(users, registry)
scheduler.add_task("bob", "08:30", "notify", "Backup selesai")
```

Kode **blocking** (I/O sinkron, `subprocess.run`, `shutil.copytree`, dan sejenisnya) jangan dipanggil langsung di dalam `execute`, karena akan menahan event loop dan semua tugas lain. Jalankan di thread:

```python
import shutil


class CopyExecutor(TaskExecutor):
    action = "copy"

    async def execute(self, task: Task) -> None:
        await asyncio.to_thread(shutil.copytree, task.target, "/backup" + task.target)
```

Executor yang butuh konfigurasi menerimanya lewat constructor, seperti `DeleteExecutor(allowed_prefixes=("/tmp/", "/var/cache/"))`.

### Kebijakan reset kuota baru

```python
from main import QuotaResetPolicy


class WeeklyReset(QuotaResetPolicy):
    def __init__(self) -> None:
        self._last_week = None

    def should_reset(self, now) -> bool:
        week = now.isocalendar()[:2]
        changed = self._last_week is not None and week != self._last_week
        self._last_week = week
        return changed


scheduler = Scheduler(users, registry, reset_policy=WeeklyReset())
```

### Penyimpanan persisten

Implementasikan `UserRepository` / `TaskRepository` (misalnya dengan SQLite), lalu inject:

```python
from main import Scheduler, TaskManagement, UserManagement

scheduler = Scheduler(
    UserManagement(SqliteUserRepository("app.db")),
    registry,
    tasks=TaskManagement(SqliteTaskRepository("app.db")),
)
```

`UserManagement` dan `TaskManagement` selalu memanggil `save()` setelah mengubah objek, sehingga repository persisten tetap sinkron.

## Testing

Unit test ada di `tests/` dan memakai pytest:

```bash
uv run pytest            # semua test
uv run pytest -v         # tampilkan nama setiap test
uv run pytest tests/test_scheduler.py -k quota   # filter berdasarkan file/nama
```

| File | Cakupan |
|---|---|
| `tests/conftest.py` | Fixture bersama (`users`, `registry`, `scheduler`, `noon`) dan executor async bantu `RecordingExecutor` / `FailingExecutor` |
| `tests/test_models.py` | `User` dan `Task`: aturan kuota, `is_due`, `parse_time`, dan enkapsulasi (atribut read-only) |
| `tests/test_repositories.py` | Repository in-memory dan ABC-nya |
| `tests/test_user_management.py` | Operasi user, protokol container, dan dependency injection repository |
| `tests/test_executors.py` | Executor bawaan, pengaman `DeleteExecutor`, dan `ExecutorRegistry` |
| `tests/test_policies.py` | `DailyReset`, `NeverReset`, `SystemClock`, dan `FixedClock` |
| `tests/test_scheduler.py` | Manajemen tugas, `add_task`, `remove_user`, `run_pending` (sekali per hari, kuota, kegagalan, clock, eksekusi paralel), kebijakan reset, dan `run_forever` |
| `tests/test_config.py` | `load_config` (default, file, env var, validasi) dan `build_scheduler` |
| `tests/test_api.py` | Semua endpoint FastAPI (via `TestClient`), mapping kode error, dan start/stop loop background |

Coroutine dijalankan dengan `asyncio.run`, dan waktu disimulasikan dengan `FixedClock` (atau parameter `now`), jadi tidak perlu menunggu jam sebenarnya:

```python
def test_defaults_to_injected_clock(users, registry, recorder, noon):
    clock = FixedClock(noon)
    scheduler = Scheduler(users, registry, clock=clock)
    task = scheduler.add_task("alice", "12:00", "record", "/a")
    assert asyncio.run(scheduler.run_pending()) == [task]
    clock.advance(datetime.timedelta(days=1))
    assert asyncio.run(scheduler.run_pending()) == [task]
```

Menambah dependency development lain:

```bash
uv add --dev <paket>
```

## Keterbatasan

- Hanya repository in-memory yang tersedia; state (kuota, `last_run`) hilang saat program restart sampai ada implementasi repository persisten.
- Jika scheduler tidak berjalan tepat pada menit jadwal, tugas di hari itu terlewat (tidak ada *catch-up*). Karena itu `poll_interval` harus di bawah 60 detik.
- Di API, tick yang lama menahan request lain karena akses scheduler dikunci selama tick berjalan.
- Waktu memakai jam lokal mesin, tanpa dukungan timezone.
- Data awal dibaca dari `config.json`; perubahan lewat API tidak ditulis balik ke file dan hilang setelah restart.
- Executor harus benar-benar async; kode blocking di dalam `execute()` menahan seluruh event loop (bungkus dengan `asyncio.to_thread`).
- API belum punya autentikasi; jangan expose ke jaringan publik.
- Jalankan API dengan satu worker saja: setiap worker punya state scheduler sendiri.
