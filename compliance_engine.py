import re
from datetime import datetime, timedelta
import pandas as pd
import requests
import numpy as np

# ============================================================
# 1. HELPER NORMALISASI & TANGGAL FISKAL
# ============================================================

def parse_time_with_date(dt_base, time_raw):
    """
    Menggabungkan tanggal dari timestamp acuan dengan string jam operasional 
    (misal '15:14:00' atau '3:14:00 PM').
    Mencegah bug tanggal tidak cocok (mismatched date) pada evaluasi oeeTepatWaktu & Late Log.
    """
    if pd.isna(dt_base) or not time_raw or str(time_raw).strip() in ['', 'nan', 'None', '<NA>', '-']:
        return pd.NaT
    try:
        t_obj = pd.to_datetime(str(time_raw).strip(), format='mixed', errors='coerce')
        if pd.isna(t_obj):
            return pd.NaT
        return pd.Timestamp.combine(dt_base.date(), t_obj.time())
    except Exception:
        return pd.NaT

def extract_full_machine_name(row):
    """
    Mengecek seluruh kemungkinan nama kolom di Google Sheet / Supabase 
    untuk memastikan nama mesin dan nomor line selalu tergabung.
    """
    if isinstance(row, dict):
        get_val = lambda k: row.get(k, '')
    else:
        get_val = lambda k: row[k] if k in row.index else ''

    # 1. Cek jika MESIN_FULL / FULL_MESIN / mesin_full sudah terisi
    for col in ['MESIN_FULL', 'FULL_MESIN', 'NAMA_MESIN_FULL', 'mesin_full']:
        v = str(get_val(col)).strip()
        if v and v.lower() not in ['nan', 'none', '', '<na>']:
            return v
    
    # 2. Ambil Nama Mesin Basis
    nama_mesin = ""
    for col in ['NAMA_MESIN', 'NAMA MESIN', 'MESIN', 'PROSES', 'NAMA PROSES', 'nama_mesin', 'proses']:
        v = str(get_val(col)).strip()
        if v and v.lower() not in ['nan', 'none', '', '<na>']:
            nama_mesin = v
            break

    # 3. Ambil Nomor Line / Mesin
    no_line = ""
    for col in ['NO_MESIN_LINE', 'NO MESIN LINE', 'NO_MESIN', 'NO MESIN', 'LINE', 'NO_LINE', 'NO LINE', 'LINE_NO', 'LINE NO', 'NOMOR_LINE', 'NOMOR LINE', 'no_mesin_line']:
        v = str(get_val(col)).strip().replace('.0', '')
        if v and v.lower() not in ['nan', 'none', '', '<na>', '0']:
            no_line = v
            break

    if no_line and no_line not in nama_mesin:
        return f"{nama_mesin} - {no_line}"
    
    return nama_mesin or "Mesin Tidak Diketahui"

def normalisasi_mesin(nama_mentah):
    if not nama_mentah or pd.isna(nama_mentah):
        return "Mesin Tidak Diketahui"
    
    text = re.sub(r'\s+', ' ', str(nama_mentah)).strip().upper()
    text = re.sub(r'^([A-Z\s]+?)\s*[-_]?\s*(\d+)$', r'\1 - \2', text)
    
    match_num = re.search(r'\d+', text)
    nomor = str(int(match_num.group(0))) if match_num else ""
    
    if "MIXING" in text and "SERBUK" not in text:
        return f"MO - {nomor or 'Tanpa No'}"
    if any(x in text for x in ["TIMBANG", "PENIMBANGAN", "TB"]) or text.startswith(("TB ", "TB-")):
        return f"TIMBANG - {nomor or 'Tanpa No'}"
    if "TRIROLER" in text or "THREE ROLL" in text or text.startswith(("TR ", "TR-")) or text.startswith("TR"):
        return f"TR - {nomor or 'Tanpa No'}"
    if "MIXING SERBUK" in text or text.startswith(("MS ", "MS-")) or text.startswith("MS"):
        return f"MS - {nomor or 'Tanpa No'}"
    if "FILLING OTOMATIS" in text or text.startswith("FO"):
        return f"FO - {nomor or 'Tanpa No'}"
    if "FILLING MANUAL" in text or text.startswith("FM"):
        return f"FM - {nomor or 'Tanpa No'}"
    if "PRESS" in text:
        return f"Press - {nomor or 'Tanpa No'}"
    if "PACKING LINE" in text or text.startswith("PL"):
        return f"PACKING LINE - {nomor or 'Tanpa No'}"
    
    return text

def normalisasi_batch(batch_mentah):
    if not batch_mentah or pd.isna(batch_mentah):
        return ""
    b = str(batch_mentah).strip().upper()
    b = re.sub(r'\s+', '', b)
    b = re.sub(r'^[\'\"]|[\'\"]$', '', b)
    return b

def normalisasi_operator(nama_mentah):
    if not nama_mentah or pd.isna(nama_mentah):
        return "-"
    op = str(nama_mentah).strip().upper()
    op = re.sub(r'\s+', ' ', op)
    return op if op not in ['NAN', 'NONE', '', '<NA>', '0'] else "-"

