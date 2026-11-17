"""
report_engine.py
Generator Daily Report (1 halaman A4, PDF + PNG) untuk periode H-1.

Tidak bergantung pada Streamlit. Seluruh angka dihitung oleh kpi_core.py dan compliance_engine.py,
yaitu fungsi yang sama dengan yang dipakai dashboard, sehingga angka report = angka dashboard.

Pemakaian:
    hasil = generate_report(report_date, df_oee, df_sic, df_qc, df_target)
    hasil['pdf']   -> bytes (PDF A4)
    hasil['png']   -> bytes (PNG A4, 200 dpi)
    hasil['data']  -> dict angka yang dipakai (untuk debugging)
"""
import io
import textwrap

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

from kpi_core import hitung_konteks_oee, hitung_komponen_oee_per_kategori, hitung_kpi_qc
from compliance_engine import get_rapport_cards_range

matplotlib.rcParams["pdf.fonttype"] = 42      # teks di PDF tetap bisa diseleksi/dicari
matplotlib.rcParams["font.family"] = "DejaVu Sans"

A4_W, A4_H = 8.27, 11.69                       # inci (A4 portrait)
MARGIN = 0.40

# Palet warna (selaras dengan dashboard: teal / biru / hijau / ungu)
NAVY = "#1F3A5F"
TEAL = "#2B7A8C"
BLUE = "#2F6FB0"
GREEN = "#5BAE6E"
PURPLE = "#8C7FC4"
LOW = "#E4572E"            # highlight user di bawah rata-rata
GRAY = "#5B6B7B"
TILE_BG = "#F4F7FA"
TILE_EDGE = "#D6DEE6"

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
         "September", "Oktober", "November", "Desember"]


def format_tanggal_id(d, dengan_hari=True):
    d = pd.Timestamp(d)
    teks = f"{d.day} {BULAN[d.month - 1]} {d.year}"
    return f"{HARI[d.weekday()]}, {teks}" if dengan_hari else teks


# ======================================================================
# 1. PERHITUNGAN DATA REPORT
# ======================================================================
def hitung_data_report(report_date, df_oee, df_sic, df_qc, df_target):
    """
    df_oee / df_sic / df_qc : data yang SUDAH melalui clean_data(), mencakup minimal H-2 s.d. H+1
                              (agar logika tanggal kerja/cut-off 06:15 pada compliance lengkap).
    KPI Produksi & QC memakai baris dengan TIMESTAMP pada tanggal H-1 (00:00-23:59),
    sama dengan dashboard bila rentang tanggal diset ke H-1 s.d. H-1.
    Compliance memakai tanggal kerja H-1 (sama dengan menu Kepatuhan di dashboard).
    """
    d = pd.Timestamp(report_date).normalize()
    ds = d.strftime("%Y-%m-%d")
    peringatan = []

    def filter_hari(df):
        if df is None or df.empty or "TIMESTAMP" not in df.columns:
            return df.iloc[0:0] if df is not None else pd.DataFrame()
        ts = pd.to_datetime(df["TIMESTAMP"], errors="coerce")
        return df[ts.dt.normalize() == d].reset_index(drop=True)

    o, s, q = filter_hari(df_oee), filter_hari(df_sic), filter_hari(df_qc)

    data = {
        "tanggal": d.date(),
        "n_baris": {"oee": len(o), "sic": len(s), "qc": len(q)},
        "prod": None,
        "kategori": pd.DataFrame(),
        "qc": hitung_kpi_qc(q, o),
        "compliance": None,
        "peringatan": peringatan,
    }

    # ---- KPI Produksi & komponen OEE per kategori mesin
    if not o.empty:
        ctx = hitung_konteks_oee(o, q, s, df_target)
        data["prod"] = {
            "total_output": ctx["total_output_fp"],
            "reject_kemas": ctx["total_reject_kemas_oee"],
            "quality": ctx["quality_rate"] * 100,
            "performance": ctx["performance_rate"] * 100,
            "availability": ctx["availability_rate"] * 100,
            "oee": ctx["nilai_oee"],
        }
        dfp = ctx["df_oee_perf_fp"]
        data["kategori"] = hitung_komponen_oee_per_kategori(dfp)
        if not dfp.empty and "SUMBER_TARGET" in dfp.columns:
            sb_default = dfp.loc[(dfp["SUMBER_TARGET"] == "Default") & (dfp["SUBBRAND_DISPLAY"] != "-"), "SUBBRAND_DISPLAY"].nunique()
            data["n_subbrand_target_default"] = int(sb_default)
            if sb_default:
                peringatan.append(f"{sb_default} subbrand belum punya target di master target (memakai default 2.500 pcs/jam).")
    else:
        peringatan.append("Tidak ada data OEE pada tanggal report.")

    # ---- Compliance (tanggal kerja H-1)
    try:
        res = get_rapport_cards_range(df_oee, df_sic, df_qc, ds, ds)
        data["compliance"] = {
            "dept_oee_sic": res["skorDeptOEESIC"],
            "dept_qc": res["skorDeptQC"],
            "akurasi": res["skorAkurasi"],
            "df_prod": res["dfProdCompliance"],
            "df_qc": res["dfQCCompliance"],
        }
    except Exception as e:  # report tetap terbentuk walau modul compliance gagal
        peringatan.append(f"Compliance tidak dapat dihitung: {e}")

    return data


