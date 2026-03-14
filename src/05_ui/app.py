"""
app.py - Dashboard Streamlit — ILERNA Smart-Industry (Hito 4)

Pestañas:
  📡 Tiempo Real       — Temperatura actual de cada máquina (InfluxDB)
  📈 Historial         — Serie temporal de temperatura normalizada
  ⚠️  Alertas          — Eventos con avg > 80°C
  🗄️  Lambda Query     — Query federada: InfluxDB (hot) + MinIO Parquet (cold) via DuckDB
  🤖 IA Anomalías      — Predicción con IsolationForest desde FastAPI
  🏥 Pipeline Health   — Estado de servicios, jobs Flink y topics Kafka
  🔐 Hash Chain        — Integridad SHA256 por dispositivo (sensors_verified / DLQ)
  🔬 Modelos Avanzados — Prophet forecasting, RandomForest, CUSUM, K-Means

Arquitectura Lambda:
  Hot path  → Flink → InfluxDB        (segundos de latencia, últimas horas)
  Cold path → Flink → MinIO/Parquet   (minutos de latencia, histórico mensual)
  Query layer → DuckDB UNION ALL      (visión unificada en tiempo real)

Uso:
  streamlit run src/05_ui/app.py --server.port 8501
"""

import json
import os
import time
import warnings

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from confluent_kafka import Consumer, KafkaException, TopicPartition
from influxdb_client import InfluxDBClient
from influxdb_client.client.warnings import MissingPivotFunction

warnings.simplefilter("ignore", MissingPivotFunction)

# ── Configuración ─────────────────────────────────────────────
INFLUX_URL    = os.getenv("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "supersecrettoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "ilerna")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "sensores")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS   = os.getenv("MINIO_ACCESS",   "admin")
MINIO_SECRET   = os.getenv("MINIO_SECRET",   "Ilerna_Programaci0n")
MINIO_BUCKET   = os.getenv("MINIO_BUCKET",   "datalake")

ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "80.0"))
REFRESH_SEC     = int(os.getenv("REFRESH_SECONDS",   "5"))
FASTAPI_URL     = os.getenv("FASTAPI_URL", "http://localhost:8000")
FLINK_URL       = os.getenv("FLINK_URL",   "http://jobmanager:8081")
KAFKA_BROKER    = os.getenv("KAFKA_BROKER", "redpanda:29092")

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
    st.divider()
    st.markdown("**Estado servicios**")
    _svc_checks = {
        "FastAPI":  FASTAPI_URL + "/health",
        "Flink":    FLINK_URL   + "/overview",
        "Kafka":    None,  # checked via confluent_kafka
        "InfluxDB": INFLUX_URL  + "/health",
    }
    for _svc, _url in _svc_checks.items():
        try:
            if _url:
                _ok = requests.get(_url, timeout=2).ok
            else:
                from confluent_kafka.admin import AdminClient as _AK
                _AK({"bootstrap.servers": KAFKA_BROKER, "socket.timeout.ms": 2000}).list_topics(timeout=2)
                _ok = True
        except Exception:
            _ok = False
        st.caption(("🟢" if _ok else "🔴") + f" {_svc}")


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
        df = df[["_time", "device_id", "avg_temp_c", "alert"]].dropna(subset=["avg_temp_c"])
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
        return df
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
    except Exception:
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


# ── Helpers Pipeline Health ───────────────────────────────────

def get_flink_jobs() -> list:
    """Consulta la API REST de Flink y devuelve lista de jobs con estado."""
    try:
        jobs_resp = requests.get(f"{FLINK_URL}/jobs", timeout=3).json().get("jobs", [])
        result = []
        for j in jobs_resp:
            try:
                detail = requests.get(f"{FLINK_URL}/jobs/{j['id']}", timeout=3).json()
                result.append({
                    "id":       j["id"][:8] + "…",
                    "nombre":   detail.get("name", "—"),
                    "estado":   detail.get("state", j.get("status", "?")),
                    "inicio":   pd.to_datetime(detail.get("start-time", 0), unit="ms", utc=True).strftime("%H:%M:%S") if detail.get("start-time") else "—",
                    "duracion": f"{int(detail.get('duration', 0) / 1000 / 60)} min" if detail.get("duration") else "—",
                })
            except Exception:
                result.append({"id": j["id"][:8] + "…", "nombre": "—", "estado": j.get("status", "?"), "inicio": "—", "duracion": "—"})
        return result
    except Exception:
        return []


def get_kafka_offsets(topics: list) -> dict:
    """Obtiene el offset máximo (número de mensajes) de cada topic via Kafka Admin."""
    result = {}
    try:
        from confluent_kafka.admin import AdminClient
        admin = AdminClient({"bootstrap.servers": KAFKA_BROKER, "socket.timeout.ms": 3000})
        meta = admin.list_topics(timeout=3)
        for topic in topics:
            if topic in meta.topics:
                partitions = meta.topics[topic].partitions
                result[topic] = {"particiones": len(partitions), "estado": "OK"}
            else:
                result[topic] = {"particiones": 0, "estado": "no existe"}
    except Exception:
        for topic in topics:
            result[topic] = {"particiones": "?", "estado": "sin conexión"}
    return result


def get_dlq_messages(max_msgs: int = 20) -> list:
    """Lee los últimos N mensajes del topic sensors_invalid (DLQ)."""
    msgs = []
    try:
        c = Consumer({
            "bootstrap.servers":  KAFKA_BROKER,
            "group.id":           "streamlit-dlq-viewer",
            "auto.offset.reset":  "earliest",
            "enable.auto.commit": False,
            "socket.timeout.ms":  3000,
            "session.timeout.ms": 6000,
        })
        c.assign([TopicPartition("sensors_invalid", 0, 0)])
        deadline = time.time() + 3
        while len(msgs) < max_msgs and time.time() < deadline:
            msg = c.poll(0.5)
            if msg is None:
                break
            if msg.error():
                break
            try:
                msgs.append(json.loads(msg.value().decode("utf-8")))
            except Exception:
                pass
        c.close()
    except Exception:
        pass
    return msgs


