"""
app.py - Dashboard Streamlit — ILERNA Smart-Industry (Hito 4)

Pestañas:
  📡 Tiempo Real     — Temperatura actual de cada máquina (InfluxDB)
  📈 Historial       — Serie temporal de temperatura normalizada
  ⚠️  Alertas        — Eventos con avg > 80°C
  🗄️  Lambda Query   — Query federada: InfluxDB (hot) + MinIO Parquet (cold) via DuckDB
  🤖 IA Anomalías    — Predicción con IsolationForest desde FastAPI

Arquitectura Lambda:
  Hot path  → Flink → InfluxDB        (segundos de latencia, últimas horas)
  Cold path → Flink → MinIO/Parquet   (minutos de latencia, histórico mensual)
  Query layer → DuckDB UNION ALL      (visión unificada en tiempo real)

Uso:
  streamlit run src/05_ui/app.py --server.port 8501
"""

import os
import time

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from influxdb_client import InfluxDBClient

# ── Configuración ─────────────────────────────────────────────
INFLUX_URL    = os.getenv("INFLUX_URL",    "http://localhost:18086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "supersecrettoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "ilerna")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "sensores")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:19000")
MINIO_ACCESS   = os.getenv("MINIO_ACCESS",   "admin")
MINIO_SECRET   = os.getenv("MINIO_SECRET",   "adminpassword")
MINIO_BUCKET   = os.getenv("MINIO_BUCKET",   "datalake")

ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "80.0"))
REFRESH_SEC     = int(os.getenv("REFRESH_SECONDS",   "5"))
FASTAPI_URL     = os.getenv("FASTAPI_URL", "http://localhost:18000")

# ── Página ────────────────────────────────────────────────────
st.set_page_config(
    page_title="ILERNA Smart-Industry",
    page_icon="🏭",
    layout="wide",
)
st.title("🏭 ILERNA Smart-Industry — Monitor de Temperatura")

# ── Sidebar ───────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Opciones")
    range_min    = st.slider("Ventana tiempo real (min)", 1, 120, 15)
    auto_refresh = st.toggle("Auto-refresco", value=True)
    if st.button("🔄 Refrescar"):
        st.rerun()
    st.divider()
    st.caption(f"Umbral alerta: **{ALERT_THRESHOLD}°C**")
    st.caption(f"InfluxDB: `{INFLUX_URL}`")


# ── Helpers InfluxDB ──────────────────────────────────────────

@st.cache_resource
def get_influx():
    return InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)


def query_machine_status() -> pd.DataFrame:
    """Última lectura por máquina desde machine_stats (escritas por Flink)."""
    api = get_influx().query_api()
    query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{range_min}m)
      |> filter(fn: (r) => r._measurement == "machine_stats")
      |> filter(fn: (r) => r._field == "avg_temp_c" or r._field == "alert")
      |> group(columns: ["device_id", "_field"])
      |> last()
      |> pivot(rowKey: ["_time", "device_id"], columnKey: ["_field"], valueColumn: "_value")
    """
    try:
        df = api.query_data_frame(query, org=INFLUX_ORG)
        if df.empty:
            return pd.DataFrame()
        return df[["device_id", "avg_temp_c", "alert"]].dropna(subset=["avg_temp_c"])
    except Exception as e:
        st.warning(f"InfluxDB: {e}")
        return pd.DataFrame()


def query_history(range_minutes: int) -> pd.DataFrame:
    """Serie temporal de temperatura por máquina."""
    api = get_influx().query_api()
    query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{range_minutes}m)
      |> filter(fn: (r) => r._measurement == "machine_stats")
      |> filter(fn: (r) => r._field == "avg_temp_c")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    """
    try:
        df = api.query_data_frame(query, org=INFLUX_ORG)
        if df.empty:
            return pd.DataFrame()
        df = df[["_time", "device_id", "avg_temp_c"]].rename(columns={"_time": "ts"})
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df.sort_values("ts")
    except Exception as e:
        st.warning(f"InfluxDB historial: {e}")
        return pd.DataFrame()