def is_qc_status_patuh(status_raw):
    """
    Memeriksa kepatuhan status QC secara fleksibel (case-insensitive)
    untuk mengakomodasi berbagai variasi teks (STATUS_MONITORING & STATUS_ALUR).
    """
    if not status_raw or pd.isna(status_raw):
        return False
    s = str(status_raw).strip().upper()
    valid_keywords = ["SELESAI", "AKHIR", "PASSED", "PASS", "OK", "RELEASED"]
    return any(kw in s for kw in valid_keywords)

def get_fiscal_date(dt_ts, oee_batches_today, oee_batches_yesterday, batch_no):
    if pd.isna(dt_ts) or dt_ts is None:
        return None
    
    jam = dt_ts.hour
    menit = dt_ts.minute
    ts_date_str = dt_ts.strftime('%Y-%m-%d')
    yesterday_str = (dt_ts - timedelta(days=1)).strftime('%Y-%m-%d')
    
    is_overlap = (jam == 6 and menit < 15)
    
    if is_overlap:
        in_today = batch_no in oee_batches_today.get(ts_date_str, {})
        in_yesterday = batch_no in oee_batches_yesterday.get(yesterday_str, {})
        return ts_date_str if (in_today and not in_yesterday) else yesterday_str
    else:
        return yesterday_str if jam < 6 else ts_date_str

# ============================================================
# 2. ENGINE AUDIT KEPATUHAN & AGREGASI COMPLIANCE
# ============================================================

