# Ficha Técnica — ILERNA Smart-Industry PAC DES

## Servicios, Tecnologías y Puertos

| Capa          | Servicio            | Tecnología              | Puerto Externo | Puerto Interno | Descripción                                                   |
|---------------|---------------------|-------------------------|:--------------:|:--------------:|---------------------------------------------------------------|
| Generación    | `sensor_simulator`  | Python + Paho-MQTT      | N/A            | N/A            | Emula 5 máquinas enviando temperatura en C, F y K con fallos simulados. |
| Ingesta       | `mosquitto`         | MQTT Broker v2          | **11883**      | 1883           | Punto de entrada de telemetría industrial. Topic: `sensors/telemetry`. |
| Ingesta       | `mqtt_bridge`       | Python + Confluent-Kafka| N/A            | N/A            | Valida JSON, filtra esquema y publica en Redpanda. Clave Kafka = `device_id`. |
| Streaming     | `redpanda`          | Kafka API compatible    | **19092**      | 9092           | Motor de streaming de baja latencia. Topics: `sensors_raw`, `sensors_clean`, `sensors_invalid`. |
| Streaming     | `redpanda-console`  | Web UI                  | **18080**      | 8080           | Inspección de tópicos y mensajes JSON en tiempo real.         |
| Proceso       | `jobmanager`        | Apache Flink 1.18.1     | **18081**      | 8081           | Orquestador Flink. REST API + UI. Ejecuta los 4 jobs Python.  |
| Proceso       | `taskmanager`       | Apache Flink 1.18.1     | N/A            | N/A            | Worker Flink. Ejecuta las UDFs Python (2 task slots).         |
| Proceso       | `flink_hash_verifier_job` | PyFlink DataStream API | N/A      | N/A            | **A1**: Verifica SHA256 hash-chain por dispositivo. sensors_raw → sensors_verified / sensors_invalid. |
| Proceso       | `flink_to_minio_job`| PyFlink + S3A FileSystem| N/A            | N/A            | **A2**: Cold path Lambda. sensors_clean → MinIO Parquet particionado por año/mes/día/hora. |
| Storage       | `influxdb`          | InfluxDB 2.x            | **18086**      | 8086           | Series temporales. Measurement `machine_stats` (Hot Path).    |
| Storage       | `minio`             | S3 Object Storage       | **19000**      | 9000           | S3 API. Bucket `datalake/raw/` con archivos Parquet (Cold Path). |
| Storage       | `minio-console`     | MinIO Web UI            | **19001**      | 9001           | Consola de administración de buckets y objetos.               |
| Proceso       | `anomaly_model`     | scikit-learn IsolationForest | N/A       | N/A            | **A3**: Detección de anomalías ML. Singleton en FastAPI. Train vía `/model/train`, predict vía `/machines/{id}/predict`. |
| Servicio      | `fastapi`           | Python + Uvicorn        | **18000**      | 8000           | API REST: estado de máquinas, alertas, historial, predict (IsolationForest), model/train. |
| Analítica     | `streamlit`         | Python Framework        | **18501**      | 8501           | Dashboard: tiempo real, historial, alertas, análisis histórico. |
| Analítica     | `duckdb`            | OLAP In-process         | N/A            | N/A            | Motor SQL embebido. Lee Parquet desde MinIO vía extensión `httpfs`. |
| Visualización | `grafana`           | Grafana 12.x            | **13000**      | 3000           | Dashboards de infraestructura. Pendiente: configurar datasource InfluxDB. |
| IDE           | `jupyterlab`        | Jupyter                 | **18888**      | 8888           | Notebooks para exploración de datos y prototipado de IA.      |

---

## Topics Kafka (Redpanda)

| Topic              | Productor                   | Consumidor(es)                                | Descripción                                         |
|--------------------|-----------------------------|-----------------------------------------------|-----------------------------------------------------|
| `sensors/telemetry`| `sensor_simulator`          | `mqtt_bridge`                                 | Telemetría MQTT cruda (C/F/K, fallos posibles). Incluye `hash` y `prev_hash` SHA256. |
| `sensors_raw`      | `mqtt_bridge`               | `flink_hash_verifier_job`, `flink_normalization_job` | JSON validado + metadatos de ingesta. Con campos hash-chain. |
| `sensors_clean`    | `flink_normalization_job`   | `flink_analytics_job`, `kafka_to_minio`, `flink_to_minio_job` | Temperatura normalizada a °C, sin corruptos. Hot+Cold path. |
| `sensors_invalid`  | `flink_normalization_job`, `flink_hash_verifier_job` | *(monitorización / alertas de calidad)* | **DLQ**: mensajes rechazados con campo `reason`. |
| `sensors_verified` | `flink_hash_verifier_job`   | *(consumidores downstream que requieren integridad)* | **A1**: Mensajes con hash-chain SHA256 verificado. |

