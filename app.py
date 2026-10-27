import streamlit as st
import pandas as pd
import numpy as np
import gspread
import plotly.express as px
from datetime import datetime, timedelta
import calendar
from oauth2client.service_account import ServiceAccountCredentials

from compliance_engine import (
    get_rapport_cards_range,
    get_daily_compliance_trend,
    send_fonnte_wa,
    build_wa_msg_operator,
    build_wa_msg_qc,
    get_wip_operational,
    get_age_mixing,
    is_qc_status_patuh
)

st.set_page_config(page_title="Dashboard Produksi & Kepatuhan", layout="wide")

# ==========================================
# 1. KONEKSI GOOGLE SHEETS & PEMUATAN DATA
# ==========================================
@st.cache_resource
def init_connection():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    return gspread.authorize(creds)

@st.cache_data(ttl=600)
def load_data():
    client = init_connection()
    sheet = client.open("Raw Data - Produksi - Cleansing") 
    
    df_qc = pd.DataFrame(sheet.worksheet("QC").get_all_records())
    df_sic = pd.DataFrame(sheet.worksheet("SIC").get_all_records())
    df_oee = pd.DataFrame(sheet.worksheet("OEE").get_all_records())
    df_target = pd.DataFrame(sheet.worksheet("Master_Target").get_all_records())
          
    return df_qc, df_sic, df_oee, df_target

# ==========================================
# 2. CLEANSING & PIPELINE PENGOLAHAN DATA
# ==========================================
@st.cache_data(ttl=600)
def clean_data(df_qc, df_sic, df_oee):
    for df in [df_qc, df_sic, df_oee]:
        for col in ['NO_BATCH', 'NAMA_MESIN', 'SUBBRAND', 'VARIAN_PRODUK', 'NO_MESIN_LINE', 'MESIN_FULL', 'PROSES']:
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
                df[col] = df[col].replace({'nan': '', 'None': '', 'NaN': '', '<NA>': ''})

    def fix_timestamp(ts_series):
        ts_clean = ts_series.astype(str).str.replace(',', '', regex=False).str.replace('.', ':', regex=False)
        return pd.to_datetime(ts_clean, dayfirst=True, format='mixed', errors='coerce')

    df_qc['TIMESTAMP'] = fix_timestamp(df_qc['TIMESTAMP'])
    df_sic['Timestamp'] = fix_timestamp(df_sic['Timestamp'])
    df_oee['TIMESTAMP'] = fix_timestamp(df_oee['TIMESTAMP'])
    
    df_sic = df_sic.rename(columns={'Timestamp': 'TIMESTAMP'})

    if 'NAMA_MESIN' in df_qc.columns: df_qc['NAMA_MESIN'] = df_qc['NAMA_MESIN'].str.title()
    if 'NAMA_MESIN' in df_sic.columns: df_sic['NAMA_MESIN'] = df_sic['NAMA_MESIN'].str.title()
    if 'PROSES' in df_oee.columns: df_oee['NAMA_MESIN'] = df_oee['PROSES'].str.title()

    invalid_batches = ['', '-', 'nan', 'None', 'NAT']
    for df in [df_qc, df_sic, df_oee]:
        if 'NO_BATCH' in df.columns:
            df['NO_BATCH_CLEAN'] = df['NO_BATCH'].where(~df['NO_BATCH'].str.upper().isin(invalid_batches), np.nan)

    valid_batches = pd.concat([
        df_qc[['TIMESTAMP', 'NAMA_MESIN', 'NO_BATCH_CLEAN']].dropna(),
        df_sic[['TIMESTAMP', 'NAMA_MESIN', 'NO_BATCH_CLEAN']].dropna(),
        df_oee[['TIMESTAMP', 'NAMA_MESIN', 'NO_BATCH_CLEAN']].dropna()
    ])
    valid_batches['TANGGAL'] = valid_batches['TIMESTAMP'].dt.date
    
    ref_batch = valid_batches.groupby(['TANGGAL', 'NAMA_MESIN'])['NO_BATCH_CLEAN'].agg(lambda x: x.mode()[0] if not x.mode().empty else np.nan).to_dict()

    for df in [df_qc, df_sic, df_oee]:
        df['TANGGAL_TEMP'] = df['TIMESTAMP'].dt.date
        df['BATCH_FILLED'] = df.set_index(['TANGGAL_TEMP', 'NAMA_MESIN']).index.map(ref_batch)
        df['NO_BATCH'] = df['NO_BATCH_CLEAN'].fillna(df['BATCH_FILLED']).fillna("")
        df.drop(columns=['NO_BATCH_CLEAN', 'TANGGAL_TEMP', 'BATCH_FILLED'], errors='ignore', inplace=True)

    def pisahkan_anomali_batch(df, sumber):
        kondisi_kosong = df['NO_BATCH'] == ""
        kondisi_timbang = df['NAMA_MESIN'].str.contains('Timbang', case=False, na=False)
        
        df_anomali = df[kondisi_kosong & ~kondisi_timbang].copy()
        df_anomali['SUMBER_DATA'] = sumber
        df_anomali['KETERANGAN_ANOMALI'] = 'No Batch Kosong (Bukan Proses Timbang)'
        
        df_bersih = df[~kondisi_kosong | kondisi_timbang].copy()
        return df_bersih, df_anomali

    df_qc, df_qc_anomali = pisahkan_anomali_batch(df_qc, 'QC')
    df_sic, df_sic_anomali = pisahkan_anomali_batch(df_sic, 'SIC')
    df_oee, df_oee_anomali = pisahkan_anomali_batch(df_oee, 'OEE')
    df_anomali_batch = pd.concat([df_qc_anomali, df_sic_anomali, df_oee_anomali], ignore_index=True)

    valid_qc = df_qc[df_qc['NO_BATCH'] != ""]
    valid_sic = df_sic[df_sic['NO_BATCH'] != ""]
    valid_oee = df_oee[df_oee['NO_BATCH'] != ""]

    all_batches = list(set(valid_qc['NO_BATCH']).union(set(valid_sic['NO_BATCH'])).union(set(valid_oee['NO_BATCH'])))
    
    sb_qc_map = valid_qc.groupby('NO_BATCH')['SUBBRAND'].first().to_dict()
    sb_sic_map = valid_sic.groupby('NO_BATCH')['SUBBRAND'].first().to_dict()
    sb_oee_map = valid_oee.groupby('NO_BATCH')['VARIAN_PRODUK'].first().to_dict()

    master_sb = {}
    for b in all_batches:
        vals = [sb_qc_map.get(b, ''), sb_sic_map.get(b, ''), sb_oee_map.get(b, '')]
        valid_vals = [v for v in vals if v and str(v).upper() not in ['NAN', 'NONE', '']]
        master_sb[b] = valid_vals[0] if valid_vals else 'UNKNOWN'

    df_qc['SUBBRAND_MASTER'] = df_qc['NO_BATCH'].map(master_sb).fillna('UNKNOWN')
    df_sic['SUBBRAND_MASTER'] = df_sic['NO_BATCH'].map(master_sb).fillna('UNKNOWN')
    df_oee['SUBBRAND_MASTER'] = df_oee['NO_BATCH'].map(master_sb).fillna('UNKNOWN')

    # Identifikasi Anomali Double Input SIC (>30 Menit)
    df_sic = df_sic.sort_values(by=['NO_BATCH', 'NAMA_MESIN', 'CYCLE', 'TIMESTAMP'])
    df_sic['TIME_DIFF'] = df_sic.groupby(['NO_BATCH', 'NAMA_MESIN', 'CYCLE'])['TIMESTAMP'].diff()
    
    df_sic_anomali_double = df_sic[df_sic['TIME_DIFF'] > pd.Timedelta(minutes=30)].copy()
    if not df_sic_anomali_double.empty:
        df_sic_anomali_double['KETERANGAN_ANOMALI'] = 'Double Input SIC (>30 Menit)'
    
    df_sic_bersih = df_sic.copy()

    if 'OUTPUT_PCS' in df_sic_bersih.columns: df_sic_bersih['OUTPUT_PCS'] = pd.to_numeric(df_sic_bersih['OUTPUT_PCS'], errors='coerce').fillna(0)
    if 'REJECT_PCS' in df_qc.columns: df_qc['REJECT_PCS'] = pd.to_numeric(df_qc['REJECT_PCS'], errors='coerce').fillna(0)
    if 'REPROSES_PCS' in df_qc.columns: df_qc['REPROSES_PCS'] = pd.to_numeric(df_qc['REPROSES_PCS'], errors='coerce').fillna(0)
    if 'KEMAS_RUSAK_PRODUKSI' in df_oee.columns: df_oee['KEMAS_RUSAK_PRODUKSI'] = pd.to_numeric(df_oee['KEMAS_RUSAK_PRODUKSI'], errors='coerce').fillna(0)

    for df in [df_qc, df_sic_bersih, df_oee, df_sic_anomali_double, df_anomali_batch]:
        for col in df.select_dtypes(include=['object']).columns:
            df[col] = df[col].astype(str)

    return df_qc, df_sic_bersih, df_oee, df_sic_anomali_double, df_anomali_batch

