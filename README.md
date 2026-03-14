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
        ├──▶ [flink_hash_verifier_job.py]   ← Seguridad
        │      · Verifica SHA256 hash-chain por device
        │      · OK  → sensors_verified
        │      · KO  → sensors_invalid (DLQ)
        │
        └──▶ [flink_normalization_job.py]   ← Hito 2
               · UDF to_celsius (F/K → °C)
               · Filtra datos corruptos
                       │  Kafka  sensors_clean
                       ▼
              [flink_analytics_job.py]      ← Hito 3
               · Tumble Window 1 min / device
               · ALERTA si avg > 80°C
                       │
                       ├──▶ [InfluxDB]  :18086  bucket=sensores
                       │     measurement: machine_stats
                       │         │
                       │         ├──▶ [FastAPI]    :18000  ← Hito 4
                       │         │     /machines/status
                       │         │     /machines/{id}
                       │         │     /alerts
                       │         │     /model/train
                       │         │     /model/predict
                       │         │
                       │         └──▶ [Streamlit]  :18501  ← Hito 4
                       │               📡 Tiempo Real
                       │               📈 Historial
                       │               ⚠️  Alertas
                       │               🗄️  Lambda Query
                       │               🤖 IA Anomalías
                       │
                       └──▶ [flink_to_minio_job.py]        ← Cold path
                              · Parquet particionado por fecha
                              · s3a://datalake/clean/year=.../
                                      │
                                      └──▶ [MinIO]  :19000/:19001
                                             DuckDB lee con hive_partitioning=true
```

## Servicios Docker

| Servicio         | Puerto externo | Descripción                    | Credenciales                |
|------------------|----------------|--------------------------------|-----------------------------|
| Mosquitto        | 11883          | Broker MQTT                    | anónimo                     |
| Redpanda         | 19092          | Kafka-compatible broker        | —                           |
| Redpanda Console | 18080          | UI de tópicos y mensajes       | —                           |
| Flink JobManager | 18081          | Cluster Flink (REST + UI)      | —                           |
| InfluxDB         | 18086          | Base de datos de series temp.  | admin / Ilerna_Programaci0n |
| MinIO            | 19000 / 19001  | S3 API / Consola web           | admin / Ilerna_Programaci0n |
| Grafana          | 13000          | Dashboards                     | sin login (acceso anónimo)  |
| FastAPI          | 18000          | API REST sensores + modelo IA  | —                           |
| Streamlit        | 18501          | Dashboard interactivo          | —                           |
| JupyterLab       | 18888          | Notebooks de análisis          | —                           |

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
│   │   ├── sensor_simulator.py         # Emula máquinas con temperatura en C/F/K + hash-chain
│   │   └── mqtt_to_redpanda_bridge.py  # Hito 1: MQTT → Redpanda con validación
│   ├── 02_processing/
│   │   └── README.md                   # Instrucciones para ejecutar jobs Flink
│   ├── 03_storage/
│   │   ├── kafka_to_influx.py          # Consumidor Kafka → InfluxDB (directo)
│   │   └── kafka_to_minio.py           # Consumidor Kafka → MinIO Parquet
│   ├── api/
│   │   └── main.py                     # Hito 4: FastAPI REST + IsolationForest
│   └── 05_ui/
│       └── app.py                      # Hito 4: Streamlit + DuckDB histórico
│
├── jobs/
│   ├── flink_normalization_job.py      # Hito 2: Table API + UDF to_celsius
│   ├── flink_analytics_job.py          # Hito 3: Tumble Window + alertas InfluxDB
│   ├── flink_hash_verifier_job.py      # Seguridad: SHA256 hash-chain por device
│   └── flink_to_minio_job.py           # Cold path: Parquet particionado en MinIO
│
├── notebooks/
│   └── 01_exploracion_datos.ipynb      # Exploración con Plotly + InfluxDB + DuckDB
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

El entorno arranca automáticamente. `start.sh` levanta Docker y espera a que todos los servicios estén healthy (puede tardar 2-3 minutos).

### 2. Inicializar el pipeline (una vez por Codespace)

Una vez que `start.sh` finalice, ejecuta en el terminal:

```bash
source .devcontainer/init_pipeline.sh
```

Esto crea los **topics Kafka**, el **bucket MinIO**, lanza los **4 jobs Flink** y añade **aliases de desarrollo** al shell.

### 3. Arrancar el pipeline completo

Abre 4 terminales (o usa `&` para background):

```bash
# Terminal 1 — Simulador de sensores
sim   # equivale a: python src/01_ingestion/sensor_simulator.py --machines 5 --fault-rate 0.1