def audit_compliance_range(df_oee, df_sic, df_qc, start_date_str, end_date_str):
    master_log = {}
    oee_batches_per_date = {}
    global_qc_map = {}
    
    total_qc_indiv, patuh_qc_indiv = 0, 0
    global_reproses, global_reject = 0, 0

    # A. PROSES DATA OEE
    for _, row in df_oee.iterrows():
        ts_oee = pd.to_datetime(row.get('TIMESTAMP', row.get('timestamp')), errors='coerce')
        tgl_prod_raw = row.get('TANGGAL_PRODUKSI', row.get('tanggal_produksi', row.get('TIMESTAMP', row.get('timestamp'))))
        tgl_prod_oee = pd.to_datetime(tgl_prod_raw, errors='coerce')
        
        if pd.isna(ts_oee):
            continue
            
        ts_oee_str = ts_oee.strftime('%Y-%m-%d')
        tgl_prod_str = tgl_prod_oee.strftime('%Y-%m-%d') if pd.notna(tgl_prod_oee) else ts_oee_str
        
        is_before_cutoff = (ts_oee.hour < 6) or (ts_oee.hour == 6 and ts_oee.minute < 15)
        if ts_oee_str != tgl_prod_str:
            tgl_acuan = tgl_prod_str
        else:
            tgl_acuan = (ts_oee - timedelta(days=1)).strftime('%Y-%m-%d') if is_before_cutoff else ts_oee_str
            
        batch = normalisasi_batch(row.get('NO_BATCH', row.get('no_batch')))
        if not batch:
            continue
            
        if tgl_acuan not in oee_batches_per_date:
            oee_batches_per_date[tgl_acuan] = {}
        oee_batches_per_date[tgl_acuan][batch] = True
        
        if start_date_str <= tgl_acuan <= end_date_str:
            raw_mesin_str = extract_full_machine_name(row)
            mesin = normalisasi_mesin(raw_mesin_str)
            output_fg = pd.to_numeric(row.get('CAPAIN_PRODUKSI_PCS', row.get('capain_produksi_pcs', 0)), errors='coerce') or 0
            op = normalisasi_operator(row.get('OPERATOR', row.get('operator', row.get('NAMA_OPERATOR'))))
            
            j_mulai_raw = row.get('JAM_MULAI_AKTUAL', row.get('jam_mulai_aktual', row.get('JAM_MULAI', row.get('jam_mulai'))))
            j_selesai_raw = row.get('JAM_SELESAI_AKTUAL', row.get('jam_selesai_aktual', row.get('JAM_SELESAI', row.get('jam_selesai'))))
            
            jam_mulai = parse_time_with_date(ts_oee, j_mulai_raw)
            selesai = parse_time_with_date(ts_oee, j_selesai_raw)
            
            key = f"{tgl_acuan}_{batch}_{mesin}"
            
            if key not in master_log:
                master_log[key] = {
                    'tanggal': tgl_acuan, 'batch': batch, 'mesin': mesin, 'operator': op,
                    'oee': False, 'sic': False, 'oeeOutput': 0, 'sicOutput': 0,
                    'picOee': op, 'picSic': "-", 'picQc': "-", 'oeeTepatWaktu': True,
                    'sicRows': 0, 'sicLastStatus': "", 'jamMulaiOEE': None, 'tsSICPertama': None
                }
                
            master_log[key]['oee'] = True
            master_log[key]['oeeOutput'] += output_fg
            master_log[key]['picOee'] = op
            if op != "-": 
                master_log[key]['operator'] = op
            if pd.notna(jam_mulai): 
                master_log[key]['jamMulaiOEE'] = jam_mulai
            
            if pd.notna(selesai) and pd.notna(ts_oee) and ts_oee < (selesai - timedelta(minutes=30)):
                master_log[key]['oeeTepatWaktu'] = False

    # B. PROSES DATA SIC
    for _, row in df_sic.iterrows():
        ts = pd.to_datetime(row.get('TIMESTAMP', row.get('timestamp')), errors='coerce')
        if pd.isna(ts):
            continue
            
        batch = normalisasi_batch(row.get('NO_BATCH', row.get('no_batch')))
        if not batch:
            continue
            
        fiscal_date_sic = get_fiscal_date(ts, oee_batches_per_date, oee_batches_per_date, batch)
        
        if fiscal_date_sic and start_date_str <= fiscal_date_sic <= end_date_str:
            raw_mesin_str = extract_full_machine_name(row)
            mesin = normalisasi_mesin(raw_mesin_str)
            output_jam = pd.to_numeric(row.get('OUTPUT_PCS', row.get('output_pcs', 0)), errors='coerce') or 0
            op = normalisasi_operator(row.get('OPERATOR', row.get('operator', row.get('NAMA_OPERATOR'))))
            status = str(row.get('STATUS_ALUR', row.get('status_alur', ''))).strip()
            
            key = f"{fiscal_date_sic}_{batch}_{mesin}"
            
            if key not in master_log:
                master_log[key] = {
                    'tanggal': fiscal_date_sic, 'batch': batch, 'mesin': mesin, 'operator': op,
                    'oee': False, 'sic': False, 'oeeOutput': 0, 'sicOutput': 0,
                    'picOee': "-", 'picSic': op, 'picQc': "-", 'oeeTepatWaktu': True,
                    'sicRows': 0, 'sicLastStatus': "", 'jamMulaiOEE': None, 'tsSICPertama': None
                }
                
            master_log[key]['sic'] = True
            master_log[key]['sicOutput'] += output_jam
            master_log[key]['picSic'] = op
            if master_log[key]['operator'] == "-": 
                master_log[key]['operator'] = op
            master_log[key]['sicRows'] += 1
            master_log[key]['sicLastStatus'] = status
            
            if master_log[key]['tsSICPertama'] is None or ts < master_log[key]['tsSICPertama']:
                master_log[key]['tsSICPertama'] = ts

    # C. PROSES DATA QC
    for _, row in df_qc.iterrows():
        ts_qc = pd.to_datetime(row.get('TIMESTAMP', row.get('timestamp')), errors='coerce')
        if pd.isna(ts_qc):
            continue
            
        batch = normalisasi_batch(row.get('NO_BATCH', row.get('no_batch')))
        if not batch:
            continue
            
        fiscal_date_qc = get_fiscal_date(ts_qc, oee_batches_per_date, oee_batches_per_date, batch)
        
        if fiscal_date_qc and start_date_str <= fiscal_date_qc <= end_date_str:
            raw_mesin_str = extract_full_machine_name(row)
            mesin = normalisasi_mesin(raw_mesin_str)
            reproses = pd.to_numeric(row.get('REPROSES_PCS', row.get('reproses_pcs', 0)), errors='coerce') or 0
            reject = pd.to_numeric(row.get('REJECT_PCS', row.get('reject_pcs', 0)), errors='coerce') or 0
            op_qc = normalisasi_operator(row.get('INSPEKTOR', row.get('OPERATOR', row.get('operator', row.get('NAMA_OPERATOR')))))
            status = str(row.get('STATUS_MONITORING', row.get('status_monitoring', row.get('STATUS_ALUR', row.get('status_alur', ''))))).strip()
            
            total_qc_indiv += 1
            if is_qc_status_patuh(status):
                patuh_qc_indiv += 1
                
            qc_lookup_key = f"{fiscal_date_qc}_{batch}_{mesin}"
            if qc_lookup_key not in global_qc_map:
                global_qc_map[qc_lookup_key] = {'terisi': True, 'rows': 0, 'lastStatus': status, 'user': op_qc}
            global_qc_map[qc_lookup_key]['rows'] += 1
            global_qc_map[qc_lookup_key]['lastStatus'] = status
            if op_qc != "-":
                global_qc_map[qc_lookup_key]['user'] = op_qc
            
            global_reproses += reproses
            global_reject += reject

    # D. DUAL EVALUATION ENGINE & AUDIT MISMATCH
    mismatch_list = []
    prod_user_stats = {}
    qc_user_stats = {}
    
    dept_total_oee_sic, dept_patuh_oee_sic = 0, 0
    dept_total_qc, dept_patuh_qc = 0, 0
    total_batch_akurasi_diuji, total_persentase_akurasi = 0, 0

    for key, item in master_log.items():
        qc_key = f"{item['tanggal']}_{item['batch']}_{item['mesin']}"
        has_qc_data = qc_key in global_qc_map
        if has_qc_data:
            item['picQc'] = global_qc_map[qc_key]['user']
            
        mesin_upper = item['mesin'].upper()
        is_proses_timbang = any(x in mesin_upper for x in ["TIMBANG", "PENIMBANGAN", "TB"])
        is_eksklusi_akurasi = any(x in mesin_upper for x in ["MO", "MS", "TR", "HAMMER"]) or is_proses_timbang
        
        # Agregasi Tim Produksi
        op_prod = item['operator']
        if op_prod and op_prod != "-":
            if op_prod not in prod_user_stats:
                prod_user_stats[op_prod] = {'total': 0, 'patuh': 0}
            prod_user_stats[op_prod]['total'] += 1
            
            oee_ok = item['oee'] and item['oeeTepatWaktu']
            sic_ok = item['sic'] and (item['sicLastStatus'].upper() in ["SELESAI PROSES", "SELESAI", "AKHIR", "ISTIRAHAT"])
            if oee_ok and sic_ok:
                prod_user_stats[op_prod]['patuh'] += 1

        if item['oee'] or item['sic']:
            dept_total_oee_sic += 1
            if item['oee'] and item['sic']:
                oee_ok = item['oeeTepatWaktu']
                sic_ok = item['sicLastStatus'].upper() in ["SELESAI PROSES", "SELESAI", "AKHIR", "ISTIRAHAT"]
                if oee_ok and sic_ok: 
                    dept_patuh_oee_sic += 1
                elif oee_ok and item['sicRows'] > 1: 
                    dept_patuh_oee_sic += 0.7
                
            if not is_proses_timbang:
                dept_total_qc += 1
                if has_qc_data:
                    qc_status = global_qc_map[qc_key]['lastStatus']
                    qc_rows = global_qc_map[qc_key]['rows']
                    if is_qc_status_patuh(qc_status): 
                        dept_patuh_qc += 1
                    elif qc_rows > 1: 
                        dept_patuh_qc += 0.7

        # Audit Kelalaian Input
        missing = []
        if not item['oee']: missing.append("OEE")
        if not item['sic']: missing.append("SIC")
        if not has_qc_data and not is_proses_timbang: missing.append("QC")
        
        if missing:
            if len(missing) == 1: ket = f"{missing[0]} Kosong"
            elif len(missing) == 2: ket = f"{missing[0]} dan {missing[1]} Kosong"
            else: ket = "OEE, SIC, dan QC Kosong"
            
            present = []
            if item['oee'] and item['picOee'] != "-": present.append({'name': "OEE", 'pic': item['picOee']})
            if item['sic'] and item['picSic'] != "-": present.append({'name': "SIC", 'pic': item['picSic']})
            if has_qc_data and item['picQc'] != "-" and not is_proses_timbang: present.append({'name': "QC", 'pic': item['picQc']})
            
            teks_missing = " & ".join(missing) + " Belum Diinput"
            teks_present = ""
            
            if len(present) == 1:
                teks_present = f"({present[0]['name']} oleh: {present[0]['pic']})"
            elif len(present) == 2:
                if present[0]['pic'] == present[1]['pic']:
                    teks_present = f"({present[0]['name']}/{present[1]['name']} oleh: {present[0]['pic']})"
                else:
                    teks_present = f"({present[0]['name']} oleh: {present[0]['pic']} dan {present[1]['name']} oleh: {present[1]['pic']})"
            elif len(present) == 3:
                if present[0]['pic'] == present[1]['pic'] and present[1]['pic'] == present[2]['pic']:
                    teks_present = f"(OEE/SIC/QC oleh: {present[0]['pic']})"
                else:
                    teks_str = " dan ".join([f"{p['name']} oleh: {p['pic']}" for p in present])
                    teks_present = f"({teks_str})"
                    
            teks_pic_log = f"{teks_missing} {teks_present}".strip()
            
            mismatch_list.append({
                'tipe': "KELALAIAN_INPUT",
                'tanggal': item['tanggal'],
                'mesin': item['mesin'],
                'batch': item['batch'],
                'keterangan': ket,
                'pic': teks_pic_log
            })

        # Audit Late Log (>30 Menit)
        if item['oee'] and item['sic'] and pd.notna(item['jamMulaiOEE']) and pd.notna(item['tsSICPertama']):
            h_oee, m_oee = item['jamMulaiOEE'].hour, item['jamMulaiOEE'].minute
            h_sic, m_sic = item['tsSICPertama'].hour, item['tsSICPertama'].minute
            
            tot_oee_m = (h_oee * 60) + m_oee
            tot_sic_m = (h_sic * 60) + m_sic
            
            diff_m = tot_sic_m - tot_oee_m
            if diff_m < -720: diff_m += 1440
                
            if diff_m > 30:
                mismatch_list.append({
                    'tipe': "KELALAIAN_INPUT",
                    'tanggal': item['tanggal'],
                    'mesin': item['mesin'],
                    'batch': item['batch'],
                    'keterangan': f"Input Terdelay {diff_m} Menit (Mulai OEE: {item['jamMulaiOEE'].strftime('%H:%M')} | SIC Pertama: {item['tsSICPertama'].strftime('%H:%M')})",
                    'pic': f"(OEE/SIC oleh: {item['operator']})"
                })

        # Audit Akurasi Output
        if item['oee'] and item['sic'] and not is_eksklusi_akurasi:
            selisih = item['oeeOutput'] - item['sicOutput']
            akurasi_batch = 100.0
            if item['oeeOutput'] > 0:
                akurasi_batch = (1.0 - (abs(selisih) / item['oeeOutput'])) * 100.0
                if akurasi_batch < 0: akurasi_batch = 0.0
            elif item['sicOutput'] > 0:
                akurasi_batch = 0.0
                
            total_batch_akurasi_diuji += 1
            total_persentase_akurasi += akurasi_batch
            
            if selisih != 0:
                mismatch_list.append({
                    'tipe': "SELISIH_OUTPUT",
                    'tanggal': item['tanggal'],
                    'mesin': item['mesin'],
                    'batch': item['batch'],
                    'oeeOut': item['oeeOutput'],
                    'sicOut': item['sicOutput'],
                    'selisih': selisih,
                    'akurasi': f"{round(akurasi_batch)}%",
                    'pic': item['operator']
                })

    for qc_key, qc_item in global_qc_map.items():
        user_qc = qc_item['user']
        if user_qc and user_qc != "-":
            if user_qc not in qc_user_stats:
                qc_user_stats[user_qc] = {'total': 0, 'patuh': 0}
            qc_user_stats[user_qc]['total'] += 1
            
            if is_qc_status_patuh(qc_item['lastStatus']):
                qc_user_stats[user_qc]['patuh'] += 1
            elif qc_item['rows'] > 1:
                qc_user_stats[user_qc]['patuh'] += 0.7

    if dept_total_oee_sic > 0:
        skor_dept_oee_sic_str = f"{round((dept_patuh_oee_sic / dept_total_oee_sic) * 100)}% ({round(dept_patuh_oee_sic)}/{dept_total_oee_sic})"
    else:
        skor_dept_oee_sic_str = "N/A (Tidak ada data)"

    if dept_total_qc > 0:
        skor_dept_qc_str = f"{round((dept_patuh_qc / dept_total_qc) * 100)}% ({round(dept_patuh_qc)}/{dept_total_qc})"
    else:
        skor_dept_qc_str = "N/A (Tidak ada data)"

    if total_batch_akurasi_diuji > 0:
        skor_akurasi_str = f"{round(total_persentase_akurasi / total_batch_akurasi_diuji)}%"
    else:
        skor_akurasi_str = "N/A (Tidak ada data)"

    prod_chart_list = []
    for op, s in prod_user_stats.items():
        rate = round((s['patuh'] / s['total']) * 100, 2) if s['total'] > 0 else 0.0
        prod_chart_list.append({'Operator': op, 'Total Batch': s['total'], 'Compliance Rate (%)': rate})
    df_prod_compliance = pd.DataFrame(prod_chart_list)

    qc_chart_list = []
    for op, s in qc_user_stats.items():
        rate = round((s['patuh'] / s['total']) * 100, 2) if s['total'] > 0 else 0.0
        qc_chart_list.append({'Inspector QC': op, 'Total Batch': s['total'], 'Compliance Rate (%)': rate})
    df_qc_compliance = pd.DataFrame(qc_chart_list)

    return {
        'totalBatches': dept_total_oee_sic,
        'skorDeptOEESIC': skor_dept_oee_sic_str,
        'skorDeptQC': skor_dept_qc_str,
        'skorAkurasi': skor_akurasi_str,
        'mismatchList': mismatch_list,
        'dfProdCompliance': df_prod_compliance,
        'dfQCCompliance': df_qc_compliance
    }