# ==========================================
# 3. HELPER FUNCTIONS
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
# 4. EKSEKUSI TAMPILAN DASHBOARD
# ==========================================
st.title("🏭 Dashboard Cerdas Produksi, QC & Kepatuhan")

try:
    with st.spinner("Menarik dan membersihkan data dari Google Sheets..."):
        raw_qc, raw_sic, raw_oee, df_target = load_data()
        df_qc, df_sic, df_oee, df_anomali_double, df_anomali_batch = clean_data(raw_qc, raw_sic, raw_oee)

    df_qc_full = df_qc.copy()
    df_sic_full = df_sic.copy()
    df_oee_full = df_oee.copy()

    # FILTER GLOBAL TANGGAL
    st.sidebar.header("Filter Data Global")
    
    all_dates = pd.concat([df_qc['TIMESTAMP'], df_sic['TIMESTAMP'], df_oee['TIMESTAMP']]).dropna()
    min_date = all_dates.min()
    max_date = all_dates.max()

    if pd.notna(max_date):
        default_start = max_date.replace(day=1).date()
        default_end = max_date.date()
    else:
        default_start = pd.Timestamp.now().replace(day=1).date()
        default_end = pd.Timestamp.now().date()

    date_range = st.sidebar.date_input(
        "Pilih Rentang Tanggal Produksi:",
        value=(default_start, default_end),
        min_value=min_date.date() if pd.notna(min_date) else None,
        max_value=max_date.date() if pd.notna(max_date) else None
    )

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

    st.info(f"📅 **Periode Data Aktif:** {periode_info} *(Bulan Berjalan Default)*")

    # TAB NAVIGATION (5 TAB)
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Ringkasan Eksekutif & OEE", 
        "🔍 Kualitas (QC) & Alur", 
        "⚠️ Verifikasi Data Anomali",
        "📋 Kepatuhan & Rapport Card",
        "🏭 Monitoring Operasional & WIP"
    ])

    # ==========================================
    # TAB 1: RINGKASAN EKSEKUTIF & OEE
    # ==========================================
    with tab1:
        st.subheader("Indikator Performa Utama (KPI) - Fokus OEE & Performa Subbrand")
        
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
            df_oee_perf['NO_MESIN_LINE_KEY'] = df_oee_perf['NO_MESIN_LINE'].astype(str).str.strip().str.replace('.0', '', regex=False)
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
        df_oee_perf_fp = df_oee_perf[kondisi_fp_oee].copy()

        df_oee_perf_fp['GROSS_OUTPUT_PCS'] = df_oee_perf_fp['CAPAIN_PRODUKSI_PCS'] + df_oee_perf_fp['REJECT_PCS'] + df_oee_perf_fp['REPROSES_PCS']
        df_oee_perf_fp['GROSS_CAPAIAN_PER_JAM'] = df_oee_perf_fp.apply(calc_gross_per_jam, axis=1)

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

        df_oee_perf_fp['SUBBRAND_DISPLAY'] = df_oee_perf_fp.apply(get_real_subbrand, axis=1)
        df_oee_perf_fp['TARGET_SUBBRAND_JAM'] = df_oee_perf_fp['SUBBRAND_DISPLAY'].apply(lambda x: get_target_capacity(x, dict_target_subbrand))
        
        df_oee_valid_perf = df_oee_perf_fp[df_oee_perf_fp['GROSS_CAPAIAN_PER_JAM'] > 0].copy()
        if not df_oee_valid_perf.empty:
            df_oee_valid_perf['RATIO_PERFORMANCE'] = (df_oee_valid_perf['GROSS_CAPAIAN_PER_JAM'] / df_oee_valid_perf['TARGET_SUBBRAND_JAM']).clip(upper=1.0)
            performance_rate = df_oee_valid_perf['RATIO_PERFORMANCE'].mean()
        else:
            performance_rate = 0.85

        availability_rate = 1.0
        nilai_oee = (availability_rate * performance_rate * quality_rate) * 100

        kondisi_fp_kpi = df_sic['NAMA_MESIN'].astype(str).str.contains('Filling|Press', case=False, na=False)
        total_output_fp = pd.to_numeric(df_sic[kondisi_fp_kpi]['OUTPUT_PCS'], errors='coerce').fillna(0).sum()

        # METRIK KPI TOP CARDS
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        col1.metric("Total Output (Filling & Press)", f"{total_output_fp:,.0f} Pcs")
        col2.metric("QTY Reject Kemasan (OEE)", f"{total_reject_kemas_oee:,.0f} Pcs")
        col3.metric("Quality Rate", f"{quality_rate * 100:.2f} %")
        col4.metric("Performance Rate (Gross FP)", f"{performance_rate * 100:.2f} %")
        col5.metric("Availability Rate", f"{availability_rate * 100:.0f} %")
        col6.metric("Nilai OEE", f"{nilai_oee:.2f} %")

        st.markdown("---")
        
        # RINGKASAN PER SUBBRAND & KINERJA MEDIAN MESIN
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

        st.markdown("---")

        # ---------------------------------------------------------
        # ANALISIS DETAIL HARIAN, MESIN & LINE
        # ---------------------------------------------------------
        list_mesin_tren = sorted(df_sic_fp['NAMA_MESIN'].unique().tolist())
        if list_mesin_tren:
            pilih_mesin_tren = st.selectbox("Pilih Jenis Mesin untuk Analisis Detail Line:", list_mesin_tren, key="sb_mesin_tren_tab1")

            # 1. TREN OUTPUT PER CYCLE & RATA-RATA OUTPUT PER JAM
            st.markdown("### 📊 Tren Output per Cycle & Rata-Rata Output per Jam")
            df_sic_selected = df_sic_fp[df_sic_fp['NAMA_MESIN'].str.upper() == pilih_mesin_tren.upper()].copy()
            df_sic_selected['LINE_DISPLAY'] = df_sic_selected['NO_MESIN_LINE'].apply(format_line_number) if 'NO_MESIN_LINE' in df_sic_selected.columns else "Line Unknown"

            col_t1, col_t2 = st.columns(2)

            with col_t1:
                st.markdown(f"**1. Tren Output per Cycle ({pilih_mesin_tren})**")
                if not df_sic_selected.empty:
                    df_sic_selected_sorted = df_sic_selected.sort_values(by='TIMESTAMP')
                    fig_cycle = px.line(
                        df_sic_selected_sorted,
                        x='TIMESTAMP',
                        y='OUTPUT_PCS',
                        color='LINE_DISPLAY',
                        markers=True,
                        labels={'TIMESTAMP': 'Waktu / Timestamp', 'OUTPUT_PCS': 'Output (Pcs)', 'LINE_DISPLAY': 'Nomor Line'}
                    )
                    fig_cycle.update_traces(hovertemplate='<b>%{data.name}</b><br>Waktu: %{x}<br>Output: %{y:,.0f} Pcs<extra></extra>')
                    fig_cycle.update_layout(height=380, legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
                    st.plotly_chart(fig_cycle, use_container_width=True)
                else:
                    st.info("Tidak ada data cycle untuk jenis mesin ini.")

            with col_t2:
                st.markdown(f"**2. Rata-Rata Output per Jam per Line ({pilih_mesin_tren})**")
                if not df_sic_selected.empty:
                    avg_hourly = df_sic_selected.groupby('LINE_DISPLAY')['OUTPUT_PCS'].mean().round(0).reset_index()
                    avg_hourly = avg_hourly.sort_values(by='LINE_DISPLAY')
                    
                    fig_avg = px.bar(
                        avg_hourly,
                        x='LINE_DISPLAY',
                        y='OUTPUT_PCS',
                        text='OUTPUT_PCS',
                        color='OUTPUT_PCS',
                        color_continuous_scale='Teal',
                        labels={'LINE_DISPLAY': 'Nomor Line', 'OUTPUT_PCS': 'Rata-Rata Output (Pcs/Jam)'}
                    )
                    fig_avg.update_traces(texttemplate='%{text:,.0f} Pcs', textposition='outside')
                    fig_avg.update_layout(height=380, showlegend=False, yaxis_range=[0, max(avg_hourly['OUTPUT_PCS'].max() * 1.25, 100)])
                    st.plotly_chart(fig_avg, use_container_width=True)
                else:
                    st.info("Tidak ada data rata-rata output untuk jenis mesin ini.")

            st.markdown("---")

            # 2. TABEL DETAIL ANALISIS OEE (DARI KODE REFERENSI ANDA)
            st.markdown(f"**📋 Tabel Detail Analisis OEE ({pilih_mesin_tren})**")
            df_oee_line_selected = df_oee_perf_fp[df_oee_perf_fp['NAMA_MESIN_KEY'].str.upper() == pilih_mesin_tren.upper()].copy()
            df_oee_line_selected['LINE_DISPLAY'] = df_oee_line_selected['NO_MESIN_LINE_KEY'].apply(format_line_number)

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
                df_table_oee['Quality'] = df_table_oee.apply(lambda r: (r['CAPAIN_PRODUKSI_PCS'] / r['GROSS_OUTPUT_PCS'] * 100) if r['GROSS_OUTPUT_PCS'] > 0 else 100.0, axis=1)

                df_table_oee['Raw_Performance'] = df_table_oee.apply(lambda r: (r['GROSS_CAPAIAN_PER_JAM'] / r['TARGET_SUBBRAND_JAM'] * 100) if r['TARGET_SUBBRAND_JAM'] > 0 else 85.0, axis=1)
                df_table_oee['Performance'] = df_table_oee['Raw_Performance'].clip(upper=100.0)
                df_table_oee['Status Target'] = df_table_oee['Raw_Performance'].apply(lambda x: f"⚡ Over Target ({x:.1f}%)" if x > 100.0 else "✅ Normal")

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

            # 3. ANALISIS TREN & STABILITAS OUTPUT ANTAK CYCLE (DARI KODE REFERENSI ANDA)
            st.markdown("---")
            st.markdown("### 📈 Analisis Tren & Stabilitas Output antar Cycle")

            df_sic_tren = df_sic_fp[df_sic_fp['NAMA_MESIN'] == pilih_mesin_tren].copy()
            if 'STATUS_ALUR' in df_sic_tren.columns:
                kondisi_istirahat_sanitasi = df_sic_tren['STATUS_ALUR'].astype(str).str.contains('ISTIRAHAT|SANITASI|BREAK|STOP', case=False, na=False)
                df_sic_produktif = df_sic_tren[(df_sic_tren['OUTPUT_PCS'] > 0) & (~kondisi_istirahat_sanitasi)].copy()
            else:
                df_sic_produktif = df_sic_tren[df_sic_tren['OUTPUT_PCS'] > 0].copy()

            df_sic_produktif['NO_MESIN_LINE'] = df_sic_produktif['NO_MESIN_LINE'].apply(format_line_number) if 'NO_MESIN_LINE' in df_sic_produktif.columns else "Line Unknown"

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
                    fig_tren.update_traces(hovertemplate='<b>%{data.name}</b><br>Siklus ke-%{x}<br>Output: %{y:,.0f} Pcs<extra></extra>')
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

        tot_reject = pd.to_numeric(df_qc['REJECT_PCS'], errors='coerce').fillna(0).sum() if 'REJECT_PCS' in df_qc.columns else 0
        tot_reproses = pd.to_numeric(df_qc['REPROSES_PCS'], errors='coerce').fillna(0).sum() if 'REPROSES_PCS' in df_qc.columns else 0
        tot_capaian = pd.to_numeric(df_oee['CAPAIN_PRODUKSI_PCS'], errors='coerce').fillna(0).sum() if 'CAPAIN_PRODUKSI_PCS' in df_oee.columns else 0
        
        total_gross_qc = tot_capaian + tot_reject + tot_reproses
        overall_quality = (tot_capaian / total_gross_qc * 100) if total_gross_qc > 0 else 100.0

        q_col1, q_col2, q_col3, q_col4 = st.columns(4)
        q_col1.metric("Total Reject (QC)", f"{tot_reject:,.0f} Pcs")
        q_col2.metric("Total Repproses (QC)", f"{tot_reproses:,.0f} Pcs")
        q_col3.metric("Total Defect (QC)", f"{(tot_reject + tot_reproses):,.0f} Pcs")
        q_col4.metric("Overall Quality Rate", f"{overall_quality:.2f} %")

        st.markdown("---")
        st.markdown("### 📈 Tren Bulanan Reject Rate & Reproses Rate (Keseluruhan Periode Data)")
        
        df_qc_temp_full = df_qc_full.copy()
        df_qc_temp_full['NO_BATCH_KEY'] = df_qc_temp_full['NO_BATCH'].astype(str).str.strip()
        df_qc_temp_full['NAMA_MESIN_KEY'] = df_qc_temp_full['NAMA_MESIN'].astype(str).str.strip().str.title()
        df_qc_temp_full['NO_MESIN_LINE_KEY'] = df_qc_temp_full['NO_MESIN_LINE'].astype(str).str.strip().str.replace('.0', '', regex=False) if 'NO_MESIN_LINE' in df_qc_temp_full.columns else ''
        df_qc_temp_full['REJECT_PCS'] = pd.to_numeric(df_qc_temp_full['REJECT_PCS'], errors='coerce').fillna(0)
        df_qc_temp_full['REPROSES_PCS'] = pd.to_numeric(df_qc_temp_full['REPROSES_PCS'], errors='coerce').fillna(0)

        qc_grouped_full = df_qc_temp_full.groupby(['NO_BATCH_KEY', 'NAMA_MESIN_KEY', 'NO_MESIN_LINE_KEY']).agg({
            'REJECT_PCS': 'sum',
            'REPROSES_PCS': 'sum'
        }).reset_index()

        df_oee_perf_full = df_oee_full.copy()
        df_oee_perf_full['NO_BATCH_KEY'] = df_oee_perf_full['NO_BATCH'].astype(str).str.strip()
        df_oee_perf_full['NAMA_MESIN_KEY'] = df_oee_perf_full['NAMA_MESIN'].astype(str).str.strip().str.title()
        if 'NO_MESIN_LINE' in df_oee_perf_full.columns:
            df_oee_perf_full['NO_MESIN_LINE_KEY'] = df_oee_perf_full['NO_MESIN_LINE'].astype(str).str.strip().str.replace('.0', '', regex=False)
        elif 'MESIN_FULL' in df_oee_perf_full.columns:
            df_oee_perf_full['NO_MESIN_LINE_KEY'] = df_oee_perf_full['MESIN_FULL'].astype(str).str.extract(r'(\d+)')[0].fillna('')
        else:
            df_oee_perf_full['NO_MESIN_LINE_KEY'] = ''

        df_oee_perf_full = df_oee_perf_full.merge(qc_grouped_full, on=['NO_BATCH_KEY', 'NAMA_MESIN_KEY', 'NO_MESIN_LINE_KEY'], how='left')
        df_oee_perf_full['REJECT_PCS'] = pd.to_numeric(df_oee_perf_full['REJECT_PCS'], errors='coerce').fillna(0)
        df_oee_perf_full['REPROSES_PCS'] = pd.to_numeric(df_oee_perf_full['REPROSES_PCS'], errors='coerce').fillna(0)
        df_oee_perf_full['CAPAIN_PRODUKSI_PCS'] = pd.to_numeric(df_oee_perf_full['CAPAIN_PRODUKSI_PCS'], errors='coerce').fillna(0)

        list_mesin_qc = sorted(df_oee_perf_full[df_oee_perf_full['NAMA_MESIN_KEY'].str.contains('Filling|Press', case=False, na=False)]['NAMA_MESIN_KEY'].unique().tolist())
        pilih_mesin_qc_bulanan = st.selectbox("Pilih Jenis Mesin untuk Tren QC:", list_mesin_qc, key="sb_mesin_qc_bulanan")

        df_qc_monthly = df_oee_perf_full[df_oee_perf_full['NAMA_MESIN_KEY'].str.upper() == pilih_mesin_qc_bulanan.upper()].copy()
        df_qc_monthly['BULAN_TAHUN'] = df_qc_monthly['TIMESTAMP'].dt.strftime('%Y-%m')

        monthly_summary = df_qc_monthly.groupby('BULAN_TAHUN').agg({
            'CAPAIN_PRODUKSI_PCS': 'sum',
            'REJECT_PCS': 'sum',
            'REPROSES_PCS': 'sum'
        }).reset_index()

        monthly_summary['GROSS_OUTPUT'] = monthly_summary['CAPAIN_PRODUKSI_PCS'] + monthly_summary['REJECT_PCS'] + monthly_summary['REPROSES_PCS']
        monthly_summary['Reject Rate (%)'] = monthly_summary.apply(lambda r: (r['REJECT_PCS'] / r['GROSS_OUTPUT'] * 100) if r['GROSS_OUTPUT'] > 0 else 0.0, axis=1).round(2)
        monthly_summary['Reproses Rate (%)'] = monthly_summary.apply(lambda r: (r['REPROSES_PCS'] / r['GROSS_OUTPUT'] * 100) if r['GROSS_OUTPUT'] > 0 else 0.0, axis=1).round(2)
        monthly_summary = monthly_summary.sort_values(by='BULAN_TAHUN')

        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.markdown(f"**Tren Bulanan Reject Rate (%) - {pilih_mesin_qc_bulanan}**")
            if not monthly_summary.empty:
                fig_rej_m = px.line(monthly_summary, x='BULAN_TAHUN', y='Reject Rate (%)', markers=True, color_discrete_sequence=['#EF553B'],
                                    labels={'BULAN_TAHUN': 'Bulan', 'Reject Rate (%)': 'Reject Rate (%)'})
                fig_rej_m.update_traces(hovertemplate='Bulan: %{x}<br>Reject Rate: %{y:.2f}%<extra></extra>')
                fig_rej_m.update_layout(height=350)
                st.plotly_chart(fig_rej_m, use_container_width=True)
            else:
                st.info("Data tren bulanan belum tersedia.")

        with col_m2:
            st.markdown(f"**Tren Bulanan Reproses Rate (%) - {pilih_mesin_qc_bulanan}**")
            if not monthly_summary.empty:
                fig_rep_m = px.line(monthly_summary, x='BULAN_TAHUN', y='Reproses Rate (%)', markers=True, color_discrete_sequence=['#FECB52'],
                                    labels={'BULAN_TAHUN': 'Bulan', 'Reproses Rate (%)': 'Reproses Rate (%)'})
                fig_rep_m.update_traces(hovertemplate='Bulan: %{x}<br>Reproses Rate: %{y:.2f}%<extra></extra>')
                fig_rep_m.update_layout(height=350)
                st.plotly_chart(fig_rep_m, use_container_width=True)
            else:
                st.info("Data tren bulanan belum tersedia.")

        st.markdown("---")
        st.markdown(f"### 📊 Perbandingan Defect Rate per Line Mesin ({pilih_mesin_qc_bulanan})")
        
        df_qc_line_data = df_oee_perf[df_oee_perf['NAMA_MESIN_KEY'].str.upper() == pilih_mesin_qc_bulanan.upper()].copy()

        if not df_qc_line_data.empty:
            df_qc_line_data['LINE_DISPLAY'] = df_qc_line_data['NO_MESIN_LINE_KEY'].apply(format_line_number)
            
            summary_line_qc = df_qc_line_data.groupby('LINE_DISPLAY').agg({
                'CAPAIN_PRODUKSI_PCS': 'sum',
                'REJECT_PCS': 'sum',
                'REPROSES_PCS': 'sum'
            }).reset_index()

            summary_line_qc['GROSS_OUTPUT'] = summary_line_qc['CAPAIN_PRODUKSI_PCS'] + summary_line_qc['REJECT_PCS'] + summary_line_qc['REPROSES_PCS']
            summary_line_qc['Reject Rate (%)'] = summary_line_qc.apply(lambda r: (r['REJECT_PCS'] / r['GROSS_OUTPUT'] * 100) if r['GROSS_OUTPUT'] > 0 else 0.0, axis=1).round(2)
            summary_line_qc['Reproses Rate (%)'] = summary_line_qc.apply(lambda r: (r['REPROSES_PCS'] / r['GROSS_OUTPUT'] * 100) if r['GROSS_OUTPUT'] > 0 else 0.0, axis=1).round(2)
            summary_line_qc = summary_line_qc.sort_values(by='LINE_DISPLAY')

            col_c1, col_c2 = st.columns(2)
            with col_c1:
                st.markdown(f"**Reject Rate (%) per Line Mesin ({pilih_mesin_qc_bulanan})**")
                if not summary_line_qc.empty:
                    fig_bar_rej = px.bar(
                        summary_line_qc, x='LINE_DISPLAY', y='Reject Rate (%)',
                        text='Reject Rate (%)', color='Reject Rate (%)', color_continuous_scale='Reds',
                        labels={'LINE_DISPLAY': 'Nomor Mesin', 'Reject Rate (%)': 'Reject Rate (%)'}
                    )
                    max_rej = summary_line_qc['Reject Rate (%)'].max()
                    fig_bar_rej.update_traces(texttemplate='%{text:.2f}%', textposition='outside')
                    fig_bar_rej.update_layout(height=350, yaxis_range=[0, max(max_rej * 1.25, 5)], showlegend=False)
                    st.plotly_chart(fig_bar_rej, use_container_width=True)
                else:
                    st.info("Data tidak tersedia.")

            with col_c2:
                st.markdown(f"**Reproses Rate (%) per Line Mesin ({pilih_mesin_qc_bulanan})**")
                if not summary_line_qc.empty:
                    fig_bar_rep = px.bar(
                        summary_line_qc, x='LINE_DISPLAY', y='Reproses Rate (%)',
                        text='Reproses Rate (%)', color='Reproses Rate (%)', color_continuous_scale='YlOrBr',
                        labels={'LINE_DISPLAY': 'Nomor Mesin', 'Reproses Rate (%)': 'Reproses Rate (%)'}
                    )
                    max_rep = summary_line_qc['Reproses Rate (%)'].max()
                    fig_bar_rep.update_traces(texttemplate='%{text:.2f}%', textposition='outside')
                    fig_bar_rep.update_layout(height=350, yaxis_range=[0, max(max_rep * 1.25, 5)], showlegend=False)
                    st.plotly_chart(fig_bar_rep, use_container_width=True)
                else:
                    st.info("Data tidak tersedia.")

    # ==========================================
    # TAB 3: VERIFIKASI ANOMALI
    # ==========================================
    with tab3:
        st.subheader("Daftar Data Anomali (Perlu Pengecekan Admin)")
        st.info("Data di bawah ini telah disingkirkan dari perhitungan grafik utama agar tidak merusak akurasi perhitungan.")
        
        st.markdown("**1. Data Double Input SIC (Selisih > 30 Menit)**")
        st.dataframe(df_anomali_double, use_container_width=True)
        
        st.markdown("**2. Data No Batch Tidak Valid (Selain Proses Timbang)**")
        st.dataframe(df_anomali_batch, use_container_width=True)

    # ==========================================
    # TAB 4: KEPATUHAN & RAPPORT CARD COMPLIANCE
    # ==========================================
    with tab4:
        st.subheader("📋 Rapport Card Compliance Operator & Missed QC Checklist")
        
        fonnte_token = st.secrets.get("FONNTE_TOKEN", "T3rQq4tCo8hEAC9ovYpa")
        wa_target_no = st.secrets.get("WA_TARGET", "6281283430510")
        
        col_m1, col_m2 = st.columns([2, 3])
        with col_m1:
            mode_audit = st.radio("Pilih Opsi Tampilan Audit:", ["Harian (Specific Date)", "Bulanan (Full Month)"], horizontal=True)
        
        max_dt = df_oee_full['TIMESTAMP'].max() if not df_oee_full.empty else datetime.now()
        
        if mode_audit == "Harian (Specific Date)":
            with col_m2:
                audit_date = st.date_input("Pilih Tanggal Audit Kepatuhan:", value=(max_dt - timedelta(days=1)).date())
                start_audit_str = audit_date.strftime("%Y-%m-%d")
                end_audit_str = audit_date.strftime("%Y-%m-%d")
                periode_judul = f"Tanggal {audit_date.strftime('%d %B %Y')}"
                
                start_trend_str = (audit_date - timedelta(days=6)).strftime("%Y-%m-%d")
                end_trend_str = end_audit_str
                judul_trend = f"Tren Kepatuhan 7 Hari Terakhir ({(audit_date - timedelta(days=6)).strftime('%d %b')} s.d. {audit_date.strftime('%d %b %Y')})"
        else:
            with col_m2:
                col_bln, col_thn = st.columns(2)
                with col_bln:
                    pilih_bulan = st.selectbox("Pilih Bulan:", list(range(1, 13)), index=max_dt.month - 1, format_func=lambda x: datetime(2000, x, 1).strftime('%B'))
                with col_thn:
                    pilih_tahun = st.number_input("Pilih Tahun:", min_value=2024, max_value=2030, value=max_dt.year)
                
                last_day = calendar.monthrange(pilih_tahun, pilih_bulan)[1]
                start_audit_str = f"{pilih_tahun:04d}-{pilih_bulan:02d}-01"
                end_audit_str = f"{pilih_tahun:04d}-{pilih_bulan:02d}-{last_day:02d}"
                periode_judul = f"Bulan {datetime(pilih_tahun, pilih_bulan, 1).strftime('%B %Y')}"
                
                start_trend_str = start_audit_str
                end_trend_str = end_audit_str
                judul_trend = f"Tren Kepatuhan Harian ({periode_judul})"

        with st.spinner("Menganalisis Kepatuhan 3-Form (OEE, SIC, QC)..."):
            compliance_res = get_rapport_cards_range(df_oee_full, df_sic_full, df_qc_full, start_audit_str, end_audit_str)

        c1, c2, c3 = st.columns(3)
        c1.metric("Kepatuhan Dept OEE - SIC", compliance_res['skorDeptOEESIC'])
        c2.metric("Kepatuhan Dept QC", compliance_res['skorDeptQC'])
        c3.metric("Akurasi Output (OEE vs SIC)", compliance_res['skorAkurasi'])

        st.markdown("---")
        st.markdown(f"### 📈 {judul_trend}")
        
        with st.spinner("Memuat tren kepatuhan..."):
            df_daily_trend = get_daily_compliance_trend(df_oee_full, df_sic_full, df_qc_full, start_trend_str, end_trend_str)

        if not df_daily_trend.empty:
            df_trend_melted = df_daily_trend.melt(
                id_vars=['Tanggal'], 
                value_vars=['Tim Produksi (OEE & SIC)', 'Tim QC (Inspector)'],
                var_name='Tim', 
                value_name='Compliance Rate (%)'
            ).dropna()

            fig_trend = px.line(
                df_trend_melted, x='Tanggal', y='Compliance Rate (%)', color='Tim', markers=True,
                color_discrete_map={'Tim Produksi (OEE & SIC)': '#008080', 'Tim QC (Inspector)': '#2B5B84'}
            )
            fig_trend.update_traces(hovertemplate='<b>%{data.name}</b><br>Tanggal: %{x}<br>Compliance Rate: %{y:.1f}%<extra></extra>')
            fig_trend.update_layout(height=350, yaxis_range=[0, 110], legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
            st.plotly_chart(fig_trend, use_container_width=True)
        else:
            st.info("Tidak ada aktivitas data produksi/QC pada rentang waktu ini.")

        st.markdown("---")
        st.markdown(f"### 📊 Perbandingan Compliance Rate User ({periode_judul})")
        
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            st.markdown("**1. Tim Operator Produksi (OEE & SIC)**")
            df_prod_comp = compliance_res['dfProdCompliance']
            if not df_prod_comp.empty and df_prod_comp['Total Batch'].sum() > 0:
                df_prod_comp = df_prod_comp.sort_values(by='Compliance Rate (%)', ascending=True)
                fig_prod_comp = px.bar(
                    df_prod_comp, x='Compliance Rate (%)', y='Operator', orientation='h',
                    text='Compliance Rate (%)', color='Compliance Rate (%)', color_continuous_scale='Teal',
                    labels={'Compliance Rate (%)': 'Compliance Rate (%)', 'Operator': 'Nama Operator'}, hover_data=['Total Batch']
                )
                fig_prod_comp.update_traces(texttemplate='%{text:.1f}%', textposition='outside')
                fig_prod_comp.update_layout(height=380, xaxis_range=[0, 115], showlegend=False)
                st.plotly_chart(fig_prod_comp, use_container_width=True)
            else:
                st.info(f"Tidak ada data aktivitas operator produksi pada {periode_judul}.")

        with col_g2:
            st.markdown("**2. Tim Inspector QC**")
            df_qc_comp = compliance_res['dfQCCompliance']
            if not df_qc_comp.empty and df_qc_comp['Total Batch'].sum() > 0:
                df_qc_comp = df_qc_comp.sort_values(by='Compliance Rate (%)', ascending=True)
                fig_qc_comp = px.bar(
                    df_qc_comp, x='Compliance Rate (%)', y='Inspector QC', orientation='h',
                    text='Compliance Rate (%)', color='Compliance Rate (%)', color_continuous_scale='Blues',
                    labels={'Compliance Rate (%)': 'Compliance Rate (%)', 'Inspector QC': 'Nama Inspector'}, hover_data=['Total Batch']
                )
                fig_qc_comp.update_traces(texttemplate='%{text:.1f}%', textposition='outside')
                fig_qc_comp.update_layout(height=380, xaxis_range=[0, 115], showlegend=False)
                st.plotly_chart(fig_qc_comp, use_container_width=True)
            else:
                st.info(f"Tidak ada data aktivitas inspector QC pada {periode_judul}.")

        st.markdown("---")
        st.markdown(f"### 👤 Detail Rapport Card Individual ({periode_judul})")
        
        op_cards = compliance_res['operatorCards']
        if op_cards:
            op_names = [card['operator'] for card in op_cards]
            selected_op = st.selectbox("🔍 Pilih Nama User untuk Detail Catatan:", op_names)
            card_user = next((c for c in op_cards if c['operator'] == selected_op), None)
            
            if card_user:
                status_color = "🔴" if card_user['statusCompliance'] == "NON-COMPLIANT" else "🟡" if card_user['statusCompliance'] == "REVIEW OUTPUT" else "🟢"
                st.markdown(f"**Status Operator:** {status_color} `{card_user['statusCompliance']}` | **Mesin:** {card_user['mesinList']} | **Total Catatan:** {card_user['totalIssue']} Item")
                
                if card_user['kelalaianInput']:
                    st.markdown("**❌ Kelalaian Input Form / Late Log (>30 Menit):**")
                    for k in card_user['kelalaianInput']:
                        tgl_k = f" ({k['tanggal']})" if k.get('tanggal') else ""
                        st.error(f"• **{k['mesin']}**{tgl_k} (Batch: `{k['batch']}`)\n  ↳ _{k['keterangan']}_")
                        
                if card_user['selisihOutput']:
                    st.markdown("**⚠️ Selisih Output Data (OEE vs SIC):**")
                    for s in card_user['selisihOutput']:
                        tgl_s = f" ({s['tanggal']})" if s.get('tanggal') else ""
                        sign = "+" if s['selisih'] > 0 else ""
                        st.warning(f"• **{s['mesin']}**{tgl_s} (Batch: `{s['batch']}`)\n  ↳ Akurasi: **{s['akurasi']}** (OEE: {s['oeeOut']:,.0f} vs SIC: {s['sicOut']:,.0f} | Diff: {sign}{s['selisih']:,.0f} Pcs)")
                
                if st.button(f"📲 Kirim Rapport Card {selected_op} via WhatsApp (Fonnte)"):
                    msg_wa = build_wa_msg_operator(card_user, periode_judul)
                    res_wa = send_fonnte_wa(fonnte_token, wa_target_no, msg_wa)
                    if res_wa.get("status") or res_wa.get("detail"):
                        st.success(f"Pesan WhatsApp Rapport Card {selected_op} Berhasil Dikirim!")
                    else:
                        st.error(f"Gagal mengirim WA: {res_wa.get('reason', 'Unknown Error')}")
        else:
            st.success(f"🎉 Seluruh operator 100% COMPLIANT pada {periode_judul}! Tidak ditemukan kelalaian input, late log, maupun selisih output.")

        st.markdown("---")
        st.markdown(f"### 🚨 Missed Checklist QC (Batch Belum Diisi Form QC)")
        qc_pending = compliance_res['qcPendingList']
        
        if qc_pending:
            st.dataframe(pd.DataFrame(qc_pending), use_container_width=True)
            if st.button("📲 Send Alert Missed QC List via WhatsApp (Fonnte)"):
                msg_qc = build_wa_msg_qc(qc_pending, periode_judul)
                res_qc = send_fonnte_wa(fonnte_token, wa_target_no, msg_qc)
                if res_qc.get("status") or res_qc.get("detail"):
                    st.success("Pesan Alert Missed QC Checklist Berhasil Dikirim!")
                else:
                    st.error(f"Gagal mengirim WA: {res_qc.get('reason', 'Unknown Error')}")
        else:
            st.success(f"✅ Seluruh form QC telah terisi lengkap untuk {periode_judul}.")

    # ==========================================
    # TAB 5: MONITORING OPERASIONAL & WIP
    # ==========================================
    with tab5:
        st.subheader("🏭 Monitoring Operasional Real-Time, WIP & Age Mixing")
        
        st.markdown("### 🟢 Real-Time Machine Status")
        
        df_all_machine_logs = pd.concat([
            df_oee[['NAMA_MESIN', 'TIMESTAMP', 'NO_BATCH', 'CAPAIN_PRODUKSI_PCS']].rename(columns={'CAPAIN_PRODUKSI_PCS': 'OUTPUT'}),
            df_sic[['NAMA_MESIN', 'TIMESTAMP', 'NO_BATCH', 'OUTPUT_PCS']].rename(columns={'OUTPUT_PCS': 'OUTPUT'})
        ]).dropna(subset=['NAMA_MESIN', 'TIMESTAMP'])
        
        if not df_all_machine_logs.empty:
            df_all_machine_logs['TIMESTAMP'] = pd.to_datetime(df_all_machine_logs['TIMESTAMP'], errors='coerce')
            latest_machines = df_all_machine_logs.sort_values('TIMESTAMP').groupby('NAMA_MESIN').last().reset_index()
            
            now = max_date if pd.notna(max_date) else pd.Timestamp.now()
            
            def get_machine_status(ts):
                if pd.isna(ts): return 'Idle'
                diff_hours = (now - ts).total_seconds() / 3600.0
                return 'On Running' if diff_hours <= 2 else 'Idle'

            latest_machines['STATUS'] = latest_machines['TIMESTAMP'].apply(get_machine_status)
            
            cnt_running = (latest_machines['STATUS'] == 'On Running').sum()
            cnt_idle = (latest_machines['STATUS'] == 'Idle').sum()
            cnt_total = len(latest_machines)

            m_col1, m_col2, m_col3 = st.columns(3)
            m_col1.metric("🟢 Mesin On Running", f"{cnt_running} Mesin")
            m_col2.metric("🔘 Mesin Idle", f"{cnt_idle} Mesin")
            m_col3.metric("🏭 Total Mesin Terdaftar", f"{cnt_total} Mesin")

            st.markdown("---")
            kat_options = ["Semua Mesin", "Filling", "Press", "Mixing / Timbang", "Packing"]
            pilih_kat_mesin = st.selectbox("🔍 Filter Tampilan Mesin per Kategori:", kat_options)

            df_filtered_m = latest_machines.copy()
            if pilih_kat_mesin != "Semua Mesin":
                df_filtered_m = df_filtered_m[df_filtered_m['NAMA_MESIN'].str.contains(pilih_kat_mesin, case=False, na=False)]

            with st.expander(f"📌 Tampilkan Kartu Status Mesin ({len(df_filtered_m)} Mesin)", expanded=True):
                cols_per_row = 4
                machine_list = df_filtered_m.to_dict('records')
                
                for i in range(0, len(machine_list), cols_per_row):
                    cols = st.columns(cols_per_row)
                    for j, m_data in enumerate(machine_list[i:i+cols_per_row]):
                        with cols[j]:
                            is_running = m_data['STATUS'] == 'On Running'
                            card_bg = "#1e4620" if is_running else "#3a3a3a"
                            border_color = "#2e7d32" if is_running else "#616161"
                            status_icon = "🟢 RUNNING" if is_running else "🔘 IDLE"
                            
                            st.markdown(
                                f"""
                                <div style="background-color: {card_bg}; padding: 12px; border-radius: 8px; border: 1px solid {border_color}; margin-bottom: 10px;">
                                    <h5 style="margin:0; color: white;">{m_data['NAMA_MESIN']}</h5>
                                    <p style="margin:2px 0; font-size: 12px; color: #ddd;"><b>Status:</b> {status_icon}</p>
                                    <p style="margin:2px 0; font-size: 11px; color: #bbb;"><b>Batch:</b> {m_data['NO_BATCH'] or '-'}</p>
                                    <p style="margin:2px 0; font-size: 11px; color: #bbb;"><b>Output:</b> {m_data['OUTPUT']:,.0f} Pcs</p>
                                </div>
                                """,
                                unsafe_allow_html=True
                            )
        else:
            st.info("Tidak ada data transaksi mesin yang terdeteksi.")

        st.markdown("---")
        col_wip1, col_wip2 = st.columns([3, 2])

        with col_wip1:
            st.markdown("### 📋 Tabel WIP Operasional (Tracking Batch)")
            df_wip_res = get_wip_operational(df_sic, df_oee)
            if not df_wip_res.empty:
                st.dataframe(df_wip_res, use_container_width=True, height=400)
            else:
                st.info("Data WIP Operasional tidak ditemukan pada periode ini.")

        with col_wip2:
            st.markdown("### ⏱️ Age Mixing (Aging Ruahan Ke Filling)")
            df_age_res = get_age_mixing(df_sic, df_oee)
            if not df_age_res.empty:
                def highlight_aging(val):
                    if isinstance(val, (int, float)) and val > 24.0:
                        return 'background-color: #7f1d1d; color: white;'
                    return ''
                
                st.dataframe(
                    df_age_res.style.applymap(highlight_aging, subset=['Umur Ruahan (Jam)']),
                    use_container_width=True, 
                    height=400
                )
            else:
                st.info("Data Age Mixing tidak ditemukan pada periode ini.")

except Exception as e:
    st.error(f"Terjadi kesalahan pada aplikasi: {e}")
