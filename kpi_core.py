"""
kpi_core.py
Logika perhitungan bersama (TANPA Streamlit) yang dipakai oleh dashboard (app.py) dan report harian (report_engine.py),
sehingga angka di dashboard dan report selalu identik.
"""
import pandas as pd
import numpy as np

# ==========================================
# NORMALISASI NAMA JENIS MESIN
# ==========================================
ALIAS_JENIS_MESIN = {
    'Pres': 'Press', 'Hummer': 'Hammer', 'Moxing 500 Kg': 'Mixing 500 Kg',
    'Blis': 'Blister', 'Pac': 'Packing Line', 'Packing': 'Packing Line',
    'Filling Lipstick': 'Filling Otomatis Lipstick',
}

def normalize_jenis_mesin(series):
    """
    Seragamkan nama jenis mesin (tanpa nomor mesin/line):
    'Filling Otomatis 21' -> 'Filling Otomatis', 'Press8' -> 'Press', 'Tr 8' -> 'Tr',
    'Mixing Serbuk 12' -> 'Mixing Serbuk'. Angka di tengah nama (Mixing 500 Kg) tidak diubah.
    """
    s = series.fillna('').astype(str).str.strip()
    s = s.replace({'nan': '', 'None': '', 'NaN': '', '<NA>': ''})
    s = s.str.replace(r'\s*-\s*\d[\d,\s]*$', '', regex=True)   # akhiran " - 5"
    s = s.str.replace(r'\s*\d+$', '', regex=True)               # akhiran angka: "Press 3", "Press8"
    s = s.str.strip().str.title()
    return s.replace(ALIAS_JENIS_MESIN)


# ==========================================
# HELPER & MASTER TARGET
# ==========================================
def format_line_number(val):
    if pd.isna(val) or str(val).strip() in ['', 'nan', 'NaN', 'None', '-']:
        return "Line Unknown"
    val_str = str(val).strip()
    if val_str.startswith("Line "):
        return val_str
    if val_str.endswith('.0'):
        val_str = val_str[:-2]
    return f"Line {val_str}"

def get_real_subbrand(row):
    vp = str(row.get('VARIAN_PRODUK', '')).strip()
    sm = str(row.get('SUBBRAND_MASTER', '')).strip()
    sb = str(row.get('SUBBRAND', '')).strip()
    
    for val in [vp, sm, sb]:
        if val and val.upper() not in ['NAN', 'NONE', 'UNKNOWN', '']:
            return val
    return '-'

DEFAULT_TARGET_PER_JAM = 2500.0

def _norm_line_target(x):
    """'05' / 5 / '5.0' -> '5'. Kosong -> ''."""
    t = str(x).strip()
    if t.lower() in ('', 'nan', 'none', '<na>'):
        return ''
    if t.endswith('.0'):
        t = t[:-2]
    return t.lstrip('0') or '0'

class TargetLookup:
    """Master target bertingkat: (subbrand, jenis mesin, no mesin). Kosong pada jenis/line = berlaku umum."""
    def __init__(self, by_key):
        self.by_key = by_key
        self.subs = sorted({k[0] for k in by_key}, key=len, reverse=True)

    def __bool__(self):
        return bool(self.by_key)

    def resolve_sub(self, sb_upper):
        """Cocok persis dulu, lalu sebagian (key terpanjang diprioritaskan)."""
        if sb_upper in self.subs:
            return sb_upper
        for key in self.subs:
            # key master terkandung di nama produk -> aman.
            # Arah sebaliknya hanya jika nama produk cukup panjang (hindari "12", "Pink", dst).
            if key in sb_upper or (len(sb_upper) >= 12 and sb_upper in key):
                return key
        return None

def build_target_dict(df_target):
    """Ubah tabel master target menjadi TargetLookup {(SUBBRAND, JENIS_MESIN, NO_LINE): target/jam}."""
    if df_target is None or df_target.empty:
        return TargetLookup({})
    d = df_target.copy()
    for col in ('JENIS_MESIN', 'NO_MESIN_LINE'):
        if col not in d.columns:
            d[col] = ''
    d['SUBBRAND'] = d['SUBBRAND'].astype(str).str.strip().str.upper()
    d['JENIS_MESIN'] = normalize_jenis_mesin(d['JENIS_MESIN'].fillna('')).str.upper()
    d['NO_MESIN_LINE'] = d['NO_MESIN_LINE'].apply(_norm_line_target)
    d['TARGET_PER_JAM'] = pd.to_numeric(d['TARGET_PER_JAM'], errors='coerce')
    d = d[(d['SUBBRAND'] != '') & (d['TARGET_PER_JAM'] > 0)]
    d = d.drop_duplicates(['SUBBRAND', 'JENIS_MESIN', 'NO_MESIN_LINE'], keep='last')
    return TargetLookup({
        (r.SUBBRAND, r.JENIS_MESIN, r.NO_MESIN_LINE): float(r.TARGET_PER_JAM) for r in d.itertuples()
    })