# ============================================================
# 3. GENERATOR RAPPORT CARD INDIVIDUAL & UNASSIGNED QC
# ============================================================

def get_rapport_cards_range(df_oee, df_sic, df_qc, start_date_str, end_date_str):
    audit_res = audit_compliance_range(df_oee, df_sic, df_qc, start_date_str, end_date_str)
    mismatch_list = audit_res.get('mismatchList', [])
    
    operator_map = {}
    qc_pending_list = []

    def init_operator(name):
        if not name or name == "-" or name == "QC": return
        if name not in operator_map:
            operator_map[name] = {
                'operator': name,
                'mesinList': set(),
                'kelalaianInput': [],
                'selisihOutput': [],
                'totalIssue': 0
            }

    for item in mismatch_list:
        if item['tipe'] == "KELALAIAN_INPUT":
            ket = item.get('keterangan', '')

            if ket == "QC Kosong":
                qc_pending_list.append({
                    'tanggal': item.get('tanggal', ''),
                    'mesin': item['mesin'],
                    'batch': item['batch'],
                    'keterangan': "Form QC Belum Diisi",
                    'picLog': item['pic']
                })
                continue

            ket_prod = ket.replace(" dan QC", "").replace("QC dan ", "").replace("QC Kosong", "").strip()
            item_prod = {
                'tanggal': item.get('tanggal', ''),
                'mesin': item['mesin'],
                'batch': item['batch'],
                'keterangan': ket_prod or "Form Produksi Kosong",
                'pic': item['pic']
            }

            text_pic = item.get('pic', '')
            matches = re.findall(r'(?:OEE|SIC|OEE/SIC)\s+oleh:\s*([A-Za-z0-9\s\.\-]+?)(?=\s*dan|\s*oleh|\s*\)|$)', text_pic, flags=re.IGNORECASE)
            
            found_any = False
            for name_found in matches:
                clean_name = name_found.strip()
                if clean_name and clean_name != "-" and clean_name.upper() != "QC":
                    init_operator(clean_name)
                    if clean_name in operator_map:
                        operator_map[clean_name]['kelalaianInput'].append(item_prod)
                        operator_map[clean_name]['totalIssue'] += 1
                        operator_map[clean_name]['mesinList'].add(item['mesin'])
                        found_any = True

            if not found_any:
                fallback_name = "Lainnya / Non-PIC Produksi"
                init_operator(fallback_name)
                operator_map[fallback_name]['kelalaianInput'].append(item_prod)
                operator_map[fallback_name]['totalIssue'] += 1

        elif item['tipe'] == "SELISIH_OUTPUT":
            op_name = item['pic']
            init_operator(op_name)
            if op_name in operator_map:
                operator_map[op_name]['selisihOutput'].append(item)
                operator_map[op_name]['totalIssue'] += 1
                operator_map[op_name]['mesinList'].add(item['mesin'])

    result_operators = []
    for op, data in operator_map.items():
        if data['totalIssue'] == 0: continue

        status = "COMPLIANT"
        if data['totalIssue'] > 0:
            status = "REVIEW OUTPUT" if data['selisihOutput'] else "NON-COMPLIANT"

        result_operators.append({
            'operator': op,
            'mesinList': ", ".join(sorted(data['mesinList'])) if data['mesinList'] else "Multi-Mesin",
            'totalIssue': data['totalIssue'],
            'kelalaianInput': data['kelalaianInput'],
            'selisihOutput': data['selisihOutput'],
            'statusCompliance': status
        })

    return {
        'totalBatches': audit_res['totalBatches'],
        'skorDeptOEESIC': audit_res['skorDeptOEESIC'],
        'skorDeptQC': audit_res['skorDeptQC'],
        'skorAkurasi': audit_res['skorAkurasi'],
        'operatorCards': result_operators,
        'qcPendingList': qc_pending_list,
        'dfProdCompliance': audit_res['dfProdCompliance'],
        'dfQCCompliance': audit_res['dfQCCompliance']
    }

