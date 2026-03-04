"""
SmartPresensi — Enterprise Attendance Analytics
================================================
Versi  : 5.0 — Full Feature Edition
Baru v5:
  - Manajemen Pegawai: Normal vs Shift, aturan jam berbeda
  - Deteksi Pulang Cepat: standar berbeda Senin-Kamis vs Jumat
  - Status per kejadian: Terlambat/Pulang Cepat/Keduanya
  - Filter per nama pegawai
  - Halaman Pegawai dengan CRUD lengkap
"""

from flask import Flask, render_template_string, request, jsonify
import pandas as pd
import io, re, csv
from datetime import datetime, time
import db

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

# ── Definisi Shift (Time Range Classification) ──────────────
# Format: (jam_mulai_int, jam_selesai_int, nama_str, jam_standar_time)
# Malam: 19:00-04:59 (lintas tengah malam → jam_selesai < jam_mulai)
# Nilai ini dipakai sebagai FALLBACK jika tabel shift di DB belum ada.
SHIFT_RANGES_DEFAULT = [
    (5,   9,  "Shift Pagi",   time(7,  0)),   # 05:00–09:59
    (10,  14, "Shift Siang",  time(10, 0)),   # 10:00–14:59
    (15,  18, "Shift Sore",   time(15, 0)),   # 15:00–18:59
    (19,  4,  "Shift Malam",  time(19, 0)),   # 19:00–04:59 (lintas tengah malam)
]
JAM_STANDAR_NON_SHIFT = time(7, 30)
JAM_MASUK_NONSHIFT    = time(7, 30)   # batas terlambat masuk Non-Shift
JAM_PULANG_NORMAL     = time(15, 30)  # batas pulang cepat Senin-Kamis
JAM_PULANG_JUMAT      = time(14, 30)  # batas pulang cepat Jumat

def _load_shift_ranges():
    """Coba ambil dari DB, fallback ke konstanta jika gagal."""
    try:
        db_ranges = db.get_shift_ranges()
        if db_ranges:
            return db_ranges
    except Exception:
        pass
    return SHIFT_RANGES_DEFAULT

# Dimuat saat startup — dapat di-reload dengan restart Flask
SHIFT_RANGES = _load_shift_ranges()
MAX_TERLAMBAT_MENIT   = 600

DATE_PAT      = re.compile(r'^(\d{1,2})\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$')
TIME_CELL_PAT = re.compile(r'^(\d{1,2})[:\.](\d{2})\s*-\s*(\d{1,2})[:\.](\d{2})$')
EMPLOYEE_MARKERS = {'ASN', 'NON ASN'}
NAMA_BULAN = ['','Januari','Februari','Maret','April','Mei','Juni',
              'Juli','Agustus','September','Oktober','November','Desember']



# ── Cache pegawai shift ──────────────────────────────────────
_PEGAWAI_SHIFT_CACHE = None

def get_pegawai_shift_set():
    """Return set nama (lowercase) pegawai berstatus Shift."""
    global _PEGAWAI_SHIFT_CACHE
    if _PEGAWAI_SHIFT_CACHE is not None:
        return _PEGAWAI_SHIFT_CACHE
    try:
        rows = db.get_daftar_pegawai()
        _PEGAWAI_SHIFT_CACHE = {
            r['nama'].strip().lower()
            for r in rows if r.get('tipe_pegawai') == 'Shift'
        }
    except Exception:
        _PEGAWAI_SHIFT_CACHE = set()
    return _PEGAWAI_SHIFT_CACHE

def invalidate_pegawai_cache():
    global _PEGAWAI_SHIFT_CACHE
    _PEGAWAI_SHIFT_CACHE = None

def is_pegawai_shift(nama):
    return nama.strip().lower() in get_pegawai_shift_set()

def jam_pulang_standar(hari):
    """Standar jam pulang berdasarkan hari (Fri=Jumat)."""
    return JAM_PULANG_JUMAT if hari == 'Fri' else JAM_PULANG_NORMAL

def hitung_pulang_cepat(jam_pulang, hari):
    """Return menit pulang cepat. 0 jika tepat/lebih lambat."""
    if jam_pulang is None: return 0
    std = jam_pulang_standar(hari)
    diff = (std.hour*60+std.minute) - (jam_pulang.hour*60+jam_pulang.minute)
    return max(0, diff)

def parse_sel_waktu(sel):
    if not sel or not isinstance(sel, str): return None, None
    m = TIME_CELL_PAT.match(sel.strip())
    if not m: return None, None
    try:
        h1,m1,h2,m2 = int(m.group(1)),int(m.group(2)),int(m.group(3)),int(m.group(4))
        if not (0<=h1<=23 and 0<=m1<=59 and 0<=h2<=23 and 0<=m2<=59): return None,None
        return time(h1,m1), time(h2,m2)
    except Exception: return None, None

def deteksi_shift(jam_masuk, shift_ranges=None):
    """
    Deteksi shift berdasarkan jam masuk (TIME/datetime.time).
    Mendukung rentang lintas tengah malam (misal Malam 19:00-04:59).
    Membandingkan secara numerik (total menit dari 00:00).
    shift_ranges: list of (jam_mulai_int, jam_selesai_int, nama_str, jam_standar_time)
                  jika None, gunakan SHIFT_RANGES global.
    """
    ranges = shift_ranges or SHIFT_RANGES
    # Konversi jam masuk ke total menit untuk perbandingan numerik
    menit_masuk = jam_masuk.hour * 60 + jam_masuk.minute
    for jam_mulai, jam_selesai, nama, jam_standar in ranges:
        menit_mulai   = jam_mulai  * 60
        menit_selesai = jam_selesai * 60 + 59  # inklusif sampai akhir jam
        if menit_mulai <= menit_selesai:
            # Rentang normal (tidak lintas tengah malam)
            if menit_mulai <= menit_masuk <= menit_selesai:
                return nama, jam_standar
        else:
            # Rentang lintas tengah malam (misal 19:00–04:59)
            # Cocok jika jam_masuk >= jam_mulai ATAU jam_masuk <= jam_selesai
            if menit_masuk >= menit_mulai or menit_masuk <= menit_selesai:
                return nama, jam_standar
    return "Non-Shift", JAM_STANDAR_NON_SHIFT

def hitung_selisih_menit(jam_masuk, jam_standar, nama_shift):
    """
    Hitung selisih menit (keterlambatan) secara numerik.
    Untuk Shift Malam (lintas tengah malam):
      - Jam masuk 19:xx–23:59 → dibandingkan langsung dengan jam_standar (misal 19:00)
      - Jam masuk 00:xx–04:59 → dianggap hari berikutnya, tambah 24 jam untuk perbandingan
    """
    menit_masuk   = jam_masuk.hour   * 60 + jam_masuk.minute
    menit_standar = jam_standar.hour * 60 + jam_standar.minute

    if nama_shift == "Shift Malam":
        # Jam standar malam (misal 19:00 = 1140 menit)
        # Jika jam masuk sudah lewat tengah malam (00:00–04:59), tambah 1440 menit (24 jam)
        if jam_masuk.hour < 12:
            menit_masuk += 1440
            # jam_standar malam di hari sebelumnya
            # tetap gunakan menit_standar tanpa penambahan karena standar >= 12
        sel = menit_masuk - menit_standar
    else:
        sel = menit_masuk - menit_standar

    if sel < 0:  return 0   # datang lebih awal → tidak terlambat
    if sel > MAX_TERLAMBAT_MENIT: return -1  # data tidak wajar
    return sel

def fmt_menit(menit):
    menit = int(menit)
    if menit < 60: return f"{menit} menit"
    j=menit//60; s=menit%60
    return f"{j} jam" if s==0 else f"{j} jam {s} menit"

def parse_timetable(csv_text, tahun, bulan):
    reader = csv.reader(io.StringIO(csv_text))
    all_rows = list(reader)
    def parse_date_row(row):
        mapping = {}
        for idx,cell in enumerate(row):
            m = DATE_PAT.match(cell.strip())
            if m: mapping[idx] = (int(m.group(1)), m.group(2))
        return mapping
    results=[]; current_nama=None; i=0
    while i < len(all_rows):
        row = all_rows[i]
        if len(row)>14 and row[2].strip() in EMPLOYEE_MARKERS:
            raw = row[14].strip().replace('\n',' ').replace('\r','')
            current_nama = ' '.join(raw.split()); i+=1; continue
        date_map = parse_date_row(row)
        if date_map and current_nama:
            if i+1 < len(all_rows):
                t_row = all_rows[i+1]
                for col_idx,(day,wday) in date_map.items():
                    if col_idx < len(t_row):
                        jin,jout = parse_sel_waktu(t_row[col_idx].strip())
                        if jin is not None:
                            results.append({'nama':current_nama,'hari':wday,
                                'tanggal':f'{day:02d}-{bulan}-{tahun}',
                                'jam_masuk':jin,'jam_pulang':jout})
            i+=2; continue
        i+=1
    return [r for r in results if r['nama']]

def baca_file_timetable(file_bytes, ext):
    if ext in ('xls','xlsx'):
        engine = 'xlrd' if ext=='xls' else 'openpyxl'
        try: df_raw = pd.read_excel(io.BytesIO(file_bytes),header=None,engine=engine,dtype=str)
        except Exception: df_raw = pd.read_excel(io.BytesIO(file_bytes),header=None,dtype=str)
        buf=io.StringIO(); df_raw.to_csv(buf,index=False,header=False); csv_text=buf.getvalue()
    else:
        csv_text = file_bytes.decode('utf-8',errors='replace')
    tahun=str(datetime.now().year); bulan=f'{datetime.now().month:02d}'
    m = re.search(r'(\d{4})-(\d{2})-\d{2}', csv_text[:500])
    if m: tahun,bulan = m.group(1),m.group(2)
    return parse_timetable(csv_text,tahun,bulan), tahun, bulan

def normalkan_kolom(df):
    df.columns=[str(c).strip().lower() for c in df.columns]
    aliases={"nama":["nama","name","pegawai","karyawan"],"tanggal":["tanggal","date","tgl"],
             "jam_masuk":["jam masuk","jam_masuk","check in","checkin","masuk","in"],
             "jam_pulang":["jam pulang","jam_pulang","check out","checkout","pulang","out"]}
    rmap={}
    for std,vlist in aliases.items():
        for col in df.columns:
            if col in vlist and col!=std: rmap[col]=std; break
    df.rename(columns=rmap,inplace=True); return df

def parse_time_generic(val):
    if val is None: return None
    try:
        if pd.isna(val): return None
    except Exception: pass
    if isinstance(val,time): return val
    if isinstance(val,datetime): return val.time()
    for fmt in ('%H:%M:%S','%H:%M','%I:%M %p','%H.%M'):
        try: return datetime.strptime(str(val).strip(),fmt).time()
        except ValueError: continue
    m=re.match(r'^(\d{1,2})[:\.](\d{2})',str(val).strip())
    if m:
        try: return time(int(m.group(1)),int(m.group(2)))
        except ValueError: pass
    return None

def rekap_keterlambatan(records):
    """
    v5: Membedakan pegawai Normal vs Shift.
    Normal : jam standar masuk 07:30, cek pulang cepat (Senin-Kamis 15:30, Jumat 14:30)
    Shift  : jam standar sesuai shift terdeteksi, tidak cek pulang cepat
    """
    rekap_dict={}; detail_list=[]
    peg_shift = get_pegawai_shift_set()
    for rec in records:
        nama=str(rec.get('nama','')).strip()
        if not nama or nama.lower() in ('nan','','none'): continue
        raw_masuk  = rec.get('jam_masuk')
        raw_pulang = rec.get('jam_pulang')
        jam_masuk  = raw_masuk  if isinstance(raw_masuk,  time) else parse_time_generic(raw_masuk)
        jam_pulang = raw_pulang if isinstance(raw_pulang, time) else parse_time_generic(raw_pulang)
        if jam_masuk is None: continue
        tanggal=rec.get('tanggal','')
        if isinstance(tanggal,datetime): tanggal=tanggal.strftime('%d-%m-%Y')
        else: tanggal=str(tanggal).strip()
        hari=rec.get('hari','')

        is_shift = nama.strip().lower() in peg_shift
        nama_shift, jam_std_shift = deteksi_shift(jam_masuk)

        if is_shift:
            jam_standar   = jam_std_shift
            tipe_pegawai  = 'Shift'
            sel_masuk     = hitung_selisih_menit(jam_masuk, jam_standar, nama_shift)
            sel_pulang    = 0
        else:
            jam_standar   = JAM_MASUK_NONSHIFT
            tipe_pegawai  = 'Normal'
            sel_masuk     = hitung_selisih_menit(jam_masuk, jam_standar, 'Non-Shift')
            sel_pulang    = hitung_pulang_cepat(jam_pulang, hari)

        if sel_masuk == -1: continue
        terlambat    = sel_masuk  > 0
        pulang_cepat = sel_pulang > 0
        if not terlambat and not pulang_cepat: continue

        if terlambat and pulang_cepat: status='Terlambat+Pulang Cepat'
        elif terlambat:                status='Terlambat'
        else:                          status='Pulang Cepat'

        if nama not in rekap_dict:
            rekap_dict[nama]={
                'tipe':nama_shift, 'tipe_pegawai':tipe_pegawai,
                'jumlah':0, 'jumlah_terlambat':0, 'jumlah_pulang_cepat':0,
                'total_menit':0, 'total_menit_pulang':0,
            }
        rekap_dict[nama]['jumlah'] += 1
        if terlambat:
            rekap_dict[nama]['jumlah_terlambat']  += 1
            rekap_dict[nama]['total_menit']        += sel_masuk
        if pulang_cepat:
            rekap_dict[nama]['jumlah_pulang_cepat'] += 1
            rekap_dict[nama]['total_menit_pulang']  += sel_pulang

        std_pulang_str = jam_pulang_standar(hari).strftime('%H:%M') if not is_shift else '-'
        detail_list.append({
            'nama':nama, 'tanggal':tanggal, 'hari':hari,
            'tipe_pegawai':tipe_pegawai,
            'jam_masuk':jam_masuk.strftime('%H:%M'),
            'jam_pulang':jam_pulang.strftime('%H:%M') if jam_pulang else '-',
            'seharusnya':jam_standar.strftime('%H:%M'),
            'std_pulang':std_pulang_str,
            'shift':nama_shift, 'selisih':sel_masuk,
            'selisih_pulang':sel_pulang, 'status':status,
        })
    rekap_list=[{'nama':k,**v} for k,v in rekap_dict.items()]
    rekap_list.sort(key=lambda x:(-x['jumlah'],-x['total_menit']))
    detail_list.sort(key=lambda x:(x['nama'],x['tanggal']))
    return rekap_list, detail_list

def ringkasan_per_shift(rekap_list):
    """Hitung statistik ringkasan per shift."""
    hasil = {}
    for r in rekap_list:
        tipe = r['tipe']
        if tipe not in hasil:
            hasil[tipe] = {'shift':tipe,'pegawai':0,'kejadian':0,'total_menit':0}
        hasil[tipe]['pegawai'] += 1
        hasil[tipe]['kejadian'] += r['jumlah']
        hasil[tipe]['total_menit'] += r['total_menit']
    for v in hasil.values():
        v['total_fmt'] = fmt_menit(v['total_menit'])
        v['rata_fmt']  = fmt_menit(round(v['total_menit']/v['kejadian'])) if v['kejadian'] else '0 menit'
    urutan = ['Shift Pagi','Shift Siang','Shift Sore','Shift Malam','Non-Shift']
    return [hasil[s] for s in urutan if s in hasil]


HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0,minimum-scale=1.0"/>
<title>SmartPresensi</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet"/>
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<style>
/* ══════════════════════════════════════════════════════
   SmartPresensi — Modern Minimalis Theme
   Palet: Putih · Abu Soft · Emerald #10b981
   ══════════════════════════════════════════════════════ */
:root{
  --bg:       #f8fafc;
  --surface:  #ffffff;
  --card:     #ffffff;
  --card-alt: #f3f4f6;
  --border:   #e5e7eb;
  --border-l: #f0f0f0;

  --em:       #10b981;
  --em-dk:    #059669;
  --em-lt:    #d1fae5;
  --em-mid:   #6ee7b7;

  --red:      #ef4444;
  --red-lt:   #fee2e2;
  --yellow:   #f59e0b;
  --yel-lt:   #fef3c7;
  --blue:     #3b82f6;
  --blue-lt:  #dbeafe;
  --purple:   #8b5cf6;
  --pur-lt:   #ede9fe;

  --t1: #111827;
  --t2: #4b5563;
  --t3: #9ca3af;
  --t4: #d1d5db;

  --r:  12px;
  --r2: 8px;
  --r3: 6px;

  --shadow-sm: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  --shadow:    0 4px 16px rgba(0,0,0,.06), 0 2px 6px rgba(0,0,0,.04);
  --shadow-md: 0 8px 24px rgba(0,0,0,.08), 0 4px 8px rgba(0,0,0,.04);
  --shadow-em: 0 4px 16px rgba(16,185,129,.2);

  --shift-pagi:  #3b82f6;
  --shift-sore:  #10b981;
  --shift-malam: #8b5cf6;
  --shift-non:   #9ca3af;
}

/* ── Reset ── */
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  font-family:'Poppins',sans-serif;
  background:var(--bg);
  color:var(--t1);
  min-height:100vh;
  -webkit-font-smoothing:antialiased;
}

