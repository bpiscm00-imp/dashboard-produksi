import streamlit as st
import pandas as pd
import numpy as np
import gspread
import plotly.express as px
from oauth2client.service_account import ServiceAccountCredentials

st.set_page_config(page_title="Dashboard Produksi", layout="wide")

@st.cache_resource
def init_connection():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    # Membaca kredensial dari Streamlit Secrets
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
        # Memastikan karakter newline (\n) pada private_key terbaca dengan benar
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        # Fallback lokal jika dijalankan di komputer sendiri
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        
    client = gspread.authorize(creds)
    return client

@st.cache_data(ttl=600)
def load_data():
    client = init_connection()
    sheet = client.open("Raw Data - Produksi - Cleansing") 
    
    data_qc = sheet.worksheet("QC").get_all_records()
    data_sic = sheet.worksheet("SIC").get_all_records()
    data_oee = sheet.worksheet("OEE").get_all_records()
    data_target = sheet.worksheet("Master_Target").get_all_records()
    
    df_qc = pd.DataFrame(data_qc)
    df_sic = pd.DataFrame(data_sic)
    df_oee = pd.DataFrame(data_oee)
    df_target = pd.DataFrame(data_target)
          
    return df_qc, df_sic, df_oee, df_target

@st.cache_data
def clean_data(df_qc, df_sic, df_oee):
    # ==========================================
    # 1. PERBAIKAN FORMAT TIMESTAMP & NAMA MESIN AWAL
    # ==========================================
    def fix_timestamp(ts_series):
        ts_clean = ts_series.astype(str).str.replace(',', '', regex=False).str.replace('.', ':', regex=False)
        return pd.to_datetime(ts_clean, dayfirst=True, format='mixed', errors='coerce')

    df_qc['TIMESTAMP'] = fix_timestamp(df_qc['TIMESTAMP'])
    df_sic['Timestamp'] = fix_timestamp(df_sic['Timestamp'])
    df_oee['TIMESTAMP'] = fix_timestamp(df_oee['TIMESTAMP'])
    
    df_sic = df_sic.rename(columns={'Timestamp': 'TIMESTAMP'})

    # Seragamkan nama mesin lebih awal agar bisa mendeteksi "Timbang"
    if 'NAMA_MESIN' in df_qc.columns: df_qc['NAMA_MESIN'] = df_qc['NAMA_MESIN'].astype(str).str.strip().str.title()
    if 'NAMA_MESIN' in df_sic.columns: df_sic['NAMA_MESIN'] = df_sic['NAMA_MESIN'].astype(str).str.strip().str.title()
    if 'PROSES' in df_oee.columns: df_oee['NAMA_MESIN'] = df_oee['PROSES'].astype(str).str.strip().str.title()

    # ==========================================
    # 2. IDENTIFIKASI & CROSS-FILL NO BATCH KOSONG
    # ==========================================
    invalid_batches = ['-', ' ', '', 'nan', 'NaN', 'None', 'NaT']
    
    for df in [df_qc, df_sic, df_oee]:
        if 'NO_BATCH' in df.columns:
            df['NO_BATCH'] = df['NO_BATCH'].astype(str).str.strip()
            df.loc[df['NO_BATCH'].isin(invalid_batches), 'NO_BATCH'] = np.nan

    valid_batches = pd.concat([
        df_qc[['TIMESTAMP', 'NAMA_MESIN', 'NO_BATCH']].dropna(),
        df_sic[['TIMESTAMP', 'NAMA_MESIN', 'NO_BATCH']].dropna(),
        df_oee[['TIMESTAMP', 'NAMA_MESIN', 'NO_BATCH']].dropna()
    ])
    valid_batches['TANGGAL'] = valid_batches['TIMESTAMP'].dt.date
    
    ref_batch = valid_batches.groupby(['TANGGAL', 'NAMA_MESIN'])['NO_BATCH'].agg(lambda x: x.mode()[0] if not x.mode().empty else np.nan).to_dict()

    def fill_missing_batch(row):
        if pd.isna(row.get('NO_BATCH')):
            tanggal = row['TIMESTAMP'].date() if pd.notna(row['TIMESTAMP']) else None
            mesin = row['NAMA_MESIN']
            if (tanggal, mesin) in ref_batch:
                return ref_batch[(tanggal, mesin)]
        return row.get('NO_BATCH')

    df_qc['NO_BATCH'] = df_qc.apply(fill_missing_batch, axis=1)
    df_sic['NO_BATCH'] = df_sic.apply(fill_missing_batch, axis=1)
    df_oee['NO_BATCH'] = df_oee.apply(fill_missing_batch, axis=1)

    # ==========================================
    # 3. PEMISAHAN ANOMALI NO BATCH (Bukan Timbang)
    # ==========================================
    def pisahkan_anomali_batch(df, sumber):
        kondisi_kosong = df['NO_BATCH'].isna()
        kondisi_timbang = df['NAMA_MESIN'].str.contains('Timbang', case=False, na=False)
        
        df_anomali = df[kondisi_kosong & ~kondisi_timbang].copy()
        df_anomali['SUMBER_DATA'] = sumber
        df_anomali['KETERANGAN_ANOMALI'] = 'No Batch Kosong (Bukan Proses Timbang)'
        
        df_bersih = df[~kondisi_kosong | kondisi_timbang].copy()
        df_bersih['NO_BATCH'] = df_bersih['NO_BATCH'].fillna("")
        
        return df_bersih, df_anomali

    df_qc, df_qc_anomali = pisahkan_anomali_batch(df_qc, 'QC')
    df_sic, df_sic_anomali = pisahkan_anomali_batch(df_sic, 'SIC')
    df_oee, df_oee_anomali = pisahkan_anomali_batch(df_oee, 'OEE')

    df_anomali_batch = pd.concat([df_qc_anomali, df_sic_anomali, df_oee_anomali], ignore_index=True)

    # ==========================================
    # 4. PENYELERASAN NAMA MESIN & SUBBRAND (CROSSCHECK 3 SUMBER)
    # ==========================================
    valid_qc = df_qc[df_qc['NO_BATCH'] != ""]
    valid_sic = df_sic[df_sic['NO_BATCH'] != ""]
    valid_oee = df_oee[df_oee['NO_BATCH'] != ""]

    def get_majority_name(batch_num, col_qc, col_sic, col_oee):
        names = [str(x).strip().upper() for x in [col_qc, col_sic, col_oee] if pd.notna(x) and str(x).strip().upper() != 'NAN' and str(x).strip() != ""]
        if not names: return "UNKNOWN"
        return max(set(names), key=names.count)

    master_batch_dict = {}
    all_batches = set(valid_qc['NO_BATCH']).union(set(valid_sic['NO_BATCH'])).union(set(valid_oee['NO_BATCH']))
    
    qc_grouped = valid_qc.groupby('NO_BATCH').first()
    sic_grouped = valid_sic.groupby('NO_BATCH').first()
    oee_grouped = valid_oee.groupby('NO_BATCH').first()

    for batch in all_batches:
        sb_qc = qc_grouped.loc[batch, 'SUBBRAND'] if batch in qc_grouped.index else np.nan
        sb_sic = sic_grouped.loc[batch, 'SUBBRAND'] if batch in sic_grouped.index else np.nan
        sb_oee = oee_grouped.loc[batch, 'VARIAN_PRODUK'] if batch in oee_grouped.index else np.nan
        
        mesin_qc = qc_grouped.loc[batch, 'NAMA_MESIN'] if batch in qc_grouped.index else np.nan
        mesin_sic = sic_grouped.loc[batch, 'NAMA_MESIN'] if batch in sic_grouped.index else np.nan
        mesin_oee = oee_grouped.loc[batch, 'NAMA_MESIN'] if batch in oee_grouped.index else np.nan
        
        master_batch_dict[batch] = {
            'SUBBRAND_MASTER': get_majority_name(batch, sb_qc, sb_sic, sb_oee),
            'MESIN_MASTER': get_majority_name(batch, mesin_qc, mesin_sic, mesin_oee).title()
        }
        
    master_df = pd.DataFrame.from_dict(master_batch_dict, orient='index')

    df_qc = df_qc.merge(master_df, left_on='NO_BATCH', right_index=True, how='left')
    df_sic = df_sic.merge(master_df, left_on='NO_BATCH', right_index=True, how='left')
    df_oee = df_oee.merge(master_df, left_on='NO_BATCH', right_index=True, how='left')

    # ==========================================
    # 5. PENANGANAN DOUBLE INPUT (Khusus SIC)
    # ==========================================
    df_sic = df_sic.sort_values(by=['NO_BATCH', 'MESIN_MASTER', 'CYCLE', 'TIMESTAMP'])
    df_sic['TIME_DIFF'] = df_sic.groupby(['NO_BATCH', 'MESIN_MASTER', 'CYCLE'])['TIMESTAMP'].diff()
    
    df_sic_anomali_double = df_sic[df_sic['TIME_DIFF'] > pd.Timedelta(minutes=30)].copy()
    if not df_sic_anomali_double.empty:
        df_sic_anomali_double['KETERANGAN_ANOMALI'] = 'Double Input SIC (>30 Menit)'
    
    kondisi_hapus = (df_sic['TIME_DIFF'] >= pd.Timedelta(seconds=0)) & (df_sic['TIME_DIFF'] <= pd.Timedelta(minutes=30))
    df_sic_bersih = df_sic[~kondisi_hapus].copy()

    if 'OUTPUT_PCS' in df_sic_bersih.columns: df_sic_bersih['OUTPUT_PCS'] = pd.to_numeric(df_sic_bersih['OUTPUT_PCS'], errors='coerce').fillna(0)
    if 'REJECT_PCS' in df_qc.columns: df_qc['REJECT_PCS'] = pd.to_numeric(df_qc['REJECT_PCS'], errors='coerce').fillna(0)
    if 'KEMAS_RUSAK_PRODUKSI' in df_oee.columns: df_oee['KEMAS_RUSAK_PRODUKSI'] = pd.to_numeric(df_oee['KEMAS_RUSAK_PRODUKSI'], errors='coerce').fillna(0)

    return df_qc, df_sic_bersih, df_oee, df_sic_anomali_double, df_anomali_batch

