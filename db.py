"""
db.py — Koneksi & Operasi Database MySQL
=========================================
SmartPresensi v4.0
Gunakan PyMySQL sebagai driver (tidak perlu DLL tambahan di Windows)
"""

import pymysql
import pymysql.cursors
from datetime import datetime

# ──────────────────────────────────────────────────────────────
# KONFIGURASI KONEKSI
# Sesuaikan dengan setting XAMPP Anda
# ──────────────────────────────────────────────────────────────
DB_CONFIG = {
    'host':     'localhost',
    'port':     3306,
    'user':     'root',       # default XAMPP
    'password': '',           # default XAMPP kosong
    'db':       'smartpresensi',
    'charset':  'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor,
    'autocommit': False,
}


def get_conn():
    """Buka koneksi baru ke MySQL. Selalu tutup setelah dipakai."""
    return pymysql.connect(**DB_CONFIG)


# ══════════════════════════════════════════════════════════════
# AMBIL DEFINISI SHIFT DARI DATABASE
# ══════════════════════════════════════════════════════════════

def get_shift_ranges():
    """
    Ambil definisi shift dari tabel `shift` di database.
    Return: list of (jam_mulai_int, jam_selesai_int, nama_str, jam_standar_time)
            sesuai format SHIFT_RANGES di app.py.
    Jika tabel belum ada / error, return None (fallback ke konstanta).
    """
    from datetime import time as dtime
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nama_shift, jam_mulai, jam_selesai, jam_standar
                FROM shift
                WHERE aktif = 1
                ORDER BY urutan ASC
            """)
            rows = cur.fetchall()
        if not rows:
            return None
        result = []
        for r in rows:
            # jam_mulai dan jam_selesai dari MySQL bisa berupa timedelta atau time
            def to_time(val):
                if isinstance(val, dtime): return val
                try:
                    # timedelta (PyMySQL kadang kembalikan ini)
                    import datetime as dt_mod
                    if isinstance(val, dt_mod.timedelta):
                        total = int(val.total_seconds())
                        return dtime(total // 3600, (total % 3600) // 60)
                except Exception:
                    pass
                return dtime(0, 0)

            jm  = to_time(r['jam_mulai'])
            js  = to_time(r['jam_selesai'])
            std = to_time(r['jam_standar'])
            result.append((jm.hour, js.hour, r['nama_shift'], std))
        return result
    except Exception:
        return None  # fallback ke SHIFT_RANGES di app.py
    finally:
        if conn:
            conn.close()


# ══════════════════════════════════════════════════════════════
# SIMPAN HASIL ANALISIS
# ══════════════════════════════════════════════════════════════

def simpan_rekap(nama_file, bulan, tahun,
                 total_pegawai, total_terlambat,
                 total_kejadian, total_menit,
                 rata_menit, format_file,
                 rekap_list):
    """
    Simpan hasil analisis ke database.
    Jika bulan+tahun sudah ada -> UPDATE (overwrite).
    Return: {'ok': True, 'rekap_id': int} atau {'ok': False, 'error': str}
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM upload_rekap WHERE bulan=%s AND tahun=%s",
                (bulan, tahun)
            )
            existing = cur.fetchone()

            if existing:
                rekap_id = existing['id']
                cur.execute("""
                    UPDATE upload_rekap SET
                        nama_file=%s, total_pegawai=%s, total_terlambat=%s,
                        total_kejadian=%s, total_menit=%s, rata_menit=%s,
                        format_file=%s, dibuat_pada=%s
                    WHERE id=%s
                """, (nama_file, total_pegawai, total_terlambat,
                      total_kejadian, total_menit, rata_menit,
                      format_file, datetime.now(), rekap_id))
                cur.execute("DELETE FROM detail_rekap WHERE rekap_id=%s", (rekap_id,))
                is_update = True
            else:
                cur.execute("""
                    INSERT INTO upload_rekap
                        (nama_file, bulan, tahun, total_pegawai, total_terlambat,
                         total_kejadian, total_menit, rata_menit, format_file, dibuat_pada)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (nama_file, bulan, tahun, total_pegawai, total_terlambat,
                      total_kejadian, total_menit, rata_menit,
                      format_file, datetime.now()))
                rekap_id = cur.lastrowid
                is_update = False

            if rekap_list:
                rows = [
                    (rekap_id, r['nama'], r['tipe'], r['jumlah'], r['total_menit'])
                    for r in rekap_list
                ]
                cur.executemany("""
                    INSERT INTO detail_rekap (rekap_id, nama, tipe_shift, jumlah, total_menit)
                    VALUES (%s,%s,%s,%s,%s)
                """, rows)

        conn.commit()
        return {'ok': True, 'rekap_id': rekap_id, 'updated': is_update}

    except Exception as e:
        if conn:
            conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        if conn:
            conn.close()


# ══════════════════════════════════════════════════════════════
# AMBIL RIWAYAT
# ══════════════════════════════════════════════════════════════

NAMA_BULAN = [
    '', 'Januari','Februari','Maret','April','Mei','Juni',
    'Juli','Agustus','September','Oktober','November','Desember'
]


def get_riwayat():
    """Ambil semua riwayat upload diurutkan terbaru."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nama_file, bulan, tahun,
                       total_pegawai, total_terlambat,
                       total_kejadian, total_menit, rata_menit,
                       format_file, dibuat_pada
                FROM upload_rekap
                ORDER BY tahun DESC, bulan DESC
            """)
            rows = cur.fetchall()
        for r in rows:
            r['bulan_nama'] = NAMA_BULAN[r['bulan']]
            r['dibuat_pada_str'] = r['dibuat_pada'].strftime('%d %b %Y %H:%M') \
                if r['dibuat_pada'] else '-'
        return rows
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def get_detail_rekap(rekap_id):
    """Ambil detail pegawai untuk satu periode rekap."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT nama, tipe_shift, jumlah, total_menit
                FROM detail_rekap
                WHERE rekap_id=%s
                ORDER BY jumlah DESC, total_menit DESC
            """, (rekap_id,))
            return cur.fetchall()
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def get_rekap_by_id(rekap_id):
    """Ambil header rekap berdasarkan ID."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM upload_rekap WHERE id=%s", (rekap_id,))
            row = cur.fetchone()
            if row:
                row['bulan_nama'] = NAMA_BULAN[row['bulan']]
            return row
    except Exception:
        return None
    finally:
        if conn:
            conn.close()