def get_target_info(subbrand_str, jenis_mesin, no_line, lookup):
    """
    Return (target/jam, sumber). Urutan pencarian, dipakai yang pertama ketemu:
      1. subbrand + jenis mesin + nomor mesin   -> 'Mesin & Line'
      2. subbrand + jenis mesin                 -> 'Jenis Mesin'
      3. subbrand saja (target umum)            -> 'Umum'
      4. tidak ada                              -> 'Default'
    """
    sb_upper = str(subbrand_str).strip().upper()
    if not sb_upper or sb_upper == '-' or not lookup:
        return DEFAULT_TARGET_PER_JAM, 'Default'
    sb_key = lookup.resolve_sub(sb_upper)
    if sb_key is None:
        return DEFAULT_TARGET_PER_JAM, 'Default'

    jenis = str(jenis_mesin).strip().upper()
    line = _norm_line_target(no_line)
    for kunci, sumber in (((sb_key, jenis, line), 'Mesin & Line'),
                          ((sb_key, jenis, ''), 'Jenis Mesin'),
                          ((sb_key, '', ''), 'Umum')):
        if kunci in lookup.by_key:
            return lookup.by_key[kunci], sumber
    return DEFAULT_TARGET_PER_JAM, 'Default'

def get_target_capacity(subbrand_str, lookup, jenis_mesin='', no_line=''):
    return get_target_info(subbrand_str, jenis_mesin, no_line, lookup)[0]

def calc_gross_per_jam(row):
    durasi_menit = pd.to_numeric(row.get('Durasi Bersih', 0), errors='coerce')
    if pd.isna(durasi_menit) or durasi_menit <= 0:
        return 0.0
    gross_pcs = row.get('GROSS_OUTPUT_PCS', 0.0)
    if durasi_menit < 60:
        return float(gross_pcs)
    else:
        operating_hours = durasi_menit / 60.0
        return float(gross_pcs) / operating_hours