def get_verified_count() -> int:
    """Estima mensajes en sensors_verified leyendo el offset máximo."""
    try:
        from confluent_kafka.admin import AdminClient, TopicPartition as TP
        admin = AdminClient({"bootstrap.servers": KAFKA_BROKER, "socket.timeout.ms": 3000})
        meta = admin.list_topics(timeout=3)
        if "sensors_verified" not in meta.topics:
            return 0
        c = Consumer({
            "bootstrap.servers":  KAFKA_BROKER,
            "group.id":           "streamlit-offset-checker",
            "auto.offset.reset":  "latest",
            "enable.auto.commit": False,
            "socket.timeout.ms":  3000,
        })
        partitions = [TopicPartition("sensors_verified", p) for p in meta.topics["sensors_verified"].partitions]
        _, high = c.get_watermark_offsets(partitions[0], timeout=3)
        c.close()
        return high
    except Exception:
        return -1


# ── Pestañas ──────────────────────────────────────────────────
tab_rt, tab_hist, tab_alerts, tab_lambda, tab_ai, tab_health, tab_hash, tab_advanced = st.tabs([
    "📡 Tiempo Real",
    "📈 Historial",
    "⚠️  Alertas",
    "🗄️  Lambda Query",
    "🤖 IA Anomalías",
    "🏥 Pipeline Health",
    "🔐 Hash Chain",
    "🔬 Modelos Avanzados",
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
        now_utc = pd.Timestamp.now(tz="UTC")
        cols = st.columns(len(status_df))
        for i, row in status_df.reset_index(drop=True).iterrows():
            temp = row.get("avg_temp_c", 0)
            is_alert = bool(row.get("alert", 0)) or temp > ALERT_THRESHOLD
            icon = "🔴" if is_alert else "🟢"
            delta_color = "inverse" if is_alert else "normal"
            secs_ago = int((now_utc - row["_time"]).total_seconds()) if "_time" in row and pd.notna(row["_time"]) else None
            label_time = f" — hace {secs_ago}s" if secs_ago is not None else ""
            with cols[i % len(cols)]:
                st.metric(
                    label=f"{icon} {row['device_id']}{label_time}",
                    value=f"{temp:.1f} °C",
                    delta="⚠ ALERTA" if is_alert else "Normal",
                    delta_color=delta_color,
                )

        # Gauges
        fig = go.Figure()
        rows_list = list(status_df.reset_index(drop=True).iterrows())
        for idx, (_, row) in enumerate(rows_list):
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
                domain={"column": idx},
            ))
        fig.update_layout(
            grid={"rows": 1, "columns": len(rows_list)},
            height=280,
            template="plotly_dark",
        )
        st.plotly_chart(fig, use_container_width=True)

        # Sparklines minitrend por máquina
        st.markdown("#### Tendencia reciente por máquina")
        spark_df = query_history(range_min)
        if not spark_df.empty:
            spark_cols = st.columns(len(status_df))
            for i, row in status_df.reset_index(drop=True).iterrows():
                dev = row["device_id"]
                d = spark_df[spark_df["device_id"] == dev].sort_values("ts").tail(20)
                with spark_cols[i % len(spark_cols)]:
                    if not d.empty:
                        fig_sp = go.Figure(go.Scatter(
                            x=d["ts"], y=d["avg_temp_c"],
                            mode="lines", fill="tozeroy",
                            line=dict(color="red" if row.get("avg_temp_c", 0) > ALERT_THRESHOLD else "steelblue", width=1.5),
                            fillcolor="rgba(255,80,80,0.15)" if row.get("avg_temp_c", 0) > ALERT_THRESHOLD else "rgba(70,130,180,0.15)",
                        ))
                        fig_sp.update_layout(
                            height=80, margin=dict(l=0, r=0, t=0, b=0),
                            showlegend=False, template="plotly_dark",
                            xaxis=dict(visible=False), yaxis=dict(visible=False),
                        )
                        st.plotly_chart(fig_sp, use_container_width=True)

        # Heatmap temperatura por máquina en el tiempo
        st.markdown("#### Mapa de calor — temperatura por máquina")
        heat_df = spark_df if not spark_df.empty else query_history(range_min)
        if not heat_df.empty and heat_df is not None:
            pivot = heat_df.pivot_table(
                index=heat_df["ts"].dt.floor("1min"),
                columns="device_id",
                values="avg_temp_c",
                aggfunc="mean",
            )
            fig_heat = px.imshow(
                pivot.T,
                color_continuous_scale="RdYlGn_r",
                zmin=0, zmax=120,
                labels={"x": "Tiempo", "y": "Máquina", "color": "°C"},
                title=f"Temperatura (°C) — últimos {range_min} min",
                aspect="auto",
            )
            fig_heat.update_layout(height=220, template="plotly_dark")
            st.plotly_chart(fig_heat, use_container_width=True)

