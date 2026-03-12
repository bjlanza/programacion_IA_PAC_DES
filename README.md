# ILERNA Smart-Industry — Programación IA PAC DES

Sistema de telemetría IoT con procesamiento en tiempo real para monitorización de temperatura de máquinas industriales.

## Arquitectura del pipeline

```
[sensor_simulator.py]
        │  MQTT (sensors/telemetry)  QoS 1
        ▼
[Mosquitto]  :11883
        │
        ▼
[mqtt_to_redpanda_bridge.py]  ← Hito 1
  · Valida esquema JSON
  · Enriquece con metadatos
        │  Kafka  sensors_raw
        ▼
[Redpanda]  :19092
        │
        ▼
[flink_normalization_job.py]  ← Hito 2
  · UDF to_celsius (F/K → °C)
  · Filtra datos corruptos
        │  Kafka  sensors_clean
        ▼
[flink_analytics_job.py]  ← Hito 3
  · Tumble Window 1 min / device
  · ALERTA si avg > 80°C
        │  HTTP Line Protocol
        ▼
[InfluxDB]  :18086  bucket=sensores
  measurement: machine_stats
        │
        ├──▶ [FastAPI]  :18000  ← Hito 4
        │     /machines/status
        │     /machines/{id}
        │     /alerts
        │     /stats
        │
        └──▶ [Streamlit]  :18501  ← Hito 4
              📡 Tiempo Real (gauges)
              📈 Historial (líneas)
              ⚠️  Alertas
              🗄️  DuckDB + MinIO/Parquet

[kafka_to_minio.py]  (paralelo)
  · Ventanas Parquet → MinIO  :19000
  · Bucket: datalake/raw/YYYY/MM/DD/HH/
```

## Servicios Docker

| Servicio         | Puerto externo | Descripción                  | Credenciales        |
|------------------|----------------|------------------------------|---------------------|
| Mosquitto        | 11883          | Broker MQTT                  | anónimo             |
| Redpanda         | 19092          | Kafka-compatible broker      | —                   |
| Redpanda Console | 18080          | UI de tópicos y mensajes     | —                   |
| Flink JobManager | 18081          | Cluster Flink (REST + UI)    | —                   |
| InfluxDB         | 18086          | Base de datos de series temp.| admin / Ilerna_Programaci0n |
| MinIO            | 19000 / 19001  | S3 API / Consola web         | admin / Ilerna_Programaci0n |
| Grafana          | 13000          | Dashboards                   | admin / Ilerna_Programaci0n |
| FastAPI          | 18000          | API REST sensores            | —                   |
| Streamlit        | 18501          | Dashboard interactivo        | —                   |
| JupyterLab       | 18888          | Notebooks de análisis        | —                   |

## Estructura del proyecto

```
.
├── .devcontainer/
│   ├── docker-compose.yml      # Todos los servicios Docker
│   ├── Dockerfile              # Imagen del workspace
│   ├── Dockerfile.jobmanager   # Imagen Flink con JARs y Python pre-instalados
│   ├── devcontainer.json       # Configuración GitHub Codespaces
│   ├── start.sh                # Levanta Docker y espera healthy (postStartCommand automático)
│   └── init_pipeline.sh        # Inicializa topics, bucket MinIO, jobs Flink y aliases (manual)
│
├── src/
│   ├── 01_ingestion/
│   │   ├── sensor_simulator.py         # Emula máquinas con temperatura en C/F/K
│   │   └── mqtt_to_redpanda_bridge.py  # Hito 1: MQTT → Redpanda con validación
│   ├── 02_processing/
│   │   └── README.md                   # Instrucciones para ejecutar jobs Flink
│   ├── 03_storage/
│   │   ├── kafka_to_influx.py          # Consumidor Kafka → InfluxDB (directo)
│   │   └── kafka_to_minio.py           # Consumidor Kafka → MinIO Parquet
│   ├── api/
│   │   └── main.py                     # Hito 4: FastAPI REST
│   └── 05_ui/
│       └── app.py                      # Hito 4: Streamlit + DuckDB histórico
│
├── jobs/
│   ├── flink_normalization_job.py      # Hito 2: Table API + UDF to_celsius
│   ├── flink_analytics_job.py          # Hito 3: Tumble Window + alertas InfluxDB
│   └── anomaly_detector.py             # Job alternativo DataStream API
│
├── notebooks/
│   └── 01_exploracion_datos.ipynb      # Exploración con Plotly + InfluxDB
│
├── config/
│   ├── mosquitto.conf                  # Configuración MQTT broker
│   └── requirements.txt               # Dependencias Python
│
├── tests/
│   ├── test_connectivity.sh            # Smoke test de servicios (bash)
│   └── test_flow.py                    # Test end-to-end del pipeline
│
└── docs/
    ├── 00_smoke_test_checklist.md      # Checklist de validación del entorno
    └── 01_practica.md                  # Guía de la práctica PAC DES
```