def get_tren_bulanan(limit=12):
    """Ambil tren 12 bulan terakhir untuk grafik."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT bulan, tahun, total_terlambat,
                       total_kejadian, total_menit, rata_menit
                FROM upload_rekap
                ORDER BY tahun DESC, bulan DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        rows.reverse()
        for r in rows:
            r['label'] = f"{NAMA_BULAN[r['bulan']][:3]} {r['tahun']}"
        return rows
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def get_tren_pegawai(limit_bulan=6):
    """Ambil total keterlambatan per pegawai (6 bulan terakhir)."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM upload_rekap
                ORDER BY tahun DESC, bulan DESC
                LIMIT %s
            """, (limit_bulan,))
            ids = [r['id'] for r in cur.fetchall()]
            if not ids:
                return []
            fmt = ','.join(['%s'] * len(ids))
            cur.execute(f"""
                SELECT nama, SUM(jumlah) AS total_kejadian,
                       SUM(total_menit) AS total_menit
                FROM detail_rekap
                WHERE rekap_id IN ({fmt})
                GROUP BY nama
                ORDER BY total_kejadian DESC
                LIMIT 15
            """, ids)
            return cur.fetchall()
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def hapus_rekap(rekap_id):
    """Hapus satu rekap beserta detailnya."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM upload_rekap WHERE id=%s", (rekap_id,))
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def cek_koneksi():
    """Test koneksi database."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS ver")
            ver = cur.fetchone()
        conn.close()
        return {'ok': True, 'versi': ver['ver'] if ver else '-'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def get_tren_pegawai_dengan_shift(limit_bulan=6):
    """
    Ambil total keterlambatan per pegawai beserta tipe_shift dominan
    (6 bulan terakhir, untuk grafik dengan filter shift).
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM upload_rekap
                ORDER BY tahun DESC, bulan DESC
                LIMIT %s
            """, (limit_bulan,))
            ids = [r['id'] for r in cur.fetchall()]
            if not ids:
                return []
            fmt = ','.join(['%s'] * len(ids))
            cur.execute(f"""
                SELECT nama,
                       tipe_shift,
                       SUM(jumlah)      AS total_kejadian,
                       SUM(total_menit) AS total_menit
                FROM detail_rekap
                WHERE rekap_id IN ({fmt})
                GROUP BY nama, tipe_shift
                ORDER BY total_kejadian DESC
                LIMIT 60
            """, ids)
            rows = cur.fetchall()
        # Gabungkan baris yang sama nama tapi shift berbeda
        # Ambil shift dominan (kejadian terbanyak) per pegawai
        from collections import defaultdict
        pegawai = defaultdict(lambda: {'total_kejadian':0,'total_menit':0,'tipe_shift':'Non-Shift'})
        for r in rows:
            nama = r['nama']
            if r['total_kejadian'] > pegawai[nama]['total_kejadian']:
                pegawai[nama]['tipe_shift'] = r['tipe_shift']
            pegawai[nama]['total_kejadian'] += r['total_kejadian']
            pegawai[nama]['total_menit']    += r['total_menit']
        result = [{'nama': k, **v} for k, v in pegawai.items()]
        result.sort(key=lambda x: -x['total_kejadian'])
        return result[:15]
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


# ══════════════════════════════════════════════════════════════
# MANAJEMEN PEGAWAI (v5.0)
# ══════════════════════════════════════════════════════════════

def get_daftar_pegawai():
    """Ambil semua pegawai diurutkan nama."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nama, tipe_pegawai, unit, dibuat_pada
                FROM pegawai
                ORDER BY nama ASC
            """)
            return cur.fetchall() or []
    except Exception:
        return []
    finally:
        if conn: conn.close()


def simpan_pegawai(nama, tipe_pegawai='Normal', unit=''):
    """Tambah pegawai baru. Return {'ok':True,'id':int} atau {'ok':False,'error':str}."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pegawai (nama, tipe_pegawai, unit) VALUES (%s,%s,%s)",
                (nama.strip(), tipe_pegawai, unit.strip())
            )
            new_id = cur.lastrowid
        conn.commit()
        return {'ok': True, 'id': new_id}
    except Exception as e:
        if conn: conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        if conn: conn.close()


def update_pegawai(pid, nama, tipe_pegawai='Normal', unit=''):
    """Edit data pegawai."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pegawai SET nama=%s, tipe_pegawai=%s, unit=%s WHERE id=%s",
                (nama.strip(), tipe_pegawai, unit.strip(), pid)
            )
        conn.commit()
        return {'ok': True}
    except Exception as e:
        if conn: conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        if conn: conn.close()


def hapus_pegawai(pid):
    """Hapus pegawai berdasarkan id."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pegawai WHERE id=%s", (pid,))
        conn.commit()
        return {'ok': True}
    except Exception as e:
        if conn: conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        if conn: conn.close()