# ============================================================
# 4. INTEGRASI INTEGRATOR WHATSAPP FONNTE
# ============================================================

def send_fonnte_wa(token, target, message):
    url = "https://api.fonnte.com/send"
    headers = {"Authorization": token}
    payload = {"target": target, "message": message}
    
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=10)
        return response.json()
    except Exception as e:
        return {"status": False, "reason": str(e)}

def build_wa_msg_operator(card_data, periode_str):
    msg = "📋 *RAPPORT CARD COMPLIANCE OPERATOR*\n"
    msg += f"👤 Operator: *{card_data['operator']}*\n"
    msg += f"📅 Periode: {periode_str}\n"
    msg += f"🏭 Mesin: {card_data['mesinList']}\n"
    msg += "────────────────────────\n"
    msg += f"🎯 *STATUS*: *{card_data['statusCompliance']}*\n"
    msg += f"⚠️ *TOTAL CATATAN*: {card_data['totalIssue']} Item\n\n"

    if card_data.get('kelalaianInput'):
        msg += "❌ *1. Kelalaian Input Form Produksi / Late Log:*\n"
        for k in card_data['kelalaianInput']:
            tgl_k = f" ({k['tanggal']})" if k.get('tanggal') else ""
            msg += f" • {k['mesin']}{tgl_k} | Batch: {k['batch']}\n   ↳ _{k['keterangan']}_\n"
        msg += "\n"

    if card_data.get('selisihOutput'):
        msg += "⚠️ *2. Selisih Output Data (OEE vs SIC):*\n"
        for s in card_data['selisihOutput']:
            tgl_s = f" ({s['tanggal']})" if s.get('tanggal') else ""
            sign = "+" if s['selisih'] > 0 else ""
            msg += f" • {s['mesin']}{tgl_s} | Batch: {s['batch']}\n   ↳ Akurasi: {s['akurasi']} (OEE: {s['oeeOut']} vs SIC: {s['sicOut']} | Diff: {sign}{s['selisih']})\n"
        msg += "\n"

    msg += "────────────────────────\n_Mohon diperbaiki pada shift berjalan. Terima kasih!_"
    return msg