## Inicio rápido

### 1. Abrir en GitHub Codespaces

El entorno arranca automáticamente. `start.sh` levanta Docker y espera a que todos los servicios estén healthy.

### 2. Inicializar el pipeline

Una vez que `start.sh` finalice, ejecuta en el terminal:

```bash
bash .devcontainer/init_pipeline.sh
```

Esto crea los topics Kafka, el bucket MinIO, lanza los jobs Flink y añade aliases de desarrollo.

### 4. Arrancar el pipeline completo

```bash
# Terminal 1 — Simulador de sensores (alias: sim)
python src/01_ingestion/sensor_simulator.py --machines 5 --fault-rate 0.1

# Terminal 2 — Bridge MQTT → Redpanda (alias: bridge)
python src/01_ingestion/mqtt_to_redpanda_bridge.py

# Terminal 3 — FastAPI (alias: api)
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 4 — Dashboard (alias: ui)
streamlit run src/05_ui/app.py --server.port 8501
```

> **Nota:** Los jobs Flink, topics Kafka y bucket MinIO se inicializan con `init_pipeline.sh`. Usa los aliases `sim`, `bridge`, `api`, `ui` como atajos.

### 5. Acceder a las UIs

Desde la pestaña **Ports** de Codespaces:

- **Dashboard**: puerto 18501
- **API docs**: puerto 18000 → `/docs`
- **Flink UI**: puerto 18081
- **Redpanda Console**: puerto 18080
- **InfluxDB**: puerto 18086

## Topics Kafka

| Topic              | Productor                    | Consumidor(es)                       | Schema                                              |
|--------------------|------------------------------|--------------------------------------|-----------------------------------------------------|
| `sensors/telemetry`| sensor_simulator             | mqtt_to_redpanda_bridge              | MQTT raw (C/F/K, fallos posibles)                   |
| `sensors_raw`      | mqtt_to_redpanda_bridge      | flink_normalization_job              | `{device_id, temperature, unit, ts, _ingested_at}`  |
| `sensors_clean`    | flink_normalization_job      | flink_analytics_job, kafka_to_minio  | `{device_id, temperature_c, unit_original, ts}`     |

## Variables de entorno principales

Los scripts Python usan **hostnames internos Docker** por defecto (red `ilerna_ia_big_data`).
Sobrescribir con variables de entorno si se ejecutan fuera del devcontainer.

| Variable          | Default (interno Docker)    | Descripción                      |
|-------------------|-----------------------------|----------------------------------|
| `MQTT_HOST`       | `mosquitto`                 | Host del broker MQTT             |
| `MQTT_PORT`       | `1883`                      | Puerto MQTT (interno)            |
| `KAFKA_BROKER`    | `redpanda:29092`            | Bootstrap servers Kafka          |
| `INFLUX_URL`      | `http://influxdb:8086`      | URL InfluxDB                     |
| `INFLUX_TOKEN`    | `supersecrettoken`          | Token de autenticación InfluxDB  |
| `MINIO_ENDPOINT`  | `minio:9000`                | Endpoint MinIO S3                |
| `ALERT_THRESHOLD` | `80.0`                      | Temperatura de alerta (°C)       |
| `MINIO_BUCKET`    | `datalake`                  | Bucket MinIO para histórico      |