# ── TAB 2: Historial ─────────────────────────────────────────
with tab_hist:
    h_col1, h_col2, h_col3 = st.columns([3, 2, 2])
    with h_col1:
        hist_range = st.slider("Ventana historial (min)", 5, 480, 60, key="hist_range")
    with h_col2:
        show_band = st.toggle("Banda ±1σ", value=True, key="hist_band")
    with h_col3:
        show_rolling = st.toggle("Media móvil (5 min)", value=False, key="hist_rolling")

    hist_df = query_history(hist_range)

    if hist_df.empty:
        st.info("Sin datos en el rango seleccionado.")
    else:
        all_devices = sorted(hist_df["device_id"].unique().tolist())

        # ── Selector máquina individual ────────────────────────
        sel_device = st.selectbox(
            "Máquina (todas o individual)",
            ["— Todas —"] + all_devices,
            key="hist_device",
        )
        plot_df = hist_df if sel_device == "— Todas —" else hist_df[hist_df["device_id"] == sel_device]

        fig = go.Figure()
        colors = px.colors.qualitative.Plotly
        for ci, dev in enumerate(plot_df["device_id"].unique()):
            d = plot_df[plot_df["device_id"] == dev].sort_values("ts")
            color = colors[ci % len(colors)]
            fig.add_trace(go.Scatter(
                x=d["ts"], y=d["avg_temp_c"],
                mode="lines", name=dev,
                line=dict(color=color, width=2),
            ))
            if show_band and len(d) > 2:
                sig = d["avg_temp_c"].std()
                fig.add_trace(go.Scatter(
                    x=pd.concat([d["ts"], d["ts"].iloc[::-1]]),
                    y=list(d["avg_temp_c"] + sig) + list((d["avg_temp_c"] - sig).iloc[::-1]),
                    fill="toself",
                    fillcolor=color.replace("rgb", "rgba").replace(")", ",0.12)") if color.startswith("rgb") else color,
                    line=dict(width=0),
                    showlegend=False,
                    name=f"{dev} ±1σ",
                    hoverinfo="skip",
                ))
            if show_rolling and len(d) >= 3:
                rolling = d["avg_temp_c"].rolling(window=5, min_periods=1).mean()
                fig.add_trace(go.Scatter(
                    x=d["ts"], y=rolling,
                    mode="lines", name=f"{dev} (MA5)",
                    line=dict(color=color, width=2, dash="dot"),
                    opacity=0.7,
                ))

        fig.add_hline(
            y=ALERT_THRESHOLD, line_dash="dash", line_color="red",
            annotation_text=f"Umbral {ALERT_THRESHOLD}°C",
        )
        fig.update_layout(
            title=f"Temperatura media por minuto — últimos {hist_range} min",
            xaxis_title="Tiempo", yaxis_title="Temperatura media (°C)",
            height=420, template="plotly_dark",
        )
        st.plotly_chart(fig, use_container_width=True)

        # ── Comparativa de dos máquinas ────────────────────────
        if len(all_devices) >= 2:
            st.markdown("#### Comparativa directa entre dos máquinas")
            cc1, cc2 = st.columns(2)
            with cc1:
                dev_a = st.selectbox("Máquina A", all_devices, index=0, key="cmp_a")
            with cc2:
                dev_b = st.selectbox("Máquina B", all_devices, index=min(1, len(all_devices)-1), key="cmp_b")

            if dev_a != dev_b:
                da = hist_df[hist_df["device_id"] == dev_a].sort_values("ts")
                db = hist_df[hist_df["device_id"] == dev_b].sort_values("ts")
                fig_cmp = go.Figure()
                fig_cmp.add_trace(go.Scatter(x=da["ts"], y=da["avg_temp_c"], name=dev_a, mode="lines"))
                fig_cmp.add_trace(go.Scatter(x=db["ts"], y=db["avg_temp_c"], name=dev_b, mode="lines"))
                fig_cmp.add_hline(y=ALERT_THRESHOLD, line_dash="dash", line_color="red")
                fig_cmp.update_layout(
                    title=f"Comparativa {dev_a} vs {dev_b}",
                    height=300, template="plotly_dark",
                    xaxis_title="Tiempo", yaxis_title="°C",
                )
                st.plotly_chart(fig_cmp, use_container_width=True)

        st.download_button(
            "⬇️ Descargar CSV (historial completo)",
            data=plot_df.to_csv(index=False),
            file_name=f"historial_{hist_range}min.csv",
            mime="text/csv",
        )
        st.dataframe(
            plot_df.tail(50).sort_values("ts", ascending=False),
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

        # ── Scatter original ──────────────────────────────────
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

        # ── Timeline estilo Gantt ─────────────────────────────
        st.markdown("#### Timeline de alertas por máquina")
        gantt_df = alerts_df.copy()
        gantt_df["ts"] = pd.to_datetime(gantt_df["ts"], utc=True)
        gantt_df["end"] = gantt_df["ts"] + pd.Timedelta(minutes=1)
        fig_gantt = px.timeline(
            gantt_df.rename(columns={"ts": "Start", "end": "Finish", "device_id": "Task"}),
            x_start="Start", x_end="Finish", y="Task",
            color="avg_temp_c",
            color_continuous_scale="Reds",
            title="Intervalos de alerta (cada barra = 1 ventana de 1 min)",
            labels={"avg_temp_c": "°C"},
        )
        fig_gantt.update_layout(height=250, template="plotly_dark")
        st.plotly_chart(fig_gantt, use_container_width=True)

        # ── Alertas por hora del día ──────────────────────────
        st.markdown("#### ¿A qué hora fallan más las máquinas?")
        hour_df = alerts_df.copy()
        hour_df["hora"] = pd.to_datetime(hour_df["ts"], utc=True).dt.hour
        hour_counts = hour_df.groupby(["hora", "device_id"]).size().reset_index(name="alertas")
        fig_hour = px.bar(
            hour_counts,
            x="hora", y="alertas",
            color="device_id",
            barmode="stack",
            title="Distribución de alertas por hora del día (UTC)",
            labels={"hora": "Hora (UTC)", "alertas": "Nº alertas"},
            template="plotly_dark",
        )
        fig_hour.update_xaxes(tickmode="linear", dtick=1)
        fig_hour.update_layout(height=300)
        st.plotly_chart(fig_hour, use_container_width=True)

        st.download_button(
            "⬇️ Descargar CSV (alertas)",
            data=alerts_df.to_csv(index=False),
            file_name=f"alertas_{alert_range}min.csv",
            mime="text/csv",
        )
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
        total      = hot_count + cold_count

        col_h, col_c, col_pct = st.columns(3)
        col_h.metric("🔴 Hot path (InfluxDB)", f"{hot_count:,} registros")
        col_c.metric("🔵 Cold path (MinIO)", f"{cold_count:,} registros")
        if total > 0:
            col_pct.metric(
                "Proporción hot/cold",
                f"{hot_count/total*100:.0f}% / {cold_count/total*100:.0f}%",
            )

        # Indicador visual de proporción
        if total > 0:
            fig_prop = go.Figure(go.Bar(
                x=[hot_count, cold_count],
                y=["Fuentes"],
                orientation="h",
                marker_color=["#EF553B", "#636EFA"],
                text=[f"InfluxDB {hot_count:,}", f"MinIO {cold_count:,}"],
                textposition="inside",
            ))
            fig_prop.update_layout(
                height=80, showlegend=False, template="plotly_dark",
                margin=dict(l=0, r=0, t=0, b=0),
                xaxis=dict(showticklabels=False),
            )
            st.plotly_chart(fig_prop, use_container_width=True)

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

            # ── Breakdown por partición (cold path) ───────────
            if not cold_df.empty and "hour" in cold_df.columns:
                st.subheader("Breakdown Cold Path — registros por partición")
                part_df = cold_df.groupby(
                    ["year", "month", "day", "hour"]
                ).size().reset_index(name="registros")
                part_df["particion"] = (
                    part_df["year"] + "-" + part_df["month"] + "-"
                    + part_df["day"] + " " + part_df["hour"] + "h"
                )
                fig_part = px.bar(
                    part_df.sort_values(["year", "month", "day", "hour"]),
                    x="particion",
                    y="registros",
                    title="Registros Parquet por partición hora (MinIO cold path)",
                    template="plotly_dark",
                    color="registros",
                    color_continuous_scale="Blues",
                )
                fig_part.update_layout(height=300, xaxis_tickangle=-45)
                st.plotly_chart(fig_part, use_container_width=True)


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
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text or f"HTTP {resp.status_code}"
                st.error(f"Error API: {detail}")
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

            if not results:
                st.warning("No se obtuvo respuesta de la API. ¿Está FastAPI corriendo (`api`)? ¿Está el modelo entrenado?")
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

                # ── Histograma de scores ──────────────────────
                st.markdown("#### Distribución de anomaly scores")
                fig_hist = go.Figure()
                for dev in res_df["device_id"].unique():
                    d_dev = res_df[res_df["device_id"] == dev]
                    fig_hist.add_trace(go.Histogram(
                        x=d_dev["score"], name=dev,
                        opacity=0.7, nbinsx=20,
                    ))
                fig_hist.update_layout(
                    barmode="overlay",
                    title="Distribución de anomaly_score por máquina (valores más negativos = más anómalo)",
                    xaxis_title="Anomaly Score", yaxis_title="Frecuencia",
                    height=300, template="plotly_dark",
                )
                st.plotly_chart(fig_hist, use_container_width=True)

                # ── Tabla resumen por máquina ─────────────────
                summary = res_df.groupby("device_id").agg(
                    lecturas    = ("score", "count"),
                    anomalias   = ("is_anomaly", "sum"),
                    score_medio = ("score", "mean"),
                    prob_media  = ("failure_prob", "mean"),
                ).reset_index()
                summary["score_medio"] = summary["score_medio"].round(4)
                summary["prob_media"]  = summary["prob_media"].apply(lambda x: f"{x:.1%}")
                st.dataframe(summary, use_container_width=True, hide_index=True)

                st.download_button(
                    "⬇️ Descargar CSV (resultados IA)",
                    data=res_df.to_csv(index=False),
                    file_name="anomalias_ia.csv",
                    mime="text/csv",
                )

# ── TAB 6: Pipeline Health ────────────────────────────────────
with tab_health:
    st.subheader("🏥 Estado del Pipeline")

    if st.button("🔄 Actualizar estado", key="refresh_health"):
        st.rerun()

    # ── Servicios ────────────────────────────────────────────
    st.markdown("### Servicios")
    services = {
        "FastAPI":   (FASTAPI_URL + "/health",         "GET"),
        "InfluxDB":  (INFLUX_URL + "/health",           "GET"),
        "Flink":     (FLINK_URL + "/overview",          "GET"),
    }
    minio_url = f"http://{MINIO_ENDPOINT}/minio/health/live"

    svc_cols = st.columns(4)
    for idx, (name, (url, _)) in enumerate(services.items()):
        with svc_cols[idx]:
            try:
                r = requests.get(url, timeout=3)
                if r.ok:
                    st.success(f"🟢 **{name}**\n\nOK")
                else:
                    st.error(f"🔴 **{name}**\n\nHTTP {r.status_code}")
            except Exception:
                st.error(f"🔴 **{name}**\n\nSin respuesta")

    with svc_cols[3]:
        try:
            r = requests.get(minio_url, timeout=3)
            st.success("🟢 **MinIO**\n\nOK") if r.ok else st.error(f"🔴 **MinIO**\n\nHTTP {r.status_code}")
        except Exception:
            st.error("🔴 **MinIO**\n\nSin respuesta")

    st.divider()

    # ── Jobs Flink ───────────────────────────────────────────
    st.markdown("### Jobs Flink")
    jobs = get_flink_jobs()
    if not jobs:
        st.warning("No se puede conectar con Flink JobManager (`jobmanager:8081`)")
    else:
        jobs_df = pd.DataFrame(jobs)
        # Colorear estado
        def _color_estado(val):
            if val == "RUNNING":
                return "background-color: #1a4a1a; color: #4caf50"
            if val in ("FAILED", "CANCELED"):
                return "background-color: #4a1a1a; color: #f44336"
            return ""

        st.dataframe(
            jobs_df.style.applymap(_color_estado, subset=["estado"]),
            use_container_width=True,
            hide_index=True,
        )

        running = sum(1 for j in jobs if j["estado"] == "RUNNING")
        failed  = sum(1 for j in jobs if j["estado"] in ("FAILED", "CANCELED"))
        c1, c2, c3 = st.columns(3)
        c1.metric("Jobs RUNNING", running)
        c2.metric("Jobs FAILED",  failed,  delta=f"-{failed}" if failed else None, delta_color="inverse")
        c3.metric("Jobs total",   len(jobs))

    st.divider()

    # ── Topics Kafka ─────────────────────────────────────────
    st.markdown("### Topics Kafka (Redpanda)")
    topics_check = ["sensors_raw", "sensors_clean", "sensors_verified", "sensors_invalid"]
    offsets = get_kafka_offsets(topics_check)
    if all(v["estado"] == "sin conexión" for v in offsets.values()):
        st.warning("No se puede conectar con Redpanda (`redpanda:29092`)")
    else:
        topic_rows = []
        for t, info in offsets.items():
            topic_rows.append({"topic": t, "particiones": info["particiones"], "estado": info["estado"]})
        st.dataframe(pd.DataFrame(topic_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── Flink overview ───────────────────────────────────────
    st.markdown("### Recursos Flink")
    try:
        ov = requests.get(f"{FLINK_URL}/overview", timeout=3).json()
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Task Slots totales",      ov.get("slots-total", "?"))
        r2.metric("Task Slots disponibles",  ov.get("slots-available", "?"))
        r3.metric("TaskManagers",            ov.get("taskmanagers", "?"))
        r4.metric("Jobs en ejecución",       ov.get("jobs-running", "?"))
    except Exception:
        st.info("Flink overview no disponible")


# ── TAB 7: Hash Chain / Seguridad ─────────────────────────────
with tab_hash:
    st.subheader("🔐 Integridad de Mensajes — Hash Chain SHA256")
    st.markdown("""
    Cada sensor firma sus mensajes con una cadena SHA256: cada hash incluye el hash anterior,
    formando una cadena inmutable. **Flink** verifica esta cadena en tiempo real.

    | Topic | Significado |
    |-------|-------------|
    | `sensors_verified` | Mensajes con cadena íntegra ✅ |
    | `sensors_invalid`  | Mensajes con hash roto o JSON inválido ❌ (DLQ) |
    """)

    if st.button("🔄 Actualizar", key="refresh_hash"):
        st.rerun()

    # ── Métricas de alto nivel ────────────────────────────────
    verified_total = get_verified_count()
    dlq_msgs = get_dlq_messages(100)

    m1, m2, m3 = st.columns(3)
    m1.metric("Mensajes verificados (offset)", verified_total if verified_total >= 0 else "—")
    m2.metric("Mensajes en DLQ (sensors_invalid)", len(dlq_msgs))
    if verified_total > 0 and dlq_msgs:
        integrity = verified_total / (verified_total + len(dlq_msgs)) * 100
        m3.metric("Integridad estimada", f"{integrity:.1f}%")
    elif verified_total > 0:
        m3.metric("Integridad", "100%")
    else:
        m3.metric("Integridad", "—")

    st.divider()

    # ── Mensajes DLQ ─────────────────────────────────────────
    st.markdown("### Últimos mensajes rechazados (DLQ)")
    if not dlq_msgs:
        st.success("✅ DLQ vacío — ningún mensaje ha sido rechazado.")
    else:
        dlq_rows = []
        for m in dlq_msgs:
            dlq_rows.append({
                "device_id": m.get("device_id", m.get("raw", "?")[:20]),
                "reason":    m.get("reason", "—"),
                "ts":        m.get("ts", "—"),
                "hash":      m.get("hash", "—")[:16] + "…" if m.get("hash") else "—",
            })
        st.dataframe(pd.DataFrame(dlq_rows), use_container_width=True, hide_index=True)

        # Gráfico de razones de rechazo
        if dlq_rows:
            reason_df = pd.DataFrame(dlq_rows)
            reason_counts = reason_df["reason"].value_counts().reset_index()
            reason_counts.columns = ["motivo", "count"]
            fig = px.bar(
                reason_counts,
                x="count",
                y="motivo",
                orientation="h",
                title="Motivos de rechazo en DLQ",
                template="plotly_dark",
                color="count",
                color_continuous_scale="Reds",
            )
            st.plotly_chart(fig, use_container_width=True)

        # Distribución por máquina
        device_counts = pd.DataFrame(dlq_rows)["device_id"].value_counts().reset_index()
        device_counts.columns = ["device_id", "rechazados"]
        fig2 = px.bar(
            device_counts,
            x="device_id",
            y="rechazados",
            title="Mensajes rechazados por máquina",
            template="plotly_dark",
            color="rechazados",
            color_continuous_scale="OrRd",
        )
        st.plotly_chart(fig2, use_container_width=True)

    st.divider()

    # ── Algoritmo explicado ───────────────────────────────────
    with st.expander("ℹ️ Cómo funciona el Hash Chain"):
        st.markdown("""
        ```
        Mensaje N:
          content = {device_id, temperature, unit, ts, _ingested_at}
          hash    = SHA256(json(content, sort_keys=True) + prev_hash)
          prev_hash = hash del mensaje N-1 (o "0"*64 para el primero)

        Flink verifica:
          1. ¿prev_hash declarado == último hash válido en estado Flink?
          2. ¿hash declarado == SHA256(content + prev_hash)?
          Si alguna falla → sensors_invalid con reason="hash_chain_broken"
        ```
        **Garantía:** Si alguien modifica un mensaje en tránsito, el hash cambia
        y Flink lo detecta. Los mensajes válidos van a `sensors_verified`.
        """)


# ── TAB 8: Modelos Avanzados ──────────────────────────────────
with tab_advanced:
    st.subheader("🔬 Modelos Avanzados de Machine Learning")
    st.markdown("""
    Modelos complementarios al IsolationForest para análisis más profundo:

    | Modelo | Tipo | Objetivo |
    |--------|------|----------|
    | **Prophet** | Forecasting temporal | Predice temperatura futura con intervalos de confianza |
    | **RandomForest** | Clasificación supervisada | Predice probabilidad de alerta con features de ingeniería |
    | **CUSUM** | Detección de cambio de régimen | Detecta degradación gradual antes de que dispare alertas |
    | **K-Means** | Clustering no supervisado | Agrupa máquinas por patrón de comportamiento |
    """)

    adv_range = st.slider("Datos históricos para análisis (min)", 30, 480, 120, key="adv_range")

    adv_df = query_history(adv_range)

    if adv_df.empty:
        st.warning("Sin datos en InfluxDB. ¿Está el pipeline corriendo?")
    else:
        adv_devices = sorted(adv_df["device_id"].unique().tolist())

        # ── 1. Prophet Forecasting ─────────────────────────────────────
        st.divider()
        st.markdown("### 📈 Prophet — Forecasting de Temperatura")
        st.markdown(
            "Modelo de series temporales de Meta/Facebook. Predice la temperatura "
            "de los próximos minutos con bandas de incertidumbre del 95%."
        )

        prop_col1, prop_col2 = st.columns([2, 1])
        with prop_col1:
            prop_device = st.selectbox("Máquina a predecir", adv_devices, key="prop_device")
        with prop_col2:
            prop_horizon = st.slider("Horizonte de predicción (min)", 10, 60, 30, key="prop_horizon")

        if st.button("📈 Entrenar Prophet y predecir", key="btn_prophet"):
            try:
                from prophet import Prophet  # noqa: PLC0415
                dev_df = adv_df[adv_df["device_id"] == prop_device].copy()
                dev_df = dev_df.sort_values("ts").rename(
                    columns={"ts": "ds", "avg_temp_c": "y"}
                )
                dev_df["ds"] = pd.to_datetime(dev_df["ds"]).dt.tz_localize(None)

                with st.spinner(f"Entrenando Prophet para {prop_device}..."):
                    m_prop = Prophet(
                        changepoint_prior_scale=0.05,
                        seasonality_mode="additive",
                        interval_width=0.95,
                        daily_seasonality=False,
                        weekly_seasonality=False,
                    )
                    m_prop.fit(dev_df[["ds", "y"]])
                    future = m_prop.make_future_dataframe(
                        periods=prop_horizon, freq="min"
                    )
                    forecast = m_prop.predict(future)

                last_ts = dev_df["ds"].max()
                future_fc = forecast[forecast["ds"] > last_ts]

                fig_prop = go.Figure()
                fig_prop.add_trace(go.Scatter(
                    x=dev_df["ds"], y=dev_df["y"],
                    mode="lines", name="Histórico",
                    line=dict(color="steelblue", width=2),
                ))
                fig_prop.add_trace(go.Scatter(
                    x=future_fc["ds"], y=future_fc["yhat"],
                    mode="lines", name=f"Predicción (+{prop_horizon} min)",
                    line=dict(color="orange", width=2, dash="dash"),
                ))
                # Confidence band
                fig_prop.add_trace(go.Scatter(
                    x=pd.concat([future_fc["ds"], future_fc["ds"].iloc[::-1]]),
                    y=list(future_fc["yhat_upper"]) + list(
                        future_fc["yhat_lower"].iloc[::-1]
                    ),
                    fill="toself",
                    fillcolor="rgba(255,165,0,0.15)",
                    line=dict(width=0),
                    name="Intervalo 95%",
                ))
                fig_prop.add_hline(
                    y=ALERT_THRESHOLD, line_dash="dash", line_color="red",
                    annotation_text=f"Umbral {ALERT_THRESHOLD}°C",
                )
                fig_prop.update_layout(
                    title=f"Prophet — Forecast temperatura {prop_device}",
                    xaxis_title="Tiempo",
                    yaxis_title="Temperatura (°C)",
                    height=420,
                    template="plotly_dark",
                )
                st.plotly_chart(fig_prop, use_container_width=True)

                future_alerts = future_fc[future_fc["yhat"] > ALERT_THRESHOLD]
                if not future_alerts.empty:
                    st.warning(
                        f"⚠️ Prophet predice **{len(future_alerts)}** ventanas de alerta "
                        f"en los próximos {prop_horizon} min"
                    )
                else:
                    st.success(
                        f"✅ Prophet no prevé alertas en los próximos {prop_horizon} min"
                    )

                if not future_fc.empty:
                    last_pred = future_fc.iloc[-1]
                    m1, m2, m3 = st.columns(3)
                    m1.metric(
                        f"Temperatura en +{prop_horizon} min",
                        f"{last_pred['yhat']:.1f}°C",
                    )
                    m2.metric(
                        "Límite inferior (95%)", f"{last_pred['yhat_lower']:.1f}°C"
                    )
                    m3.metric(
                        "Límite superior (95%)", f"{last_pred['yhat_upper']:.1f}°C"
                    )

            except ImportError:
                st.error(
                    "Prophet no está instalado. Ejecuta: `pip install prophet`\n\n"
                    "En el devcontainer: `pip install prophet` en el terminal."
                )
            except Exception as e:
                st.error(f"Error en Prophet: {e}")

        # ── 2. RandomForest Classifier ─────────────────────────────────
        st.divider()
        st.markdown("### 🌲 RandomForest — Clasificador de Alertas")
        st.markdown("""
        Modelo de clasificación supervisado (scikit-learn).
        **Features:** temperatura, hora del día, media/desv./max. de ventana deslizante.
        **Etiqueta:** `alert = 1` si temperatura > umbral de alerta.
        """)

        if st.button("🌲 Entrenar RandomForest", key="btn_rf"):
            from sklearn.ensemble import RandomForestClassifier  # noqa: PLC0415
            from sklearn.metrics import classification_report, confusion_matrix  # noqa: PLC0415
            from sklearn.model_selection import train_test_split  # noqa: PLC0415
            import numpy as np  # noqa: PLC0415

            with st.spinner("Preparando features y entrenando..."):
                rf_df = adv_df.copy()
                rf_df["ts"] = pd.to_datetime(rf_df["ts"], utc=True)
                rf_df["hour"] = rf_df["ts"].dt.hour
                rf_df["minute"] = rf_df["ts"].dt.minute
                rf_df["device_enc"] = (
                    rf_df["device_id"].astype("category").cat.codes
                )
                rf_df["alert"] = (
                    rf_df["avg_temp_c"] > ALERT_THRESHOLD
                ).astype(int)

                rf_df = rf_df.sort_values(["device_id", "ts"])
                for col, func in [
                    ("roll_mean", "mean"),
                    ("roll_std",  "std"),
                    ("roll_max",  "max"),
                ]:
                    rf_df[col] = rf_df.groupby("device_id")["avg_temp_c"].transform(
                        lambda x, f=func: getattr(
                            x.rolling(5, min_periods=1), f
                        )().fillna(0)
                    )

                features_rf = [
                    "avg_temp_c", "hour", "minute",
                    "device_enc", "roll_mean", "roll_std", "roll_max",
                ]
                rf_df = rf_df.dropna(subset=features_rf)
                X = rf_df[features_rf].values
                y = rf_df["alert"].values

                if len(np.unique(y)) < 2:
                    st.warning(
                        "No hay variedad suficiente en las etiquetas. "
                        "Necesitas datos con y sin alertas — amplía el rango de tiempo."
                    )
                else:
                    X_train, X_test, y_train, y_test = train_test_split(
                        X, y, test_size=0.2, random_state=42
                    )
                    clf = RandomForestClassifier(
                        n_estimators=100, random_state=42, n_jobs=-1
                    )
                    clf.fit(X_train, y_train)
                    y_pred = clf.predict(X_test)

                    report = classification_report(
                        y_test, y_pred, output_dict=True, zero_division=0
                    )
                    acc = (y_pred == y_test).mean()
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Accuracy", f"{acc:.1%}")
                    c2.metric(
                        "Precisión (alert=1)",
                        f"{report.get('1', {}).get('precision', 0):.1%}",
                    )
                    c3.metric(
                        "Recall (alert=1)",
                        f"{report.get('1', {}).get('recall', 0):.1%}",
                    )

                    # Feature importance
                    imp_df = pd.DataFrame({
                        "feature":    features_rf,
                        "importance": clf.feature_importances_,
                    }).sort_values("importance", ascending=True)

                    fig_imp = go.Figure(go.Bar(
                        x=imp_df["importance"],
                        y=imp_df["feature"],
                        orientation="h",
                        marker_color="steelblue",
                    ))
                    fig_imp.update_layout(
                        title="Importancia de features — RandomForest",
                        xaxis_title="Importancia relativa",
                        height=300,
                        template="plotly_dark",
                    )
                    st.plotly_chart(fig_imp, use_container_width=True)

                    # Confusion matrix
                    cm = confusion_matrix(y_test, y_pred)
                    fig_cm = px.imshow(
                        cm,
                        labels={"x": "Predicción", "y": "Real", "color": "Muestras"},
                        x=["Normal (0)", "Alerta (1)"],
                        y=["Normal (0)", "Alerta (1)"],
                        color_continuous_scale="Blues",
                        text_auto=True,
                        title="Matriz de confusión (test set)",
                    )
                    fig_cm.update_layout(height=300, template="plotly_dark")
                    st.plotly_chart(fig_cm, use_container_width=True)

                    # Probabilidad predicha en el tiempo para una máquina
                    rf_df["prob_alert"] = clf.predict_proba(X)[:, 1]
                    sel_rf = adv_devices[0]
                    dev_rf = rf_df[rf_df["device_id"] == sel_rf].sort_values("ts")
                    if not dev_rf.empty:
                        fig_prob = go.Figure()
                        fig_prob.add_trace(go.Scatter(
                            x=dev_rf["ts"], y=dev_rf["avg_temp_c"],
                            name="Temperatura (°C)", mode="lines",
                            line=dict(color="steelblue"),
                            yaxis="y1",
                        ))
                        fig_prob.add_trace(go.Scatter(
                            x=dev_rf["ts"], y=dev_rf["prob_alert"],
                            name="Prob. alerta (RF)", mode="lines",
                            line=dict(color="orange", dash="dash"),
                            yaxis="y2",
                        ))
                        fig_prob.update_layout(
                            title=f"Temperatura vs Probabilidad de alerta — {sel_rf}",
                            yaxis=dict(title="Temperatura (°C)", side="left"),
                            yaxis2=dict(
                                title="Prob. alerta",
                                side="right",
                                overlaying="y",
                                range=[0, 1],
                            ),
                            height=350,
                            template="plotly_dark",
                        )
                        st.plotly_chart(fig_prob, use_container_width=True)

        # ── 3. CUSUM Change Point Detection ────────────────────────────
        st.divider()
        st.markdown("### 📉 CUSUM — Detección de Cambios de Régimen")
        st.markdown("""
        CUSUM (Cumulative Sum) detecta **cambios graduales** en la media de temperatura.
        Es más sensible que un umbral fijo: detecta cuando el nivel base de una máquina
        cambia, aunque no supere el umbral de alerta.
        Uses `ruptures` (PELT) si está instalado, o CUSUM manual en caso contrario.
        """)

        cusum_c1, cusum_c2 = st.columns([2, 1])
        with cusum_c1:
            cusum_device = st.selectbox(
                "Máquina", adv_devices, key="cusum_device"
            )
        with cusum_c2:
            cusum_thresh = st.slider(
                "Sensibilidad",
                2.0, 10.0, 4.0, step=0.5, key="cusum_thresh",
                help="Umbral en desviaciones estándar. Menor = más sensible.",
            )

        if st.button("📉 Detectar cambios de régimen", key="btn_cusum"):
            dev_cusum = adv_df[
                adv_df["device_id"] == cusum_device
            ].sort_values("ts").copy()

            if len(dev_cusum) < 10:
                st.warning(
                    "Pocos datos para CUSUM. Amplía el rango de tiempo."
                )
            else:
                series_vals = dev_cusum["avg_temp_c"].values
                try:
                    import ruptures as rpt  # noqa: PLC0415
                    model_rpt = rpt.Pelt(model="rbf").fit(series_vals)
                    breakpoints = model_rpt.predict(pen=cusum_thresh * 10)[:-1]
                    method_used = "ruptures PELT (rbf)"
                except ImportError:
                    mean_s = series_vals.mean()
                    std_s = series_vals.std() or 1.0
                    norm = (series_vals - mean_s) / std_s
                    drift = 0.5
                    s_pos, s_neg = 0.0, 0.0
                    breakpoints = []
                    for i_c, x_c in enumerate(norm):
                        s_pos = max(0.0, s_pos + x_c - drift)
                        s_neg = max(0.0, s_neg - x_c - drift)
                        if s_pos > cusum_thresh or s_neg > cusum_thresh:
                            breakpoints.append(i_c)
                            s_pos, s_neg = 0.0, 0.0
                    method_used = "CUSUM manual (sin ruptures)"

                st.info(
                    f"Método: **{method_used}** — "
                    f"**{len(breakpoints)}** punto(s) de cambio detectado(s)"
                )

                fig_cusum = go.Figure()
                fig_cusum.add_trace(go.Scatter(
                    x=dev_cusum["ts"], y=dev_cusum["avg_temp_c"],
                    mode="lines", name="Temperatura",
                    line=dict(color="steelblue", width=1.5),
                ))
                for bp in breakpoints:
                    if bp < len(dev_cusum):
                        fig_cusum.add_vline(
                            x=dev_cusum["ts"].iloc[bp],
                            line_dash="dash", line_color="orange",
                            annotation_text="cambio",
                        )
                fig_cusum.add_hline(
                    y=ALERT_THRESHOLD, line_dash="dot", line_color="red",
                    annotation_text=f"Umbral {ALERT_THRESHOLD}°C",
                )
                fig_cusum.update_layout(
                    title=f"CUSUM — Cambios de régimen en {cusum_device}",
                    xaxis_title="Tiempo",
                    yaxis_title="Temperatura (°C)",
                    height=380,
                    template="plotly_dark",
                )
                st.plotly_chart(fig_cusum, use_container_width=True)

                if breakpoints:
                    st.markdown("#### Estadísticas por régimen")
                    indices = [0] + list(breakpoints) + [len(dev_cusum)]
                    regime_rows = []
                    for ri in range(len(indices) - 1):
                        seg = dev_cusum.iloc[indices[ri]:indices[ri + 1]]
                        if seg.empty:
                            continue
                        regime_rows.append({
                            "régimen": ri + 1,
                            "desde":   str(seg["ts"].iloc[0])[:19],
                            "hasta":   str(seg["ts"].iloc[-1])[:19],
                            "puntos":  len(seg),
                            "media_c": round(seg["avg_temp_c"].mean(), 2),
                            "std_c":   round(seg["avg_temp_c"].std(), 2),
                        })
                    st.dataframe(
                        pd.DataFrame(regime_rows),
                        use_container_width=True,
                        hide_index=True,
                    )

        # ── 4. K-Means Clustering ───────────────────────────────────────
        st.divider()
        st.markdown("### 🎯 K-Means — Clustering de Comportamiento de Máquinas")
        st.markdown("""
        Agrupa las máquinas según su **patrón de temperatura** usando K-Means (scikit-learn).
        **Features por máquina:** temperatura media, desviación estándar,
        temperatura máxima y tasa de alertas.
        """)

        km_col1, km_col2 = st.columns(2)
        with km_col1:
            n_clusters = st.slider(
                "Número de clusters",
                2, min(5, len(adv_devices)),
                min(3, len(adv_devices)),
                key="km_clusters",
            )

        if st.button("🎯 Calcular clusters K-Means", key="btn_kmeans"):
            from sklearn.cluster import KMeans  # noqa: PLC0415
            from sklearn.preprocessing import StandardScaler  # noqa: PLC0415

            feat_rows = []
            for dev in adv_devices:
                d = adv_df[adv_df["device_id"] == dev]["avg_temp_c"]
                feat_rows.append({
                    "device_id":  dev,
                    "mean_c":     d.mean(),
                    "std_c":      d.std(),
                    "max_c":      d.max(),
                    "min_c":      d.min(),
                    "alert_rate": (d > ALERT_THRESHOLD).mean(),
                    "p90_c":      d.quantile(0.90),
                })
            feat_df = pd.DataFrame(feat_rows)
            features_km = ["mean_c", "std_c", "max_c", "alert_rate"]
            X_km = feat_df[features_km].fillna(0).values

            if len(feat_df) < 2:
                st.warning(
                    "Necesitas al menos 2 máquinas con datos para hacer clustering."
                )
            else:
                scaler_km = StandardScaler()
                X_scaled = scaler_km.fit_transform(X_km)
                km_model = KMeans(
                    n_clusters=n_clusters, random_state=42, n_init=10
                )
                feat_df["cluster"] = km_model.fit_predict(X_scaled).astype(str)

                # Scatter: mean_c vs std_c
                fig_km1 = px.scatter(
                    feat_df,
                    x="mean_c", y="std_c",
                    color="cluster",
                    size="alert_rate",
                    hover_name="device_id",
                    text="device_id",
                    title="K-Means — Temperatura media vs Variabilidad",
                    labels={
                        "mean_c":     "Temperatura media (°C)",
                        "std_c":      "Desv. estándar (°C)",
                        "cluster":    "Cluster",
                        "alert_rate": "Tasa alertas",
                    },
                    template="plotly_dark",
                    color_discrete_sequence=px.colors.qualitative.Set1,
                )
                fig_km1.add_vline(
                    x=ALERT_THRESHOLD, line_dash="dot", line_color="red",
                    annotation_text="Umbral alerta",
                )
                fig_km1.update_traces(textposition="top center")
                fig_km1.update_layout(height=420)
                st.plotly_chart(fig_km1, use_container_width=True)

                # Scatter: max_c vs alert_rate
                fig_km2 = px.scatter(
                    feat_df,
                    x="max_c", y="alert_rate",
                    color="cluster",
                    hover_name="device_id",
                    text="device_id",
                    title="K-Means — Temperatura máxima vs Tasa de alertas",
                    labels={
                        "max_c":      "Temperatura máxima (°C)",
                        "alert_rate": "Tasa de alertas",
                        "cluster":    "Cluster",
                    },
                    template="plotly_dark",
                    color_discrete_sequence=px.colors.qualitative.Set1,
                )
                fig_km2.update_traces(textposition="top center")
                fig_km2.update_layout(height=350)
                st.plotly_chart(fig_km2, use_container_width=True)

                # Tabla de asignación
                st.markdown("#### Asignación de clusters")
                display_df = feat_df[
                    ["device_id", "cluster", "mean_c", "std_c",
                     "max_c", "alert_rate", "p90_c"]
                ].sort_values("cluster").copy()
                display_df["mean_c"]     = display_df["mean_c"].round(2)
                display_df["std_c"]      = display_df["std_c"].round(2)
                display_df["max_c"]      = display_df["max_c"].round(2)
                display_df["p90_c"]      = display_df["p90_c"].round(2)
                display_df["alert_rate"] = display_df["alert_rate"].apply(
                    lambda x: f"{x:.1%}"
                )
                st.dataframe(display_df, use_container_width=True, hide_index=True)

                # Centroides
                st.markdown("#### Centroides de clusters (escala original)")
                centroid_vals = scaler_km.inverse_transform(
                    km_model.cluster_centers_
                )
                centroid_df = pd.DataFrame(
                    centroid_vals, columns=features_km
                ).round(2)
                centroid_df.insert(0, "cluster", [str(i) for i in range(n_clusters)])
                st.dataframe(centroid_df, use_container_width=True, hide_index=True)


# ── Auto-refresco ─────────────────────────────────────────────
if auto_refresh:
    time.sleep(REFRESH_SEC)
    st.rerun()