def build_wa_msg_qc(qc_list, periode_str):
    msg = "🚨 *ALERT UNASSIGNED LOG QC (MISSED CHECKLIST)* 🚨\n"
    msg += f"📅 Periode: {periode_str}\n"
    msg += "────────────────────────\n"
    msg += "Berikut batch produksi yang *BELUM diisi Form QC-nya*:\n\n"

    for idx, item in enumerate(qc_list, 1):
        tgl_q = f" ({item['tanggal']})" if item.get('tanggal') else ""
        msg += f"{idx}. *{item['mesin']}*{tgl_q}\n"
        msg += f"   • Batch: {item['batch']}\n"
        msg += f"   • Status: _{item['keterangan']}_\n"
        msg += f"   • {item['picLog']}\n\n"

    msg += "────────────────────────\n_Mohon Supervisor QC menindaklanjuti verifikasi lini tersebut._"
    return msg

# ============================================================
# 5. TREN COMPLIANCE & WIP MONITORING
# ============================================================

def get_daily_compliance_trend(df_oee, df_sic, df_qc, start_date_str, end_date_str):
    date_range = pd.date_range(start=start_date_str, end=end_date_str)
    daily_list = []

    for dt in date_range:
        d_str = dt.strftime('%Y-%m-%d')
        res = audit_compliance_range(df_oee, df_sic, df_qc, d_str, d_str)

        skor_prod_str = res.get('skorDeptOEESIC', '')
        skor_qc_str = res.get('skorDeptQC', '')

        rate_prod = float(skor_prod_str.split('%')[0]) if '%' in skor_prod_str else None
        rate_qc = float(skor_qc_str.split('%')[0]) if '%' in skor_qc_str else None

        if rate_prod is not None or rate_qc is not None:
            daily_list.append({
                'Tanggal': dt.strftime('%d %b'),
                'Tim Produksi (OEE & SIC)': rate_prod,
                'Tim QC (Inspector)': rate_qc
            })

    return pd.DataFrame(daily_list)