---

## Stack Tecnológico

| Categoría        | Tecnología / Librería         | Versión     | Uso en el proyecto                                    |
|------------------|-------------------------------|-------------|-------------------------------------------------------|
| Lenguaje         | Python                        | 3.11+       | Todos los scripts y jobs                              |
| Protocolo IoT    | MQTT v5                       | —           | Ingesta de telemetría de sensores                     |
| Protocolo Stream | Kafka Protocol                | —           | Transporte de mensajes entre etapas                   |
| Stream Engine    | Apache Flink (PyFlink)        | 1.18.1      | Normalización (UDF), ventanas, alertas                |
| Broker Kafka     | Redpanda                      | v25.3.x     | Sustituto de Kafka, compatible con API                |
| Time Series DB   | InfluxDB                      | 2.x         | Almacenamiento de métricas en tiempo real (hot path)  |
| Object Storage   | MinIO                         | latest      | Histórico en Parquet via S3 API (cold path)           |
| OLAP Engine      | DuckDB                        | latest      | Análisis batch sobre Parquet desde MinIO con `httpfs` |
| Web Framework    | FastAPI + Uvicorn             | latest      | API REST de servicio de datos                         |
| Dashboard        | Streamlit                     | latest      | Interfaz de usuario interactiva                       |
| Visualización    | Plotly                        | latest      | Gráficas interactivas en Streamlit y notebooks        |
| Dashboards Ops   | Grafana                       | 12.x        | Monitorización de infraestructura                     |
| Notebooks        | JupyterLab                    | latest      | Exploración y prototipado                             |
| Cliente MQTT     | paho-mqtt                     | ≥ 2.0       | `CallbackAPIVersion.VERSION2` en bridge y simulador   |
| Cliente Kafka    | confluent-kafka               | latest      | Productor/consumidor Python con idempotencia          |
| Cliente InfluxDB | influxdb-client               | latest      | Consultas Flux desde FastAPI y Streamlit              |
| Cliente MinIO    | minio                         | latest      | Operaciones S3 desde `kafka_to_minio.py`              |
| Serialización    | pyarrow                       | latest      | Escritura de archivos Parquet                         |
| Data Analysis    | pandas                        | latest      | Manipulación de DataFrames en Streamlit               |
| ML               | scikit-learn (IsolationForest)| latest      | **A3**: Detección de anomalías no supervisada en FastAPI. `contamination=0.1`, pickle persistence. |
| Infraestructura  | Docker + Docker Compose       | —           | Orquestación de todos los servicios                   |
| Dev Environment  | GitHub Codespaces / DevContainers | —       | Entorno de desarrollo reproducible en la nube         |

---

## Credenciales de acceso

| Servicio  | Usuario | Contraseña      | Token                  |
|-----------|---------|-----------------|------------------------|
| InfluxDB  | admin   | adminpassword   | `supersecrettoken`     |
| MinIO     | admin   | adminpassword   | —                      |
| Grafana   | admin   | admin           | —                      |
| MQTT      | —       | —               | anónimo (sin auth)     |

---

## Verificación de discrepancias respecto a la ficha original

Las siguientes diferencias entre la ficha de referencia y la implementación real han sido resueltas o anotadas:

| # | Ficha original           | Implementación real                                              | Estado       |
|---|--------------------------|------------------------------------------------------------------|--------------|
| 1 | `sensor_publisher`       | Archivo: `src/01_ingestion/sensor_simulator.py`                  | ✅ Equivalente (renombrado) |
| 2 | No menciona DLQ          | Topic `sensors_invalid` añadido. Flink usa `StatementSet` con dos `INSERT INTO` en el mismo job. | ✅ Añadido |
| 3 | `httpx` en librerías     | No se usa `httpx`. Flink usa `urllib.request` (stdlib) para InfluxDB. FastAPI usa Pydantic. | ℹ️ Nota: `httpx` no está instalado ni es necesario. |
| 4 | Grafana "monitorización" | Grafana está levantado pero **sin datasource de InfluxDB configurado**. | ⚠️ Pendiente: configurar datasource manual o via provisioning |
| 5 | `scikit-learn` en stack  | Implementado en `src/04_api/anomaly_model.py` como `AnomalyDetector(IsolationForest)`. Singleton en FastAPI. Endpoints: `POST /model/train`, `GET /model/status`, `GET /machines/{id}/predict`. | ✅ Implementado (Aportación A3) |
| 6 | Topic `sensors_raw`      | Schema enriquecido: añade `_mqtt_topic`, `_mqtt_qos`, `_ingested_at` | ✅ Superconjunto de lo especificado |
| 7 | Puerto MinIO "19000"     | S3 API en 19000, Console UI en 19001 (dos puertos distintos)     | ✅ Correcto |
| 8 | TaskManager sin puerto   | Correcto: el taskmanager no expone puerto externo                | ✅ Correcto |
| 9 | Hash-Chain no en ficha   | Añadido en `sensor_simulator.py` (campos `hash`/`prev_hash`) y `flink_hash_verifier_job.py`. Topic `sensors_verified` nuevo. | ✅ Aportación A1 |
| 10| Lambda cold path         | `flink_to_minio_job.py` escribe Parquet particionado en MinIO con S3A plugin. DuckDB en Streamlit lo consulta con `hive_partitioning=true`. | ✅ Aportación A2 |
| 11| Strelit solo 3 tabs      | Dashboard ampliado a 5 tabs: Tiempo Real, Historial, Alertas, Lambda Query, IA Anomalías. | ✅ Ampliado |

