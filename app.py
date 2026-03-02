#!/usr/bin/env python3
"""
HOW TO RUN:
- pip install -r requirements.txt
- python ingest.py --pdf_dir /pad/naar/pdfs --db_path nematodes.db
- streamlit run app.py
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import plotly.express as px
import streamlit as st
from rapidfuzz import process, fuzz

import ingest

DB_DEFAULT = "nematodes.db"


@st.cache_resource
def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    ingest.init_db(conn)
    return conn


def read_df(conn: sqlite3.Connection, query: str, params: Optional[tuple] = None) -> pd.DataFrame:
    return pd.read_sql_query(query, conn, params=params or ())


def save_mapping(conn: sqlite3.Connection, mapping: Dict[str, str]) -> None:
    for raw, field in mapping.items():
        if not raw:
            continue
        field_value = field.strip() or None
        conn.execute(
            """
            INSERT INTO field_mapping(raw_field_name, field_name)
            VALUES(?, ?)
            ON CONFLICT(raw_field_name) DO UPDATE SET field_name=excluded.field_name
            """,
            (raw, field_value),
        )
    conn.execute(
        """
        UPDATE samples
        SET field_name = (
            SELECT fm.field_name FROM field_mapping fm WHERE fm.raw_field_name = samples.raw_field_name
        )
        WHERE raw_field_name IS NOT NULL
        """
    )
    conn.commit()


def import_from_folder(conn: sqlite3.Connection, folder: str, db_path: str) -> Dict[str, int]:
    pdf_dir = Path(folder)
    return ingest.ingest_directory(pdf_dir=pdf_dir, db_path=Path(db_path), failed_path=Path("failed_samples.jsonl"))


def import_uploaded_files(conn: sqlite3.Connection, files, db_path: str) -> Dict[str, int]:
    tmp_root = Path(tempfile.mkdtemp(prefix="nema_upload_"))
    for f in files:
        (tmp_root / f.name).write_bytes(f.getvalue())
    return ingest.ingest_directory(pdf_dir=tmp_root, db_path=Path(db_path), failed_path=Path("failed_samples.jsonl"))


def tab_import(conn: sqlite3.Connection, db_path: str) -> None:
    st.subheader("PDF import")
    folder = st.text_input("Map met PDF's", value="")
    uploads = st.file_uploader("Of upload PDF-bestanden", type=["pdf"], accept_multiple_files=True)

    if st.button("Importeer / Update", type="primary"):
        total = {"new_samples": 0, "skipped_samples": 0, "pdf_count": 0}
        if folder.strip():
            stats = import_from_folder(conn, folder.strip(), db_path)
            total = {k: total[k] + stats.get(k, 0) for k in total}
        if uploads:
            stats = import_uploaded_files(conn, uploads, db_path)
            total = {k: total[k] + stats.get(k, 0) for k in total}

        st.success(
            f"Import klaar. PDFs: {total['pdf_count']} | Nieuwe samples: {total['new_samples']} | Overgeslagen: {total['skipped_samples']}"
        )


def tab_mapping(conn: sqlite3.Connection) -> None:
    st.subheader("Percelen matchen")

    raw_df = read_df(
        conn,
        """
        SELECT s.raw_field_name, COALESCE(fm.field_name, s.field_name) AS field_name
        FROM (SELECT DISTINCT raw_field_name, field_name FROM samples WHERE raw_field_name IS NOT NULL) s
        LEFT JOIN field_mapping fm ON fm.raw_field_name = s.raw_field_name
        ORDER BY s.raw_field_name
        """,
    )

    csv_file = st.file_uploader("Optioneel: upload CSV met officiële perceelnamen", type=["csv"], key="official_csv")
    official_names: List[str] = []
    if csv_file:
        cdf = pd.read_csv(csv_file)
        for col in cdf.columns:
            if "field" in col.lower() or "perceel" in col.lower() or col.lower() == "name":
                official_names.extend(cdf[col].dropna().astype(str).tolist())
        if not official_names and len(cdf.columns) > 0:
            official_names = cdf.iloc[:, 0].dropna().astype(str).tolist()

    known_names = set(read_df(conn, "SELECT DISTINCT field_name FROM field_mapping WHERE field_name IS NOT NULL")['field_name'])
    known_names.update([x for x in official_names if x])

    st.caption(f"Unieke raw percelen: {len(raw_df)}")
    edits: Dict[str, str] = {}

    for _, row in raw_df.iterrows():
        raw = row["raw_field_name"]
        current = row["field_name"] if pd.notna(row["field_name"]) else ""
        suggestions = process.extract(raw, list(known_names), scorer=fuzz.WRatio, limit=5) if known_names else []

        with st.expander(f"Raw: {raw}"):
            st.write(f"Huidige mapping: **{current or '-'}**")
            if suggestions:
                st.write("Suggesties:")
                for name, score, _ in suggestions:
                    st.write(f"- {name} ({score})")
            edits[raw] = st.text_input(f"Echte perceelsnaam voor '{raw}'", value=current, key=f"map_{raw}")

    if st.button("Opslaan mapping", type="primary"):
        save_mapping(conn, edits)
        st.success("Mapping opgeslagen en samples bijgewerkt.")


def tab_analysis(conn: sqlite3.Connection) -> None:
    st.subheader("Analyse & Grafieken")

    samples_df = read_df(conn, "SELECT * FROM samples")
    results_df = read_df(conn, "SELECT * FROM results_long")

    if samples_df.empty or results_df.empty:
        st.info("Nog geen data beschikbaar. Importeer eerst PDF's.")
        return

    merged = results_df.merge(samples_df[["sample_id", "field_name", "raw_field_name", "report_date"]], on="sample_id", how="left")
    merged["field_display"] = merged["field_name"].fillna(merged["raw_field_name"])

    fields = sorted([x for x in merged["field_display"].dropna().unique()])
    field_choice = st.selectbox("Perceel", options=fields)
    analysis_type = st.selectbox("Analyse type", options=["vrije_aaltjes", "cysteaaltjes"])

    sub = merged[(merged["field_display"] == field_choice) & (merged["analysis_type"] == analysis_type)].copy()
    if sub.empty:
        st.warning("Geen data voor gekozen filter")
        return

    sub["report_date_dt"] = pd.to_datetime(sub["report_date"], errors="coerce")
    min_d = sub["report_date_dt"].min()
    max_d = sub["report_date_dt"].max()

    if pd.notna(min_d) and pd.notna(max_d):
        date_range = st.date_input("Datum range (report_date)", [min_d.date(), max_d.date()])
        if len(date_range) == 2:
            start, end = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])
            sub = sub[(sub["report_date_dt"].isna()) | ((sub["report_date_dt"] >= start) & (sub["report_date_dt"] <= end))]

    species_options = sorted(sub["species"].dropna().unique())
    species_selected = st.multiselect("Species", options=species_options, default=species_options)
    sub = sub[sub["species"].isin(species_selected)]

    metric = "value"
    if analysis_type == "cysteaaltjes":
        metric = st.radio("Y-as voor cysteaaltjes", options=["cysts", "lle"], horizontal=True)

    timeline = sub.sort_values(["report_date_dt", "report_date"])
    timeline_plot = timeline.drop(columns=["sample_id"], errors="ignore")
    fig = px.line(
        timeline_plot,
        x="report_date",
        y=metric,
        color="species",
        markers=True,
        hover_data={"infection_class": True, "unit": True},
        title="Tijdlijn per species",
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("#### Top soorten op laatste meting")
    latest_date = timeline["report_date_dt"].max()
    latest_df = timeline[timeline["report_date_dt"] == latest_date] if pd.notna(latest_date) else timeline.tail(10)
    top_df = latest_df.groupby("species", as_index=False)[metric].sum().sort_values(metric, ascending=False).head(10)
    st.plotly_chart(px.bar(top_df, x="species", y=metric, title="Top soorten (laatste meting)"), use_container_width=True)

    st.markdown("#### Trend t.o.v. vorige meting")
    trend = (
        timeline.sort_values(["species", "report_date_dt", "report_date"])
        .groupby("species", as_index=False)
        .tail(2)
        .copy()
    )
    if not trend.empty:
        trend["rank"] = trend.groupby("species").cumcount()
        pivot = trend.pivot_table(index="species", columns="rank", values=metric, aggfunc="first")
        prev_vals = pivot[0] if 0 in pivot.columns else pd.Series(0, index=pivot.index)
        latest_vals = pivot[1] if 1 in pivot.columns else prev_vals
        pivot["diff_vs_prev"] = latest_vals.fillna(0) - prev_vals.fillna(0)
        pivot = pivot.reset_index()[["species", "diff_vs_prev"]]
        st.plotly_chart(px.bar(pivot, x="species", y="diff_vs_prev", title="Verschil t.o.v. vorige meting"), use_container_width=True)

    st.markdown("#### Data")
    st.dataframe(timeline)
    csv_bytes = timeline.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", data=csv_bytes, file_name="analyse_data.csv", mime="text/csv")

    st.markdown("---")
    st.markdown("### Percelen ranking per jaar")
    st.caption("Kies een jaar en soort om percelen van hoog naar laag te vergelijken.")

    ranking_source = merged[merged["analysis_type"] == analysis_type].copy()
    ranking_source["report_date_dt"] = pd.to_datetime(ranking_source["report_date"], errors="coerce")
    ranking_source["year"] = ranking_source["report_date_dt"].dt.year
    ranking_source = ranking_source.dropna(subset=["field_display", "species", "year"])

    if ranking_source.empty:
        st.info("Geen data beschikbaar voor ranking per jaar.")
        return

    years = sorted(ranking_source["year"].astype(int).unique())
    selected_year = st.selectbox("Jaar", options=years, index=len(years) - 1)

    species_by_year = sorted(
        ranking_source[ranking_source["year"] == selected_year]["species"].dropna().unique()
    )
    if not species_by_year:
        st.info("Geen soorten beschikbaar voor het gekozen jaar.")
        return

    selected_species = st.selectbox("Soort", options=species_by_year, key="ranking_species")
    ranking_metric = metric
    if analysis_type == "cysteaaltjes":
        ranking_metric = st.radio(
            "Ranking op",
            options=["cysts", "lle"],
            horizontal=True,
            key="ranking_cyst_metric",
        )

    ranking_df = (
        ranking_source[
            (ranking_source["year"] == selected_year)
            & (ranking_source["species"] == selected_species)
        ]
        .groupby("field_display", as_index=False)[ranking_metric]
        .sum()
        .sort_values(ranking_metric, ascending=False)
    )

    if ranking_df.empty:
        st.info("Geen percelen gevonden voor deze combinatie van jaar en soort.")
        return

    ranking_df["rang"] = range(1, len(ranking_df) + 1)
    ranking_fig = px.bar(
        ranking_df,
        x="field_display",
        y=ranking_metric,
        text="rang",
        title=f"Percelen ranking ({selected_year}) - {selected_species}",
        labels={"field_display": "Perceel", ranking_metric: "Uitslag"},
    )
    ranking_fig.update_layout(xaxis_tickangle=-45)
    st.plotly_chart(ranking_fig, use_container_width=True)
    st.dataframe(ranking_df[["rang", "field_display", ranking_metric]], use_container_width=True)


def tab_export(conn: sqlite3.Connection) -> None:
    st.subheader("Export")

    if st.button("Exporteer naar Excel", type="primary"):
        samples_df = read_df(conn, "SELECT * FROM samples")
        results_df = read_df(conn, "SELECT * FROM results_long")
        mapping_df = read_df(conn, "SELECT * FROM field_mapping")

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            samples_df.to_excel(writer, sheet_name="samples", index=False)
            results_df.to_excel(writer, sheet_name="results_long", index=False)
            mapping_df.to_excel(writer, sheet_name="field_mapping", index=False)
        st.download_button(
            "Download Excel",
            data=buffer.getvalue(),
            file_name="nematodes_export.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def main() -> None:
    st.set_page_config(page_title="Nematodes Dashboard", layout="wide")
    st.title("Nematoden / Aaltjes Dashboard")

    db_path = st.text_input("SQLite database pad", DB_DEFAULT)
    conn = get_conn(db_path)

    tabs = st.tabs(["Import", "Percelen matchen", "Analyse & Grafieken", "Export"])
    with tabs[0]:
        tab_import(conn, db_path)
    with tabs[1]:
        tab_mapping(conn)
    with tabs[2]:
        tab_analysis(conn)
    with tabs[3]:
        tab_export(conn)


if __name__ == "__main__":
    main()