# ==========================================
# PERHITUNGAN KPI OEE (identik dengan Tab 1 dashboard)
# ==========================================
def hitung_konteks_oee(df_oee, df_qc, df_sic, df_target):
    """
    Hitung seluruh komponen KPI Produksi/OEE.
    Return dict berisi: df_oee_perf, df_oee_perf_fp, dict_target_subbrand, total_reject_kemas_oee,
    quality_rate, performance_rate, availability_rate, nilai_oee, kondisi_fp, total_output_fp.
    """
    df_qc_temp = df_qc.copy()
    df_qc_temp['NO_BATCH_KEY'] = df_qc_temp['NO_BATCH'].astype(str).str.strip()
    df_qc_temp['NAMA_MESIN_KEY'] = df_qc_temp['NAMA_MESIN'].astype(str).str.strip().str.title()
    df_qc_temp['NO_MESIN_LINE_KEY'] = df_qc_temp['NO_MESIN_LINE'].astype(str).str.strip().str.replace('.0', '', regex=False) if 'NO_MESIN_LINE' in df_qc_temp.columns else ''

    df_qc_temp['REJECT_PCS'] = pd.to_numeric(df_qc_temp['REJECT_PCS'], errors='coerce').fillna(0)
    df_qc_temp['REPROSES_PCS'] = pd.to_numeric(df_qc_temp['REPROSES_PCS'], errors='coerce').fillna(0)

    qc_grouped = df_qc_temp.groupby(['NO_BATCH_KEY', 'NAMA_MESIN_KEY', 'NO_MESIN_LINE_KEY']).agg({
        'REJECT_PCS': 'sum',
        'REPROSES_PCS': 'sum'
    }).reset_index()

    df_oee_perf = df_oee.copy()
    df_oee_perf['NO_BATCH_KEY'] = df_oee_perf['NO_BATCH'].astype(str).str.strip()
    df_oee_perf['NAMA_MESIN_KEY'] = df_oee_perf['NAMA_MESIN'].astype(str).str.strip().str.title()

    if 'NO_MESIN_LINE' in df_oee_perf.columns:
        df_oee_perf['NO_MESIN_LINE_KEY'] = df_oee_perf['NO_MESIN_LINE'].fillna('').astype(str).str.strip().str.replace('.0', '', regex=False).replace({'nan': '', 'None': ''})
        if 'MESIN_FULL' in df_oee_perf.columns:
            _line_full = df_oee_perf['MESIN_FULL'].fillna('').astype(str).str.extract(r'-\s*0*(\d+)')[0].fillna('')
            _kosong = df_oee_perf['NO_MESIN_LINE_KEY'] == ''
            df_oee_perf.loc[_kosong, 'NO_MESIN_LINE_KEY'] = _line_full[_kosong]
    elif 'MESIN_FULL' in df_oee_perf.columns:
        df_oee_perf['NO_MESIN_LINE_KEY'] = df_oee_perf['MESIN_FULL'].astype(str).str.extract(r'(\d+)')[0].fillna('')
    else:
        df_oee_perf['NO_MESIN_LINE_KEY'] = ''

    df_oee_perf = df_oee_perf.merge(qc_grouped, on=['NO_BATCH_KEY', 'NAMA_MESIN_KEY', 'NO_MESIN_LINE_KEY'], how='left')
    df_oee_perf['REJECT_PCS'] = pd.to_numeric(df_oee_perf['REJECT_PCS'], errors='coerce').fillna(0)
    df_oee_perf['REPROSES_PCS'] = pd.to_numeric(df_oee_perf['REPROSES_PCS'], errors='coerce').fillna(0)
    df_oee_perf['CAPAIN_PRODUKSI_PCS'] = pd.to_numeric(df_oee_perf['CAPAIN_PRODUKSI_PCS'], errors='coerce').fillna(0)

    total_reject_kemas_oee = pd.to_numeric(df_oee['KEMAS_RUSAK_PRODUKSI'], errors='coerce').fillna(0).sum() if 'KEMAS_RUSAK_PRODUKSI' in df_oee.columns else 0
    capaian_oee_pcs = pd.to_numeric(df_oee['CAPAIN_PRODUKSI_PCS'], errors='coerce').fillna(0).sum() if 'CAPAIN_PRODUKSI_PCS' in df_oee.columns else 0
    total_reject_qc = pd.to_numeric(df_qc['REJECT_PCS'], errors='coerce').fillna(0).sum() if 'REJECT_PCS' in df_qc.columns else 0
    total_reproses_qc = pd.to_numeric(df_qc['REPROSES_PCS'], errors='coerce').fillna(0).sum() if 'REPROSES_PCS' in df_qc.columns else 0

    denominasi_quality = capaian_oee_pcs + total_reproses_qc + total_reject_qc
    quality_rate = (capaian_oee_pcs / denominasi_quality) if denominasi_quality > 0 else 1.0

    kondisi_fp_oee = df_oee_perf['NAMA_MESIN_KEY'].astype(str).str.contains('Filling|Press', case=False, na=False)
    df_oee_perf_fp = df_oee_perf[kondisi_fp_oee].copy().reset_index(drop=True)

    df_oee_perf_fp['GROSS_OUTPUT_PCS'] = df_oee_perf_fp['CAPAIN_PRODUKSI_PCS'] + df_oee_perf_fp['REJECT_PCS'] + df_oee_perf_fp['REPROSES_PCS']
    df_oee_perf_fp['GROSS_CAPAIAN_PER_JAM'] = df_oee_perf_fp.apply(calc_gross_per_jam, axis=1)

    dict_target_subbrand = build_target_dict(df_target)
    df_oee_perf_fp['SUBBRAND_DISPLAY'] = df_oee_perf_fp.apply(get_real_subbrand, axis=1)
    _info_target = [
        get_target_info(sb, jm, ln, dict_target_subbrand)
        for sb, jm, ln in zip(df_oee_perf_fp['SUBBRAND_DISPLAY'], df_oee_perf_fp['NAMA_MESIN_KEY'], df_oee_perf_fp['NO_MESIN_LINE_KEY'])
    ]
    df_oee_perf_fp['TARGET_SUBBRAND_JAM'] = [t[0] for t in _info_target]
    df_oee_perf_fp['SUMBER_TARGET'] = [t[1] for t in _info_target]

    df_oee_valid_perf = df_oee_perf_fp[df_oee_perf_fp['GROSS_CAPAIAN_PER_JAM'] > 0].copy().reset_index(drop=True)
    if not df_oee_valid_perf.empty:
        df_oee_valid_perf['RATIO_PERFORMANCE'] = (df_oee_valid_perf['GROSS_CAPAIAN_PER_JAM'] / df_oee_valid_perf['TARGET_SUBBRAND_JAM']).clip(upper=1.0)
        performance_rate = df_oee_valid_perf['RATIO_PERFORMANCE'].mean()
    else:
        performance_rate = 0.85

    availability_rate = 1.0
    nilai_oee = (availability_rate * performance_rate * quality_rate) * 100

    kondisi_fp = df_sic['NAMA_MESIN'].astype(str).str.contains('Filling|Press', case=False, na=False) if not df_sic.empty else []
    total_output_fp = pd.to_numeric(df_sic[kondisi_fp]['OUTPUT_PCS'], errors='coerce').fillna(0).sum() if not df_sic.empty else 0

    return {
        'df_oee_perf': df_oee_perf,
        'df_oee_perf_fp': df_oee_perf_fp,
        'dict_target_subbrand': dict_target_subbrand,
        'total_reject_kemas_oee': total_reject_kemas_oee,
        'quality_rate': quality_rate,
        'performance_rate': performance_rate,
        'availability_rate': availability_rate,
        'nilai_oee': nilai_oee,
        'kondisi_fp': kondisi_fp,
        'total_output_fp': total_output_fp,
    }