---

## Notas de configuración para Codespaces

**Red interna Docker**: Los servicios se comunican entre sí por nombre de servicio:
- `mosquitto:1883`, `redpanda:29092`, `influxdb:8086`, `minio:9000`, `jobmanager:8081`

**Acceso externo desde el host/Codespaces**: Puertos mapeados al rango `10000+` para evitar conflictos:
- Los scripts Python ejecutados en el `workspace` deben usar `localhost:<PUERTO_EXTERNO>`
- Los jobs Flink ejecutados en el contenedor `jobmanager` deben usar los nombres internos

**Autodetección de puertos en Codespaces**: Configurado `"autoForwardPorts": false` en `devcontainer.json`. Solo se exponen los puertos declarados explícitamente en `forwardPorts`.

**Persistencia de datos**: Volúmenes Docker nombrados para Redpanda, InfluxDB, MinIO y Grafana. Los datos sobreviven reinicios de contenedores pero se pierden si se ejecuta `docker volume prune`.

**Flink y Python**: El contenedor `flink:1.18.1-java11` necesita Python 3 y el JAR Kafka para ejecutar jobs PyFlink.

**Desde el siguiente rebuild de Codespace**: automático — `Dockerfile.jobmanager` (`.devcontainer/`) pre-instala Python 3, el Kafka SQL Connector JAR y el plugin S3/MinIO en la imagen. Ambos contenedores (`jobmanager` y `taskmanager`) usan la misma imagen personalizada.

**En la sesión actual** (sin rebuild): ejecutar una sola vez en AMBOS contenedores (jobmanager + taskmanager comparten la misma classpath):
```bash
for SVC in jobmanager taskmanager; do
  docker exec "$(docker ps -qf "label=com.docker.compose.service=${SVC}")" \
    bash /opt/flink/jobs/download_flink_jars.sh
done
docker restart $(docker ps -qf "label=com.docker.compose.service=jobmanager")
docker restart $(docker ps -qf "label=com.docker.compose.service=taskmanager")
```

**Auto-lanzado de jobs Flink**: `start.sh` incluye un paso que lanza automáticamente los tres jobs Flink (`normalization`, `hash_verifier`, `analytics`) al arrancar el Codespace, si no hay jobs ya corriendo.

---

## Aportaciones Avanzadas (resumen técnico)

| ID | Nombre                     | Archivos clave                                          | Descripción técnica                                                                                  |
|----|----------------------------|---------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| A1 | Hash-Chain SHA256          | `sensor_simulator.py`, `flink_hash_verifier_job.py`     | El simulador encadena mensajes con SHA256 (prev_hash→hash). Flink verifica la cadena con `ValueState` keyed por device_id. Mensajes íntegros → `sensors_verified`, tampered → `sensors_invalid`. |
| A2 | Lambda Architecture Parquet| `flink_to_minio_job.py`, `download_flink_jars.sh`, `app.py` | Cold path: Flink FileSystem connector + S3A plugin escribe Parquet SNAPPY en MinIO con particionado Hive (year/month/day/hour). DuckDB en Streamlit consulta ambos paths con UNION ALL. |
| A3 | IsolationForest ML         | `anomaly_model.py`, `main.py` (FastAPI), `app.py`       | Singleton `AnomalyDetector` con `IsolationForest(contamination=0.1)`. Entrena con histórico de InfluxDB. Devuelve `is_anomaly`, `failure_prob ∈ [0,1]`, `interpretation`. Pickle persistence en `/tmp/`. |