def query_alerts(range_minutes: int) -> pd.DataFrame:
    """Registros con alerta activa."""
    api = get_influx().query_api()
    query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{range_minutes}m)
      |> filter(fn: (r) => r._measurement == "machine_stats")
      |> filter(fn: (r) => r._field == "avg_temp_c")
      |> filter(fn: (r) => r._value > {ALERT_THRESHOLD})
      |> sort(columns: ["_time"], desc: true)
    """
    try:
        df = api.query_data_frame(query, org=INFLUX_ORG)
        if df.empty:
            return pd.DataFrame()
        df = df[["_time", "device_id", "_value"]].rename(
            columns={"_time": "ts", "_value": "avg_temp_c"}
        )
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df
    except Exception:
        return pd.DataFrame()


# ── DuckDB helpers ────────────────────────────────────────────

def _init_duckdb_s3(conn: duckdb.DuckDBPyConnection):
    """Configura DuckDB con credenciales MinIO (S3-compatible)."""
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    conn.execute(f"""
        SET s3_endpoint          = '{MINIO_ENDPOINT}';
        SET s3_access_key_id     = '{MINIO_ACCESS}';
        SET s3_secret_access_key = '{MINIO_SECRET}';
        SET s3_use_ssl           = false;
        SET s3_url_style         = 'path';
    """)


@st.cache_data(ttl=120, show_spinner="Cargando datos históricos desde MinIO...")
def query_cold_path() -> pd.DataFrame:
    """Lee Parquet desde MinIO/cold path (flink_to_minio_job o kafka_to_minio)."""
    conn = duckdb.connect()
    try:
        _init_duckdb_s3(conn)
        # Intenta primero la ruta particionada de Flink (hive_partitioning)
        try:
            df = conn.execute(f"""
                SELECT device_id,
                       CAST(temperature_c AS DOUBLE) AS value,
                       ts,
                       year, month, day, hour
                FROM read_parquet(
                    's3://{MINIO_BUCKET}/clean/**/*.parquet',
                    hive_partitioning = true
                )
                WHERE temperature_c IS NOT NULL
            """).df()
            if not df.empty:
                df["source"] = "flink-parquet"
                return df
        except Exception:
            pass
        # Fallback: ruta del writer Python (kafka_to_minio.py)
        df = conn.execute(f"""
            SELECT device_id,
                   CAST(value AS DOUBLE) AS value,
                   ts,
                   strftime(CAST(ts AS TIMESTAMP), '%Y') AS year,
                   strftime(CAST(ts AS TIMESTAMP), '%m') AS month,
                   strftime(CAST(ts AS TIMESTAMP), '%d') AS day,
                   strftime(CAST(ts AS TIMESTAMP), '%H') AS hour
            FROM read_parquet('s3://{MINIO_BUCKET}/raw/**/*.parquet', hive_partitioning=false)
            WHERE value IS NOT NULL
        """).df()
        df["source"] = "python-parquet"
        return df
    except Exception as e:
        st.error(f"Error leyendo MinIO: {e}")
        return pd.DataFrame()
    finally:
        conn.close()


def query_lambda_federated(hot_df: pd.DataFrame, cold_df: pd.DataFrame) -> pd.DataFrame:
    """
    Query federada Lambda Architecture con DuckDB en memoria.

    Combina:
      · hot_df  → datos recientes de InfluxDB (hot path, baja latencia)
      · cold_df → datos históricos de MinIO Parquet (cold path, alta escala)

    DuckDB hace un UNION ALL en memoria sin necesidad de mover datos a disco.
    """
    if hot_df.empty and cold_df.empty:
        return pd.DataFrame()

    conn = duckdb.connect()
    try:
        # Registrar DataFrames como tablas virtuales en DuckDB
        if not hot_df.empty:
            conn.register("hot_data",  hot_df)
        if not cold_df.empty:
            conn.register("cold_data", cold_df)

        if not hot_df.empty and not cold_df.empty:
            query = """
            SELECT device_id, value, ts, 'InfluxDB (hot)'  AS source FROM hot_data
            UNION ALL
            SELECT device_id, value, ts, 'MinIO (cold)' AS source FROM cold_data
            ORDER BY ts DESC
            LIMIT 5000
            """
        elif not hot_df.empty:
            query = "SELECT device_id, value, ts, 'InfluxDB (hot)' AS source FROM hot_data ORDER BY ts DESC"
        else:
            query = "SELECT device_id, value, ts, 'MinIO (cold)' AS source FROM cold_data ORDER BY ts DESC LIMIT 5000"

        return conn.execute(query).df()
    finally:
        conn.close()


# ── Pestañas ──────────────────────────────────────────────────
tab_rt, tab_hist, tab_alerts, tab_lambda, tab_ai = st.tabs([
    "📡 Tiempo Real",
    "📈 Historial",
    "⚠️  Alertas",
    "🗄️  Lambda Query",
    "🤖 IA Anomalías",
])

# ── TAB 1: Tiempo Real ────────────────────────────────────────
with tab_rt:
    st.subheader(f"Estado actual de máquinas (últimos {range_min} min)")
    status_df = query_machine_status()

    if status_df.empty:
        st.info(
            "Sin datos. ¿Está el pipeline corriendo?\n\n"
            "```bash\n"
            "python src/01_ingestion/sensor_simulator.py\n"
            "python src/01_ingestion/mqtt_to_redpanda_bridge.py\n"
            "# (Flink jobs en el contenedor jobmanager)\n"
            "```"
        )
    else:
        cols = st.columns(len(status_df))
        for i, row in status_df.iterrows():
            temp = row.get("avg_temp_c", 0)
            is_alert = bool(row.get("alert", 0)) or temp > ALERT_THRESHOLD
            icon = "🔴" if is_alert else "🟢"
            delta_color = "inverse" if is_alert else "normal"
            with cols[i % len(cols)]:
                st.metric(
                    label=f"{icon} {row['device_id']}",
                    value=f"{temp:.1f} °C",
                    delta="⚠ ALERTA" if is_alert else "Normal",
                    delta_color=delta_color,
                )

        # Gauge visual
        fig = go.Figure()
        for _, row in status_df.iterrows():
            temp = row.get("avg_temp_c", 0)
            fig.add_trace(go.Indicator(
                mode="gauge+number",
                value=temp,
                title={"text": row["device_id"]},
                gauge={
                    "axis": {"range": [0, 120]},
                    "bar":  {"color": "red" if temp > ALERT_THRESHOLD else "green"},
                    "steps": [
                        {"range": [0,    ALERT_THRESHOLD], "color": "lightgray"},
                        {"range": [ALERT_THRESHOLD, 120],  "color": "lightyellow"},
                    ],
                    "threshold": {
                        "line": {"color": "red", "width": 4},
                        "thickness": 0.75,
                        "value": ALERT_THRESHOLD,
                    },
                },
                domain={"column": list(status_df.index).index(_)},
            ))
        fig.update_layout(
            grid={"rows": 1, "columns": len(status_df)},
            height=280,
            template="plotly_dark",
        )
        st.plotly_chart(fig, use_container_width=True)

# ── TAB 2: Historial ─────────────────────────────────────────
with tab_hist:
    hist_range = st.slider("Ventana historial (min)", 5, 480, 60, key="hist_range")
    hist_df = query_history(hist_range)

    if hist_df.empty:
        st.info("Sin datos en el rango seleccionado.")
    else:
        fig = px.line(
            hist_df,
            x="ts",
            y="avg_temp_c",
            color="device_id",
            title=f"Temperatura media por minuto — últimos {hist_range} min",
            labels={"ts": "Tiempo", "avg_temp_c": "Temperatura media (°C)", "device_id": "Máquina"},
            template="plotly_dark",
        )
        fig.add_hline(
            y=ALERT_THRESHOLD,
            line_dash="dash",
            line_color="red",
            annotation_text=f"Umbral {ALERT_THRESHOLD}°C",
        )
        fig.update_layout(height=420)
        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            hist_df.tail(50).sort_values("ts", ascending=False),
            use_container_width=True,
            hide_index=True,
        )

# ── TAB 3: Alertas ────────────────────────────────────────────
with tab_alerts:
    alert_range = st.slider("Buscar alertas en los últimos (min)", 10, 1440, 120, key="alert_range")
    alerts_df = query_alerts(alert_range)

    if alerts_df.empty:
        st.success(f"✅ Sin alertas en los últimos {alert_range} minutos.")
    else:
        st.error(f"⚠️  {len(alerts_df)} evento(s) sobre {ALERT_THRESHOLD}°C")
        fig = px.scatter(
            alerts_df,
            x="ts",
            y="avg_temp_c",
            color="device_id",
            size="avg_temp_c",
            title="Eventos de alerta por máquina",
            template="plotly_dark",
        )
        fig.add_hline(y=ALERT_THRESHOLD, line_dash="dash", line_color="orange")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(alerts_df, use_container_width=True, hide_index=True)

# ── TAB 4: Lambda Architecture Query Federada ─────────────────
with tab_lambda:
    st.subheader("🗄️ Arquitectura Lambda — Query Federada con DuckDB")
    st.markdown("""
    **Concepto:** Los datos fluyen por dos caminos paralelos:

    | Path | Tecnología | Latencia | Uso |
    |------|-----------|----------|-----|
    | **Hot** (tiempo real) | Flink → InfluxDB | segundos | Alertas, monitorización |
    | **Cold** (histórico) | Flink → MinIO/Parquet | minutos | Análisis mensual, ML |

    **DuckDB** actúa como capa de consulta unificada: hace `UNION ALL` de ambas fuentes
    en memoria sin mover datos, gracias a la extensión `httpfs` para S3.
    """)

    col1, col2 = st.columns(2)
    with col1:
        hist_range_l = st.slider("Datos InfluxDB (min)", 5, 480, 60, key="lambda_range")
    with col2:
        run_lambda = st.button("🔗 Ejecutar Query Federada")

    if run_lambda:
        with st.spinner("Consultando InfluxDB (hot path)..."):
            hot_df = query_history(hist_range_l)
            if not hot_df.empty:
                hot_df = hot_df.rename(columns={"avg_temp_c": "value"})
                hot_df["ts"] = hot_df["ts"].astype(str)

        with st.spinner("Consultando MinIO Parquet (cold path)..."):
            cold_df = query_cold_path()
            if not cold_df.empty and "ts" in cold_df.columns:
                cold_df["ts"] = cold_df["ts"].astype(str)

        hot_count  = len(hot_df)  if not hot_df.empty  else 0
        cold_count = len(cold_df) if not cold_df.empty else 0

        col_h, col_c = st.columns(2)
        col_h.metric("🔴 Hot path (InfluxDB)", f"{hot_count:,} registros")
        col_c.metric("🔵 Cold path (MinIO)", f"{cold_count:,} registros")

        if hot_count + cold_count == 0:
            st.warning("Sin datos en ninguna fuente. ¿Está el pipeline corriendo?")
        else:
            with st.spinner("DuckDB: unificando fuentes (UNION ALL)..."):
                unified = query_lambda_federated(hot_df, cold_df)

            st.success(f"✅ Query federada completada: {len(unified):,} registros totales")

            # ── Gráfica unificada ─────────────────────────────
            unified["ts_dt"] = pd.to_datetime(unified["ts"], errors="coerce", utc=True)
            fig = px.scatter(
                unified.dropna(subset=["ts_dt"]),
                x="ts_dt",
                y="value",
                color="source",
                symbol="device_id",
                title="Vista unificada Lambda: Hot (InfluxDB) + Cold (MinIO Parquet)",
                labels={"ts_dt": "Tiempo", "value": "Temperatura media (°C)", "source": "Fuente"},
                template="plotly_dark",
                color_discrete_map={
                    "InfluxDB (hot)":  "#EF553B",
                    "MinIO (cold)":    "#636EFA",
                },
                opacity=0.7,
            )
            fig.add_hline(y=ALERT_THRESHOLD, line_dash="dash", line_color="orange",
                          annotation_text=f"Umbral {ALERT_THRESHOLD}°C")
            fig.update_layout(height=400)
            st.plotly_chart(fig, use_container_width=True)

            # ── Estadísticas DuckDB en memoria ────────────────
            st.subheader("Estadísticas por fuente y máquina (DuckDB in-memory)")
            conn = duckdb.connect()
            conn.register("unified", unified)
            stats_df = conn.execute("""
                SELECT
                    source,
                    device_id,
                    COUNT(*)                    AS registros,
                    ROUND(AVG(value), 2)        AS media_c,
                    ROUND(MIN(value), 2)        AS min_c,
                    ROUND(MAX(value), 2)        AS max_c,
                    ROUND(STDDEV(value), 2)     AS desv_std,
                    SUM(CASE WHEN value > 80.0 THEN 1 ELSE 0 END) AS sobre_umbral
                FROM unified
                WHERE value IS NOT NULL
                GROUP BY source, device_id
                ORDER BY source, device_id
            """).df()
            conn.close()
            st.dataframe(stats_df, use_container_width=True, hide_index=True)


# ── TAB 5: IA — Detección de Anomalías ───────────────────────
with tab_ai:
    st.subheader("🤖 IA Cloud — Detección de Anomalías con IsolationForest")
    st.markdown("""
    **Edge vs Cloud Intelligence:**
    - **Flink** (Edge): regla fija `avg_temp > 80°C` — determinista, sin modelo.
    - **FastAPI + IsolationForest** (Cloud): aprende el patrón normal de cada máquina
      y detecta anomalías estadísticas, aunque no superen el umbral fijo.

    *IsolationForest* (scikit-learn): algoritmo no supervisado que "aísla" puntos
    atípicos usando árboles de decisión aleatorios. No necesita datos etiquetados.
    """)

    # ── Estado del modelo ─────────────────────────────────────
    col_status, col_train = st.columns(2)

    with col_status:
        st.markdown("**Estado del modelo**")
        try:
            resp = requests.get(f"{FASTAPI_URL}/model/status", timeout=3)
            if resp.ok:
                status = resp.json()
                if status.get("trained"):
                    st.success(f"✅ Modelo entrenado con {status['samples']:,} muestras")
                    if "stats" in status:
                        s = status["stats"]
                        st.json({
                            "media": f"{s.get('mean', 0):.1f}°C",
                            "std":   f"{s.get('std', 0):.1f}°C",
                            "rango": f"[{s.get('p5', 0):.1f}, {s.get('p95', 0):.1f}]°C (p5-p95)",
                        })
                else:
                    st.warning("⚠️ Modelo no entrenado")
        except Exception:
            st.error("FastAPI no disponible")

    with col_train:
        st.markdown("**Entrenar modelo**")
        train_range = st.slider("Minutos de histórico para entrenamiento", 10, 480, 120)
        contamination = st.slider("Tasa de anomalías esperada (%)", 1, 30, 10) / 100
        if st.button("🧠 Entrenar IsolationForest"):
            try:
                resp = requests.post(
                    f"{FASTAPI_URL}/model/train",
                    params={"range_minutes": train_range, "contamination": contamination},
                    timeout=30,
                )
                if resp.ok:
                    result = resp.json()
                    st.success(f"✅ Entrenado con {result['samples']:,} muestras")
                    st.json(result.get("stats", {}))
                else:
                    st.error(f"Error: {resp.json().get('detail', resp.text)}")
            except Exception as e:
                st.error(f"Error conectando a FastAPI: {e}")

    st.divider()

    # ── Predicción individual ─────────────────────────────────
    st.markdown("**Predicción manual**")
    col_m, col_t, col_btn = st.columns([2, 2, 1])
    with col_m:
        pred_device = st.selectbox("Máquina", list({
            "machine-001", "machine-002", "machine-003", "machine-004", "machine-005"
        }))
    with col_t:
        pred_temp = st.number_input("Temperatura (°C)", value=75.0, min_value=-50.0, max_value=200.0)
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        predict_btn = st.button("🔍 Predecir")

    if predict_btn:
        try:
            resp = requests.get(
                f"{FASTAPI_URL}/machines/{pred_device}/predict",
                params={"temperature_c": pred_temp},
                timeout=5,
            )
            if resp.ok:
                r = resp.json()
                if r.get("is_anomaly"):
                    st.error(f"⚠️ **ANOMALÍA DETECTADA** — Probabilidad de fallo: {r['failure_prob']:.1%}")
                else:
                    st.success(f"✅ Comportamiento normal — Score: {r['anomaly_score']:.3f}")
                st.info(r.get("interpretation", ""))
                st.json({k: v for k, v in r.items() if k != "model_stats"})
            else:
                st.error(f"Error API: {resp.json().get('detail', resp.text)}")
        except Exception as e:
            st.error(f"Error: {e}")

    st.divider()

    # ── Análisis de rango completo ────────────────────────────
    st.markdown("**Análisis de anomalías en el histórico reciente**")
    if st.button("🔬 Analizar todas las máquinas (últimos 30 min)"):
        hist_df = query_history(30)
        if hist_df.empty:
            st.warning("Sin datos en InfluxDB.")
        else:
            results = []
            for _, row in hist_df.iterrows():
                try:
                    resp = requests.get(
                        f"{FASTAPI_URL}/machines/{row['device_id']}/predict",
                        params={"temperature_c": row["avg_temp_c"]},
                        timeout=3,
                    )
                    if resp.ok:
                        r = resp.json()
                        results.append({
                            "ts":           row["ts"],
                            "device_id":    row["device_id"],
                            "avg_temp_c":   row["avg_temp_c"],
                            "is_anomaly":   r["is_anomaly"],
                            "failure_prob": r["failure_prob"],
                            "score":        r["anomaly_score"],
                        })
                except Exception:
                    pass

            if results:
                res_df = pd.DataFrame(results)
                anomalies = res_df[res_df["is_anomaly"]]
                st.metric("Anomalías detectadas", f"{len(anomalies)} / {len(res_df)}")

                fig = px.scatter(
                    res_df,
                    x="ts",
                    y="avg_temp_c",
                    color="failure_prob",
                    symbol="device_id",
                    size="failure_prob",
                    color_continuous_scale="RdYlGn_r",
                    title="Probabilidad de fallo por lectura (IsolationForest)",
                    template="plotly_dark",
                )
                fig.add_hline(y=ALERT_THRESHOLD, line_dash="dash", line_color="orange")
                st.plotly_chart(fig, use_container_width=True)

# ── Auto-refresco ─────────────────────────────────────────────
if auto_refresh:
    time.sleep(REFRESH_SEC)
    st.rerun()