# ======================================================================
# 2. RENDER A4
# ======================================================================
def _fmt_int(x):
    return f"{x:,.0f}"


def render_report(data, waktu_dibuat=None):
    waktu_dibuat = waktu_dibuat or pd.Timestamp.now(tz="Asia/Jakarta")
    fig = plt.figure(figsize=(A4_W, A4_H), dpi=100)
    fig.patch.set_facecolor("white")

    bg = fig.add_axes([0, 0, 1, 1])
    bg.set_xlim(0, A4_W)
    bg.set_ylim(A4_H, 0)          # sumbu y dari atas ke bawah (satuan inci)
    bg.axis("off")

    def ax_in(x, y, w, h):
        return fig.add_axes([x / A4_W, 1 - (y + h) / A4_H, w / A4_W, h / A4_H])

    content_w = A4_W - 2 * MARGIN

    # ---------------- Header
    bg.add_patch(Rectangle((0, 0), A4_W, 0.85, fc=NAVY, ec="none"))
    bg.text(MARGIN, 0.31, "DAILY REPORT PRODUKSI, QC & KEPATUHAN", fontsize=15, weight="bold", color="white", va="center")
    bg.text(MARGIN, 0.60, f"Periode: {format_tanggal_id(data['tanggal'])}  (H-1)", fontsize=9.5, color="#D9E4F2", va="center")
    bg.text(A4_W - MARGIN, 0.60, f"Dibuat: {waktu_dibuat.strftime('%d/%m/%Y %H:%M')} WIB", fontsize=7.5, color="#D9E4F2", va="center", ha="right")

    def section(y, teks):
        bg.text(MARGIN, y, teks, fontsize=9.5, weight="bold", color=NAVY, va="center")
        bg.plot([MARGIN, A4_W - MARGIN], [y + 0.15, y + 0.15], color=TEAL, lw=0.9)

    def tile(x, y, w, h, label, value, sub="", accent=TEAL, vsize=11.5, wrap=22):
        bg.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.06", fc=TILE_BG, ec=TILE_EDGE, lw=0.8))
        bg.add_patch(Rectangle((x, y + 0.08), 0.05, h - 0.16, fc=accent, ec="none"))
        bg.text(x + 0.13, y + 0.07, textwrap.fill(label, wrap), fontsize=6, color=GRAY, va="top", ha="left", linespacing=1.15)
        bg.text(x + 0.13, y + h * 0.66, value, fontsize=vsize, weight="bold", color=NAVY, va="center", ha="left")
        if sub:
            bg.text(x + 0.13, y + h - 0.09, sub, fontsize=5.8, color=GRAY, va="center", ha="left")

    def tidak_ada_data(x, y, w, h, teks):
        bg.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.06", fc="#FAFAFA", ec=TILE_EDGE, lw=0.8, ls="--"))
        bg.text(x + w / 2, y + h / 2, teks, fontsize=7.5, color=GRAY, ha="center", va="center")

    gap6 = 0.1
    w6 = (content_w - 5 * gap6) / 6

    # ---------------- 1. KPI Produksi
    section(0.98, "1. Ringkasan Indikator Performa (KPI) Produksi")
    y0, h0 = 1.22, 0.64
    p = data["prod"]
    if p:
        items = [
            ("Total Output (Filling & Press)", _fmt_int(p["total_output"]), "Pcs", TEAL),
            ("QTY Reject Kemasan (OEE)", _fmt_int(p["reject_kemas"]), "Pcs", LOW),
            ("Quality Rate", f"{p['quality']:.2f}%", "", GREEN),
            ("Performance Rate (Gross FP)", f"{p['performance']:.2f}%", "", BLUE),
            ("Availability Rate", f"{p['availability']:.0f}%", "", PURPLE),
            ("Nilai OEE", f"{p['oee']:.2f}%", "", NAVY),
        ]
        for i, (lb, val, sub, col) in enumerate(items):
            tile(MARGIN + i * (w6 + gap6), y0, w6, h0, lb, val, sub, col)
    else:
        tidak_ada_data(MARGIN, y0, content_w, h0, "Tidak ada data produksi (OEE) pada tanggal ini.")

    # ---------------- 2. KPI QC
    section(2.04, "2. Ringkasan Indikator Performa (KPI) Kualitas (QC)")
    y1 = 2.28
    qc = data["qc"]
    sumber_qc_ada = data["n_baris"]["qc"] > 0 or data["n_baris"]["oee"] > 0
    if sumber_qc_ada:
        items = [
            ("Total Reject (QC)", _fmt_int(qc["total_reject"]), "Pcs", LOW),
            ("Total Reproses (QC)", _fmt_int(qc["total_reproses"]), "Pcs", "#E0A100"),
            ("Total Defect (QC)", _fmt_int(qc["total_defect"]), "Pcs", NAVY),
            ("Overall Quality Rate", f"{qc['overall_quality']:.2f}%", "", GREEN),
            ("Rata-rata Reject Rate", f"{qc['reject_rate']:.2f}%", "terhadap gross output", LOW),
            ("Rata-rata Reproses Rate", f"{qc['reproses_rate']:.2f}%", "terhadap gross output", "#E0A100"),
        ]
        for i, (lb, val, sub, col) in enumerate(items):
            tile(MARGIN + i * (w6 + gap6), y1, w6, h0, lb, val, sub, col)
    else:
        tidak_ada_data(MARGIN, y1, content_w, h0, "Tidak ada data QC pada tanggal ini.")

    # ---------------- 3. Komponen OEE per kategori mesin
    section(3.10, "3. Rata-Rata Komponen OEE per Kategori Mesin")
    dfk = data["kategori"]
    cw, ch, gapx, gapy = 3.62, 1.70, content_w - 2 * 3.62, 0.08
    ytop = 3.38
    if dfk is not None and not dfk.empty:
        labels = [textwrap.fill(f"{r['Kategori Mesin']}", 13) + f"\n({int(r['Jumlah Line'])} line)" for _, r in dfk.iterrows()]
        metrik = [("OEE (%)", "Nilai OEE", TEAL), ("Performance (%)", "Performance Rate", BLUE),
                  ("Quality (%)", "Quality Rate", GREEN), ("Availability (%)", "Availability Rate", PURPLE)]
        for idx, (kol, judul, warna) in enumerate(metrik):
            r_, c_ = divmod(idx, 2)
            x = MARGIN + c_ * (cw + gapx)
            y = ytop + r_ * (ch + gapy)
            ax = ax_in(x + 0.30, y + 0.22, cw - 0.32, ch - 0.22 - 0.44)
            vals = dfk[kol].astype(float).values
            xs = np.arange(len(vals))
            ax.bar(xs, vals, color=warna, width=0.62)
            for xi, v in zip(xs, vals):
                ax.text(xi, v + 1.5, f"{v:.2f}%", ha="center", va="bottom", fontsize=5.6, color="#333")
            ax.set_ylim(0, 120)
            ax.set_yticks([0, 20, 40, 60, 80, 100])
            ax.tick_params(axis="y", labelsize=5.8, length=2)
            ax.set_xticks(xs)
            ax.set_xticklabels(labels, fontsize=5.2, linespacing=1.05)
            ax.tick_params(axis="x", length=0, pad=2)
            ax.set_xlim(-0.6, len(vals) - 0.4)
            for sp in ("top", "right"):
                ax.spines[sp].set_visible(False)
            ax.spines["left"].set_color("#BBB")
            ax.spines["bottom"].set_color("#BBB")
            ax.grid(axis="y", color="#E6E6E6", lw=0.5)
            ax.set_axisbelow(True)
            bg.text(x + 0.30, y + 0.08, judul, fontsize=7.5, weight="bold", color=NAVY, va="center")
    else:
        tidak_ada_data(MARGIN, ytop, content_w, 2 * ch + gapy, "Tidak ada data Filling & Press pada tanggal ini.")

    # ---------------- 4. Ringkasan Compliance
    section(6.96, "4. Ringkasan Kepatuhan (Compliance)")
    y4 = 7.24
    comp = data["compliance"]
    gap3 = 0.1
    w3 = (content_w - 2 * gap3) / 3
    if comp:
        def _vs(teks):
            return 14 if len(str(teks)) <= 14 else 9
        tile(MARGIN, y4, w3, 0.58, "Kepatuhan Dept OEE - SIC", str(comp["dept_oee_sic"]), "", TEAL, vsize=_vs(comp["dept_oee_sic"]), wrap=40)
        tile(MARGIN + w3 + gap3, y4, w3, 0.58, "Kepatuhan Dept QC", str(comp["dept_qc"]), "", BLUE, vsize=_vs(comp["dept_qc"]), wrap=40)
        tile(MARGIN + 2 * (w3 + gap3), y4, w3, 0.58, "Akurasi Output (OEE vs SIC)", str(comp["akurasi"]), "", PURPLE, vsize=_vs(comp["akurasi"]), wrap=40)
    else:
        tidak_ada_data(MARGIN, y4, content_w, 0.58, "Data compliance tidak tersedia.")

    # ---------------- 5. Compliance per user
    section(7.96, "5. Compliance Rate per User")
    y5, h5 = 8.26, 2.68
    half = (content_w - 0.2) / 2

    def grafik_user(x, judul, df, kolom_nama, warna_normal):
        bg.text(x, y5 + 0.04, judul, fontsize=7.5, weight="bold", color=NAVY, va="center")
        d = df[df["Total Batch"] > 0].copy() if (df is not None and not df.empty and "Total Batch" in df.columns) else pd.DataFrame()
        if d.empty:
            tidak_ada_data(x, y5 + 0.2, half, h5 - 0.2, "Tidak ada data aktivitas user.")
            return None
        d = d.sort_values("Compliance Rate (%)", ascending=True).reset_index(drop=True)
        n = len(d)
        rata = float(d["Compliance Rate (%)"].mean())
        rate = d["Compliance Rate (%)"].astype(float).values
        warna = [LOW if v < rata - 1e-9 else warna_normal for v in rate]
        fs = max(4.6, min(6.4, 78.0 / n))
        ax = ax_in(x + 1.38, y5 + 0.34, half - 1.38 - 0.05, h5 - 0.34 - 0.20)
        ax.barh(np.arange(n), rate, color=warna, height=0.68)
        rendah = rate < rata - 1e-9
        for i, v in enumerate(rate):
            ax.text(v + 1.2, i, f"{v:.1f}%", va="center", ha="left", fontsize=fs - 0.2,
                    color=(LOW if rendah[i] else "#333"), weight=("bold" if rendah[i] else "normal"),
                    bbox=dict(fc="white", ec="none", pad=0.6, alpha=0.85), zorder=5)
        ax.axvline(rata, color="#333", ls="--", lw=0.8)
        ax.annotate(f"Rata-rata {rata:.1f}%", xy=(rata, 1.0), xycoords=("data", "axes fraction"), xytext=(0, 2),
                    textcoords="offset points", ha="center", va="bottom", fontsize=5.6, color="#333")
        ax.set_yticks(np.arange(n))
        ax.set_yticklabels([str(nm)[:28] for nm in d[kolom_nama]], fontsize=fs)
        for lbl, rd in zip(ax.get_yticklabels(), rendah):
            if rd:
                lbl.set_color(LOW)
                lbl.set_fontweight("bold")
        ax.set_xlim(0, 122)
        ax.set_ylim(-0.6, n - 0.4)
        ax.set_xticks([0, 20, 40, 60, 80, 100])
        ax.tick_params(axis="x", labelsize=5.6, length=2)
        ax.tick_params(axis="y", length=0, pad=2)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.spines["left"].set_color("#BBB")
        ax.spines["bottom"].set_color("#BBB")
        ax.grid(axis="x", color="#E6E6E6", lw=0.5)
        ax.set_axisbelow(True)
        return int((rate < rata - 1e-9).sum())

    if comp:
        grafik_user(MARGIN, "Tim Operator Produksi (OEE & SIC)", comp["df_prod"], "Operator", TEAL)
        grafik_user(MARGIN + half + 0.2, "Tim Inspector QC", comp["df_qc"], "Inspector QC", BLUE)
    else:
        tidak_ada_data(MARGIN, y5 + 0.2, content_w, h5 - 0.2, "Data compliance tidak tersedia.")

    # legenda
    ly = 11.10
    bg.add_patch(Rectangle((MARGIN, ly - 0.045), 0.14, 0.09, fc=LOW, ec="none"))
    bg.text(MARGIN + 0.19, ly, "User di bawah rata-rata compliance kelompoknya", fontsize=5.8, color=GRAY, va="center")
    bg.add_patch(Rectangle((MARGIN + 2.95, ly - 0.045), 0.14, 0.09, fc=TEAL, ec="none"))
    bg.add_patch(Rectangle((MARGIN + 3.09, ly - 0.045), 0.14, 0.09, fc=BLUE, ec="none"))
    bg.text(MARGIN + 3.28, ly, "Sama dengan / di atas rata-rata", fontsize=5.8, color=GRAY, va="center")
    bg.plot([MARGIN + 5.05, MARGIN + 5.33], [ly, ly], color="#333", ls="--", lw=0.8)
    bg.text(MARGIN + 5.38, ly, "Rata-rata kelompok", fontsize=5.8, color=GRAY, va="center")

    # ---------------- Footer
    bg.plot([MARGIN, A4_W - MARGIN], [11.21, 11.21], color="#CCC", lw=0.6)
    catatan = (
        f"Periode data: {format_tanggal_id(data['tanggal'])}, 00:00-23:59 WIB (KPI Produksi & QC). Compliance mengikuti tanggal kerja (cut-off 06:15). "
        "Komponen OEE per kategori = rata-rata nilai seluruh nomor mesin dalam kategori. Reject/Reproses Rate = reject (reproses) / (capaian + reject + reproses)."
    )
    bg.text(MARGIN, 11.27, textwrap.fill(catatan, 150), fontsize=5, color=GRAY, va="top", linespacing=1.25)
    extra = []
    if data.get("n_subbrand_target_default"):
        extra.append(f"Catatan: {data['n_subbrand_target_default']} subbrand memakai target default 2.500 pcs/jam (belum ada di master target).")
    for w_ in data["peringatan"]:
        if "master target" not in w_:
            extra.append(w_)
    if extra:
        bg.text(MARGIN, 11.55, " | ".join(extra)[:230], fontsize=5, color=LOW, va="top")

    return fig


# ======================================================================
# 3. OUTPUT (PDF / PNG)
# ======================================================================
def figure_ke_bytes(fig, fmt):
    buf = io.BytesIO()
    if fmt == "pdf":
        fig.savefig(buf, format="pdf", metadata={"Title": "Daily Report Produksi, QC & Kepatuhan", "Author": "Dashboard Cerdas"})
    else:
        fig.savefig(buf, format="png", dpi=200)
    return buf.getvalue()


def generate_report(report_date, df_oee, df_sic, df_qc, df_target):
    """Hitung data lalu hasilkan PDF + PNG A4 (1 halaman)."""
    data = hitung_data_report(report_date, df_oee, df_sic, df_qc, df_target)
    fig = render_report(data)
    try:
        pdf = figure_ke_bytes(fig, "pdf")
        png = figure_ke_bytes(fig, "png")
    finally:
        plt.close(fig)
    return {"pdf": pdf, "png": png, "data": data, "tanggal": data["tanggal"]}