/* ══ TOPBAR ══════════════════════════════════════════ */
.topbar{
  background:var(--surface);
  border-bottom:1px solid var(--border);
  box-shadow:var(--shadow-sm);
  padding:0 clamp(14px,3vw,40px);
  height:60px;
  display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:200;
  width:100%;box-sizing:border-box;
}
.brand{display:flex;align-items:center;gap:11px}
.brand-logo{
  width:36px;height:36px;border-radius:10px;
  background:linear-gradient(135deg,var(--em),var(--em-dk));
  display:flex;align-items:center;justify-content:center;font-size:16px;
  box-shadow:var(--shadow-em);
}
.brand-name{font-weight:700;font-size:.95rem;color:var(--t1)}
.brand-sub{font-size:.6rem;color:var(--t3);font-weight:500}
.topbar-right{display:flex;align-items:center;gap:14px}
.topbar-clock{
  font-family:'DM Mono',monospace;font-size:.72rem;color:var(--t2);
  display:flex;align-items:center;gap:6px;
  background:var(--bg);padding:4px 10px;border-radius:100px;
  border:1px solid var(--border);
}
.dot-live{
  width:6px;height:6px;border-radius:50%;
  background:var(--em);box-shadow:0 0 6px var(--em);
  animation:blink 2s ease infinite;
}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.db-badge{font-size:.65rem;padding:3px 10px;border-radius:100px;font-weight:600;font-family:'Poppins',sans-serif}
.db-ok{background:var(--em-lt);color:var(--em-dk);border:1px solid #a7f3d0}
.db-err{background:var(--red-lt);color:var(--red);border:1px solid #fca5a5}

/* ══ PAGE NAV ════════════════════════════════════════ */
.page-nav{
  background:var(--surface);
  border-bottom:1px solid var(--border);
  display:flex;overflow-x:auto;
  padding:0 clamp(12px,2.5vw,40px);
  -webkit-overflow-scrolling:touch;
  scrollbar-width:none;
}
.page-nav::-webkit-scrollbar{display:none}
.pnav-btn{
  padding:16px 22px;
  background:transparent;border:none;
  border-bottom:2.5px solid transparent;
  color:var(--t3);font-size:.8rem;font-weight:600;
  cursor:pointer;white-space:nowrap;
  transition:all .2s;font-family:'Poppins',sans-serif;
  display:flex;align-items:center;gap:6px;
}
.pnav-btn:hover{color:var(--em);border-bottom-color:var(--em-mid)}
.pnav-btn.on{color:var(--em);border-bottom-color:var(--em)}
.page{display:none;width:100%;box-sizing:border-box;overflow-x:hidden}.page.on{display:block}

/* ══ WRAP ════════════════════════════════════════════ */
.wrap{width:100%;box-sizing:border-box;padding:28px clamp(16px,2vw,36px) 80px}

/* ══ HERO ════════════════════════════════════════════ */
.hero{text-align:center;margin-bottom:32px}
.hero h1{
  font-size:clamp(1.5rem,3vw,2.1rem);font-weight:800;
  color:var(--t1);letter-spacing:-.02em;
}
.hero h1 span{color:var(--em)}
.hero p{color:var(--t2);font-size:.87rem;margin-top:8px;font-weight:400}

/* ══ CARD ════════════════════════════════════════════ */
.card-d{
  background:var(--card);
  border:1px solid var(--border);
  border-radius:var(--r);
  box-shadow:var(--shadow-sm);
  transition:box-shadow .25s,border-color .25s;
}
.card-d:hover{box-shadow:var(--shadow);border-color:#d1d5db}
.card-hd{
  padding:16px 20px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:8px;
}
.card-hd-l{display:flex;align-items:center;gap:10px}
.card-hd-ico{
  width:32px;height:32px;border-radius:8px;
  display:flex;align-items:center;justify-content:center;font-size:14px;
}
.card-hd h5{font-size:.85rem;font-weight:700;margin:0;color:var(--t1)}
.card-hd small{font-size:.68rem;color:var(--t3);font-weight:400}

/* ══ DROPZONE ════════════════════════════════════════ */
.dz{
  border:2px dashed var(--border);
  border-radius:var(--r);padding:36px 20px;
  text-align:center;cursor:pointer;position:relative;
  transition:all .25s;background:var(--bg);
}
.dz:hover,.dz.drag{
  border-color:var(--em);
  background:rgba(16,185,129,.03);
}
.dz.ok{border-color:var(--em);border-style:solid;background:rgba(16,185,129,.04)}
.dz-ico{
  width:52px;height:52px;border-radius:14px;
  background:var(--em-lt);color:var(--em);
  display:flex;align-items:center;justify-content:center;
  font-size:22px;margin:0 auto 12px;transition:transform .25s;
}
.dz:hover .dz-ico{transform:translateY(-4px);box-shadow:0 8px 20px rgba(16,185,129,.2)}
.dz h6{font-size:.87rem;font-weight:700;margin-bottom:4px;color:var(--t1)}
.dz p{font-size:.74rem;color:var(--t3);margin:0}
input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer}

/* ── Notif strips ── */
.fi-ok{
  display:none;align-items:center;gap:8px;
  background:var(--em-lt);border:1px solid #a7f3d0;
  border-radius:var(--r2);padding:8px 12px;margin-top:10px;
  font-size:.78rem;color:var(--em-dk);font-weight:500;
}
.fi-ok.show{display:flex}
.err{
  display:none;align-items:flex-start;gap:7px;
  background:var(--red-lt);border:1px solid #fca5a5;
  border-radius:var(--r2);padding:9px 12px;margin-top:9px;
  font-size:.79rem;color:var(--red);
}
.saved-notif{
  display:none;align-items:center;gap:7px;
  background:var(--em-lt);border:1px solid #a7f3d0;
  border-radius:var(--r2);padding:9px 12px;margin-top:10px;
  font-size:.79rem;color:var(--em-dk);font-weight:500;
}
.saved-notif.show{display:flex}

/* ── Progress bar ── */
.prog{display:none;margin-top:12px}
.prog-bar{
  height:3px;border-radius:2px;
  background:linear-gradient(90deg,var(--em),var(--em-mid),var(--em));
  background-size:200%;animation:slide 1.5s linear infinite;
}
@keyframes slide{0%{background-position:200% 0}100%{background-position:-200% 0}}
.prog-txt{font-size:.72rem;color:var(--t2);display:flex;align-items:center;gap:6px;margin-top:6px}

/* ══ BUTTONS ═════════════════════════════════════════ */
.btn-main{
  width:100%;padding:12px;margin-top:14px;
  background:var(--em);
  border:none;border-radius:var(--r2);
  color:#fff;font-size:.87rem;font-weight:700;
  cursor:pointer;transition:all .25s;
  font-family:'Poppins',sans-serif;
  box-shadow:0 2px 8px rgba(16,185,129,.25);
  letter-spacing:.01em;
}
.btn-main:hover{background:var(--em-dk);box-shadow:var(--shadow-em);transform:translateY(-1px)}
.btn-main:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}

.btn-s{
  padding:5px 13px;border-radius:var(--r3);
  font-size:.72rem;font-weight:600;cursor:pointer;
  transition:all .2s;font-family:'Poppins',sans-serif;
  border:1px solid transparent;
}
.btn-blue{
  background:var(--blue-lt);color:var(--blue);
  border-color:#bfdbfe;
}
.btn-blue:hover{background:#bfdbfe}
.btn-red{
  background:var(--red-lt);color:var(--red);
  border-color:#fca5a5;
}
.btn-red:hover{background:#fca5a5}
.btn-em{
  background:var(--em);color:#fff;border-color:var(--em);
}
.btn-em:hover{background:var(--em-dk)}
.btn-em-out{
  background:#fff;color:var(--em);border-color:var(--em);
}
.btn-em-out:hover{background:var(--em-lt)}

/* ══ SHIFT PILLS ═════════════════════════════════════ */
.shift-pills{
  display:flex;gap:6px;flex-wrap:wrap;
  padding:12px 18px;border-bottom:1px solid var(--border);
  align-items:center;background:var(--bg);
}
.shift-pill{
  display:inline-flex;align-items:center;gap:5px;
  padding:5px 13px;border-radius:100px;
  font-size:.72rem;font-weight:600;cursor:pointer;
  border:1.5px solid var(--border);
  transition:all .2s;font-family:'Poppins',sans-serif;
  background:#fff;color:var(--t2);
}
.shift-pill:hover{border-color:var(--t3);color:var(--t1)}
.shift-pill.pill-semua.on{background:var(--t1);color:#fff;border-color:var(--t1)}
.shift-pill.pill-pagi.on{background:#dcfce7;color:#16a34a;border-color:#bbf7d0}
.shift-pill.pill-siang.on{background:#dbeafe;color:#2563eb;border-color:#bfdbfe}
.shift-pill.pill-sore.on{background:#ffedd5;color:#ea580c;border-color:#fed7aa}
.shift-pill.pill-malam.on{background:var(--pur-lt);color:var(--purple);border-color:#ddd6fe}
.shift-pill.pill-non.on{background:var(--card-alt);color:var(--t2);border-color:var(--t4)}
.pill-dot{width:7px;height:7px;border-radius:50%}
.pill-pagi .pill-dot{background:#16a34a}
.pill-siang .pill-dot{background:#2563eb}
.pill-sore .pill-dot{background:#ea580c}
.pill-malam .pill-dot{background:var(--purple)}
.pill-non .pill-dot{background:var(--shift-non)}
.pill-semua .pill-dot{background:var(--t3)}
.pill-count{font-size:.63rem;opacity:.65}

/* ══ SHIFT STAT CARDS ════════════════════════════════ */
.shift-stat-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
  gap:10px;padding:16px 18px;border-bottom:1px solid var(--border);
}
.shift-stat{
  border-radius:var(--r2);padding:13px 15px;border:1px solid transparent;
  transition:box-shadow .2s;
}
.shift-stat:hover{box-shadow:var(--shadow-sm)}
.shift-stat.ss-pagi{background:#dcfce7;border-color:#bbf7d0}
.shift-stat.ss-siang{background:#dbeafe;border-color:#bfdbfe}
.shift-stat.ss-sore{background:#ffedd5;border-color:#fed7aa}
.shift-stat.ss-malam{background:var(--pur-lt);border-color:#ddd6fe}
.shift-stat.ss-non{background:var(--card-alt);border-color:var(--border)}
.ss-label{font-size:.61rem;font-weight:700;text-transform:uppercase;letter-spacing:.9px;margin-bottom:7px}
.ss-pagi .ss-label{color:#16a34a}
.ss-siang .ss-label{color:#2563eb}
.ss-sore .ss-label{color:#ea580c}
.ss-malam .ss-label{color:var(--purple)}
.ss-non .ss-label{color:var(--t3)}
.ss-nums{display:grid;grid-template-columns:1fr 1fr;gap:4px}
.ss-item{font-size:.68rem;color:var(--t3);font-weight:500}
.ss-val{font-family:'DM Mono',monospace;font-size:.92rem;font-weight:700;color:var(--t1)}

/* ══ RULES GRID ══════════════════════════════════════ */
.rules-g{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.rule-item{
  background:var(--bg);border:1px solid var(--border);
  border-radius:var(--r2);padding:12px 14px;
  transition:border-color .2s;
}
.rule-item:hover{border-color:var(--em)}
.rule-lbl{font-size:.61rem;text-transform:uppercase;letter-spacing:1px;color:var(--t3);font-weight:700;margin-bottom:4px}
.rule-val{font-family:'DM Mono',monospace;font-size:.82rem;color:var(--em);font-weight:600}
.rule-sub{font-size:.67rem;color:var(--t2);margin-top:3px}
.info-note{
  padding:10px 13px;
  background:#f0fdf4;border:1px solid #bbf7d0;
  border-radius:var(--r2);font-size:.71rem;
  color:var(--em-dk);margin-top:10px;line-height:1.7;
}

/* ══ STAT CARDS ══════════════════════════════════════ */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;margin-bottom:18px}
.stat{
  background:var(--card);border:1px solid var(--border);
  border-radius:var(--r);padding:18px 20px;
  position:relative;overflow:hidden;
  transition:all .25s;box-shadow:var(--shadow-sm);
}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:var(--r) var(--r) 0 0}
.stat.c-b::before{background:var(--blue)}
.stat.c-r::before{background:var(--red)}
.stat.c-y::before{background:var(--yellow)}
.stat.c-p::before{background:var(--em)}
.stat:hover{box-shadow:var(--shadow);transform:translateY(-2px)}
.stat-lbl{font-size:.63rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--t3);margin-bottom:6px}
.stat-val{font-family:'DM Mono',monospace;font-size:1.75rem;font-weight:700;line-height:1;color:var(--t1)}
.stat.c-b .stat-val{color:var(--blue)}
.stat.c-r .stat-val{color:var(--red)}
.stat.c-y .stat-val{color:var(--yellow)}
.stat.c-p .stat-val{color:var(--em)}
.stat-sub{font-size:.66rem;color:var(--t3);margin-top:4px}

/* ══ TABS ════════════════════════════════════════════ */
.tab-nav{
  display:flex;gap:3px;
  background:var(--bg);border-radius:var(--r2);padding:3px;
  border:1px solid var(--border);
}
.tab-btn{
  flex:1;padding:7px 12px;background:transparent;
  border:none;border-radius:6px;cursor:pointer;
  font-size:.74rem;font-weight:600;color:var(--t3);
  transition:all .2s;font-family:'Poppins',sans-serif;
}
.tab-btn.on{background:var(--em);color:#fff;box-shadow:0 2px 8px rgba(16,185,129,.25)}
.tab-btn:hover:not(.on){background:#fff;color:var(--t2)}
.tab-pane{display:none}.tab-pane.on{display:block}

/* ══ TABLE ═══════════════════════════════════════════ */
.tbl{width:100%;min-width:520px;border-collapse:collapse;font-size:.81rem}
.tbl thead tr{background:var(--card-alt);border-bottom:2px solid var(--border)}
.tbl thead th{
  padding:10px 14px;font-size:.65rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.7px;color:var(--t3);
  white-space:nowrap;position:sticky;top:0;background:var(--card-alt);
}
.tbl tbody tr{border-bottom:1px solid var(--border-l);transition:background .15s}
.tbl tbody tr:nth-child(even){background:rgba(248,250,252,.8)}
.tbl tbody tr:hover{background:rgba(16,185,129,.06) !important}
.tbl tbody td{padding:11px 14px;vertical-align:middle;color:var(--t1)}
.tbl tbody tr:last-child{border-bottom:none}
.tr-hidden{display:none}

/* ══ BADGES ══════════════════════════════════════════ */
.rk{
  width:26px;height:26px;border-radius:7px;
  font-size:.73rem;font-weight:700;
  font-family:'DM Mono',monospace;
  display:inline-flex;align-items:center;justify-content:center;
}
.rk-1{background:#fef3c7;color:#d97706}
.rk-2{background:#f3f4f6;color:#6b7280}
.rk-3{background:#fff7ed;color:#ea580c}
.rk-n{background:#f9fafb;color:var(--t3)}

.badge-shift{
  display:inline-block;padding:3px 9px;border-radius:100px;
  font-size:.67rem;font-weight:600;border:1px solid transparent;
}
.sh-pagi{background:#dcfce7;color:#16a34a;border-color:#bbf7d0}
.sh-siang{background:#dbeafe;color:#2563eb;border-color:#bfdbfe}
.sh-sore{background:#ffedd5;color:#ea580c;border-color:#fed7aa}
.sh-malam{background:var(--pur-lt);color:var(--purple);border-color:#ddd6fe}
.sh-non{background:var(--card-alt);color:var(--t3);border-color:var(--border)}

.badge-n{
  display:inline-block;padding:3px 9px;border-radius:100px;
  font-size:.67rem;font-weight:600;border:1px solid transparent;
}
.bn-red{background:var(--red-lt);color:var(--red);border-color:#fca5a5}
.bn-yel{background:var(--yel-lt);color:var(--yellow);border-color:#fcd34d}
.bn-grn{background:var(--em-lt);color:var(--em-dk);border-color:#a7f3d0}

/* ── Progress mini bar ── */
.mbar-wrap{display:flex;align-items:center;gap:5px}
.mbar{
  height:5px;border-radius:3px;min-width:3px;
  background:var(--em);transition:width .6s ease;
}

/* ── Table foot ── */
.tbl-foot{
  padding:10px 16px;border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:6px;background:var(--bg);
  border-radius:0 0 var(--r) var(--r);
}
.tbl-foot span{font-size:.7rem;color:var(--t3);font-weight:500}
.row-warn td:first-child{border-left:3px solid var(--red)}

/* ══ CHART ═══════════════════════════════════════════ */
.chart-wrap{padding:18px;position:relative;height:clamp(220px,20vw,340px);min-height:200px}
.chart-empty{
  display:flex;align-items:center;justify-content:center;
  height:200px;color:var(--t4);font-size:.82rem;
  flex-direction:column;gap:8px;
}
.chart-empty i{font-size:32px;color:var(--t4)}

/* ══ EMPTY / MODAL ═══════════════════════════════════ */
.empty-state{
  padding:44px 20px;text-align:center;color:var(--t3);
}
.empty-state i{font-size:36px;margin-bottom:12px;display:block;color:var(--t4)}
.empty-state p{font-size:.83rem}

.modal-dark .modal-content{
  background:var(--card);border:1px solid var(--border);
  color:var(--t1);border-radius:var(--r);
  box-shadow:var(--shadow-md);
}
.modal-dark .modal-header{border-bottom-color:var(--border)}
.modal-dark .modal-footer{border-top-color:var(--border)}
.modal-dark .modal-title{font-size:.9rem;font-weight:700;color:var(--t1)}
.modal-dark .btn-close{opacity:.4;filter:none}

/* ══ SCROLLBAR ═══════════════════════════════════════ */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--t4);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--t3)}

/* ══ TREN PAGE ═══════════════════════════════════════ */
.tren-sum-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}
.tren-sum-card{
  background:var(--card);border:1px solid var(--border);
  border-radius:var(--r);padding:16px 18px;
  box-shadow:var(--shadow-sm);transition:all .25s;
}
.tren-sum-card:hover{box-shadow:var(--shadow);border-color:var(--em);transform:translateY(-2px)}
.tsum-lbl{font-size:.63rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--t3);margin-bottom:5px}
.tsum-val{font-family:'DM Mono',monospace;font-size:1.5rem;font-weight:700;color:var(--t1);line-height:1.1}
.tsum-sub{font-size:.67rem;color:var(--t3);margin-top:3px}
.tsum-trend{font-size:.72rem;font-weight:600;margin-top:5px;display:flex;align-items:center;gap:4px}
.tsum-up{color:var(--red)}.tsum-dn{color:var(--em)}.tsum-eq{color:var(--t3)}
.chart-insight{
  padding:9px 18px 13px;font-size:.75rem;color:var(--t2);
  border-top:1px solid var(--border);
  display:flex;gap:18px;flex-wrap:wrap;background:var(--bg);
}
.ins-item{display:flex;align-items:center;gap:5px}
.ins-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.chart-legend-wrap{display:flex;gap:10px;flex-wrap:wrap}
.cleg{display:flex;align-items:center;gap:5px;font-size:.7rem;color:var(--t2)}
.cleg-dot{width:10px;height:10px;border-radius:2px;flex-shrink:0}

/* ── Tabel perbandingan ── */
.tbl-cmp{width:100%;border-collapse:collapse;font-size:.81rem}
.tbl-cmp thead th{
  padding:11px 15px;font-size:.67rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.7px;color:var(--t3);
  border-bottom:2px solid var(--border);white-space:nowrap;
  background:var(--card-alt);position:sticky;top:0;
}
.tbl-cmp tbody tr{border-bottom:1px solid var(--border-l);transition:background .15s}
.tbl-cmp tbody tr:hover{background:rgba(16,185,129,.05)}
.tbl-cmp tbody td{padding:12px 15px;vertical-align:middle}
.tbl-cmp .td-bulan{font-weight:700;font-size:.85rem;color:var(--t1)}
.tbl-cmp .td-mono{font-family:'DM Mono',monospace;font-size:.78rem}
.tbl-cmp .td-muted{color:var(--t2)}
.trend-pill{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:100px;font-size:.7rem;font-weight:700}
.tp-up{background:var(--red-lt);color:var(--red);border:1px solid #fca5a5}
.tp-dn{background:var(--em-lt);color:var(--em-dk);border:1px solid #a7f3d0}
.tp-eq{background:var(--card-alt);color:var(--t3);border:1px solid var(--border)}
.bar-mini{height:6px;border-radius:3px;transition:width .6s ease}

/* ── Dist table ── */
.dist-tbl{width:100%;border-collapse:collapse;font-size:.82rem}
.dist-tbl td{padding:11px 15px;vertical-align:middle;border-bottom:1px solid var(--border-l)}
.dist-tbl tr:last-child td{border-bottom:none}
.dist-bar-bg{height:8px;background:var(--card-alt);border-radius:4px;overflow:hidden;min-width:80px}
.dist-bar-fill{height:100%;border-radius:4px;transition:width .8s ease}

/* ══ FOOTER ══════════════════════════════════════════ */
.footer{
  text-align:center;padding:18px;
  color:var(--t3);font-size:.66rem;
  border-top:1px solid var(--border);
  background:var(--surface);
}

/* ══ RESPONSIVE ══════════════════════════════════════ */

/* Tablet ≤ 900px */
@media(max-width:900px){
  .rules-g{grid-template-columns:1fr 1fr}
  .wrap{padding:20px 18px 70px}
  .stat-grid{grid-template-columns:repeat(2,1fr)}
  .shift-stat-grid{grid-template-columns:repeat(2,1fr)}
  .tren-sum-grid{grid-template-columns:repeat(2,1fr)}
}

/* Tablet kecil ≤ 768px */
@media(max-width:768px){
  .rules-g{grid-template-columns:1fr}
  .wrap{padding:16px 14px 60px}
  .topbar{padding:0 14px;height:54px}
  .pnav-btn{padding:12px 14px;font-size:.75rem}
  .stat-grid{grid-template-columns:repeat(2,1fr)}
  .shift-stat-grid{grid-template-columns:repeat(2,1fr)}
  .tren-sum-grid{grid-template-columns:repeat(2,1fr)}
  .card-hd{flex-wrap:wrap;gap:8px}
  .hero h1{font-size:1.55rem}
}

/* HP 480px ke bawah */
@media(max-width:480px){
  .topbar-clock{display:none}
  .brand-sub{display:none}
  .wrap{padding:12px 10px 56px}
  .topbar{padding:0 12px;height:50px}
  .pnav-btn{padding:10px 10px;font-size:.7rem;gap:3px}
  .hero h1{font-size:1.25rem}
  .hero p{font-size:.76rem}
  .stat-grid{grid-template-columns:repeat(2,1fr);gap:8px}
  .shift-stat-grid{grid-template-columns:repeat(2,1fr);gap:8px}
  .tren-sum-grid{grid-template-columns:repeat(2,1fr)}
  .tbl th,.tbl td{font-size:.7rem;padding:6px 7px}
  .btn-s{font-size:.68rem;padding:5px 8px}
  .shift-pill{font-size:.67rem;padding:5px 8px}
  .form-pegawai-grid{grid-template-columns:1fr!important}
  .rules-g{grid-template-columns:1fr}
  .chart-wrap{height:190px!important;min-height:160px!important}
}

/* Landscape HP */
@media(max-width:768px) and (orientation:landscape){
  .topbar{height:44px}
  .wrap{padding:8px 14px 48px}
  .chart-wrap{height:160px!important;min-height:140px!important}
}

/* ── Global full-width & anti-overflow ───────────────── */
html,body{overflow-x:hidden;width:100%}
.page{width:100%;overflow-x:hidden}
.card-d,.card{width:100%;box-sizing:border-box}
canvas{max-width:100%!important}
img{max-width:100%;height:auto}

/* ─── Rekap Cards ────────────────────────────────── */
/* ── Rekap Tabel Baru ──────────────────────────────── */

/* Grup ringkasan 3 status */
.rgrp{
  border-radius:12px;padding:14px 16px;cursor:pointer;
  border:2px solid transparent;transition:all .18s;
  display:flex;flex-direction:column;gap:6px;
}
.rgrp:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.1)}
.rgrp.active{border-color:currentColor}
.rgrp-kritis {background:#fef2f2;color:#dc2626}
.rgrp-waspada{background:#fffbeb;color:#d97706}
.rgrp-baik   {background:#f0fdf4;color:#16a34a}
.rgrp-num{font-size:2rem;font-weight:800;line-height:1}
.rgrp-lbl{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.5px;opacity:.8}
.rgrp-sub{font-size:.68rem;opacity:.65;margin-top:2px}

/* Tabel rekap utama */
.rtbl tbody tr{cursor:pointer;transition:background .12s}
.rtbl tbody tr:hover{background:#f0fdf4!important}
.rtbl tbody tr.rtr-selected{background:#d1fae5!important;outline:2px solid #10b981;outline-offset:-2px}
.rtbl tbody tr.rtr-kritis  td:first-child{border-left:4px solid #ef4444}
.rtbl tbody tr.rtr-waspada td:first-child{border-left:4px solid #f59e0b}
.rtbl tbody tr.rtr-baik    td:first-child{border-left:4px solid #10b981}
.rtbl td{vertical-align:middle!important}
.rtr-rank{
  width:28px;height:28px;border-radius:8px;
  display:inline-flex;align-items:center;justify-content:center;
  font-size:.8rem;font-weight:800;background:var(--card-alt);color:var(--t2);
}
.rtr-rank.gold  {background:#fef3c7;color:#b45309}
.rtr-rank.silver{background:#f1f5f9;color:#64748b}
.rtr-rank.bronze{background:#fef3e2;color:#9a4f1a}
.rtr-nama{font-weight:700;font-size:.85rem;color:var(--t1)}
.rtr-tipe{font-size:.65rem;color:var(--t3);margin-top:2px}
.rtr-num{font-size:.95rem;font-weight:800}
.rtr-bar{height:5px;border-radius:3px;margin-top:5px;background:var(--border)}
.rtr-bar-fill{height:100%;border-radius:3px}
.rtr-status{
  display:inline-flex;align-items:center;gap:5px;
  font-size:.72rem;font-weight:700;padding:4px 10px;
  border-radius:100px;white-space:nowrap;
}
.rtr-status.kritis {background:#fee2e2;color:#991b1b}
.rtr-status.waspada{background:#fef3c7;color:#92400e}
.rtr-status.baik   {background:#d1fae5;color:#065f46}
.rtr-dot{width:7px;height:7px;border-radius:50%}
.rtr-dot.kritis {background:#ef4444}
.rtr-dot.waspada{background:#f59e0b}
.rtr-dot.baik   {background:#10b981}
.rtr-chevron{font-size:.8rem;color:var(--t4);transition:transform .2s}
.rtr-chevron.open{transform:rotate(180deg);color:var(--em)}

/* Panel detail inline */
#rekap-detail-panel{
  margin:0 16px 16px;
  border:1.5px solid var(--em);
  border-radius:12px;overflow:hidden;
  box-shadow:0 4px 20px rgba(16,185,129,.12);
}
.rdp-header{
  background:linear-gradient(135deg,#ecfdf5,#d1fae5);
  padding:14px 18px;
  display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:10px;
  border-bottom:1px solid #a7f3d0;
}
.rdp-title{font-size:.95rem;font-weight:700;color:#065f46}
.rdp-chips{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.rdp-stats{
  display:grid;
  grid-template-columns:repeat(5,1fr);
  border-bottom:1px solid var(--border);
}
.rdp-stat{padding:12px 14px;text-align:center;border-right:1px solid var(--border)}
.rdp-stat:last-child{border-right:none}
.rdp-stat-lbl{font-size:.6rem;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}
.rdp-stat-val{font-size:1.1rem;font-weight:800;color:var(--t1)}
.rdp-stat-sub{font-size:.62rem;color:var(--t3);margin-top:3px}
.rdp-rows-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.rdp-rows-hdr{
  display:grid;grid-template-columns:100px 80px 100px 1fr 100px 130px;
  gap:0;padding:7px 18px;
  background:var(--card-alt);border-bottom:1px solid var(--border);
}
.rdp-rows-hdr span{font-size:.62rem;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.5px}
.rdp-row{
  display:grid;grid-template-columns:100px 80px 100px 1fr 100px 130px;
  gap:0;padding:10px 18px;align-items:center;
  border-bottom:1px solid var(--border-l);
}
.rdp-row:last-child{border-bottom:none}
.rdp-row:hover{background:#f0fdf4}
.rdp-row:nth-child(even){background:var(--bg)}
.rdp-row:nth-child(even):hover{background:#f0fdf4}
.rdp-date{font-family:'DM Mono',monospace;font-size:.78rem;font-weight:600;color:var(--t1)}
.rdp-hari{font-size:.76rem;color:var(--t2)}
.rdp-jam {font-family:'DM Mono',monospace;font-size:.8rem;color:var(--t1)}
.rdp-jam-std{font-size:.68rem;color:var(--t4)}
.rdp-tl  {font-family:'DM Mono',monospace;font-size:.82rem;font-weight:700}
.rdp-st  {}

@media(max-width:700px){
  #rekap-groups{grid-template-columns:1fr!important}
  .rdp-stats{grid-template-columns:repeat(3,1fr)}
  .rdp-stat:nth-child(n+4){border-top:1px solid var(--border)}
  .rdp-rows-hdr,.rdp-row{grid-template-columns:90px 70px 1fr 90px}
  .rdp-rows-hdr span:nth-child(3),.rdp-row .rdp-shift-col,
  .rdp-rows-hdr span:nth-child(5),.rdp-row .rdp-tl{display:none}
}
@media(max-width:480px){
  .rdp-stats{grid-template-columns:repeat(2,1fr)}
  .rdp-stat:nth-child(n+3){border-top:1px solid var(--border)}
}
@media(max-width:480px){
  .rc-stats-grid{grid-template-columns:repeat(2,1fr)}
  .rc-stat-box:nth-child(n+3){border-top:1px solid var(--border)}
  .rc-header{padding:12px 14px 10px}
  .rc-stat-box{padding:12px 8px}
}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><div class="brand-logo">&#9201;</div><div><div class="brand-name">SmartPresensi</div><div class="brand-sub">v4.2 &mdash; Modern Minimalis</div></div></div>
  <div class="topbar-right"><span class="db-badge" id="db-badge">&#9679; DB</span><div class="topbar-clock"><span class="dot-live"></span><span id="clk">--:--:--</span></div></div>
</header>
<nav class="page-nav">
  <button class="pnav-btn on" onclick="pilihPage('upload',this)"><i class="bi bi-cloud-upload me-1"></i>Upload &amp; Analisis</button>
  <button class="pnav-btn" onclick="pilihPage('riwayat',this)"><i class="bi bi-clock-history me-1"></i>Riwayat</button>
  <button class="pnav-btn" onclick="pilihPage('tren',this);muatTren()"><i class="bi bi-graph-up me-1"></i>Tren &amp; Grafik</button>
  <button class="pnav-btn" onclick="pilihPage('pegawai',this);muatPegawai()"><i class="bi bi-people-fill me-1"></i>Pegawai</button>
</nav>

<!-- ══ PAGE: UPLOAD ══ -->
<div class="page on" id="page-upload"><main class="wrap">
  <div class="hero"><h1>Rekap Keterlambatan <span>Pegawai</span></h1><p>Upload file fingerprint &mdash; shift terdeteksi otomatis, data dikelompokkan per shift.</p></div>
  <div class="row g-3 mb-4">
    <div class="col-lg-7"><div class="card-d h-100">
      <div class="card-hd"><div class="card-hd-l"><div class="card-hd-ico" style="background:#eff6ff;color:#3b82f6"><i class="bi bi-cloud-arrow-up-fill"></i></div><div><h5>Import File Fingerprint</h5><small>.xls &bull; .xlsx &bull; .csv &bull; Maks 64 MB</small></div></div></div>
      <div class="p-3">
        <div class="dz" id="dz"><input type="file" id="fi" accept=".xls,.xlsx,.csv" onchange="onPick(this)"/><div class="dz-ico"><i class="bi bi-file-earmark-spreadsheet-fill"></i></div><h6>Drag &amp; drop atau klik</h6><p>.xlsx &bull; .xls &bull; .csv</p></div>
        <div class="fi-ok" id="fi-ok"><i class="bi bi-check-circle-fill" style="color:var(--green)"></i><span id="fi-name" style="font-weight:500"></span><span id="fi-size" style="color:var(--t3);margin-left:auto"></span></div>
        <div class="err" id="err-box"><i class="bi bi-exclamation-triangle-fill" style="flex-shrink:0"></i><span id="err-msg"></span></div>
        <div class="prog" id="prog"><div class="prog-bar"></div><div class="prog-txt"><div class="spinner-border spinner-border-sm" style="width:11px;height:11px;color:var(--em)"></div>Menganalisis &amp; menyimpan ke database&hellip;</div></div>
        <div class="saved-notif" id="saved-notif"><i class="bi bi-check-circle-fill" style="color:var(--em)"></i><span id="saved-msg"></span></div>
        <button class="btn-main" id="btn-run" onclick="jalankan()"><i class="bi bi-play-fill me-1"></i>Mulai Analisis &amp; Simpan</button>
        <div class="info-note" style="margin-top:10px"><i class="bi bi-info-circle me-1" style="color:var(--em)"></i>Hasil disimpan ke MySQL. Gunakan filter shift di bawah untuk memilah data per kelompok shift.</div>
      </div>
    </div></div>
    <div class="col-lg-5"><div class="card-d h-100">
      <div class="card-hd"><div class="card-hd-l"><div class="card-hd-ico" style="background:#fef9c3;color:#ca8a04"><i class="bi bi-shield-check-fill"></i></div><div><h5>Aturan Jam Kerja</h5><small>Deteksi otomatis dari jam masuk</small></div></div></div>
      <div class="p-3">
        <div class="rules-g">
          <div class="rule-item"><div class="rule-lbl" style="color:#16a34a">Shift Pagi</div><div class="rule-val">07:00</div><div class="rule-sub">masuk 05:00&ndash;09:59</div></div>
          <div class="rule-item"><div class="rule-lbl" style="color:#2563eb">Shift Siang</div><div class="rule-val">10:00</div><div class="rule-sub">masuk 10:00&ndash;14:59</div></div>
          <div class="rule-item"><div class="rule-lbl" style="color:#ea580c">Shift Sore</div><div class="rule-val">15:00</div><div class="rule-sub">masuk 15:00&ndash;18:59</div></div>
          <div class="rule-item"><div class="rule-lbl" style="color:var(--purple)">Shift Malam</div><div class="rule-val">19:00</div><div class="rule-sub">masuk 19:00&ndash;04:59 &bull; lintas tengah malam</div></div>
        </div>
      </div>
    </div></div>
  </div>

  <!-- HASIL -->
  <div id="hasil" style="display:none">
    <div class="stat-grid" id="stat-grid"></div>
    <div class="card-d">
      <div class="card-hd">
        <div class="card-hd-l"><div class="card-hd-ico" style="background:#eff6ff;color:#3b82f6"><i class="bi bi-table"></i></div><div><h5>Laporan Keterlambatan</h5><small id="laporan-sub">-</small></div></div>
        <div class="tab-nav">
          <button class="tab-btn on" onclick="pilihTab('rekap',this)"><i class="bi bi-bar-chart-steps me-1"></i>Rekap</button>
          <button class="tab-btn" onclick="pilihTab('detail',this)"><i class="bi bi-list-ul me-1"></i>Detail</button>
        </div>
      </div>
      <!-- Ringkasan per shift -->
      <div class="shift-stat-grid" id="shift-stat-grid"></div>
      <!-- Filter pills -->
      <div class="shift-pills" id="shift-pills-rekap">
        <span style="font-size:.7rem;color:var(--t3);margin-right:4px"><i class="bi bi-funnel me-1"></i>Filter:</span>
        <button class="shift-pill pill-semua on" onclick="filterShift('rekap','Semua',this)"><span class="pill-dot"></span>Semua<span class="pill-count" id="pc-rekap-semua"></span></button>
        <button class="shift-pill pill-pagi"  onclick="filterShift('rekap','Shift Pagi',this)"><span class="pill-dot"></span>Shift Pagi<span class="pill-count" id="pc-rekap-pagi"></span></button>
        <button class="shift-pill pill-siang" onclick="filterShift('rekap','Shift Siang',this)"><span class="pill-dot"></span>Shift Siang<span class="pill-count" id="pc-rekap-siang"></span></button>
        <button class="shift-pill pill-sore"  onclick="filterShift('rekap','Shift Sore',this)"><span class="pill-dot"></span>Shift Sore<span class="pill-count" id="pc-rekap-sore"></span></button>
        <button class="shift-pill pill-malam" onclick="filterShift('rekap','Shift Malam',this)"><span class="pill-dot"></span>Shift Malam<span class="pill-count" id="pc-rekap-malam"></span></button>
        <button class="shift-pill pill-non"   onclick="filterShift('rekap','Non-Shift',this)"><span class="pill-dot"></span>Non-Shift<span class="pill-count" id="pc-rekap-non"></span></button>
      </div>
      <div class="tab-pane on" id="tp-rekap">
        <!-- Ringkasan 3 Grup Status -->
        <div id="rekap-groups" style="display:none;padding:14px 16px 0;gap:10px;display:none;grid-template-columns:repeat(3,1fr)"></div>
        <!-- Tabel Rekap Utama -->
        <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
          <table class="tbl rtbl" id="tbl-rekap-main">
            <thead>
              <tr>
                <th style="width:36px">#</th>
                <th>Nama Pegawai</th>
                <th>Shift</th>
                <th style="text-align:center">Terlambat</th>
                <th style="text-align:center">Pulang Cepat</th>
                <th style="text-align:center">Total Waktu</th>
                <th style="text-align:center">Rata-rata</th>
                <th style="min-width:120px">Status</th>
                <th style="width:36px"></th>
              </tr>
            </thead>
            <tbody id="tb-rekap-main"></tbody>
          </table>
        </div>
        <!-- Panel Detail Inline (muncul saat klik baris) -->
        <div id="rekap-detail-panel" style="display:none"></div>
      </div>
      <div class="tab-pane" id="tp-detail">
        <div class="shift-pills" id="shift-pills-detail">
          <span style="font-size:.7rem;color:var(--t3);margin-right:4px"><i class="bi bi-funnel me-1"></i>Filter:</span>
          <button class="shift-pill pill-semua on" onclick="filterShift('detail','Semua',this)"><span class="pill-dot"></span>Semua</button>
          <button class="shift-pill pill-pagi"  onclick="filterShift('detail','Shift Pagi',this)"><span class="pill-dot"></span>Shift Pagi</button>
          <button class="shift-pill pill-siang" onclick="filterShift('detail','Shift Siang',this)"><span class="pill-dot"></span>Shift Siang</button>
          <button class="shift-pill pill-sore"  onclick="filterShift('detail','Shift Sore',this)"><span class="pill-dot"></span>Shift Sore</button>
          <button class="shift-pill pill-malam" onclick="filterShift('detail','Shift Malam',this)"><span class="pill-dot"></span>Shift Malam</button>
          <button class="shift-pill pill-non"   onclick="filterShift('detail','Non-Shift',this)"><span class="pill-dot"></span>Non-Shift</button>
        </div>
        <div style="overflow-x:auto;-webkit-overflow-scrolling:touch;max-height:min(540px,68vh);overflow-y:auto"><table class="tbl"><thead><tr><th style="min-width:120px">Nama</th><th>Tgl</th><th>Hari</th><th>Shift</th><th>Masuk</th><th>Pulang</th><th>Status</th><th>Terlambat</th><th>Pulang Cepat</th></tr></thead><tbody id="tb-detail"></tbody></table></div>
      </div>
      <div style="padding:10px 16px;background:var(--bg);border-top:1px solid var(--border)">
        <input id="cari-nama" type="text" placeholder="🔍  Cari nama pegawai..." oninput="filterNama()" style="width:100%;max-width:360px;padding:7px 12px;border:1px solid var(--border);border-radius:var(--r2);font-size:.8rem;font-family:'Poppins',sans-serif;outline:none;background:#fff;color:var(--t1);transition:border .2s" onfocus="this.style.borderColor='var(--em)'" onblur="this.style.borderColor='var(--border)'">
      </div>
      <div class="tbl-foot"><span id="tbl-info">-</span>
        <div style="display:flex;gap:7px">
          <button class="btn-s btn-blue" onclick="exportExcel()"><i class="bi bi-file-earmark-excel-fill me-1"></i>Export Excel</button>
        </div>
      </div>
    </div>
  </div>
</main></div>

<!-- ══ PAGE: RIWAYAT ══ -->
<div class="page" id="page-riwayat"><main class="wrap">
  <div class="hero"><h1>Riwayat <span>Rekap</span></h1><p>Semua rekap tersimpan. Klik detail untuk melihat data dikelompokkan per shift.</p></div>
  <div class="card-d">
    <div class="card-hd"><div class="card-hd-l"><div class="card-hd-ico" style="background:var(--em-lt);color:var(--em-dk)"><i class="bi bi-archive-fill"></i></div><div><h5>Data Tersimpan</h5><small id="riwayat-sub">-</small></div></div><button class="btn-s btn-blue" onclick="muatRiwayat()"><i class="bi bi-arrow-clockwise me-1"></i>Refresh</button></div>
    <div class="tbl-scroll" style="min-height:200px"><div id="riwayat-body"><div class="empty-state"><i class="bi bi-hourglass-split"></i><p>Memuat&hellip;</p></div></div></div>
  </div>
</main></div>

<!-- ══ PAGE: TREN ══ -->
<div class="page" id="page-tren"><main class="wrap">
  <div class="hero">
    <h1>Tren &amp; <span>Grafik</span></h1>
    <p>Analisis tren keterlambatan per bulan, distribusi shift, dan peringkat pegawai.</p>
  </div>

  <!-- ── RINGKASAN CEPAT ── -->
  <div id="tren-summary-grid" class="tren-sum-grid mb-3"></div>

  <!-- ── BARIS 1: Tren Kejadian + Rata-rata Menit ── -->
  <div class="row g-3 mb-3">
    <!-- Tren Kejadian (lebih lebar) -->
    <div class="col-lg-8">
      <div class="card-d">
        <div class="card-hd">
          <div class="card-hd-l">
            <div class="card-hd-ico" style="background:#eff6ff;color:#3b82f6"><i class="bi bi-bar-chart-fill"></i></div>
            <div>
              <h5>Tren Kejadian Keterlambatan</h5>
              <small>Jumlah kejadian &amp; pegawai terlambat per bulan (12 bulan terakhir)</small>
            </div>
          </div>
          <span class="chart-legend-wrap" id="legend-tren"></span>
        </div>
        <div class="chart-wrap" style="height:clamp(180px,26vw,260px)">
          <div class="chart-empty" id="chart-tren-empty"><i class="bi bi-bar-chart"></i><p>Belum ada data tersimpan</p></div>
          <canvas id="chartTren" style="display:none"></canvas>
        </div>
        <div class="chart-insight" id="insight-tren"></div>
      </div>
    </div>

    <!-- Rata-rata menit -->
    <div class="col-lg-4">
      <div class="card-d h-100">
        <div class="card-hd">
          <div class="card-hd-l">
            <div class="card-hd-ico" style="background:#fef9c3;color:#ca8a04"><i class="bi bi-clock-fill"></i></div>
            <div>
              <h5>Rata-rata Keterlambatan</h5>
              <small>Menit per kejadian per bulan</small>
            </div>
          </div>
        </div>
        <div class="chart-wrap" style="height:clamp(180px,26vw,260px)">
          <div class="chart-empty" id="chart-menit-empty"><i class="bi bi-clock"></i><p>Belum ada data</p></div>
          <canvas id="chartMenit" style="display:none"></canvas>
        </div>
        <div class="chart-insight" id="insight-menit"></div>
      </div>
    </div>
  </div>

  <!-- ── BARIS 2: Distribusi Shift ── -->
  <div class="card-d mb-3">
    <div class="card-hd">
      <div class="card-hd-l">
        <div class="card-hd-ico" style="background:#cffafe;color:#0891b2"><i class="bi bi-pie-chart-fill"></i></div>
        <div>
          <h5>Distribusi Keterlambatan per Shift</h5>
          <small>Proporsi kejadian dari seluruh data tersimpan</small>
        </div>
      </div>
    </div>
    <div class="row g-0 align-items-center">
      <div class="col-md-4">
        <div class="chart-wrap" style="height:clamp(180px,24vw,240px)">
          <div class="chart-empty" id="chart-donut-empty"><i class="bi bi-pie-chart"></i><p>Belum ada data</p></div>
          <canvas id="chartDonut" style="display:none"></canvas>
        </div>
      </div>
      <div class="col-md-8">
        <div id="shift-dist-table" style="padding:14px 20px 14px 8px"></div>
      </div>
    </div>
  </div>

  <!-- ── BARIS 3: Peringkat Pegawai ── -->
  <div class="card-d mb-3">
    <div class="card-hd">
      <div class="card-hd-l">
        <div class="card-hd-ico" style="background:var(--pur-lt);color:var(--purple)"><i class="bi bi-trophy-fill"></i></div>
        <div>
          <h5>Peringkat Pegawai Paling Sering Terlambat</h5>
          <small id="pegawai-chart-sub">Akumulasi 6 bulan terakhir &mdash; warna menunjukkan shift dominan</small>
        </div>
      </div>
    </div>
    <!-- Filter Pills dengan jumlah per shift -->
    <div class="shift-pills" id="shift-pills-pegawai">
      <span style="font-size:.72rem;color:var(--t2);font-weight:600;margin-right:6px"><i class="bi bi-funnel-fill me-1"></i>Tampilkan:</span>
      <button class="shift-pill pill-semua on"  onclick="filterPegawaiChart('Semua',this)"><span class="pill-dot"></span>Semua Shift<span class="pill-count" id="pcount-semua"></span></button>
      <button class="shift-pill pill-pagi"       onclick="filterPegawaiChart('Shift Pagi',this)"><span class="pill-dot"></span>Shift Pagi<span class="pill-count" id="pcount-pagi"></span></button>
      <button class="shift-pill pill-siang"      onclick="filterPegawaiChart('Shift Siang',this)"><span class="pill-dot"></span>Shift Siang<span class="pill-count" id="pcount-siang"></span></button>
      <button class="shift-pill pill-sore"       onclick="filterPegawaiChart('Shift Sore',this)"><span class="pill-dot"></span>Shift Sore<span class="pill-count" id="pcount-sore"></span></button>
      <button class="shift-pill pill-malam"      onclick="filterPegawaiChart('Shift Malam',this)"><span class="pill-dot"></span>Shift Malam<span class="pill-count" id="pcount-malam"></span></button>
      <button class="shift-pill pill-non"        onclick="filterPegawaiChart('Non-Shift',this)"><span class="pill-dot"></span>Non-Shift<span class="pill-count" id="pcount-non"></span></button>
    </div>
    <!-- Tabel peringkat + chart -->
    <div class="row g-0">
      <!-- Tabel kiri -->
      <div class="col-lg-5" style="border-right:1px solid var(--border)">
        <div style="overflow-y:auto;max-height:380px">
          <table class="tbl" id="tbl-pegawai-rank">
            <thead><tr><th style="width:40px;background:var(--card-alt)">Rank</th><th>Nama Pegawai</th><th>Shift</th><th>Kejadian</th></tr></thead>
            <tbody id="tb-pegawai-rank"></tbody>
          </table>
        </div>
      </div>
      <!-- Grafik kanan -->
      <div class="col-lg-7">
        <div style="position:relative;height:clamp(280px,40vw,380px);padding:12px">
          <div class="chart-empty" id="chart-pegawai-empty"><i class="bi bi-person-badge"></i><p>Belum ada data</p></div>
          <canvas id="chartPegawai" style="display:none;height:100%;width:100%"></canvas>
        </div>
      </div>
    </div>
  </div>

  <!-- ── BARIS 4: Perbandingan Antar Bulan ── -->
  <div class="card-d">
    <div class="card-hd">
      <div class="card-hd-l">
        <div class="card-hd-ico" style="background:var(--em-lt);color:var(--em-dk)"><i class="bi bi-calendar3-range"></i></div>
        <div>
          <h5>Perbandingan Antar Bulan</h5>
          <small id="perbandingan-sub">Semua periode tersimpan</small>
        </div>
      </div>
    </div>
    <div id="perbandingan-body"><div class="empty-state"><i class="bi bi-hourglass-split"></i><p>Memuat&hellip;</p></div></div>
  </div>

</main></div>

<!-- MODAL DETAIL RIWAYAT -->
<div class="modal fade modal-dark" id="modalDetail" tabindex="-1"><div class="modal-dialog modal-lg modal-dialog-scrollable"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title" id="modal-title">Detail Rekap</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
  <div class="modal-body p-0">
    <!-- Filter pills dalam modal -->
    <div class="shift-pills" id="shift-pills-modal" >
      <span style="font-size:.7rem;color:var(--t3);margin-right:4px"><i class="bi bi-funnel me-1"></i>Filter:</span>
      <button class="shift-pill pill-semua on" onclick="filterShiftModal('Semua',this)"><span class="pill-dot"></span>Semua</button>
      <button class="shift-pill pill-pagi"  onclick="filterShiftModal('Shift Pagi',this)"><span class="pill-dot"></span>Shift Pagi</button>
      <button class="shift-pill pill-siang" onclick="filterShiftModal('Shift Siang',this)"><span class="pill-dot"></span>Shift Siang</button>
      <button class="shift-pill pill-sore"  onclick="filterShiftModal('Shift Sore',this)"><span class="pill-dot"></span>Shift Sore</button>
      <button class="shift-pill pill-malam" onclick="filterShiftModal('Shift Malam',this)"><span class="pill-dot"></span>Shift Malam</button>
      <button class="shift-pill pill-non"   onclick="filterShiftModal('Non-Shift',this)"><span class="pill-dot"></span>Non-Shift</button>
    </div>
    <div id="modal-body"></div>
  </div>
  <div class="modal-footer"><button type="button" class="btn-s btn-blue" data-bs-dismiss="modal">Tutup</button></div>
</div></div></div>

<!-- MODAL HAPUS -->
<div class="modal fade modal-dark" id="modalHapus" tabindex="-1"><div class="modal-dialog"><div class="modal-content">
  <div class="modal-header"><h5 class="modal-title"><i class="bi bi-trash3 me-2" style="color:var(--red)"></i>Konfirmasi Hapus</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
  <div class="modal-body" style="font-size:.85rem;padding:20px">Hapus rekap <strong id="hapus-label"></strong>? Data detail juga akan terhapus.</div>
  <div class="modal-footer gap-2"><button type="button" class="btn-s btn-red" onclick="konfirmasiHapus()"><i class="bi bi-trash3 me-1"></i>Ya, Hapus</button><button type="button" class="btn-s btn-blue" data-bs-dismiss="modal">Batal</button></div>
</div></div></div>



<!-- ══ PAGE: PEGAWAI ══ -->
<div class="page" id="page-pegawai"><main class="wrap">
  <div class="hero"><h1>Daftar <span>Pegawai</span></h1><p>Kelola pegawai dan tentukan status Normal atau Shift. Pegawai Shift menggunakan jam standar shift, bukan 07:30.</p></div>

  <!-- Info box -->
  <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:var(--r);padding:14px 18px;margin-bottom:18px;font-size:.82rem;color:var(--em-dk)">
    <b><i class="bi bi-info-circle-fill me-1"></i>Cara Kerja:</b>
    Pegawai <b>Normal</b> → jam standar masuk <b>07:30</b>, cek pulang cepat (Sen-Kam <b>15:30</b>, Jumat <b>14:30</b>).
    Pegawai <b>Shift</b> → jam standar sesuai shift terdeteksi, tidak cek pulang cepat.
  </div>

  <div class="card-d">
    <div class="card-hd">
      <div class="card-hd-l">
        <div class="card-hd-ico" style="background:var(--em-lt);color:var(--em-dk)"><i class="bi bi-people-fill"></i></div>
        <div><h5>Daftar Pegawai</h5><small id="peg-sub">-</small></div>
      </div>
      <button class="btn-s btn-em" onclick="bukaFormPegawai()"><i class="bi bi-plus-lg me-1"></i>Tambah Pegawai</button>
    </div>

    <!-- Form tambah/edit (tersembunyi) -->
    <div id="form-pegawai" style="display:none;padding:16px 18px;border-bottom:1px solid var(--border);background:var(--bg)">
      <div class="form-pegawai-grid" style="display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:10px;align-items:end">
        <div>
          <label style="font-size:.72rem;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.7px">Nama Pegawai</label>
          <input id="inp-nama" type="text" placeholder="Nama lengkap..." style="width:100%;margin-top:4px;padding:8px 11px;border:1px solid var(--border);border-radius:var(--r2);font-size:.82rem;font-family:'Poppins',sans-serif;outline:none;background:#fff;color:var(--t1)" onfocus="this.style.borderColor='var(--em)'" onblur="this.style.borderColor='var(--border)'">
        </div>
        <div>
          <label style="font-size:.72rem;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.7px">Tipe</label>
          <select id="inp-tipe" style="width:100%;margin-top:4px;padding:8px 11px;border:1px solid var(--border);border-radius:var(--r2);font-size:.82rem;font-family:'Poppins',sans-serif;outline:none;background:#fff;color:var(--t1)">
            <option value="Normal">Normal (masuk 07:30)</option>
            <option value="Shift">Shift (jam standar shift)</option>
          </select>
        </div>
        <div>
          <label style="font-size:.72rem;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.7px">Unit / Bagian</label>
          <input id="inp-unit" type="text" placeholder="opsional..." style="width:100%;margin-top:4px;padding:8px 11px;border:1px solid var(--border);border-radius:var(--r2);font-size:.82rem;font-family:'Poppins',sans-serif;outline:none;background:#fff;color:var(--t1)" onfocus="this.style.borderColor='var(--em)'" onblur="this.style.borderColor='var(--border)'">
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn-s btn-em" onclick="simpanPegawai()"><i class="bi bi-check-lg me-1"></i>Simpan</button>
          <button class="btn-s btn-red" onclick="tutupFormPegawai()"><i class="bi bi-x-lg"></i></button>
        </div>
      </div>
      <div id="peg-err" style="display:none;margin-top:8px;padding:7px 11px;background:var(--red-lt);border:1px solid #fca5a5;border-radius:var(--r2);font-size:.78rem;color:var(--red)"></div>
    </div>

    <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
      <table class="tbl" id="tbl-pegawai">
        <thead><tr>
          <th style="width:40px">No</th>
          <th>Nama Pegawai</th>
          <th>Tipe</th>
          <th>Unit / Bagian</th>
          <th>Standar Masuk</th>
          <th>Standar Pulang</th>
          <th style="width:100px">Aksi</th>
        </tr></thead>
        <tbody id="peg-tbody"><tr><td colspan="7" class="empty-state"><i class="bi bi-hourglass-split"></i><p>Memuat&hellip;</p></td></tr></tbody>
      </table>
    </div>
    <div class="tbl-foot"><span id="peg-count">-</span></div>
  </div>
</main></div>

<footer class="footer">SmartPresensi v5.0 &bull; Flask &amp; MySQL &bull; Full Feature &bull; &copy; 2026</footer>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ── STATE ──
let REKAP=[], DETAIL=[], PEGAWAI_ALL=[], chartTren=null, chartMenit=null, chartPegawai=null, chartDonut=null, hapusId=null;
const SHIFT_COLORS={'Shift Pagi':'#16a34a','Shift Siang':'#2563eb','Shift Sore':'#ea580c','Shift Malam':'#8b5cf6','Non-Shift':'#9ca3af'};

// ── CLOCK ──
(function tick(){document.getElementById('clk').textContent=new Date().toLocaleTimeString('id-ID',{hour:'2-digit',minute:'2-digit',second:'2-digit'});setTimeout(tick,1000)})();

// ── DB STATUS ──
(async function(){try{const r=await fetch('/cek-db');const d=await r.json();const el=document.getElementById('db-badge');el.className='db-badge '+(d.ok?'db-ok':'db-err');el.textContent='● '+(d.ok?'DB Terhubung':'DB Error');if(!d.ok)el.title=d.error||'';}catch(e){document.getElementById('db-badge').className='db-badge db-err';document.getElementById('db-badge').textContent='● DB Error';}})();

// ── PAGE NAV ──
function pilihPage(nama,btn){document.querySelectorAll('.page').forEach(e=>e.classList.remove('on'));document.querySelectorAll('.pnav-btn').forEach(e=>e.classList.remove('on'));document.getElementById('page-'+nama).classList.add('on');btn.classList.add('on');if(nama==='riwayat')muatRiwayat();if(nama==='pegawai')muatPegawai();}

// ── DRAG DROP ──
const dz=document.getElementById('dz');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag')});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');if(e.dataTransfer.files[0])applyFile(e.dataTransfer.files[0])});
function onPick(el){if(el.files[0])applyFile(el.files[0]);}
function applyFile(f){document.getElementById('fi-name').textContent=f.name;document.getElementById('fi-size').textContent=fmtBytes(f.size);document.getElementById('fi-ok').classList.add('show');dz.classList.add('ok');hideErr();document.getElementById('saved-notif').classList.remove('show');const dt=new DataTransfer();dt.items.add(f);document.getElementById('fi').files=dt.files;}
function fmtBytes(b){if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';return(b/1048576).toFixed(1)+' MB';}

// ── PROSES ──
async function jalankan(){
  const f=document.getElementById('fi').files[0];
  if(!f){showErr('Pilih file terlebih dahulu.');return;}
  hideErr();setLoad(true);document.getElementById('saved-notif').classList.remove('show');
  const fd=new FormData();fd.append('file',f);
  try{
    const res=await fetch('/proses',{method:'POST',body:fd});
    const data=await res.json();
    if(!res.ok||data.error){showErr(data.error||'Kesalahan server.');return;}
    REKAP=data.rekap;DETAIL=data.detail;
    // Simpan state untuk export CSV
    window._LAST_BULAN_NAMA = data.bulan_nama || '-';
    window._LAST_TAHUN      = data.tahun      || new Date().getFullYear();
    window._LAST_BULAN_INT  = data.bulan_int  || (new Date().getMonth()+1);
    window._LAST_TOTAL_PEG  = data.total_pegawai || 0;
    tampilkan(data);
    const sm=data.db_saved?(data.db_updated?`Data ${data.bulan_nama} ${data.tahun} diperbarui.`:`Data ${data.bulan_nama} ${data.tahun} disimpan ke database.`):(data.db_error?`Analisis OK, gagal simpan: ${data.db_error}`:'');
    if(sm){document.getElementById('saved-msg').textContent=sm;document.getElementById('saved-notif').classList.add('show');}
  }catch(e){showErr('Gagal: '+e.message);}
  finally{setLoad(false);}
}

// ── HELPERS ──
function shiftCls(s){if(!s)return'sh-non';const l=s.toLowerCase();if(l.includes('pagi'))return'sh-pagi';if(l.includes('siang'))return'sh-siang';if(l.includes('sore'))return'sh-sore';if(l.includes('malam'))return'sh-malam';return'sh-non';}
function pillCls(s){if(!s)return'pill-non';const l=s.toLowerCase();if(l.includes('pagi'))return'pill-pagi';if(l.includes('sore'))return'pill-sore';if(l.includes('malam'))return'pill-malam';return'pill-non';}
function fmtMenit(m){m=parseInt(m)||0;if(m<60)return m+' mnt';const j=Math.floor(m/60),s=m%60;return s===0?j+' jam':j+' jam '+s+' mnt';}

// ── RENDER HASIL ──
function tampilkan(d){
  const{rekap,detail,total_kejadian,total_menit_str,total_menit_raw,total_pegawai,rata_str,format_file,shift_stat}=d;
  const maxJ=rekap.length?rekap[0].jumlah:1;

  // Stat cards
  document.getElementById('stat-grid').innerHTML=`
    <div class="stat c-b"><div class="stat-lbl">Pegawai Terlambat</div><div class="stat-val">${rekap.length}</div><div class="stat-sub">dari ${total_pegawai} terdeteksi</div></div>
    <div class="stat c-r"><div class="stat-lbl">Total Kejadian</div><div class="stat-val">${total_kejadian}</div><div class="stat-sub">keterlambatan</div></div>
    <div class="stat c-y"><div class="stat-lbl">Total Waktu</div><div class="stat-val" style="font-size:1.2rem">${total_menit_str}</div><div class="stat-sub">${total_menit_raw} menit</div></div>
    <div class="stat c-p"><div class="stat-lbl">Rata-rata/Kejadian</div><div class="stat-val" style="font-size:1.2rem">${rata_str}</div><div class="stat-sub">per keterlambatan</div></div>`;

  // Shift stat cards
  const ssMap={'Shift Pagi':'ss-pagi','Shift Siang':'ss-siang','Shift Sore':'ss-sore','Shift Malam':'ss-malam','Non-Shift':'ss-non'};
  let ssHtml='';
  (shift_stat||[]).forEach(s=>{
    const cls=ssMap[s.shift]||'ss-non';
    ssHtml+=`<div class="shift-stat ${cls}">
      <div class="ss-label">${s.shift}</div>
      <div class="ss-nums">
        <div><div class="ss-item">Pegawai</div><div class="ss-val">${s.pegawai}</div></div>
        <div><div class="ss-item">Kejadian</div><div class="ss-val">${s.kejadian}</div></div>
        <div><div class="ss-item">Total</div><div class="ss-val" style="font-size:.75rem">${s.total_fmt}</div></div>
        <div><div class="ss-item">Rata-rata</div><div class="ss-val" style="font-size:.75rem">${s.rata_fmt}</div></div>
      </div>
    </div>`;
  });
  document.getElementById('shift-stat-grid').innerHTML=ssHtml;

  // Hitung jumlah per shift untuk pill count
  const shiftCount={};
  rekap.forEach(r=>{shiftCount[r.tipe]=(shiftCount[r.tipe]||0)+1;});
  document.getElementById('pc-rekap-semua').textContent=' ('+rekap.length+')';
  document.getElementById('pc-rekap-pagi').textContent=' ('+(shiftCount['Shift Pagi']||0)+')';
  document.getElementById('pc-rekap-siang').textContent=' ('+(shiftCount['Shift Siang']||0)+')';
  document.getElementById('pc-rekap-sore').textContent=' ('+(shiftCount['Shift Sore']||0)+')';
  document.getElementById('pc-rekap-malam').textContent=' ('+(shiftCount['Shift Malam']||0)+')';
  document.getElementById('pc-rekap-non').textContent=' ('+(shiftCount['Non-Shift']||0)+')';

  // ── Render Rekap sebagai Kartu
  const DMAP={};
  detail.forEach(d=>{if(!DMAP[d.nama])DMAP[d.nama]=[];DMAP[d.nama].push(d);});
  const DID2={Mon:'Sen',Tue:'Sel',Wed:'Rab',Thu:'Kam',Fri:'Jum',Sat:'Sab',Sun:'Min'};
  // ── Grup Ringkasan 3 Status ──────────────────────────
  const grpEl = document.getElementById('rekap-groups');
  let gKritis=[], gWaspada=[], gBaik=[];
  rekap.forEach(row=>{
    const r = row.jumlah>0 ? Math.round(row.total_menit/row.jumlah) : 0;
    if(r>60) gKritis.push(row);
    else if(r>30) gWaspada.push(row);
    else gBaik.push(row);
  });
  grpEl.style.display = rekap.length ? 'grid' : 'none';
  grpEl.innerHTML = `
    <div class="rgrp rgrp-kritis" onclick="filterGrup('kritis')" id="grp-kritis" title="Klik untuk filter">
      <div class="rgrp-num">${gKritis.length}</div>
      <div class="rgrp-lbl">&#9888; Perlu Perhatian Serius</div>
      <div class="rgrp-sub">Rata-rata &gt; 60 menit</div>
    </div>
    <div class="rgrp rgrp-waspada" onclick="filterGrup('waspada')" id="grp-waspada" title="Klik untuk filter">
      <div class="rgrp-num">${gWaspada.length}</div>
      <div class="rgrp-lbl">&#128998; Perlu Perhatian</div>
      <div class="rgrp-sub">Rata-rata 30–60 menit</div>
    </div>
    <div class="rgrp rgrp-baik" onclick="filterGrup('baik')" id="grp-baik" title="Klik untuk filter">
      <div class="rgrp-num">${gBaik.length}</div>
      <div class="rgrp-lbl">&#10003; Normal</div>
      <div class="rgrp-sub">Rata-rata &lt; 30 menit</div>
    </div>`;

  // ── Tabel Rekap Utama ─────────────────────────────────
  const tbody = document.getElementById('tb-rekap-main');
  tbody.innerHTML = '';
  const panelEl = document.getElementById('rekap-detail-panel');
  panelEl.style.display = 'none';
  let selectedRow = null;

  const dayLabel={Mon:'Senin',Tue:'Selasa',Wed:'Rabu',Thu:'Kamis',Fri:'Jumat',Sat:'Sabtu',Sun:'Minggu'};

  rekap.forEach((row, i) => {
    const rata = row.jumlah>0 ? Math.round(row.total_menit/row.jumlah) : 0;
    const rstr = row.rata_fmt||(rata+' mnt');
    const pct  = Math.max(3, Math.round(row.jumlah/maxJ*100));
    const sc   = shiftCls(row.tipe);

    let sCls, sLbl, trCls;
    if(rata>60){sCls='kritis'; sLbl='Perlu Perhatian Serius'; trCls='rtr-kritis';}
    else if(rata>30){sCls='waspada'; sLbl='Perlu Perhatian'; trCls='rtr-waspada';}
    else{sCls='baik'; sLbl='Normal'; trCls='rtr-baik';}

    const rTxt = i===0?'🥇':i===1?'🥈':i===2?'🥉':(i+1);
    const rCls = i===0?'gold':i===1?'silver':i===2?'bronze':'';
    const tipeLbl = row.tipe_pegawai==='Shift'
      ?`<span class="badge-shift sh-sore" style="font-size:.6rem;padding:1px 6px">Shift</span>`
      :`<span style="font-size:.62rem;color:var(--t3)">Normal</span>`;

    const tr = document.createElement('tr');
    tr.className = trCls;
    tr.setAttribute('data-shift', row.tipe);
    tr.setAttribute('data-idx', i);
    tr.innerHTML = `
      <td><span class="rtr-rank ${rCls}">${rTxt}</span></td>
      <td>
        <div class="rtr-nama">${row.nama}</div>
        <div class="rtr-tipe">${tipeLbl} &nbsp;<span class="badge-shift ${sc}" style="font-size:.6rem;padding:1px 6px">${row.tipe}</span></div>
      </td>
      <td><span class="badge-shift ${sc}" style="font-size:.65rem">${row.tipe}</span></td>
      <td style="text-align:center">
        <div class="rtr-num" style="color:#dc2626">${row.jumlah_terlambat||row.jumlah}</div>
        <div style="font-size:.62rem;color:var(--t3)">kali</div>
      </td>
      <td style="text-align:center">
        <div class="rtr-num" style="color:#d97706">${row.jumlah_pulang_cepat||0}</div>
        <div style="font-size:.62rem;color:var(--t3)">kali</div>
      </td>
      <td style="text-align:center">
        <div style="font-size:.85rem;font-weight:700">${row.total_fmt}</div>
        <div style="font-size:.62rem;color:var(--t3)">${row.total_menit} mnt</div>
      </td>
      <td style="text-align:center">
        <div style="font-size:.88rem;font-weight:800;color:${rata>60?'#dc2626':rata>30?'#d97706':'#059669'}">${rstr}</div>
        <div class="rtr-bar"><div class="rtr-bar-fill" style="width:${pct}%;background:${rata>60?'#ef4444':rata>30?'#f59e0b':'#10b981'}"></div></div>
      </td>
      <td><span class="rtr-status ${sCls}"><span class="rtr-dot ${sCls}"></span>${sLbl}</span></td>
      <td><i class="bi bi-chevron-down rtr-chevron" id="chev-${i}"></i></td>`;

    tr.addEventListener('click', ()=> bukaDetailPegawai(row, i, tr, rata, rstr, pct, sCls, sLbl, sc));
    tbody.appendChild(tr);
  });

  // Fungsi buka detail inline
  window.bukaDetailPegawai = function(row, idx, trEl, rata, rstr, pct, sCls, sLbl, sc) {
    // Toggle — klik lagi untuk tutup
    if(selectedRow===idx){
      selectedRow=null;
      panelEl.style.display='none';
      trEl.classList.remove('rtr-selected');
      const chev=document.getElementById('chev-'+idx);
      if(chev)chev.classList.remove('open');
      return;
    }
    // Tutup yang lama
    if(selectedRow!==null){
      const oldTr=tbody.querySelector('tr[data-idx="'+selectedRow+'"]');
      if(oldTr)oldTr.classList.remove('rtr-selected');
      const oldChev=document.getElementById('chev-'+selectedRow);
      if(oldChev)oldChev.classList.remove('open');
    }
    selectedRow=idx;
    trEl.classList.add('rtr-selected');
    const chev=document.getElementById('chev-'+idx);
    if(chev)chev.classList.add('open');

    const tipeLbl = row.tipe_pegawai==='Shift'
      ?`<span class="badge-shift sh-sore" style="font-size:.65rem;padding:2px 8px">Shift</span>`
      :`<span style="font-size:.67rem;color:var(--t3);background:var(--card-alt);padding:2px 8px;border-radius:4px;border:1px solid var(--border)">Normal</span>`;

    // Baris riwayat kejadian
    const dRows = (DMAP[row.nama]||[]);
    const dHtml = dRows.map(d=>{
      const stL={'Terlambat':'kritis','Pulang Cepat':'waspada','Terlambat+Pulang Cepat':'kritis'};
      const lv = stL[d.status]||'baik';
      const stColor = lv==='kritis'?'#dc2626':lv==='waspada'?'#d97706':'#059669';
      const stBg    = lv==='kritis'?'#fee2e2':lv==='waspada'?'#fef3c7':'#d1fae5';
      const tlTxt   = d.selisih>0
        ? `<span style="font-weight:800;color:${d.selisih>60?'#dc2626':d.selisih>30?'#d97706':'#059669'};font-family:'DM Mono',monospace">+${d.selisih} mnt</span>`
        : `<span style="color:var(--t4)">&#8212;</span>`;
      const pcNote  = d.selisih_pulang>0
        ? `<div style="font-size:.67rem;color:#d97706;margin-top:2px">Pulang cepat &#8722;${d.selisih_pulang} mnt</div>` : '';
      return `<div class="rdp-row">
        <span class="rdp-date">${d.tanggal}</span>
        <span class="rdp-hari">${dayLabel[d.hari]||d.hari||'&#8212;'}</span>
        <span class="rdp-shift-col"><span class="badge-shift ${shiftCls(d.shift)}" style="font-size:.6rem;padding:1px 6px">${d.shift}</span></span>
        <span><span class="rdp-jam">${d.jam_masuk} &#8594; ${d.jam_pulang||'&#8212;'}</span> <span class="rdp-jam-std">(std: ${d.seharusnya})</span>${pcNote}</span>
        <span class="rdp-tl">${tlTxt}</span>
        <span class="rdp-st"><span style="background:${stBg};color:${stColor};font-size:.7rem;font-weight:700;padding:4px 10px;border-radius:6px;display:inline-block">${d.status}</span></span>
      </div>`;
    }).join('');

    panelEl.innerHTML = `
      <div class="rdp-header">
        <div>
          <div class="rdp-title">${row.nama}</div>
          <div style="margin-top:5px;display:flex;gap:6px;flex-wrap:wrap">${tipeLbl}<span class="badge-shift ${sc}" style="font-size:.65rem;padding:2px 8px">${row.tipe}</span></div>
        </div>
        <span class="rtr-status ${sCls}" style="font-size:.78rem;padding:6px 14px"><span class="rtr-dot ${sCls}"></span>${sLbl}</span>
      </div>
      <div class="rdp-stats">
        <div class="rdp-stat">
          <div class="rdp-stat-lbl">Terlambat</div>
          <div class="rdp-stat-val" style="color:#dc2626">${row.jumlah_terlambat||row.jumlah} kali</div>
          <div class="rdp-stat-sub">kejadian masuk</div>
        </div>
        <div class="rdp-stat">
          <div class="rdp-stat-lbl">Pulang Cepat</div>
          <div class="rdp-stat-val" style="color:#d97706">${row.jumlah_pulang_cepat||0} kali</div>
          <div class="rdp-stat-sub">lebih awal</div>
        </div>
        <div class="rdp-stat">
          <div class="rdp-stat-lbl">Total Kejadian</div>
          <div class="rdp-stat-val">${row.jumlah} kali</div>
          <div class="rdp-stat-sub">semua pelanggaran</div>
        </div>
        <div class="rdp-stat">
          <div class="rdp-stat-lbl">Total Waktu</div>
          <div class="rdp-stat-val" style="font-size:.95rem">${row.total_fmt}</div>
          <div class="rdp-stat-sub">${row.total_menit} menit</div>
        </div>
        <div class="rdp-stat">
          <div class="rdp-stat-lbl">Rata-rata</div>
          <div class="rdp-stat-val" style="font-size:.95rem;color:${rata>60?'#dc2626':rata>30?'#d97706':'#059669'}">${rstr}</div>
          <div class="rdp-stat-sub">per kejadian</div>
        </div>
      </div>
      ${dRows.length>0?`
      <div class="rdp-rows-wrap">
        <div class="rdp-rows-hdr">
          <span>Tanggal</span><span>Hari</span><span>Shift</span>
          <span>Jam Masuk &#8594; Pulang</span><span>Terlambat</span><span>Status</span>
        </div>
        ${dHtml}
      </div>`:'<div style="padding:16px 18px;color:var(--t3);font-size:.82rem">Tidak ada riwayat detail.</div>'}`;

    panelEl.style.display = 'block';
    // Scroll ke panel
    setTimeout(()=>panelEl.scrollIntoView({behavior:'smooth',block:'nearest'}), 50);
  };

  // Filter grup (klik kotak merah/kuning/hijau)
  window.filterGrup = function(level){
    const grps=['kritis','waspada','baik'];
    grps.forEach(g=>{
      const el=document.getElementById('grp-'+g);
      if(el) el.classList.toggle('active', g===level);
    });
    // Cek apakah klik yang sama (toggle off)
    const isActive = document.getElementById('grp-'+level)?.classList.contains('active');
    tbody.querySelectorAll('tr').forEach(tr=>{
      const sCls = tr.className;
      const match = sCls.includes('rtr-'+level);
      tr.style.display = (!isActive || match) ? '' : 'none';
    });
    panelEl.style.display='none';
    selectedRow=null;
    document.getElementById('tbl-info').textContent = isActive
      ? `Filter: ${level==='kritis'?'Perlu Perhatian Serius':level==='waspada'?'Perlu Perhatian':'Normal'}`
      : `${rekap.length} pegawai terlambat`;
  };

  // Tabel detail
  const tbd=document.getElementById('tb-detail');tbd.innerHTML='';
  const dayID={Mon:'Senin',Tue:'Selasa',Wed:'Rabu',Thu:'Kamis',Fri:'Jumat',Sat:'Sabtu',Sun:'Minggu'};
  detail.forEach((d,di)=>{
    const sc=shiftCls(d.shift);
    const stM={'Terlambat':'bn-red','Pulang Cepat':'bn-yel','Terlambat+Pulang Cepat':'bn-red','Tepat Waktu':'bn-grn'};
    const stCls=stM[d.status]||'bn-grn';
    const tpB=d.tipe_pegawai==='Shift'?'<span class="badge-shift sh-sore" style="font-size:.58rem;padding:1px 5px;margin-left:3px">S</span>':'';
    const tlTxt=d.selisih>0?`<span class="badge-n ${d.selisih>60?'bn-red':d.selisih>30?'bn-yel':'bn-grn'}" style="font-size:.72rem">+${d.selisih} mnt</span>`:'<span style="color:var(--t4)">&#8212;</span>';
    const pcTxt=d.selisih_pulang>0?`<span class="badge-n bn-yel" style="font-size:.72rem">-${d.selisih_pulang} mnt</span>`:'<span style="color:var(--t4)">&#8212;</span>';
    tbd.innerHTML+=`<tr data-shift="${d.shift}" style="${di%2?'background:var(--bg)':''}">
      <td style="font-weight:600;white-space:nowrap">${d.nama}${tpB}</td>
      <td style="font-family:'DM Mono',monospace;font-size:.75rem;white-space:nowrap">${d.tanggal}</td>
      <td style="font-size:.74rem;color:var(--t2);white-space:nowrap">${dayID[d.hari]||d.hari||'&#8212;'}</td>
      <td><span class="badge-shift ${sc}" style="font-size:.65rem">${d.shift}</span></td>
      <td style="font-family:'DM Mono',monospace;font-size:.8rem;font-weight:600">${d.jam_masuk}</td>
      <td style="font-family:'DM Mono',monospace;font-size:.8rem">${d.jam_pulang||'&#8212;'}</td>
      <td><span class="badge-n ${stCls}" style="font-size:.7rem">${d.status||'&#8212;'}</span></td>
      <td>${tlTxt}</td>
      <td>${pcTxt}</td>
    </tr>`;
  });

  document.getElementById('laporan-sub').textContent=`${rekap.length} pegawai \u2022 ${total_kejadian} kejadian \u2022 ${format_file}`;
  document.getElementById('tbl-info').textContent=`${rekap.length} pegawai terlambat \u2022 ${detail.length} kejadian`;
  document.getElementById('hasil').style.display='block';
  document.getElementById('hasil').scrollIntoView({behavior:'smooth',block:'start'});
  // Reset filter ke Semua
  resetPills('shift-pills-rekap');resetPills('shift-pills-detail');
}

// ── TOGGLE DETAIL CARD ──
function toggleDetail(cid,btn){
  const r=document.getElementById(cid+'-rows');if(!r)return;
  r.classList.toggle('open');
  const open=r.classList.contains('open');
  const n=r.querySelectorAll('.rc-row-item').length;
  btn.innerHTML=open?'<i class="bi bi-chevron-up"></i> Sembunyikan riwayat':`<i class="bi bi-chevron-down"></i> Lihat ${n} riwayat kejadian`;
}

// ── FILTER SHIFT ──
function filterShift(tabel, shift, btn){
  const pillContainer=tabel==='rekap'?'shift-pills-rekap':'shift-pills-detail';
  resetPills(pillContainer);btn.classList.add('on');
  if(tabel==='rekap'){
    let vis=0;
    document.querySelectorAll('#tb-rekap-main tr').forEach(tr=>{
      const ts=tr.getAttribute('data-shift')||'';
      const show=(shift==='Semua'||ts===shift);
      tr.style.display=show?'':'none';
      if(show)vis++;
    });
    document.getElementById('rekap-detail-panel').style.display='none';
    document.getElementById('tbl-info').textContent=`Menampilkan ${vis} pegawai ${shift==='Semua'?'(semua shift)':'('+shift+')'}`;
  } else {
    document.getElementById('tb-detail').querySelectorAll('tr').forEach(tr=>{
      const ts=tr.getAttribute('data-shift')||'';
      tr.classList.toggle('tr-hidden',shift!=='Semua'&&ts!==shift);
    });
  }
}

function filterShiftModal(shift, btn){
  resetPills('shift-pills-modal');btn.classList.add('on');
  document.querySelectorAll('#modal-body tr[data-shift]').forEach(tr=>{
    const ts=tr.getAttribute('data-shift')||'';
    tr.classList.toggle('tr-hidden',shift!=='Semua'&&ts!==shift);
  });
}

function filterPegawai(shift, btn){
  resetPills('shift-pills-pegawai');btn.classList.add('on');
  renderChartPegawai(PEGAWAI_ALL, shift);
}

function resetPills(containerId){
  document.querySelectorAll('#'+containerId+' .shift-pill').forEach(p=>p.classList.remove('on'));
}

// ── TABS ──
function pilihTab(nama,btn){document.querySelectorAll('.tab-pane').forEach(e=>e.classList.remove('on'));document.querySelectorAll('.tab-btn').forEach(e=>e.classList.remove('on'));document.getElementById('tp-'+nama).classList.add('on');btn.classList.add('on');}

// ── EXPORT CSV ──
function exportExcel(){
  if(!REKAP.length){ alert('Tidak ada data untuk diexport.'); return; }
  if(typeof XLSX === 'undefined'){ alert('Library Excel belum siap, coba beberapa detik lagi.'); return; }

  // ── Helpers ─────────────────────────────────────────────
  function fmtM(m){
    m=parseInt(m||0);
    if(!m) return '0 menit';
    const j=Math.floor(m/60), s=m%60;
    return s===0 ? j+' jam' : j>0 ? j+' jam '+s+' menit' : s+' menit';
  }
  function nowStr(){
    const d=new Date();
    return [d.getFullYear(),String(d.getMonth()+1).padStart(2,'0'),String(d.getDate()).padStart(2,'0')].join('-')
      +' '+[String(d.getHours()).padStart(2,'0'),String(d.getMinutes()).padStart(2,'0')].join(':');
  }
  // Style helper — semua styling via cell style object
  function cs(bold,bg,color,align,border,wrap){
    const s={};
    s.font={name:'Calibri',sz:11};
    if(bold) s.font.bold=true;
    if(color) s.font.color={rgb:color};
    if(bg) s.fill={fgColor:{rgb:bg},patternType:'solid'};
    s.alignment={horizontal:align||'left',vertical:'center',wrapText:!!wrap};
    if(border){
      const thin={style:'thin',color:{rgb:'DDDDDD'}};
      s.border={top:thin,bottom:thin,left:thin,right:thin};
    }
    return s;
  }

  const bulan   = window._LAST_BULAN_NAMA || '-';
  const tahun   = window._LAST_TAHUN      || new Date().getFullYear();
  const dicetak = nowStr();
  const HARI    = {Mon:'Senin',Tue:'Selasa',Wed:'Rabu',Thu:'Kamis',Fri:'Jumat',Sat:'Sabtu',Sun:'Minggu'};

  // ════════════════════════════════════════════════════════
  // SHEET 1 — RINGKASAN REKAP
  // ════════════════════════════════════════════════════════
  const wb  = XLSX.utils.book_new();

  // Hitung total
  const totalKej = REKAP.reduce((s,r)=>s+r.jumlah,0);
  const totalMnt = REKAP.reduce((s,r)=>s+r.total_menit,0);
  const avgMnt   = totalKej>0 ? Math.round(totalMnt/totalKej) : 0;

  // Hitung per shift
  const shiftSumm={};
  REKAP.forEach(r=>{
    const t=r.tipe||'Non-Shift';
    if(!shiftSumm[t]) shiftSumm[t]={peg:0,kej:0,mnt:0};
    shiftSumm[t].peg++; shiftSumm[t].kej+=r.jumlah; shiftSumm[t].mnt+=r.total_menit;
  });

  // Build aoa (array of arrays) untuk sheet 1
  const aoa1=[
    ['LAPORAN REKAPITULASI KETERLAMBATAN PEGAWAI'],
    ['Periode: '+bulan+' '+tahun+'     |     Dicetak: '+dicetak],
    [],
    ['RINGKASAN UMUM'],
    ['Keterangan','Nilai'],
    ['Total Pegawai Terlambat', REKAP.length],
    ['Total Kejadian Terlambat', totalKej],
    ['Total Waktu Terlambat (menit)', totalMnt],
    ['Total Waktu Terlambat', fmtM(totalMnt)],
    ['Rata-rata per Kejadian', fmtM(avgMnt)],
    [],
    ['RINGKASAN PER SHIFT'],
    ['Jenis Shift','Jumlah Pegawai','Total Kejadian','Total Waktu (menit)','Total Waktu','Rata-rata/Kejadian'],
  ];
  ['Shift Pagi','Shift Siang','Shift Sore','Shift Malam','Non-Shift'].forEach(sh=>{
    if(shiftSumm[sh]){
      const d=shiftSumm[sh];
      const rata=d.kej>0?Math.round(d.mnt/d.kej):0;
      aoa1.push([sh, d.peg, d.kej, d.mnt, fmtM(d.mnt), fmtM(rata)]);
    }
  });
  aoa1.push([]);
  aoa1.push(['PERINGKAT KETERLAMBATAN PEGAWAI (Diurutkan: Terbanyak → Terendah)']);
  aoa1.push(['No','Nama Pegawai','Jenis Shift','Jumlah Terlambat','Total Menit','Total Waktu','Rata-rata/Kejadian','Keterangan']);
  REKAP.forEach((r,i)=>{
    const rata=r.jumlah>0?Math.round(r.total_menit/r.jumlah):0;
    const ket=rata>60?'Perlu Perhatian Serius':rata>30?'Perlu Perhatian':'Normal';
    aoa1.push([i+1, r.nama, r.tipe||'Non-Shift', r.jumlah, r.total_menit, fmtM(r.total_menit), fmtM(rata), ket]);
  });

  const ws1 = XLSX.utils.aoa_to_sheet(aoa1);

  // Lebar kolom sheet 1
  ws1['!cols']=[{wch:4},{wch:35},{wch:16},{wch:18},{wch:16},{wch:20},{wch:20},{wch:22}];

  // Merge judul
  ws1['!merges']=[
    {s:{r:0,c:0},e:{r:0,c:7}},
    {s:{r:1,c:0},e:{r:1,c:7}},
    {s:{r:3,c:0},e:{r:3,c:7}},
    {s:{r:11,c:0},e:{r:11,c:7}},
  ];
  // Hitung baris header tabel peringkat (setelah ringkasan per shift)
  const headerRekap = 11 + 1 + Object.keys(shiftSumm).length + 2;
  ws1['!merges'].push({s:{r:headerRekap,c:0},e:{r:headerRekap,c:7}});

  XLSX.utils.book_append_sheet(wb, ws1, 'Ringkasan');

  // ════════════════════════════════════════════════════════
  // SHEET 2 — DETAIL KEJADIAN PER HARI
  // ════════════════════════════════════════════════════════
  const aoa2=[
    ['DETAIL KEJADIAN KETERLAMBATAN — '+bulan+' '+tahun],
    ['Total: '+DETAIL.length+' kejadian dari '+REKAP.length+' pegawai'],
    [],
    ['No','Nama Pegawai','Tipe Pegawai','Tanggal','Hari','Jenis Shift','Jam Masuk','Jam Pulang','Std Masuk','Std Pulang','Terlambat (menit)','Pulang Cepat (menit)','Durasi Terlambat','Durasi Pulang Cepat','Status'],
  ];
  DETAIL.forEach((d,i)=>{
    aoa2.push([
      i+1, d.nama, d.tipe_pegawai||'Normal',
      d.tanggal, HARI[d.hari]||d.hari||'-',
      d.shift||'Non-Shift',
      d.jam_masuk||'-', d.jam_pulang||'-',
      d.seharusnya||'-', d.std_pulang||'-',
      d.selisih||0, d.selisih_pulang||0,
      fmtM(d.selisih||0), fmtM(d.selisih_pulang||0),
      d.status||'-'
    ]);
  });

  const ws2=XLSX.utils.aoa_to_sheet(aoa2);
  ws2['!cols']=[{wch:4},{wch:30},{wch:10},{wch:13},{wch:9},{wch:13},{wch:10},{wch:10},{wch:10},{wch:12},{wch:16},{wch:16},{wch:16},{wch:16},{wch:22}];
  ws2['!merges']=[
    {s:{r:0,c:0},e:{r:0,c:14}},
    {s:{r:1,c:0},e:{r:1,c:14}},
  ];
  XLSX.utils.book_append_sheet(wb, ws2, 'Detail Kejadian');

  // ════════════════════════════════════════════════════════
  // SHEET 3 — ANALISIS HARIAN & PER PEGAWAI
  // ════════════════════════════════════════════════════════
  const hariCount={};
  DETAIL.forEach(d=>{
    const h=HARI[d.hari]||d.hari||'Lainnya';
    if(!hariCount[h]) hariCount[h]={kej:0,mnt:0};
    hariCount[h].kej++; hariCount[h].mnt+=d.selisih||0;
  });

  const aoa3=[
    ['ANALISIS KETERLAMBATAN — '+bulan+' '+tahun],
    [],
    ['DISTRIBUSI PER HARI'],
    ['Hari','Jumlah Kejadian','Total Menit','Total Waktu','Rata-rata/Kejadian'],
  ];
  ['Senin','Selasa','Rabu','Kamis','Jumat','Sabtu','Minggu'].forEach(h=>{
    if(hariCount[h]){
      const d=hariCount[h];
      const rata=d.kej>0?Math.round(d.mnt/d.kej):0;
      aoa3.push([h, d.kej, d.mnt, fmtM(d.mnt), fmtM(rata)]);
    }
  });
  aoa3.push([]);
  aoa3.push(['RINGKASAN STATUS PEGAWAI']);
  aoa3.push(['No','Nama Pegawai','Jenis Shift','Jumlah Terlambat','Total Menit','Rata-rata','Status Frekuensi']);
  REKAP.forEach((r,i)=>{
    const rata=r.jumlah>0?Math.round(r.total_menit/r.jumlah):0;
    const status=r.jumlah>=10?'Sering (>=10x)':r.jumlah>=5?'Sedang (5-9x)':'Jarang (<5x)';
    aoa3.push([i+1, r.nama, r.tipe||'Non-Shift', r.jumlah, r.total_menit, rata, status]);
  });

  const ws3=XLSX.utils.aoa_to_sheet(aoa3);
  ws3['!cols']=[{wch:5},{wch:35},{wch:16},{wch:18},{wch:14},{wch:12},{wch:18}];
  ws3['!merges']=[{s:{r:0,c:0},e:{r:0,c:6}}];
  XLSX.utils.book_append_sheet(wb, ws3, 'Analisis');

  // ════════════════════════════════════════════════════════
  // DOWNLOAD
  // ════════════════════════════════════════════════════════
  const fname='SmartPresensi_'+bulan+'_'+tahun+'.xlsx';
  XLSX.writeFile(wb, fname);
}


// ── RIWAYAT ──
async function muatRiwayat(){
  document.getElementById('riwayat-body').innerHTML='<div class="empty-state"><i class="bi bi-hourglass-split"></i><p>Memuat&hellip;</p></div>';
  try{
    const res=await fetch('/riwayat');const data=await res.json();
    document.getElementById('riwayat-sub').textContent=`${data.length} rekap tersimpan`;
    if(!data.length){document.getElementById('riwayat-body').innerHTML='<div class="empty-state"><i class="bi bi-inbox"></i><p>Belum ada data.</p></div>';return;}
    let html=`<table class="tbl"><thead><tr><th>Bulan</th><th>File</th><th>Pegawai Terlambat</th><th>Kejadian</th><th>Total Waktu</th><th>Rata-rata</th><th>Diupload</th><th style="width:100px">Aksi</th></tr></thead><tbody>`;
    data.forEach(r=>{html+=`<tr><td style="font-weight:600">${r.bulan_nama} ${r.tahun}</td><td style="font-size:.73rem;color:var(--t2);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.nama_file}</td><td><span class="badge-n bn-red">${r.total_terlambat} org</span></td><td>${r.total_kejadian}</td><td style="font-family:'DM Mono',monospace;font-size:.76rem">${fmtMenit(r.total_menit)}</td><td style="font-family:'DM Mono',monospace;font-size:.76rem">${fmtMenit(r.rata_menit)}</td><td style="font-size:.72rem;color:var(--t3)">${r.dibuat_pada_str}</td><td><div style="display:flex;gap:5px"><button class="btn-s btn-blue" onclick="lihatDetail(${r.id},'${r.bulan_nama} ${r.tahun}')"><i class="bi bi-eye"></i></button><button class="btn-s btn-red" onclick="mintaHapus(${r.id},'${r.bulan_nama} ${r.tahun}')"><i class="bi bi-trash3"></i></button></div></td></tr>`;});
    html+='</tbody></table>';document.getElementById('riwayat-body').innerHTML=html;
  }catch(e){document.getElementById('riwayat-body').innerHTML=`<div class="empty-state"><p>Gagal: ${e.message}</p></div>`;}
}

async function lihatDetail(id,label){
  document.getElementById('modal-title').textContent='Detail Rekap \u2014 '+label;
  document.getElementById('modal-body').innerHTML='<div class="empty-state"><i class="bi bi-hourglass-split"></i><p>Memuat&hellip;</p></div>';
  resetPills('shift-pills-modal');
  document.querySelector('#shift-pills-modal .pill-semua').classList.add('on');
  const m=new bootstrap.Modal(document.getElementById('modalDetail'));m.show();
  try{
    const res=await fetch(`/riwayat/${id}`);const data=await res.json();
    if(!data.detail||!data.detail.length){document.getElementById('modal-body').innerHTML='<div class="empty-state"><i class="bi bi-inbox"></i><p>Tidak ada data.</p></div>';return;}
    // Ringkasan shift di modal
    const shiftSumm={};
    data.detail.forEach(r=>{if(!shiftSumm[r.tipe_shift])shiftSumm[r.tipe_shift]={peg:0,kej:0};shiftSumm[r.tipe_shift].peg++;shiftSumm[r.tipe_shift].kej+=r.jumlah;});
    const ssMap={'Shift Pagi':'ss-pagi','Shift Siang':'ss-siang','Shift Sore':'ss-sore','Shift Malam':'ss-malam','Non-Shift':'ss-non'};
    let ssHtml='<div class="shift-stat-grid" style="padding:12px 16px;border-bottom:1px solid var(--border)">';
    Object.entries(shiftSumm).forEach(([s,v])=>{ssHtml+=`<div class="shift-stat ${ssMap[s]||'ss-non'}"><div class="ss-label">${s}</div><div class="ss-nums"><div><div class="ss-item">Pegawai</div><div class="ss-val">${v.peg}</div></div><div><div class="ss-item">Kejadian</div><div class="ss-val">${v.kej}</div></div></div></div>`;});
    ssHtml+='</div>';
    let tblHtml=`<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;max-height:420px;overflow-y:auto"><table class="tbl"><thead><tr><th>No</th><th>Nama</th><th>Shift</th><th>Kejadian</th><th>Total Waktu</th><th>Rata-rata</th></tr></thead><tbody>`;
    data.detail.forEach((r,i)=>{
    const sc=shiftCls(r.tipe_shift);
    const rata=r.jumlah>0?Math.round(r.total_menit/r.jumlah):0;
    const pcBadge=(r.jumlah_pulang_cepat||0)>0?`<span class="badge-n bn-yel" style="font-size:.65rem;margin-left:2px">${r.jumlah_pulang_cepat}x pulang cepat</span>`:'';
    tblHtml+=`<tr data-shift="${r.tipe_shift}">
      <td style="color:var(--t3)">${i+1}</td>
      <td style="font-weight:600">${r.nama} <span class="badge-n ${r.tipe_pegawai==='Shift'?'bn-yel':'bn-grn'}" style="font-size:.6rem">${r.tipe_pegawai||'Normal'}</span></td>
      <td><span class="badge-shift ${sc}">${r.tipe_shift}</span></td>
      <td><span class="badge-n bn-red">${r.jumlah_terlambat||r.jumlah}&times; terlambat</span>${pcBadge}</td>
      <td style="font-family:'DM Mono',monospace;font-size:.76rem">${fmtMenit(r.total_menit)}</td>
      <td><span class="badge-n ${rata>60?'bn-red':rata>30?'bn-yel':'bn-grn'}">${fmtMenit(rata)}</span></td>
    </tr>`;
  });
    tblHtml+='</tbody></table></div>';
    document.getElementById('modal-body').innerHTML=ssHtml+tblHtml;
  }catch(e){document.getElementById('modal-body').innerHTML=`<div class="empty-state"><p>Error: ${e.message}</p></div>`;}
}

function mintaHapus(id,label){hapusId=id;document.getElementById('hapus-label').textContent=label;new bootstrap.Modal(document.getElementById('modalHapus')).show();}
async function konfirmasiHapus(){if(!hapusId)return;bootstrap.Modal.getInstance(document.getElementById('modalHapus')).hide();try{const res=await fetch(`/hapus/${hapusId}`,{method:'DELETE'});const d=await res.json();if(d.ok)muatRiwayat();else alert('Gagal: '+(d.error||''));}catch(e){alert('Error: '+e.message);}hapusId=null;}

// ── TREN ──
// ══════════════════════════════
// TREN — MUAT SEMUA DATA
// ══════════════════════════════
async function muatTren(){
  // Tampilkan loading di semua bagian
  ['chart-tren-empty','chart-menit-empty','chart-donut-empty','chart-pegawai-empty'].forEach(id=>{
    const el=document.getElementById(id);
    if(el){el.innerHTML='<i class="bi bi-hourglass-split"></i><p>Memuat data&hellip;</p>';el.style.display='flex';}
  });
  try{
    const [r1,r2] = await Promise.all([fetch('/tren-bulanan'), fetch('/tren-pegawai')]);
    const tren = await r1.json();
    const peg  = await r2.json();
    PEGAWAI_ALL = peg;

    renderTrenSummary(tren);
    renderChartTren(tren);
    renderChartMenit(tren);
    renderChartDonut(peg);
    renderPegawaiSection(peg, 'Semua');
    renderTabelPerbandingan(tren);
  } catch(e) {
    console.error('muatTren error:', e);
  }
}

// ── Ringkasan cepat (4 kartu angka di atas) ──
function renderTrenSummary(data){
  const el = document.getElementById('tren-summary-grid');
  if(!data.length){ el.innerHTML=''; return; }
  const totalKej  = data.reduce((s,d)=>s+d.total_kejadian,0);
  const totalMnt  = data.reduce((s,d)=>s+d.total_menit,0);
  const avgKej    = Math.round(totalKej/data.length);
  const avgMnt    = Math.round(data.reduce((s,d)=>s+d.rata_menit,0)/data.length);
  const last      = data[data.length-1];
  const prev      = data.length>1?data[data.length-2]:null;
  const trendKej  = prev?last.total_kejadian-prev.total_kejadian:0;
  const trendMnt  = prev?last.rata_menit-prev.rata_menit:0;
  const trendPill = (v,lbl)=>{
    if(!prev) return `<span class="tsum-sub">${lbl} bulan ini</span>`;
    const cls = v>0?'tsum-up':v<0?'tsum-dn':'tsum-eq';
    const ico = v>0?'&#9650;':v<0?'&#9660;':'&#9644;';
    const sign= v>0?'+':'';
    return `<div class="tsum-trend ${cls}">${ico} ${sign}${v} vs bulan lalu</div>`;
  };
  el.innerHTML=`
    <div class="tren-sum-card">
      <div class="tsum-lbl"><i class="bi bi-calendar3 me-1"></i>Periode Data</div>
      <div class="tsum-val">${data.length}</div>
      <div class="tsum-sub">bulan tersimpan</div>
    </div>
    <div class="tren-sum-card">
      <div class="tsum-lbl"><i class="bi bi-exclamation-circle me-1"></i>Bulan Ini</div>
      <div class="tsum-val" style="color:var(--red)">${last.total_kejadian}</div>
      ${trendPill(trendKej,'kejadian')}
    </div>
    <div class="tren-sum-card">
      <div class="tsum-lbl"><i class="bi bi-bar-chart me-1"></i>Rata-rata/Bulan</div>
      <div class="tsum-val" style="color:var(--em)">${avgKej}</div>
      <div class="tsum-sub">kejadian per bulan</div>
    </div>
    <div class="tren-sum-card">
      <div class="tsum-lbl"><i class="bi bi-clock me-1"></i>Rata-rata Menit</div>
      <div class="tsum-val" style="color:var(--yellow)">${avgMnt}</div>
      ${trendPill(trendMnt,'mnt/kejadian')}
    </div>`;
}

// ── Tren Kejadian Bulanan (Bar + Line) ──
function renderChartTren(data){
  if(chartTren){chartTren.destroy();chartTren=null;}
  const el=document.getElementById('chartTren'), empty=document.getElementById('chart-tren-empty');
  if(!data.length){el.style.display='none';empty.style.display='flex';return;}
  el.style.display='block'; empty.style.display='none';

  const maxKej = Math.max(...data.map(d=>d.total_kejadian));

  chartTren = new Chart(el, {
    data:{
      labels: data.map(d=>d.label),
      datasets:[
        {
          type:'bar', label:'Jumlah Kejadian',
          data: data.map(d=>d.total_kejadian),
          backgroundColor: data.map(d=>{
            const pct = d.total_kejadian/maxKej;
            const alpha = 0.35+pct*0.5;
            return `rgba(16,185,129,${alpha})`;
          }),
          borderColor:'#10b981', borderWidth:1.5, borderRadius:5,
          order:2,
        },
        {
          type:'line', label:'Pegawai Terlambat',
          data: data.map(d=>d.total_terlambat),
          borderColor:'#ef4444', backgroundColor:'rgba(248,81,73,.1)',
          borderWidth:2, tension:.4, pointRadius:5,
          pointBackgroundColor:'#ef4444', pointBorderColor:'#0d1117',
          pointBorderWidth:2, fill:false, order:1,
          yAxisID:'y2',
        }
      ]
    },
    options:{
      responsive:true, maintainAspectRatio:false,
      interaction:{mode:'index', intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'#ffffff',
          borderColor:'#e5e7eb', borderWidth:1,
          titleColor:'#111827', bodyColor:'#4b5563',
          padding:10, titleFont:{size:12,weight:'bold'},
          callbacks:{
            title: ctx => '📅 '+ctx[0].label,
            label: ctx => {
              if(ctx.dataset.label==='Jumlah Kejadian') return `  Kejadian: ${ctx.parsed.y}×`;
              return `  Pegawai terlambat: ${ctx.parsed.y} orang`;
            }
          }
        }
      },
      scales:{
        x:{ticks:{color:'#8b949e',font:{size:10.5}}, grid:{color:'rgba(0,0,0,.04)'}},
        y:{
          ticks:{color:'#79c0ff',font:{size:10}}, grid:{color:'rgba(0,0,0,.05)'},
          beginAtZero:true,
          title:{display:true,text:'Kejadian',color:'#3b82f6',font:{size:10}}
        },
        y2:{
          position:'right', ticks:{color:'#f85149',font:{size:10}},
          grid:{display:false}, beginAtZero:true,
          title:{display:true,text:'Pegawai',color:'#ef4444',font:{size:10}}
        }
      }
    }
  });

  // Insight teks
  const maxBulan = data.reduce((a,b)=>a.total_kejadian>b.total_kejadian?a:b);
  const minBulan = data.reduce((a,b)=>a.total_kejadian<b.total_kejadian?a:b);
  document.getElementById('insight-tren').innerHTML=`
    <span class="ins-item"><span class="ins-dot" style="background:#f85149"></span>Tertinggi: <strong style="color:#e6edf3">${maxBulan.label}</strong> (${maxBulan.total_kejadian} kejadian)</span>
    <span class="ins-item"><span class="ins-dot" style="background:#3fb950"></span>Terendah: <strong style="color:#e6edf3">${minBulan.label}</strong> (${minBulan.total_kejadian} kejadian)</span>
    <span class="ins-item"><span class="ins-dot" style="background:#388bfd"></span>Rata-rata: <strong style="color:#e6edf3">${Math.round(data.reduce((s,d)=>s+d.total_kejadian,0)/data.length)}</strong> kejadian/bulan</span>`;
}

// ── Rata-rata Menit per Bulan ──
function renderChartMenit(data){
  if(chartMenit){chartMenit.destroy();chartMenit=null;}
  const el=document.getElementById('chartMenit'), empty=document.getElementById('chart-menit-empty');
  if(!data.length){el.style.display='none';empty.style.display='flex';return;}
  el.style.display='block'; empty.style.display='none';

  const avg = Math.round(data.reduce((s,d)=>s+d.rata_menit,0)/data.length);

  chartMenit = new Chart(el,{
    type:'line',
    data:{
      labels: data.map(d=>d.label),
      datasets:[
        {
          label:'Rata-rata Menit', data:data.map(d=>d.rata_menit),
          borderColor:'#e3b341', backgroundColor:'rgba(210,153,34,.1)',
          fill:true, tension:.4, pointRadius:5,
          pointBackgroundColor: data.map(d=>d.rata_menit>avg?'#ef4444':'#10b981'),
          pointBorderColor:'#0d1117', pointBorderWidth:2,
          borderWidth:2,
        },
        {
          label:'Rata-rata Keseluruhan', data:data.map(()=>avg),
          borderColor:'rgba(16,185,129,.3)', borderDash:[5,4],
          borderWidth:1.5, pointRadius:0, fill:false, tension:0,
        }
      ]
    },
    options:{
      responsive:true, maintainAspectRatio:false,
      interaction:{mode:'index', intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'#ffffff',
          borderColor:'#e5e7eb', borderWidth:1,
          titleColor:'#111827', bodyColor:'#4b5563', padding:10,
          callbacks:{
            title: ctx=>'📅 '+ctx[0].label,
            label: ctx=>{
              if(ctx.dataset.label==='Rata-rata Keseluruhan') return `  Rata-rata: ${ctx.parsed.y} mnt`;
              const v=ctx.parsed.y;
              return `  Rata-rata: ${v} mnt ${v>avg?'(di atas rata-rata ⚠)':'(normal ✓)'}`;
            }
          }
        }
      },
      scales:{
        x:{ticks:{color:'#9ca3af',font:{size:10.5}},grid:{color:'rgba(0,0,0,.04)'}},
        y:{ticks:{color:'#9ca3af',font:{size:10}},grid:{color:'rgba(0,0,0,.05)'},beginAtZero:true,
           title:{display:true,text:'Menit',color:'#9ca3af',font:{size:10}}}
      }
    }
  });

  const tertinggi = data.reduce((a,b)=>a.rata_menit>b.rata_menit?a:b);
  document.getElementById('insight-menit').innerHTML=`
    <span class="ins-item"><span class="ins-dot" style="background:#e3b341"></span>Titik merah = di atas rata-rata (${avg} mnt). Titik hijau = normal.</span>
    <span class="ins-item"><span class="ins-dot" style="background:#f85149"></span>Tertinggi: <strong style="color:#e6edf3">${tertinggi.label}</strong> (${tertinggi.rata_menit} mnt)</span>`;
}

// ── Distribusi per Shift (Donut + Tabel) ──
function renderChartDonut(data){
  if(chartDonut){chartDonut.destroy();chartDonut=null;}
  const el=document.getElementById('chartDonut'), empty=document.getElementById('chart-donut-empty');
  if(!data.length){el.style.display='none';empty.style.display='flex';document.getElementById('shift-dist-table').innerHTML='';return;}

  // Agregasi per shift dari data pegawai
  const agg={};
  data.forEach(d=>{
    const s=d.tipe_shift||'Non-Shift';
    if(!agg[s])agg[s]={kej:0,mnt:0,peg:0};
    agg[s].kej += parseInt(d.total_kejadian)||0;
    agg[s].mnt += parseInt(d.total_menit)||0;
    agg[s].peg++;
  });

  const urutan = ['Shift Pagi','Shift Siang','Shift Sore','Shift Malam','Non-Shift'];
  const labels  = urutan.filter(s=>agg[s]);
  const vals    = labels.map(l=>agg[l].kej);
  const colors  = labels.map(l=>SHIFT_COLORS[l]||'#8b949e');
  const total   = vals.reduce((a,b)=>a+b,0)||1;

  el.style.display='block'; empty.style.display='none';
  chartDonut = new Chart(el,{
    type:'doughnut',
    data:{
      labels,
      datasets:[{
        data:vals,
        backgroundColor:colors.map(c=>c+'bb'),
        borderColor:colors, borderWidth:2.5, hoverOffset:10,
      }]
    },
    options:{
      responsive:true, maintainAspectRatio:false, cutout:'62%',
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'#ffffff',
          borderColor:'#e5e7eb', borderWidth:1,
          titleColor:'#111827', bodyColor:'#4b5563', padding:10,
          callbacks:{
            label: ctx=>`  ${ctx.label}: ${ctx.parsed} kejadian (${Math.round(ctx.parsed/total*100)}%)`
          }
        }
      }
    }
  });

  // Tabel distribusi dengan mini progress bar
  let html='<table class="dist-tbl">';
  labels.forEach((l,i)=>{
    const pct = Math.round(agg[l].kej/total*100);
    const sc  = shiftCls(l);
    html+=`<tr>
      <td style="width:110px"><span class="badge-shift ${sc}">${l}</span></td>
      <td>
        <div class="dist-bar-bg"><div class="dist-bar-fill" style="width:${pct}%;background:${colors[i]}"></div></div>
      </td>
      <td style="width:42px;font-family:'DM Mono',monospace;font-size:.78rem;font-weight:700;color:${colors[i]};text-align:right">${pct}%</td>
      <td style="width:80px;font-family:'DM Mono',monospace;font-size:.74rem;color:var(--t2);text-align:right">${agg[l].kej} kej</td>
      <td style="width:90px;font-family:'DM Mono',monospace;font-size:.72rem;color:var(--t3);text-align:right">${fmtMenit(agg[l].mnt)}</td>
    </tr>`;
  });
  html+='</table>';
  document.getElementById('shift-dist-table').innerHTML=html;
}

// ── Peringkat Pegawai: tabel + grafik + filter ──
function renderPegawaiSection(data, activeShift){
  // Update pill counts
  const counts={'Semua':data.length};
  data.forEach(d=>{const s=d.tipe_shift||'Non-Shift';counts[s]=(counts[s]||0)+1;});
  document.getElementById('pcount-semua').textContent = ` (${counts['Semua']||0})`;
  document.getElementById('pcount-pagi').textContent   = ` (${counts['Shift Pagi']||0})`;
  document.getElementById('pcount-siang').textContent  = ` (${counts['Shift Siang']||0})`;
  document.getElementById('pcount-sore').textContent   = ` (${counts['Shift Sore']||0})`;
  document.getElementById('pcount-malam').textContent  = ` (${counts['Shift Malam']||0})`;
  document.getElementById('pcount-non').textContent    = ` (${counts['Non-Shift']||0})`;

  // Filter
  const filtered = activeShift==='Semua' ? data : data.filter(d=>(d.tipe_shift||'Non-Shift')===activeShift);

  // Tabel kiri
  const tbody = document.getElementById('tb-pegawai-rank');
  tbody.innerHTML='';
  if(!filtered.length){
    tbody.innerHTML='<tr><td colspan="4" style="text-align:center;padding:20px;color:var(--t3);font-size:.8rem">Tidak ada data untuk shift ini</td></tr>';
  } else {
    filtered.slice(0,15).forEach((r,i)=>{
      const rc=i===0?'rk-1':i===1?'rk-2':i===2?'rk-3':'rk-n';
      const ri=i===0?'🥇':i===1?'🥈':i===2?'🥉':(i+1);
      const sc=shiftCls(r.tipe_shift);
      tbody.innerHTML+=`<tr>
        <td><span class="rk ${rc}">${ri}</span></td>
        <td style="font-weight:600;font-size:.82rem">${r.nama}</td>
        <td><span class="badge-shift ${sc}" style="font-size:.62rem">${r.tipe_shift||'Non-Shift'}</span></td>
        <td style="font-family:'DM Mono',monospace;font-size:.78rem"><span class="badge-n bn-red">${r.total_kejadian}×</span></td>
      </tr>`;
    });
  }

  // Grafik kanan
  renderChartPegawai(filtered);
}

function filterPegawaiChart(shift, btn){
  // Update pills
  document.querySelectorAll('#shift-pills-pegawai .shift-pill').forEach(p=>p.classList.remove('on'));
  btn.classList.add('on');
  // Re-render section
  renderPegawaiSection(PEGAWAI_ALL, shift);
}

// Alias untuk kompatibilitas kode lama
function filterPegawai(shift, btn){ filterPegawaiChart(shift, btn); }

function renderChartPegawai(data){
  if(chartPegawai){chartPegawai.destroy();chartPegawai=null;}
  const el=document.getElementById('chartPegawai'), empty=document.getElementById('chart-pegawai-empty');
  const top = data.slice(0,15);
  if(!top.length){el.style.display='none';empty.style.display='flex';return;}
  el.style.display='block'; empty.style.display='none';

  const colors = top.map(d=>SHIFT_COLORS[d.tipe_shift||'Non-Shift']||'#8b949e');
  const maxVal = Math.max(...top.map(d=>d.total_kejadian));

  chartPegawai = new Chart(el,{
    type:'bar',
    data:{
      labels: top.map(d=>d.nama),
      datasets:[{
        label:'Total Kejadian',
        data: top.map(d=>d.total_kejadian),
        backgroundColor: top.map((d,i)=>{
          const pct = d.total_kejadian/maxVal;
          const c = colors[i];
          return c+(Math.round(80+pct*100)).toString(16).padStart(2,'0').slice(0,2);
        }),
        borderColor: colors, borderWidth:1.5, borderRadius:6,
      }]
    },
    options:{
      indexAxis:'y', responsive:true, maintainAspectRatio:false,
      interaction:{mode:'index', intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'#ffffff',
          borderColor:'#e5e7eb', borderWidth:1,
          titleColor:'#111827', bodyColor:'#4b5563', padding:10,
          callbacks:{
            title: ctx=>ctx[0].label,
            label: ctx=>`  ${ctx.parsed.x} kejadian (${top[ctx.dataIndex].tipe_shift||'Non-Shift'})`
          }
        }
      },
      scales:{
        x:{
          ticks:{color:'#9ca3af',font:{size:10}},
          grid:{color:'rgba(0,0,0,.05)'}, beginAtZero:true,
          title:{display:true,text:'Total Kejadian Terlambat',color:'#9ca3af',font:{size:10}}
        },
        y:{
          ticks:{color:'#111827',font:{size:10.5},maxTicksLimit:15},
          grid:{color:'rgba(0,0,0,.03)'}
        }
      }
    }
  });
}

// ── Tabel Perbandingan Antar Bulan (baru, lebih mudah dibaca) ──
function renderTabelPerbandingan(data){
  document.getElementById('perbandingan-sub').textContent=`${data.length} periode tersimpan`;
  if(!data.length){
    document.getElementById('perbandingan-body').innerHTML='<div class="empty-state"><i class="bi bi-inbox"></i><p>Belum ada data tersimpan.</p></div>';
    return;
  }

  const maxKej = Math.max(...data.map(d=>d.total_kejadian))||1;

  let html=`<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
  <table class="tbl-cmp">
    <thead>
      <tr>
        <th style="min-width:110px">Bulan</th>
        <th>Pegawai<br>Terlambat</th>
        <th>Kejadian<br>Total</th>
        <th style="min-width:130px">Proporsi Kejadian</th>
        <th>Total<br>Waktu</th>
        <th>Rata-rata<br>per Kejadian</th>
        <th style="min-width:110px">Tren vs<br>Bulan Lalu</th>
      </tr>
    </thead>
    <tbody>`;

  data.forEach((r,i)=>{
    const prev = i>0?data[i-1]:null;
    const pct  = Math.round(r.total_kejadian/maxKej*100);
    const isLatest = i===data.length-1;

    let trendHTML = '<span class="td-muted" style="font-size:.75rem">—</span>';
    if(prev){
      const diff = r.total_kejadian - prev.total_kejadian;
      const pctDiff = prev.total_kejadian>0?Math.round(diff/prev.total_kejadian*100):0;
      const sign  = diff>0?'+':'';
      if(diff>0)       trendHTML=`<span class="trend-pill tp-up">&#9650; ${sign}${diff} (${sign}${pctDiff}%)</span>`;
      else if(diff<0)  trendHTML=`<span class="trend-pill tp-dn">&#9660; ${diff} (${pctDiff}%)</span>`;
      else             trendHTML=`<span class="trend-pill tp-eq">&#9644; Sama</span>`;
    }

    // Warna rata-rata
    const avg = data.reduce((s,d)=>s+d.rata_menit,0)/data.length;
    const rataColor = r.rata_menit > avg*1.2 ? '#ff7b72' : r.rata_menit > avg ? '#e3b341' : '#56d364';

    html+=`<tr ${isLatest?'style="background:rgba(56,139,253,.05)"':''}>
      <td class="td-bulan">
        ${isLatest?'<span style="font-size:.6rem;color:#79c0ff;background:var(--em-lt);color:var(--em-dk);padding:1px 5px;border-radius:3px;margin-right:5px">TERBARU</span>':''}
        ${r.label}
      </td>
      <td><span class="badge-n bn-red" style="font-size:.75rem">${r.total_terlambat} orang</span></td>
      <td class="td-mono" style="font-size:.85rem;font-weight:700">${r.total_kejadian}</td>
      <td>
        <div style="display:flex;align-items:center;gap:7px">
          <div style="flex:1;height:7px;background:rgba(255,255,255,.07);border-radius:4px;overflow:hidden;min-width:60px">
            <div class="bar-mini" style="width:${pct}%;background:linear-gradient(90deg,#388bfd,#39c5cf)"></div>
          </div>
          <span class="td-mono" style="font-size:.72rem;color:var(--t2);width:28px">${pct}%</span>
        </div>
      </td>
      <td class="td-mono td-muted">${fmtMenit(r.total_menit)}</td>
      <td class="td-mono" style="color:${rataColor};font-weight:600">${r.rata_menit} mnt</td>
      <td>${trendHTML}</td>
    </tr>`;
  });

  html+='</tbody></table></div>';
  document.getElementById('perbandingan-body').innerHTML=html;
}



// ── FILTER NAMA ──
function filterNama(){
  const q=(document.getElementById('cari-nama').value||'').toLowerCase().trim();
  document.querySelectorAll('#tb-rekap-main tr').forEach(tr=>{
    const nm=(tr.querySelector('.rtr-nama')||{}).textContent||'';
    tr.style.display=(!q||nm.toLowerCase().includes(q))?'':'none';
  });
  document.getElementById('rekap-detail-panel').style.display='none';
  document.querySelectorAll('#tb-detail tr').forEach(tr=>{
    const nm=(tr.querySelector('td')||{}).textContent||'';
    tr.style.display=(!q||nm.toLowerCase().includes(q))?'':'none';
  });
}

// ══════════════════════════════════════════════════════════════
// PEGAWAI CRUD
// ══════════════════════════════════════════════════════════════
let PEGAWAI_LIST=[], editPegId=null;

async function muatPegawai(){
  try{
    const res=await fetch('/pegawai'); const data=await res.json();
    PEGAWAI_LIST=data;
    renderTblPegawai(data);
    document.getElementById('peg-sub').textContent=data.length+' pegawai terdaftar';
    document.getElementById('peg-count').textContent=data.length+' pegawai';
  }catch(e){console.error(e);}
}

function renderTblPegawai(list){
  const tb=document.getElementById('peg-tbody');
  if(!list||!list.length){tb.innerHTML='<tr><td colspan="7"><div class="empty-state"><i class="bi bi-people"></i><p>Belum ada pegawai. Klik Tambah Pegawai.</p></div></td></tr>';return;}
  const STD_PULANG={Normal:'Sen-Kam 15:30 | Jumat 14:30',Shift:'(sesuai shift)'};
  tb.innerHTML=list.map((p,i)=>`
    <tr>
      <td style="color:var(--t3)">${i+1}</td>
      <td style="font-weight:600">${p.nama}</td>
      <td><span class="badge-n ${p.tipe_pegawai==='Shift'?'bn-yel':'bn-grn'}">${p.tipe_pegawai}</span></td>
      <td style="color:var(--t2)">${p.unit||'<span style="color:var(--t4)">-</span>'}</td>
      <td style="font-family:'DM Mono',monospace;font-size:.78rem">${p.tipe_pegawai==='Shift'?'(jam shift)':'07:30'}</td>
      <td style="font-family:'DM Mono',monospace;font-size:.78rem">${STD_PULANG[p.tipe_pegawai]||'-'}</td>
      <td><div style="display:flex;gap:5px">
        <button class="btn-s btn-blue" onclick="editPegawai(${p.id})"><i class="bi bi-pencil"></i></button>
        <button class="btn-s btn-red" onclick="konfirmasiHapusPeg(${p.id},'${p.nama.replace(/'/g,"\\'")}')"><i class="bi bi-trash3"></i></button>
      </div></td>
    </tr>`).join('');
}

function bukaFormPegawai(){
  editPegId=null;
  document.getElementById('inp-nama').value='';
  document.getElementById('inp-tipe').value='Normal';
  document.getElementById('inp-unit').value='';
  document.getElementById('peg-err').style.display='none';
  document.getElementById('form-pegawai').style.display='block';
  document.getElementById('inp-nama').focus();
}

function tutupFormPegawai(){
  document.getElementById('form-pegawai').style.display='none';
  editPegId=null;
}

function editPegawai(id){
  const p=PEGAWAI_LIST.find(x=>x.id===id); if(!p) return;
  editPegId=id;
  document.getElementById('inp-nama').value=p.nama;
  document.getElementById('inp-tipe').value=p.tipe_pegawai;
  document.getElementById('inp-unit').value=p.unit||'';
  document.getElementById('peg-err').style.display='none';
  document.getElementById('form-pegawai').style.display='block';
  document.getElementById('inp-nama').focus();
}

async function simpanPegawai(){
  const nama=document.getElementById('inp-nama').value.trim();
  const tipe=document.getElementById('inp-tipe').value;
  const unit=document.getElementById('inp-unit').value.trim();
  const errEl=document.getElementById('peg-err');
  if(!nama){errEl.textContent='Nama tidak boleh kosong.';errEl.style.display='block';return;}
  try{
    const url=editPegId?`/pegawai/${editPegId}`:'/pegawai';
    const method=editPegId?'PUT':'POST';
    const res=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify({nama,tipe_pegawai:tipe,unit})});
    const data=await res.json();
    if(data.ok){tutupFormPegawai();muatPegawai();}
    else{errEl.textContent=data.error||'Gagal menyimpan.';errEl.style.display='block';}
  }catch(e){errEl.textContent='Error: '+e.message;errEl.style.display='block';}
}

async function konfirmasiHapusPeg(id,nama){
  if(!confirm('Hapus pegawai "'+nama+'" dari daftar?')) return;
  try{
    const res=await fetch('/pegawai/'+id,{method:'DELETE'});
    const data=await res.json();
    if(data.ok) muatPegawai();
    else alert('Gagal hapus: '+(data.error||''));
  }catch(e){alert('Error: '+e.message);}
}

// ── UI HELPERS ──
function setLoad(on){document.getElementById('prog').style.display=on?'block':'none';document.getElementById('btn-run').disabled=on;document.getElementById('btn-run').innerHTML=on?'<i class="bi bi-hourglass-split me-1"></i>Memproses\u2026':'<i class="bi bi-play-fill me-1"></i>Mulai Analisis &amp; Simpan';}
function showErr(m){document.getElementById('err-msg').textContent=m;document.getElementById('err-box').style.display='flex';}
function hideErr(){document.getElementById('err-box').style.display='none';}
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/cek-db')
def cek_db():
    return jsonify(db.cek_koneksi())

@app.route('/proses', methods=['POST'])
def proses():
    if 'file' not in request.files:
        return jsonify({'error': 'Tidak ada file.'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'Nama file kosong.'}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ('xls', 'xlsx', 'csv'):
        return jsonify({'error': f'Format ".{ext}" tidak didukung.'}), 400
    try:
        file_bytes = file.read()
    except Exception as e:
        return jsonify({'error': f'Gagal membaca file: {e}'}), 400

    # Refresh cache pegawai setiap upload agar perubahan terbaru dipakai
    invalidate_pegawai_cache()

    try:
        if ext in ('xls', 'xlsx'):
            engine = 'xlrd' if ext == 'xls' else 'openpyxl'
            df_sniff = pd.read_excel(io.BytesIO(file_bytes), header=None, engine=engine, dtype=str, nrows=3)
            header_txt = ' '.join(str(v) for v in df_sniff.values.flatten()).lower()
        else:
            header_txt = file_bytes.decode('utf-8', errors='replace')[:300].lower()
        is_timetable = any(k in header_txt for k in ('timetable', 'on-duty', 'off-duty'))
    except Exception:
        is_timetable = False

    tahun = str(datetime.now().year)
    bulan_int = datetime.now().month
    records = []; format_label = ''

    try:
        if is_timetable:
            records, tahun, bulan_str = baca_file_timetable(file_bytes, ext)
            bulan_int = int(bulan_str)
            format_label = f'Timetable Fingerprint \u2022 {len(records)} absensi'
            total_pegawai = len(set(r['nama'] for r in records))
            if not records:
                return jsonify({'error': 'Format timetable terdeteksi tapi tidak ada data jam.'}), 400
        else:
            engine = 'xlrd' if ext == 'xls' else ('openpyxl' if ext == 'xlsx' else None)
            df = pd.read_excel(io.BytesIO(file_bytes), dtype=str, engine=engine) if engine \
                 else pd.read_csv(io.BytesIO(file_bytes), dtype=str)
            df = normalkan_kolom(df)
            missing = [k for k in ['nama', 'jam_masuk'] if k not in df.columns]
            if missing:
                return jsonify({'error': f'Kolom tidak ditemukan: {", ".join(missing)}'}), 400
            records = df.to_dict('records')
            format_label = f'Tabel Biasa \u2022 {len(records)} baris'
            total_pegawai = df['nama'].nunique() if 'nama' in df.columns else 0
    except Exception as e:
        return jsonify({'error': f'Gagal memproses file: {e}'}), 500

    try:
        rekap, detail = rekap_keterlambatan(records)
    except Exception as e:
        return jsonify({'error': f'Gagal menghitung keterlambatan: {e}'}), 500

    total_kejadian  = sum(r['jumlah'] for r in rekap)
    total_menit_raw = sum(r['total_menit'] for r in rekap)
    rata_raw        = round(total_menit_raw / total_kejadian) if total_kejadian else 0

    for r in rekap:
        r['total_fmt'] = fmt_menit(r['total_menit'])
        r['rata_fmt']  = fmt_menit(round(r['total_menit'] / r['jumlah'])) if r['jumlah'] else '0 menit'

    shift_stat = ringkasan_per_shift(rekap)

    db_result = db.simpan_rekap(
        nama_file=file.filename, bulan=bulan_int, tahun=int(tahun),
        total_pegawai=total_pegawai, total_terlambat=len(rekap),
        total_kejadian=total_kejadian, total_menit=total_menit_raw,
        rata_menit=rata_raw, format_file=format_label, rekap_list=rekap,
    )

    return jsonify({
        'rekap': rekap, 'detail': detail,
        'shift_stat': shift_stat,
        'total_kejadian': total_kejadian, 'total_menit_raw': total_menit_raw,
        'total_menit_str': fmt_menit(total_menit_raw), 'rata_str': fmt_menit(rata_raw),
        'total_pegawai': total_pegawai, 'format_file': format_label,
        'bulan_nama': NAMA_BULAN[bulan_int], 'tahun': tahun,
        'db_saved': db_result.get('ok', False),
        'db_updated': db_result.get('updated', False),
        'db_error': db_result.get('error', ''),
    })

@app.route('/riwayat')
def riwayat():
    return jsonify(db.get_riwayat())

@app.route('/riwayat/<int:rekap_id>')
def riwayat_detail(rekap_id):
    return jsonify({'header': db.get_rekap_by_id(rekap_id), 'detail': db.get_detail_rekap(rekap_id)})

@app.route('/hapus/<int:rekap_id>', methods=['DELETE'])
def hapus(rekap_id):
    return jsonify({'ok': db.hapus_rekap(rekap_id)})

@app.route('/tren-bulanan')
def tren_bulanan():
    return jsonify(db.get_tren_bulanan())

@app.route('/tren-pegawai')
def tren_pegawai():
    # Tambahkan tipe_shift ke data pegawai untuk filter grafik
    data = db.get_tren_pegawai_dengan_shift()
    return jsonify(data)


@app.route('/pegawai', methods=['GET'])
def get_pegawai():
    return jsonify(db.get_daftar_pegawai())

@app.route('/pegawai', methods=['POST'])
def tambah_pegawai():
    data = request.get_json()
    nama = (data.get('nama') or '').strip()
    tipe = data.get('tipe_pegawai', 'Normal')
    unit = (data.get('unit') or '').strip()
    if not nama:
        return jsonify({'ok': False, 'error': 'Nama tidak boleh kosong'}), 400
    result = db.simpan_pegawai(nama, tipe, unit)
    if result.get('ok'):
        invalidate_pegawai_cache()
    return jsonify(result)

@app.route('/pegawai/<int:pid>', methods=['PUT'])
def edit_pegawai(pid):
    data = request.get_json()
    nama = (data.get('nama') or '').strip()
    tipe = data.get('tipe_pegawai', 'Normal')
    unit = (data.get('unit') or '').strip()
    if not nama:
        return jsonify({'ok': False, 'error': 'Nama tidak boleh kosong'}), 400
    result = db.update_pegawai(pid, nama, tipe, unit)
    if result.get('ok'):
        invalidate_pegawai_cache()
    return jsonify(result)

@app.route('/pegawai/<int:pid>', methods=['DELETE'])
def hapus_pegawai(pid):
    result = db.hapus_pegawai(pid)
    if result.get('ok'):
        invalidate_pegawai_cache()
    return jsonify(result)

if __name__ == '__main__':
    # Cari IP lokal otomatis untuk ditampilkan ke pengguna
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_lokal = s.getsockname()[0]
        s.close()
    except Exception:
        ip_lokal = "tidak terdeteksi"

    print("=" * 60)
    print("  SmartPresensi Enterprise v5.0 - Full Feature Edition")
    print(f"  Local  : http://127.0.0.1:5000")
    print(f"  Network: http://{ip_lokal}:5000  <-- bagikan ke perangkat lain")
    print("  Pastikan XAMPP MySQL sudah aktif!")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=False)