def get_wip_operational(df_sic, df_oee):
    logs_sic = df_sic[['TIMESTAMP', 'NO_BATCH', 'NAMA_MESIN', 'SUBBRAND_MASTER', 'OUTPUT_PCS']].copy() if not df_sic.empty else pd.DataFrame()
    
    logs_oee = df_oee[['TIMESTAMP', 'NO_BATCH', 'NAMA_MESIN', 'SUBBRAND_MASTER', 'CAPAIN_PRODUKSI_PCS']].rename(
        columns={'CAPAIN_PRODUKSI_PCS': 'OUTPUT_PCS'}
    ).copy() if not df_oee.empty else pd.DataFrame()

    df_all = pd.concat([logs_sic, logs_oee], ignore_index=True)
    if df_all.empty:
        return pd.DataFrame()

    df_all['NO_BATCH'] = df_all['NO_BATCH'].astype(str).str.strip()
    df_all['NAMA_MESIN'] = df_all['NAMA_MESIN'].astype(str).str.strip().str.upper()
    df_all['OUTPUT_PCS'] = pd.to_numeric(df_all['OUTPUT_PCS'], errors='coerce').fillna(0)
    df_all['TIMESTAMP'] = pd.to_datetime(df_all['TIMESTAMP'], errors='coerce')
    
    df_all = df_all[~df_all['NO_BATCH'].isin(['', '-', 'NAN', 'NONE', '<NA>'])].dropna(subset=['TIMESTAMP'])
    df_all = df_all.sort_values(by=['NO_BATCH', 'TIMESTAMP'])

    def kat_proses(nama_mesin):
        m = str(nama_mesin).upper()
        if 'TIMBANG' in m:
            return 'EXCLUDE'
        elif any(x in m for x in ['MIXING', 'MO', 'MS']):
            return 'MIXING'
        elif any(x in m for x in ['FILLING', 'PRESS', 'FO', 'FM']):
            return 'FILLING_PRESS'
        elif any(x in m for x in ['PACKING', 'PACK', 'KEMAS']):
            return 'PACKING'
        return 'OTHER'

    df_all['KATEGORI_PROSES'] = df_all['NAMA_MESIN'].apply(kat_proses)
    df_all = df_all[df_all['KATEGORI_PROSES'] != 'EXCLUDE'].copy()

    wip_records = []
    for no_batch, group in df_all.groupby('NO_BATCH'):
        subbrand_series = group['SUBBRAND_MASTER'].replace({'UNKNOWN': np.nan, '': np.nan}).dropna()
        subbrand = subbrand_series.iloc[0] if not subbrand_series.empty else '-'

        g_mix = group[group['KATEGORI_PROSES'] == 'MIXING']
        g_fp = group[group['KATEGORI_PROSES'] == 'FILLING_PRESS']
        g_pack = group[group['KATEGORI_PROSES'] == 'PACKING']

        ts_mix_start = g_mix['TIMESTAMP'].min() if not g_mix.empty else pd.NaT
        out_mix_kg = 0.0
        if not g_mix.empty:
            valid_mix_out = g_mix[g_mix['OUTPUT_PCS'] > 0]['OUTPUT_PCS']
            out_mix_kg = float(valid_mix_out.iloc[-1]) if not valid_mix_out.empty else float(g_mix['OUTPUT_PCS'].iloc[-1])

        ts_fp_start = g_fp['TIMESTAMP'].min() if not g_fp.empty else pd.NaT
        out_fp_kumulatif = g_fp['OUTPUT_PCS'].sum() if not g_fp.empty else 0

        ts_pack_start = g_pack['TIMESTAMP'].min() if not g_pack.empty else pd.NaT
        out_pack_kumulatif = g_pack['OUTPUT_PCS'].sum() if not g_pack.empty else 0

        durasi_mix_fp_str = "-"
        if pd.notna(ts_mix_start) and pd.notna(ts_fp_start):
            diff_sec = (ts_fp_start - ts_mix_start).total_seconds()
            if diff_sec >= 0:
                hours = int(diff_sec // 3600)
                mins = int((diff_sec % 3600) // 60)
                durasi_mix_fp_str = f"{hours}j {mins}m" if hours > 0 else f"{mins}m"

        durasi_fp_pack_str = "-"
        if pd.notna(ts_fp_start) and pd.notna(ts_pack_start):
            diff_sec = (ts_pack_start - ts_fp_start).total_seconds()
            if diff_sec >= 0:
                hours = int(diff_sec // 3600)
                mins = int((diff_sec % 3600) // 60)
                durasi_fp_pack_str = f"{hours}j {mins}m" if hours > 0 else f"{mins}m"

        ts_first = group['TIMESTAMP'].min()
        ts_last = group['TIMESTAMP'].max()
        durasi_total_str = "-"
        if pd.notna(ts_first) and pd.notna(ts_last):
            diff_sec = (ts_last - ts_first).total_seconds()
            hours = int(diff_sec // 3600)
            mins = int((diff_sec % 3600) // 60)
            durasi_total_str = f"{hours}j {mins}m" if hours > 0 else f"{mins}m"

        selisih_fp_pack = out_fp_kumulatif - out_pack_kumulatif
        mulai_tgl_jam = ts_mix_start.strftime('%d/%m/%Y %H:%M') if pd.notna(ts_mix_start) else ts_first.strftime('%d/%m/%Y %H:%M')

        wip_records.append({
            'No Batch': no_batch,
            'Subbrand': subbrand,
            'Mulai Mixing (Tgl & Jam)': mulai_tgl_jam,
            'Proses 1 - Mixing (Kg)': out_mix_kg,
            'Durasi (Mixing ➔ FP)': durasi_mix_fp_str,
            'Proses 2 - Filling/Press (Pcs)': out_fp_kumulatif,
            'Durasi (FP ➔ Packing)': durasi_fp_pack_str,
            'Proses 3 - Packing (Pcs)': out_pack_kumulatif,
            'Selisih (FP vs Packing) (Pcs)': selisih_fp_pack,
            'Total Lead Time': durasi_total_str
        })

    return pd.DataFrame(wip_records)

def get_age_mixing(df_sic, df_oee):
    comb = []
    for df, col_out, col_m in [(df_sic, 'OUTPUT_PCS', 'NAMA_MESIN'), (df_oee, 'CAPAIN_PRODUKSI_PCS', 'PROSES')]:
        if not df.empty:
            for _, r in df.iterrows():
                b = str(r.get('NO_BATCH', '')).strip()
                m = str(r.get(col_m, r.get('NAMA_MESIN', ''))).upper()
                ts = pd.to_datetime(r.get('TIMESTAMP'), errors='coerce')
                if b and b.upper() not in ['NAN', 'NONE', ''] and pd.notna(ts):
                    comb.append({
                        'NO_BATCH': b,
                        'SUBBRAND': r.get('SUBBRAND_MASTER', r.get('SUBBRAND', '-')),
                        'MESIN': m,
                        'TIMESTAMP': ts
                    })
    
    df_c = pd.DataFrame(comb)
    if df_c.empty:
        return pd.DataFrame()

    age_rows = []
    for batch_no, group in df_c.groupby('NO_BATCH'):
        subbrand = group['SUBBRAND'].iloc[0]
        
        mix_data = group[group['MESIN'].str.contains('MIXING|MO|MS', case=False, na=False)]
        fill_data = group[group['MESIN'].str.contains('FILLING|FO|FM|PRESS', case=False, na=False)]
        
        if not mix_data.empty:
            ts_mix_done = mix_data['TIMESTAMP'].max()
            
            if not fill_data.empty:
                ts_fill_start = fill_data['TIMESTAMP'].min()
                durasi_jam = (ts_fill_start - ts_mix_done).total_seconds() / 3600.0
                status_aging = "Sudah Filling"
            else:
                now_ts = pd.Timestamp.now()
                durasi_jam = (now_ts - ts_mix_done).total_seconds() / 3600.0
                status_aging = "Menunggu Filling (Ruahan)"

            durasi_jam = max(0.0, round(durasi_jam, 1))
            
            age_rows.append({
                'No Batch': batch_no,
                'Subbrand': subbrand,
                'Waktu Selesai Mixing': ts_mix_done.strftime('%d/%m %H:%M'),
                'Waktu Mulai Filling': ts_fill_start.strftime('%d/%m %H:%M') if not fill_data.empty else 'Belum Mulai',
                'Umur Ruahan (Jam)': durasi_jam,
                'Status': status_aging
            })

    df_age = pd.DataFrame(age_rows)
    if not df_age.empty:
        df_age = df_age.sort_values(by='Umur Ruahan (Jam)', ascending=False)
        
    return df_age