# Terminal 2 — Bridge MQTT → Redpanda
bridge   # equivale a: python src/01_ingestion/mqtt_to_redpanda_bridge.py

# Terminal 3 — FastAPI REST + modelo IA
api   # equivale a: uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 4 — Dashboard Streamlit
ui   # equivale a: streamlit run src/05_ui/app.py --server.port 8501
```

> Ver todos los aliases disponibles: escribe `aliases` en el terminal.

### 4. Entrenar el modelo de anomalías (primera vez)

Una vez que haya datos fluyendo (~1 min), entrena el modelo IsolationForest:

```bash
curl -s -X POST http://localhost:8000/model/train | python3 -m json.tool
```

O desde el dashboard Streamlit → pestaña **🤖 IA Anomalías** → botón **Entrenar modelo**.

### 5. Acceder a las UIs

Desde la pestaña **Ports** de Codespaces o en local:

| Interfaz          | URL                         | Descripción                    |
|-------------------|-----------------------------|--------------------------------|
| Streamlit         | http://localhost:18501      | Dashboard principal            |
| FastAPI docs      | http://localhost:18000/docs | API interactiva (Swagger)      |
| JupyterLab        | http://localhost:18888      | Notebooks de análisis          |
| Flink UI          | http://localhost:18081      | Jobs y métricas Flink          |
| Redpanda Console  | http://localhost:18080      | Topics Kafka y mensajes        |
| InfluxDB          | http://localhost:18086      | Series temporales (hot path)   |
| MinIO Console     | http://localhost:19001      | Archivos Parquet (cold path)   |
| Grafana           | http://localhost:13000      | Dashboards (sin login)         |

### 6. JupyterLab (análisis ad-hoc)

```bash
nb   # equivale a: jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --ServerApp.token="" ...
# Abrir: notebooks/01_exploracion_datos.ipynb
```

El notebook cubre: hot path (InfluxDB), cold path (MinIO Parquet vía DuckDB), Lambda Query (UNION ALL), entrenamiento IsolationForest y verificación de hash-chain.

## Topics Kafka

| Topic               | Productor                 | Consumidor(es)                         | Contenido                                           |
|---------------------|---------------------------|----------------------------------------|-----------------------------------------------------|
| `sensors/telemetry` | sensor_simulator          | mqtt_to_redpanda_bridge                | MQTT raw (C/F/K, fallos posibles)                   |
| `sensors_raw`       | mqtt_to_redpanda_bridge   | flink_normalization, flink_hash_verifier | `{device_id, temperature, unit, ts, hash, prev_hash}` |
| `sensors_clean`     | flink_normalization_job   | flink_analytics_job, flink_to_minio    | `{device_id, temperature_c, unit_original, ts}`     |
| `sensors_verified`  | flink_hash_verifier_job   | —                                      | Mensajes con hash-chain íntegra                     |
| `sensors_invalid`   | flink_hash_verifier_job   | —                                      | DLQ: mensajes con hash roto o JSON inválido         |

## Jobs Flink

| Job                        | Entrada          | Salida                        | Descripción                          |
|----------------------------|------------------|-------------------------------|--------------------------------------|
| flink_normalization_job    | sensors_raw      | sensors_clean                 | Normaliza unidades a °C              |
| flink_analytics_job        | sensors_clean    | InfluxDB (machine_stats)      | Ventana 1 min, alertas > 80°C        |
| flink_hash_verifier_job    | sensors_raw      | sensors_verified/sensors_invalid | Verifica integridad SHA256        |
| flink_to_minio_job         | sensors_clean    | MinIO datalake/clean/         | Parquet particionado (cold path)     |

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