# --- BLOK EKSEKUSI TAMPILAN (UI STREAMLIT) ---
st.title("🏭 Dashboard Cerdas Produksi & Quality Control")

try:
    with st.spinner("Menarik dan membersihkan data dari Google Sheets..."):
        raw_qc, raw_sic, raw_oee, df_target = load_data()
        df_qc, df_sic, df_oee, df_anomali_double, df_anomali_batch = clean_data(raw_qc, raw_sic, raw_oee)

    # ==========================================
    # FILTER GLOBAL TANGGAL & BULAN BERJALAN
    # ==========================================
    st.sidebar.header("Filter Data Global")
    
    # Ambil batas tanggal minimum dan maksimum dari data
    all_dates = pd.concat([df_qc['TIMESTAMP'], df_sic['TIMESTAMP'], df_oee['TIMESTAMP']]).dropna()
    min_date = all_dates.min()
    max_date = all_dates.max()

    # Tentukan default rentang tanggal: Bulan Berjalan (berdasarkan tanggal terbaru di data)
    if pd.notna(max_date):
        default_start = max_date.replace(day=1).date()
        default_end = max_date.date()
    else:
        default_start = pd.Timestamp.now().replace(day=1).date()
        default_end = pd.Timestamp.now().date()

    # Widget Filter Rentang Tanggal
    date_range = st.sidebar.date_input(
        "Pilih Rentang Tanggal Produksi:",
        value=(default_start, default_end),
        min_value=min_date.date() if pd.notna(min_date) else None,
        max_value=max_date.date() if pd.notna(max_date) else None
    )

    # Terapkan filter tanggal pada dataset
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
        df_qc = df_qc[(df_qc['TIMESTAMP'].dt.date >= start_date) & (df_qc['TIMESTAMP'].dt.date <= end_date)]
        df_sic = df_sic[(df_sic['TIMESTAMP'].dt.date >= start_date) & (df_sic['TIMESTAMP'].dt.date <= end_date)]
        df_oee = df_oee[(df_oee['TIMESTAMP'].dt.date >= start_date) & (df_oee['TIMESTAMP'].dt.date <= end_date)]
        periode_info = f"{start_date.strftime('%d %b %Y')} s.d. {end_date.strftime('%d %b %Y')}"
    elif isinstance(date_range, tuple) and len(date_range) == 1:
        start_date = date_range[0]
        df_qc = df_qc[df_qc['TIMESTAMP'].dt.date == start_date]
        df_sic = df_sic[df_sic['TIMESTAMP'].dt.date == start_date]
        df_oee = df_oee[df_oee['TIMESTAMP'].dt.date == start_date]
        periode_info = f"{start_date.strftime('%d %b %Y')}"
    else:
        periode_info = "Seluruh Periode Data"

    # Informasi Periode Data Aktif
    st.info(f"📅 **Periode Data Aktif:** {periode_info} *(Bulan Berjalan Default)*")

    # --- MEMBUAT TAB HALAMAN ---
    tab1, tab2, tab3 = st.tabs(["📊 Ringkasan Eksekutif & OEE", "🔍 Kualitas (QC) & Alur", "⚠️ Verifikasi Data Anomali"])

    # ==========================================
    # TAB 1: RINGKASAN EKSEKUTIF
    # ==========================================
    with tab1:
        st.subheader("Indikator Performa Utama (KPI) - Fokus OEE & Performa Subbrand")
        
        # ---------------------------------------------------------
        # 1. GROUPING QC PRESISI (BATCH + NAMA MESIN + NO MESIN LINE)
        # ---------------------------------------------------------
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

        def format_line_number(val):
            if pd.isna(val) or str(val).strip() in ['', 'nan', 'NaN', 'None', '-']:
                return "Line Unknown"
            val_str = str(val).strip()
            if val_str.startswith("Line "):
                return val_str
            if val_str.endswith('.0'):
                val_str = val_str[:-2]
            return f"Line {val_str}"

        df_oee_perf = df_oee.copy()
        df_oee_perf['NO_BATCH_KEY'] = df_oee_perf['NO_BATCH'].astype(str).str.strip()
        df_oee_perf['NAMA_MESIN_KEY'] = df_oee_perf['NAMA_MESIN'].astype(str).str.strip().str.title()
        
        if 'NO_MESIN_LINE' in df_oee_perf.columns:
            df_oee_perf['NO_MESIN_LINE_KEY'] = df_oee_perf['NO_MESIN_LINE'].astype(str).str.strip().str.replace('.0', '', regex=False)
        elif 'MESIN_FULL' in df_oee_perf.columns:
            df_oee_perf['NO_MESIN_LINE_KEY'] = df_oee_perf['MESIN_FULL'].astype(str).str.extract(r'(\d+)')[0].fillna('')
        else:
            df_oee_perf['NO_MESIN_LINE_KEY'] = ''

        df_oee_perf = df_oee_perf.merge(
            qc_grouped, 
            on=['NO_BATCH_KEY', 'NAMA_MESIN_KEY', 'NO_MESIN_LINE_KEY'], 
            how='left'
        )

        df_oee_perf['REJECT_PCS'] = pd.to_numeric(df_oee_perf['REJECT_PCS'], errors='coerce').fillna(0)
        df_oee_perf['REPROSES_PCS'] = pd.to_numeric(df_oee_perf['REPROSES_PCS'], errors='coerce').fillna(0)
        df_oee_perf['CAPAIN_PRODUKSI_PCS'] = pd.to_numeric(df_oee_perf['CAPAIN_PRODUKSI_PCS'], errors='coerce').fillna(0)

        # ---------------------------------------------------------
        # 2. METRIK AKUMULASI DARI DATA OEE & QC
        # ---------------------------------------------------------
        total_reject_kemas_oee = pd.to_numeric(df_oee['KEMAS_RUSAK_PRODUKSI'], errors='coerce').fillna(0).sum() if 'KEMAS_RUSAK_PRODUKSI' in df_oee.columns else 0

        capaian_oee_pcs = pd.to_numeric(df_oee['CAPAIN_PRODUKSI_PCS'], errors='coerce').fillna(0).sum() if 'CAPAIN_PRODUKSI_PCS' in df_oee.columns else 0
        total_reject_qc = pd.to_numeric(df_qc['REJECT_PCS'], errors='coerce').fillna(0).sum() if 'REJECT_PCS' in df_qc.columns else 0
        total_reproses_qc = pd.to_numeric(df_qc['REPROSES_PCS'], errors='coerce').fillna(0).sum() if 'REPROSES_PCS' in df_qc.columns else 0
        
        denominasi_quality = capaian_oee_pcs + total_reproses_qc + total_reject_qc
        quality_rate = (capaian_oee_pcs / denominasi_quality) if denominasi_quality > 0 else 1.0

        # ---------------------------------------------------------
        # 3. PERFORMANCE RATE (KHUSUS MESIN FILLING & PRESS)
        # ---------------------------------------------------------
        kondisi_fp_oee = df_oee_perf['NAMA_MESIN_KEY'].astype(str).str.contains('Filling|Press', case=False, na=False)
        df_oee_perf_fp = df_oee_perf[kondisi_fp_oee].copy()

        df_oee_perf_fp['GROSS_OUTPUT_PCS'] = (
            df_oee_perf_fp['CAPAIN_PRODUKSI_PCS'] +
            df_oee_perf_fp['REJECT_PCS'] +
            df_oee_perf_fp['REPROSES_PCS']
        )
        
        # PERBAIKAN LOGIKA CAPAIAN (GROSS) PER JAM
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

        df_oee_perf_fp['GROSS_CAPAIAN_PER_JAM'] = df_oee_perf_fp.apply(calc_gross_per_jam, axis=1)

        # Pembacaan Master Target dari Google Sheets
        dict_target_subbrand = {}
        if not df_target.empty:
            col_sb = [c for c in df_target.columns if 'SUBBRAND' in c.upper() or 'VARIAN' in c.upper() or 'PRODUK' in c.upper()]
            col_tgt = [c for c in df_target.columns if 'TARGET' in c.upper() or 'PCS' in c.upper() or 'KAPASITAS' in c.upper()]
            
            sb_col_name = col_sb[0] if col_sb else df_target.columns[0]
            tgt_col_name = col_tgt[0] if col_tgt else (df_target.columns[1] if len(df_target.columns) > 1 else df_target.columns[0])
            
            df_target_clean = df_target.copy()
            df_target_clean['SB_KEY'] = df_target_clean[sb_col_name].astype(str).str.strip().str.upper()
            df_target_clean['TGT_VAL'] = pd.to_numeric(df_target_clean[tgt_col_name], errors='coerce').fillna(2500)
            
            dict_target_subbrand = dict(zip(df_target_clean['SB_KEY'], df_target_clean['TGT_VAL']))

        def get_real_subbrand(row):
            vp = str(row.get('VARIAN_PRODUK', '')).strip()
            sm = str(row.get('SUBBRAND_MASTER', '')).strip()
            sb = str(row.get('SUBBRAND', '')).strip()
            
            for val in [vp, sm, sb]:
                if val and val.upper() not in ['NAN', 'NONE', 'UNKNOWN', '']:
                    return val
            return '-'

        def get_target_capacity(subbrand_str, target_dict):
            sb_upper = str(subbrand_str).strip().upper()
            if not sb_upper or sb_upper == '-':
                return 2500.0
            
            if sb_upper in target_dict:
                return target_dict[sb_upper]
            
            for key, val in target_dict.items():
                if key in sb_upper or sb_upper in key:
                    return val
            
            return 2500.0

        df_oee_perf_fp['SUBBRAND_DISPLAY'] = df_oee_perf_fp.apply(get_real_subbrand, axis=1)
        df_oee_perf_fp['TARGET_SUBBRAND_JAM'] = df_oee_perf_fp['SUBBRAND_DISPLAY'].apply(
            lambda x: get_target_capacity(x, dict_target_subbrand)
        )
        
        df_oee_valid_perf = df_oee_perf_fp[df_oee_perf_fp['GROSS_CAPAIAN_PER_JAM'] > 0].copy()
        if not df_oee_valid_perf.empty:
            df_oee_valid_perf['RATIO_PERFORMANCE'] = (df_oee_valid_perf['GROSS_CAPAIAN_PER_JAM'] / df_oee_valid_perf['TARGET_SUBBRAND_JAM']).clip(upper=1.0)
            performance_rate = df_oee_valid_perf['RATIO_PERFORMANCE'].mean()
        else:
            performance_rate = 0.85

        # ---------------------------------------------------------
        # 4. AVAILABILITY RATE & OEE
        # ---------------------------------------------------------
        availability_rate = 1.0
        nilai_oee = (availability_rate * performance_rate * quality_rate) * 100

        kondisi_fp_kpi = df_sic['NAMA_MESIN'].astype(str).str.contains('Filling|Press', case=False, na=False)
        total_output_fp = pd.to_numeric(df_sic[kondisi_fp_kpi]['OUTPUT_PCS'], errors='coerce').fillna(0).sum()

        # ---------------------------------------------------------
        # TAMPILAN METRIK KPI (6 KOLOM)
        # ---------------------------------------------------------
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        
        col1.metric("Total Output (Filling & Press)", f"{total_output_fp:,.0f} Pcs")
        col2.metric("QTY Reject Kemasan (OEE)", f"{total_reject_kemas_oee:,.0f} Pcs")
        col3.metric("Quality Rate", f"{quality_rate * 100:.2f} %")
        col4.metric("Performance Rate (Gross FP)", f"{performance_rate * 100:.2f} %")
        col5.metric("Availability Rate", f"{availability_rate * 100:.0f} %")
        col6.metric("Nilai OEE", f"{nilai_oee:.2f} %")

        st.markdown("---")
        
        col_chart1, col_chart2 = st.columns(2)
        
        with col_chart1:
            st.markdown("**10 Subbrand dengan Produksi Tertinggi (Pcs)**")
            output_per_brand = df_sic.groupby('SUBBRAND_MASTER')['OUTPUT_PCS'].sum().reset_index()
            output_per_brand = output_per_brand.sort_values(by='OUTPUT_PCS', ascending=False).head(10)
            
            fig_brand = px.bar(output_per_brand, x='OUTPUT_PCS', y='SUBBRAND_MASTER', orientation='h',
                               labels={'OUTPUT_PCS': 'Total Output', 'SUBBRAND_MASTER': 'Varian Produk'},
                               color='OUTPUT_PCS', color_continuous_scale='Blues')
            fig_brand.update_layout(yaxis={'categoryorder':'total ascending'}, showlegend=False, height=400)
            st.plotly_chart(fig_brand, use_container_width=True)

        with col_chart2:
            st.markdown("**Perbandingan Capaian (Khusus Mesin Filling & Press)**")
            
            kondisi_fp = df_sic['NAMA_MESIN'].astype(str).str.contains('Filling|Press', case=False, na=False)
            df_sic_fp = df_sic[kondisi_fp].copy()

            capaian_mesin_sic = df_sic_fp.groupby('NAMA_MESIN')['OUTPUT_PCS'].median().round(0).reset_index()
            capaian_mesin_sic = capaian_mesin_sic.sort_values(by='OUTPUT_PCS', ascending=False).head(10)
            
            if not capaian_mesin_sic.empty:
                fig_mesin = px.bar(capaian_mesin_sic, x='NAMA_MESIN', y='OUTPUT_PCS',
                                   labels={'OUTPUT_PCS': 'Median Output / Cycle (Pcs)', 'NAMA_MESIN': 'Nama Mesin'},
                                   color='OUTPUT_PCS', color_continuous_scale='Teal', text='OUTPUT_PCS')
                fig_mesin.update_layout(height=400)
                st.plotly_chart(fig_mesin, use_container_width=True)
            else:
                st.info("Data tidak tersedia.")

        # ---------------------------------------------------------
        # 5. PERBANDINGAN KOMPONEN OEE PER LINE MESIN
        # ---------------------------------------------------------
        st.markdown("---")
        st.markdown("### 📊 Analisis Komponen OEE per Nomor/Line Mesin")
        
        list_mesin_tren = sorted(df_sic_fp['NAMA_MESIN'].unique().tolist())
        
        col_fil1, col_fil2 = st.columns([2, 3])
        with col_fil1:
            pilih_mesin_tren = st.selectbox("Pilih Jenis Mesin untuk Analisis Detail Line:", list_mesin_tren)
        with col_fil2:
            mode_tampilan = st.radio(
                "Mode Tampilan Grafik Komponen OEE:",
                ["Agregat Total (Bar Chart)", "Tren Harian (Line Chart)"],
                horizontal=True
            )
        
        df_oee_line_selected = df_oee_perf_fp[df_oee_perf_fp['NAMA_MESIN_KEY'].str.upper() == pilih_mesin_tren.upper()].copy()
        df_oee_line_selected['LINE_DISPLAY'] = df_oee_line_selected['NO_MESIN_LINE_KEY'].apply(format_line_number)

        # Grafik Agregat / Tren Harian
        if mode_tampilan == "Agregat Total (Bar Chart)":
            oee_by_line = []
            for line_name, group in df_oee_line_selected.groupby('LINE_DISPLAY'):
                c_pcs = group['CAPAIN_PRODUKSI_PCS'].sum()
                rej_pcs = group['REJECT_PCS'].sum()
                rep_pcs = group['REPROSES_PCS'].sum()
                den_q = c_pcs + rej_pcs + rep_pcs
                q_line = (c_pcs / den_q * 100) if den_q > 0 else 100.0

                valid_perf = group[group['GROSS_CAPAIAN_PER_JAM'] > 0]
                if not valid_perf.empty:
                    p_line = (valid_perf['GROSS_CAPAIAN_PER_JAM'] / valid_perf['TARGET_SUBBRAND_JAM']).clip(upper=1.0).mean() * 100
                else:
                    p_line = 85.0

                a_line = 100.0
                oee_line = (q_line / 100.0) * (p_line / 100.0) * (a_line / 100.0) * 100.0

                oee_by_line.append({
                    'Nomor Mesin': line_name,
                    'OEE (%)': round(oee_line, 2),
                    'Performance (%)': round(p_line, 2),
                    'Quality (%)': round(q_line, 2),
                    'Availability (%)': round(a_line, 2)
                })

            df_line_kpi = pd.DataFrame(oee_by_line)

            if not df_line_kpi.empty:
                col_oee1, col_oee2 = st.columns(2)
                with col_oee1:
                    st.markdown(f"**Nilai OEE Total ({pilih_mesin_tren})**")
                    fig_oee = px.bar(df_line_kpi, x='Nomor Mesin', y='OEE (%)', text='OEE (%)', color='OEE (%)', color_continuous_scale='Teal')
                    fig_oee.update_traces(texttemplate='%{text:.2f}%', textposition='outside')
                    fig_oee.update_layout(height=350, yaxis_range=[0, 115])
                    st.plotly_chart(fig_oee, use_container_width=True)

                with col_oee2:
                    st.markdown(f"**Performance Rate Total ({pilih_mesin_tren})**")
                    fig_perf = px.bar(df_line_kpi, x='Nomor Mesin', y='Performance (%)', text='Performance (%)', color='Performance (%)', color_continuous_scale='Blues')
                    fig_perf.update_traces(texttemplate='%{text:.2f}%', textposition='outside')
                    fig_perf.update_layout(height=350, yaxis_range=[0, 115])
                    st.plotly_chart(fig_perf, use_container_width=True)

                col_oee3, col_oee4 = st.columns(2)
                with col_oee3:
                    st.markdown(f"**Quality Rate Total ({pilih_mesin_tren})**")
                    fig_qual = px.bar(df_line_kpi, x='Nomor Mesin', y='Quality (%)', text='Quality (%)', color='Quality (%)', color_continuous_scale='Greens')
                    fig_qual.update_traces(texttemplate='%{text:.2f}%', textposition='outside')
                    fig_qual.update_layout(height=350, yaxis_range=[0, 115])
                    st.plotly_chart(fig_qual, use_container_width=True)

                with col_oee4:
                    st.markdown(f"**Availability Rate Total ({pilih_mesin_tren})**")
                    fig_avail = px.bar(df_line_kpi, x='Nomor Mesin', y='Availability (%)', text='Availability (%)', color='Availability (%)', color_continuous_scale='Purples')
                    fig_avail.update_traces(texttemplate='%{text:.0f}%', textposition='outside')
                    fig_avail.update_layout(height=350, yaxis_range=[0, 115])
                    st.plotly_chart(fig_avail, use_container_width=True)
            else:
                st.info("Data komponen OEE per line belum tersedia.")

        else:
            df_oee_line_selected['TANGGAL'] = df_oee_line_selected['TIMESTAMP'].dt.date
            
            daily_oee_list = []
            for (tgl, line_name), group in df_oee_line_selected.groupby(['TANGGAL', 'LINE_DISPLAY']):
                c_pcs = group['CAPAIN_PRODUKSI_PCS'].sum()
                rej_pcs = group['REJECT_PCS'].sum()
                rep_pcs = group['REPROSES_PCS'].sum()
                den_q = c_pcs + rej_pcs + rep_pcs
                q_line = (c_pcs / den_q * 100) if den_q > 0 else 100.0

                valid_perf = group[group['GROSS_CAPAIAN_PER_JAM'] > 0]
                if not valid_perf.empty:
                    p_line = (valid_perf['GROSS_CAPAIAN_PER_JAM'] / valid_perf['TARGET_SUBBRAND_JAM']).clip(upper=1.0).mean() * 100
                else:
                    p_line = 85.0

                a_line = 100.0
                oee_line = (q_line / 100.0) * (p_line / 100.0) * (a_line / 100.0) * 100.0

                daily_oee_list.append({
                    'Tanggal': tgl,
                    'Nomor Mesin': line_name,
                    'OEE (%)': round(oee_line, 2),
                    'Performance (%)': round(p_line, 2),
                    'Quality (%)': round(q_line, 2),
                    'Availability (%)': round(a_line, 2)
                })

            df_daily_kpi = pd.DataFrame(daily_oee_list)

            if not df_daily_kpi.empty:
                df_daily_kpi = df_daily_kpi.sort_values(by=['Tanggal', 'Nomor Mesin'])

                col_oee1, col_oee2 = st.columns(2)
                with col_oee1:
                    st.markdown(f"**Tren Harian Nilai OEE ({pilih_mesin_tren})**")
                    fig_oee = px.line(df_daily_kpi, x='Tanggal', y='OEE (%)', color='Nomor Mesin', markers=True)
                    fig_oee.update_traces(hovertemplate='<b>%{data.name}</b><br>Tanggal: %{x}<br>OEE: %{y:.2f}%<extra></extra>')
                    fig_oee.update_layout(height=350, yaxis_range=[0, 110], legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
                    st.plotly_chart(fig_oee, use_container_width=True)

                with col_oee2:
                    st.markdown(f"**Tren Harian Performance Rate ({pilih_mesin_tren})**")
                    fig_perf = px.line(df_daily_kpi, x='Tanggal', y='Performance (%)', color='Nomor Mesin', markers=True)
                    fig_perf.update_traces(hovertemplate='<b>%{data.name}</b><br>Tanggal: %{x}<br>Performance: %{y:.2f}%<extra></extra>')
                    fig_perf.update_layout(height=350, yaxis_range=[0, 110], legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
                    st.plotly_chart(fig_perf, use_container_width=True)

                col_oee3, col_oee4 = st.columns(2)
                with col_oee3:
                    st.markdown(f"**Tren Harian Quality Rate ({pilih_mesin_tren})**")
                    fig_qual = px.line(df_daily_kpi, x='Tanggal', y='Quality (%)', color='Nomor Mesin', markers=True)
                    fig_qual.update_traces(hovertemplate='<b>%{data.name}</b><br>Tanggal: %{x}<br>Quality: %{y:.2f}%<extra></extra>')
                    fig_qual.update_layout(height=350, yaxis_range=[0, 110], legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
                    st.plotly_chart(fig_qual, use_container_width=True)

                with col_oee4:
                    st.markdown(f"**Tren Harian Availability Rate ({pilih_mesin_tren})**")
                    fig_avail = px.line(df_daily_kpi, x='Tanggal', y='Availability (%)', color='Nomor Mesin', markers=True)
                    fig_avail.update_traces(hovertemplate='<b>%{data.name}</b><br>Tanggal: %{x}<br>Availability: %{y:.0f}%<extra></extra>')
                    fig_avail.update_layout(height=350, yaxis_range=[0, 110], legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
                    st.plotly_chart(fig_avail, use_container_width=True)
            else:
                st.info("Data harian komponen OEE per line belum tersedia.")

        # ---------------------------------------------------------
        # TABEL DETAIL ANALISIS OEE PER BATCH & MESIN
        # ---------------------------------------------------------
        st.markdown(f"**📋 Tabel Detail Analisis OEE ({pilih_mesin_tren})**")
        
        if not df_oee_line_selected.empty:
            df_table_oee = df_oee_line_selected.copy()
            
            df_table_oee['Tanggal Produksi'] = df_table_oee['TIMESTAMP'].dt.strftime('%Y-%m-%d')
            df_table_oee['Subbrand'] = df_table_oee.apply(get_real_subbrand, axis=1)
                
            if 'MESIN_FULL' in df_table_oee.columns and df_table_oee['MESIN_FULL'].notna().any():
                df_table_oee['Mesin_Full'] = df_table_oee['MESIN_FULL']
            else:
                df_table_oee['Mesin_Full'] = df_table_oee['NAMA_MESIN_KEY'] + " - " + df_table_oee['LINE_DISPLAY']
                
            df_table_oee['Capaian Produksi Gross'] = df_table_oee['GROSS_OUTPUT_PCS']
            df_table_oee['Total Reject'] = df_table_oee['REJECT_PCS']
            df_table_oee['Total Reproses'] = df_table_oee['REPROSES_PCS']
            df_table_oee['Durasi'] = pd.to_numeric(df_table_oee['Durasi Bersih'], errors='coerce').fillna(0)
            
            df_table_oee['Capaian (Gross) / Jam'] = pd.to_numeric(df_table_oee['GROSS_CAPAIAN_PER_JAM'], errors='coerce').fillna(0)
            df_table_oee['Target Kapasitas / Jam'] = pd.to_numeric(df_table_oee['TARGET_SUBBRAND_JAM'], errors='coerce').fillna(0)
            
            df_table_oee['Availability'] = 100.0
            
            df_table_oee['Quality'] = df_table_oee.apply(
                lambda r: (r['CAPAIN_PRODUKSI_PCS'] / r['GROSS_OUTPUT_PCS'] * 100) if r['GROSS_OUTPUT_PCS'] > 0 else 100.0,
                axis=1
            )
            
            df_table_oee['Raw_Performance'] = df_table_oee.apply(
                lambda r: (r['GROSS_CAPAIAN_PER_JAM'] / r['TARGET_SUBBRAND_JAM'] * 100) if r['TARGET_SUBBRAND_JAM'] > 0 else 85.0,
                axis=1
            )
            
            df_table_oee['Performance'] = df_table_oee['Raw_Performance'].clip(upper=100.0)
            
            df_table_oee['Status Target'] = df_table_oee['Raw_Performance'].apply(
                lambda x: f"⚡ Over Target ({x:.1f}%)" if x > 100.0 else "✅ Normal"
            )
            
            df_table_oee['OEE'] = (df_table_oee['Availability'] / 100) * (df_table_oee['Performance'] / 100) * (df_table_oee['Quality'] / 100) * 100
            
            kolom_tampil = [
                'Tanggal Produksi', 'NO_BATCH', 'Subbrand', 'Mesin_Full',
                'Capaian Produksi Gross', 'Total Reject', 'Total Reproses',
                'Durasi', 'Capaian (Gross) / Jam', 'Target Kapasitas / Jam',
                'Availability', 'Performance', 'Quality', 'OEE', 'Status Target'
            ]
            
            df_table_display = df_table_oee[kolom_tampil].rename(columns={'NO_BATCH': 'No Batch'}).sort_values(by='Tanggal Produksi', ascending=False)
            
            st.dataframe(
                df_table_display.style.format({
                    'Capaian Produksi Gross': '{:,.0f}',
                    'Total Reject': '{:,.0f}',
                    'Total Reproses': '{:,.0f}',
                    'Durasi': '{:,.0f}',
                    'Capaian (Gross) / Jam': '{:,.0f}',
                    'Target Kapasitas / Jam': '{:,.0f}',
                    'Availability': '{:.2f}%',
                    'Performance': '{:.2f}%',
                    'Quality': '{:.2f}%',
                    'OEE': '{:.2f}%'
                }),
                use_container_width=True
            )
        else:
            st.info("Tidak ada data detail OEE untuk mesin yang dipilih.")

        # ---------------------------------------------------------
        # 6. ANALISIS TREN & STABILITAS OUTPUT ANTAR CYCLE
        # ---------------------------------------------------------
        st.markdown("---")
        st.markdown("### 📈 Analisis Tren & Stabilitas Output antar Cycle")
        
        df_sic_tren = df_sic_fp[df_sic_fp['NAMA_MESIN'] == pilih_mesin_tren].copy()
        
        if 'STATUS_ALUR' in df_sic_tren.columns:
            kondisi_istirahat_sanitasi = df_sic_tren['STATUS_ALUR'].astype(str).str.contains('ISTIRAHAT|SANITASI|BREAK|STOP', case=False, na=False)
            df_sic_produktif = df_sic_tren[(df_sic_tren['OUTPUT_PCS'] > 0) & (~kondisi_istirahat_sanitasi)].copy()
        else:
            df_sic_produktif = df_sic_tren[df_sic_tren['OUTPUT_PCS'] > 0].copy()

        if 'NO_MESIN_LINE' in df_sic_produktif.columns:
            df_sic_produktif['NO_MESIN_LINE'] = df_sic_produktif['NO_MESIN_LINE'].apply(format_line_number)
        else:
            df_sic_produktif['NO_MESIN_LINE'] = "Line Unknown"
            
        df_sic_produktif = df_sic_produktif.sort_values(by=['NO_BATCH', 'NO_MESIN_LINE', 'TIMESTAMP'])
        df_sic_produktif['CYCLE_AKTIF'] = df_sic_produktif.groupby(['NO_BATCH', 'NO_MESIN_LINE']).cumcount() + 1
        df_sic_produktif['DIFF_OUTPUT'] = df_sic_produktif.groupby(['NO_BATCH', 'NO_MESIN_LINE'])['OUTPUT_PCS'].diff().fillna(0)
        
        col_diff1, col_diff2 = st.columns(2)
        
        with col_diff1:
            st.markdown(f"**Tren Rata-rata Output ({pilih_mesin_tren})**")
            
            tren_cycle = df_sic_produktif[df_sic_produktif['CYCLE_AKTIF'] <= 10].groupby(['NO_MESIN_LINE', 'CYCLE_AKTIF'])['OUTPUT_PCS'].mean().round(0).reset_index()
            
            if not tren_cycle.empty:
                fig_tren = px.line(tren_cycle, x='CYCLE_AKTIF', y='OUTPUT_PCS', color='NO_MESIN_LINE', markers=True,
                                   labels={'OUTPUT_PCS': 'Rata-rata Output (Pcs)', 'CYCLE_AKTIF': 'Siklus Aktif ke-', 'NO_MESIN_LINE': 'Nomor Mesin'})
                
                fig_tren.update_traces(
                    hovertemplate='<b>%{data.name}</b><br>Siklus ke-%{x}<br>Output: %{y:,.0f} Pcs<extra></extra>'
                )
                fig_tren.update_xaxes(dtick=1) 
                fig_tren.update_layout(legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
                
                st.plotly_chart(fig_tren, use_container_width=True)
            else:
                st.info("Data trend belum tersedia.")

        with col_diff2:
            st.markdown(f"**Rata-rata Penurunan/Penambahan Output ({pilih_mesin_tren})**")
            st.caption("*Melihat mesin mana yang paling stabil antar siklus.*")
            
            df_diff_valid = df_sic_produktif[df_sic_produktif['CYCLE_AKTIF'] > 1]
            diff_avg = df_diff_valid.groupby('NO_MESIN_LINE')['DIFF_OUTPUT'].mean().round(0).reset_index()
            
            if not diff_avg.empty:
                fig_diff = px.bar(diff_avg, x='NO_MESIN_LINE', y='DIFF_OUTPUT',
                                  labels={'DIFF_OUTPUT': 'Selisih Output (Pcs)', 'NO_MESIN_LINE': 'Nomor Mesin'},
                                  color='DIFF_OUTPUT', color_continuous_scale='RdBu', text='DIFF_OUTPUT')
                
                fig_diff.add_hline(y=0, line_dash="dash", line_color="black")
                st.plotly_chart(fig_diff, use_container_width=True)
            else:
                st.info("Data differensial belum cukup (butuh minimal 2 siklus aktif).")

   # ==========================================
    # TAB 2: KUALITAS (QC) & ALUR
    # ==========================================
    with tab2:
        st.subheader("🔍 Ringkasan Performance Kualitas & Analisis Defect (QC)")

        # ---------------------------------------------------------
        # 1. SUMMARY KPI METRICS QC
        # ---------------------------------------------------------
        tot_reject = pd.to_numeric(df_qc['REJECT_PCS'], errors='coerce').fillna(0).sum() if 'REJECT_PCS' in df_qc.columns else 0
        tot_reproses = pd.to_numeric(df_qc['REPROSES_PCS'], errors='coerce').fillna(0).sum() if 'REPROSES_PCS' in df_qc.columns else 0
        tot_capaian = pd.to_numeric(df_oee['CAPAIN_PRODUKSI_PCS'], errors='coerce').fillna(0).sum() if 'CAPAIN_PRODUKSI_PCS' in df_oee.columns else 0
        
        total_gross_qc = tot_capaian + tot_reject + tot_reproses
        overall_quality = (tot_capaian / total_gross_qc * 100) if total_gross_qc > 0 else 100.0

        q_col1, q_col2, q_col3, q_col4 = st.columns(4)
        q_col1.metric("Total Reject (QC)", f"{tot_reject:,.0f} Pcs")
        q_col2.metric("Total Reproses (QC)", f"{tot_reproses:,.0f} Pcs")
        q_col3.metric("Total Defect (QC)", f"{(tot_reject + tot_reproses):,.0f} Pcs")
        q_col4.metric("Overall Quality Rate", f"{overall_quality:.2f} %")

        st.markdown("---")

        # ---------------------------------------------------------
        # 2. GRAFIK VOLUME DEFECT PER MESIN & SUBBRAND
        # ---------------------------------------------------------
        col_q1, col_q2 = st.columns(2)

        with col_q1:
            st.markdown("**1. Volume Defect per Jenis Mesin (Khusus Filling & Press)**")
            if 'NAMA_MESIN_KEY' in df_oee_perf.columns:
                # Filter HANYA mesin Filling & Press (Samakan dengan Tab 1)
                kondisi_fp_qc = df_oee_perf['NAMA_MESIN_KEY'].astype(str).str.contains('Filling|Press', case=False, na=False)
                df_defect_mesin = df_oee_perf[kondisi_fp_qc].groupby('NAMA_MESIN_KEY').agg({
                    'REJECT_PCS': 'sum',
                    'REPROSES_PCS': 'sum'
                }).reset_index()
                
                # Melt dataframe untuk membuat Stacked Bar Chart
                df_defect_melted = df_defect_mesin.melt(
                    id_vars=['NAMA_MESIN_KEY'], 
                    value_vars=['REJECT_PCS', 'REPROSES_PCS'],
                    var_name='Jenis Defect', value_name='Pcs'
                )
                df_defect_melted['Jenis Defect'] = df_defect_melted['Jenis Defect'].replace({
                    'REJECT_PCS': 'Reject', 'REPROSES_PCS': 'Reproses'
                })

                fig_defect_mesin = px.bar(
                    df_defect_melted, x='Pcs', y='NAMA_MESIN_KEY', color='Jenis Defect',
                    orientation='h', barmode='stack',
                    color_discrete_map={'Reject': '#EF553B', 'Reproses': '#FECB52'},
                    labels={'NAMA_MESIN_KEY': 'Nama Mesin', 'Pcs': 'Total Cacat (Pcs)'}
                )
                fig_defect_mesin.update_layout(
                    height=400,
                    yaxis={'categoryorder': 'total ascending'},
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
                )
                st.plotly_chart(fig_defect_mesin, use_container_width=True)
            else:
                st.info("Data defect per mesin belum tersedia.")

        with col_q2:
            st.markdown("**2. Top 10 Subbrand dengan Total Reject Terbanyak (Pcs)**")
            if 'SUBBRAND_DISPLAY' in df_oee_perf_fp.columns:
                q_sb_reject = df_oee_perf_fp.groupby('SUBBRAND_DISPLAY')['REJECT_PCS'].sum().reset_index()
                q_sb_reject = q_sb_reject[q_sb_reject['REJECT_PCS'] > 0].sort_values(by='REJECT_PCS', ascending=False).head(10)

                if not q_sb_reject.empty:
                    fig_q_sb = px.bar(
                        q_sb_reject, x='REJECT_PCS', y='SUBBRAND_DISPLAY', orientation='h',
                        text='REJECT_PCS', color='REJECT_PCS',
                        color_continuous_scale='Reds', labels={'SUBBRAND_DISPLAY': 'Subbrand', 'REJECT_PCS': 'Reject (Pcs)'}
                    )
                    fig_q_sb.update_traces(texttemplate='%{text:,.0f}', textposition='outside')
                    fig_q_sb.update_layout(yaxis={'categoryorder': 'total ascending'}, height=400, showlegend=False)
                    st.plotly_chart(fig_q_sb, use_container_width=True)
                else:
                    st.info("Tidak ada data reject pada subbrand.")
            else:
                st.info("Data subbrand belum tersedia.")

        st.markdown("---")

        # ---------------------------------------------------------
        # 3. PARETO ALASAN REJECT & DEEP DIVE BATCH
        # ---------------------------------------------------------
        col_qc1, col_qc2 = st.columns(2)

        with col_qc1:
            st.markdown("**3. Pareto Alasan Reject & Reproses Utama**")
            if 'ALASAN_REJECT_REPRO' in df_qc.columns:
                alasan_df = df_qc.copy()
                alasan_df['TOTAL_DEFECT'] = pd.to_numeric(alasan_df['REJECT_PCS'], errors='coerce').fillna(0) + pd.to_numeric(alasan_df['REPROSES_PCS'], errors='coerce').fillna(0)
                
                alasan_summary = alasan_df.groupby('ALASAN_REJECT_REPRO')['TOTAL_DEFECT'].sum().reset_index()
                alasan_summary = alasan_summary[~alasan_summary['ALASAN_REJECT_REPRO'].astype(str).str.strip().str.upper().isin(['', 'NAN', 'NONE', '-'])]
                alasan_summary = alasan_summary.sort_values(by='TOTAL_DEFECT', ascending=False).head(7)

                if not alasan_summary.empty:
                    fig_alasan = px.bar(
                        alasan_summary, x='TOTAL_DEFECT', y='ALASAN_REJECT_REPRO', orientation='h',
                        text='TOTAL_DEFECT', color='TOTAL_DEFECT', color_continuous_scale='Oranges',
                        labels={'TOTAL_DEFECT': 'Jumlah Defect (Pcs)', 'ALASAN_REJECT_REPRO': 'Alasan Defect'}
                    )
                    fig_alasan.update_layout(yaxis={'categoryorder': 'total ascending'}, height=400, showlegend=False)
                    st.plotly_chart(fig_alasan, use_container_width=True)
                else:
                    st.info("Tidak ada data alasan reject/reproses.")

        with col_qc2:
            st.markdown("**4. Deep Dive Pencarian Alur Batch**")
            list_batch = sorted([b for b in df_sic['NO_BATCH'].dropna().unique().tolist() if str(b).strip() != ""])
            cari_batch = st.selectbox("Ketik atau Pilih Nomor Batch:", [""] + list_batch)

            if cari_batch != "":
                alur_batch = df_sic[df_sic['NO_BATCH'] == cari_batch]
                if not alur_batch.empty:
                    alur_ringkas = alur_batch.groupby('MESIN_MASTER').agg({'TIMESTAMP': 'min', 'OUTPUT_PCS': 'sum'}).sort_values(by='TIMESTAMP')
                    st.write(f"Perjalanan Batch **{cari_batch}**:")
                    st.dataframe(alur_ringkas.style.format({'OUTPUT_PCS': '{:,.0f}'}))
                else:
                    st.warning("Data batch tidak ditemukan di tabel SIC.")

    # ==========================================
    # TAB 3: VERIFIKASI ANOMALI
    # ==========================================
    with tab3:
        st.subheader("Daftar Data Anomali (Perlu Pengecekan Admin)")
        st.info("Data di bawah ini telah disingkirkan dari perhitungan grafik utama agar tidak merusak akurasi perhitungan.")
        
        st.markdown("**1. Data Double Input SIC (Selisih > 30 Menit)**")
        st.dataframe(df_anomali_double)
        
        st.markdown("**2. Data No Batch Tidak Valid (Selain Proses Timbang)**")
        st.dataframe(df_anomali_batch)

except Exception as e:
    st.error(f"Terjadi kesalahan pada aplikasi: {e}")