def hitung_komponen_oee_per_line(df_line):
    """
    Komponen OEE per nomor mesin/line (identik dengan grafik 'Analisis Komponen OEE per Nomor/Line Mesin').
    df_line: baris df_oee_perf_fp untuk satu jenis mesin, wajib punya kolom LINE_DISPLAY.
    """
    oee_by_line = []
    for line_name, group in df_line.groupby('LINE_DISPLAY'):
        c_pcs = group['CAPAIN_PRODUKSI_PCS'].sum()
        rej_pcs = group['REJECT_PCS'].sum()
        rep_pcs = group['REPROSES_PCS'].sum()
        den_q = c_pcs + rej_pcs + rep_pcs
        q_line = (c_pcs / den_q * 100) if den_q > 0 else 100.0

        valid_perf = group[group['GROSS_CAPAIAN_PER_JAM'] > 0]
        p_line = (valid_perf['GROSS_CAPAIAN_PER_JAM'] / valid_perf['TARGET_SUBBRAND_JAM']).clip(upper=1.0).mean() * 100 if not valid_perf.empty else 85.0
        a_line = 100.0
        oee_line = (q_line / 100.0) * (p_line / 100.0) * (a_line / 100.0) * 100.0

        oee_by_line.append({'Nomor Mesin': line_name, 'OEE (%)': round(oee_line, 2), 'Performance (%)': round(p_line, 2),
                            'Quality (%)': round(q_line, 2), 'Availability (%)': round(a_line, 2)})
    return oee_by_line


def hitung_komponen_oee_per_kategori(df_oee_perf_fp):
    """
    Rata-rata komponen OEE untuk tiap kategori/jenis mesin = rata-rata nilai seluruh nomor mesin di kategori itu
    (nilai tiap nomor mesin dihitung persis seperti grafik per-line di dashboard).
    Return DataFrame: Kategori Mesin, Jumlah Line, OEE (%), Performance (%), Quality (%), Availability (%).
    """
    kolom = ['Kategori Mesin', 'Jumlah Line', 'OEE (%)', 'Performance (%)', 'Quality (%)', 'Availability (%)']
    if df_oee_perf_fp is None or df_oee_perf_fp.empty:
        return pd.DataFrame(columns=kolom)
    rows = []
    for kategori, grp in df_oee_perf_fp.groupby('NAMA_MESIN_KEY'):
        g = grp.copy()
        g['LINE_DISPLAY'] = g['NO_MESIN_LINE_KEY'].apply(format_line_number)
        per_line = pd.DataFrame(hitung_komponen_oee_per_line(g))
        if per_line.empty:
            continue
        rows.append({
            'Kategori Mesin': kategori,
            'Jumlah Line': len(per_line),
            'OEE (%)': per_line['OEE (%)'].mean(),
            'Performance (%)': per_line['Performance (%)'].mean(),
            'Quality (%)': per_line['Quality (%)'].mean(),
            'Availability (%)': per_line['Availability (%)'].mean(),
        })
    return pd.DataFrame(rows, columns=kolom).sort_values('Kategori Mesin').reset_index(drop=True)


def hitung_kpi_qc(df_qc, df_oee):
    """KPI QC (identik dengan Tab 2) + Reject Rate & Reproses Rate terhadap gross output."""
    tot_reject = pd.to_numeric(df_qc['REJECT_PCS'], errors='coerce').fillna(0).sum() if (df_qc is not None and 'REJECT_PCS' in df_qc.columns) else 0
    tot_reproses = pd.to_numeric(df_qc['REPROSES_PCS'], errors='coerce').fillna(0).sum() if (df_qc is not None and 'REPROSES_PCS' in df_qc.columns) else 0
    tot_capaian = pd.to_numeric(df_oee['CAPAIN_PRODUKSI_PCS'], errors='coerce').fillna(0).sum() if (df_oee is not None and 'CAPAIN_PRODUKSI_PCS' in df_oee.columns) else 0
    total_gross = tot_capaian + tot_reject + tot_reproses
    return {
        'total_reject': tot_reject,
        'total_reproses': tot_reproses,
        'total_defect': tot_reject + tot_reproses,
        'overall_quality': (tot_capaian / total_gross * 100) if total_gross > 0 else 100.0,
        'reject_rate': (tot_reject / total_gross * 100) if total_gross > 0 else 0.0,
        'reproses_rate': (tot_reproses / total_gross * 100) if total_gross > 0 else 0.0,
    }
