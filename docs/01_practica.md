# Guía de la Práctica — ILERNA Smart-Industry PAC DES

## Contexto

**ILERNA Smart-Industry** necesita monitorizar la temperatura de sus máquinas en tiempo real. Los sensores envían datos en diferentes unidades (Celsius, Fahrenheit, Kelvin) y a veces presentan fallos o intentos de manipulación. El objetivo es construir un pipeline de datos completo que recoja, limpie, analice y visualice esta información para detectar anomalías antes de que ocurra una avería.

---

## Arquitectura completa del sistema

```
┌─────────────────────────────────────────────────────────────────────┐
│  GENERACIÓN                                                          │
│  sensor_simulator.py                                                 │
│  · 5 máquinas | C/F/K aleatorio | fallos 5% | hash-chaining SHA256  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ MQTT QoS 1  topic: sensors/telemetry
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  INGESTA                                                             │
│  Mosquitto :11883  →  mqtt_to_redpanda_bridge.py                    │
│  · Valida schema (device_id, temperature, unit∈{C,F,K}, ts)        │
│  · Enriquece (_mqtt_topic, _ingested_at)                            │
│  · enable.idempotence=True | QoS 1 | clave Kafka = device_id       │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ Kafka  topic: sensors_raw
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  REDPANDA (Kafka API)  :19092                                        │
│  Topics: sensors_raw · sensors_clean · sensors_invalid              │
│          sensors_verified · sensors_aggregated                      │
└──────┬───────────────────┬───────────────────────────────────────────┘
       │                   │
       │ Flink Job A       │ Flink Job B (opcional, seguridad)
       ▼                   ▼
┌──────────────────┐  ┌───────────────────────────────────────┐
│ NORMALIZACIÓN    │  │ HASH VERIFIER                          │
│ Hito 2           │  │ · KeyedProcessFunction por device_id  │
│ Table API + UDF  │  │ · Verifica SHA256 chain por máquina   │
│ to_celsius(F,K→C)│  │ · Válidos → sensors_verified           │
│ DLQ StatementSet │  │ · Roto/Tampered → sensors_invalid(DLQ)│
│ ✅sensors_clean  │  └───────────────────────────────────────┘
│ ❌sensors_invalid│
└──────┬───────────┘
       │ Kafka  topic: sensors_clean
       ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ANALÍTICA  Hito 3 — flink_analytics_job.py                         │
│  · Tumble Window 1 min por device_id (watermark 10s)                │
│  · AVG/MAX temperature_c + count                                    │
│  · ALERTA si avg_temp_c > 80°C (flag alert=1)                       │
│  · Sink: InfluxDB via HTTP Line Protocol (urllib, sin deps extras)   │
└──────────────────────────┬──────────────────────────────────────────┘
       ┌────────────────────┴─────────────────┐
       │ measurement: machine_stats            │ Flink Job C (cold path)
       ▼                                      ▼
┌──────────────────┐              ┌───────────────────────────────────┐
│  HOT PATH        │              │  COLD PATH                         │
│  InfluxDB :18086 │              │  flink_to_minio_job.py             │
│  bucket: sensores│              │  FileSystem connector              │
│  (tiempo real)   │              │  Parquet SNAPPY particionado:      │
└──────────────────┘              │  s3a://datalake/clean/             │
       │                          │  year=.../month=.../day=.../hour=/│
       │                          │  MinIO :19000                      │
       │                          └───────────────────────────────────┘
       │                                       │
       └─────────────────┬─────────────────────┘
                         │ Arquitectura Lambda: Hot + Cold
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SERVICIO  Hito 4                                                    │
│  ┌───────────────────────────┐  ┌──────────────────────────────────┐│
│  │ FastAPI :18000             │  │ Streamlit :18501                 ││
│  │ /machines/status           │  │ 📡 Tiempo Real (gauges)          ││
│  │ /machines/{id}             │  │ 📈 Historial (series temporales) ││
│  │ /machines/{id}/predict ←IA │  │ ⚠️  Alertas (scatter)           ││
│  │ /alerts                    │  │ 🗄️  Lambda Query (DuckDB UNION) ││
│  │ /model/train               │  │ 🤖 IA Anomalías (IsolationForest)││
│  │ /model/status              │  └──────────────────────────────────┘│
│  └───────────────────────────┘                                       │
│  IsolationForest (scikit-learn) — entrenado con datos de InfluxDB   │
│  Edge: Flink detecta avg>80°C | Cloud: ML detecta anomalías raras   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Guía paso a paso — Puesta en marcha completa

### Paso 0 — Verificar el entorno Docker

```bash
# Comprobar que todos los contenedores están healthy
bash tests/test_connectivity.sh

# Si alguno falla, reiniciar el entorno:
docker compose -f .devcontainer/docker-compose.yml down --remove-orphans
docker compose -f .devcontainer/docker-compose.yml up -d
```

### Paso 1 — Preparar Flink

Automático — `Dockerfile.jobmanager` incluye el JAR Kafka, Python 3 y el plugin S3. `start.sh` lanza los jobs al arrancar.

### Paso 2 — Crear los topics Kafka

Automático — `start.sh` crea los topics al arrancar. Para verificar:

```bash
RP=$(docker ps -qf "label=com.docker.compose.service=redpanda")
docker exec $RP rpk topic list
```

### Paso 3 — Crear bucket MinIO para el cold path

Automático — `start.sh` crea el bucket `datalake` al arrancar. Para verificar:

```bash
python3 -c "
from minio import Minio
c = Minio('minio:9000', access_key='admin', secret_key='Ilerna_Programaci0n', secure=False)
if c.bucket_exists('datalake'):
    print('Bucket datalake existe')
else:
    print('Bucket datalake NO existe')
"
```

### Paso 4 — Lanzar los jobs Flink

```bash
JM=$(docker ps -qf "label=com.docker.compose.service=jobmanager")

# Hito 2: Normalización + DLQ
docker exec $JM flink run -py /opt/flink/jobs/flink_normalization_job.py &

# Seguridad: Hash verifier
docker exec $JM flink run -py /opt/flink/jobs/flink_hash_verifier_job.py &

# Hito 3: Analítica + alertas → InfluxDB
docker exec $JM flink run -py /opt/flink/jobs/flink_analytics_job.py &

# Cold path: Flink → MinIO Parquet
docker exec $JM flink run -py /opt/flink/jobs/flink_to_minio_job.py &

# Verificar que los 4 jobs están RUNNING
curl -s http://localhost:18081/jobs | python3 -m json.tool
```

### Paso 5 — Arrancar el pipeline Python

Abre terminales separadas en Codespaces (botón `+` en el panel de terminales):

```bash
# Terminal 1 — Simulador con hash-chaining + fallos
python src/01_ingestion/sensor_simulator.py --machines 5 --interval 2 --fault-rate 0.1

# Terminal 2 — Bridge MQTT → Redpanda (Hito 1)
python src/01_ingestion/mqtt_to_redpanda_bridge.py

# Terminal 3 — Archivador Python → MinIO (fallback del cold path)
python src/03_storage/kafka_to_minio.py

# Terminal 4 — FastAPI (Hito 4)
uvicorn src.04_api.main:app --host 0.0.0.0 --port 18000 --reload

# Terminal 5 — Dashboard Streamlit (Hito 4)
streamlit run src/05_ui/app.py --server.port 18501
```

### Paso 6 — Entrenar el modelo de IA

```bash
# Esperar al menos 2-3 minutos para acumular datos en InfluxDB
curl -X POST "http://localhost:18000/model/train?range_minutes=5&contamination=0.1"

# Verificar estado del modelo
curl -s http://localhost:18000/model/status | python3 -m json.tool
```

### Paso 7 — Verificar el sistema completo

```bash
# Estado de máquinas
curl -s http://localhost:18000/machines/status | python3 -m json.tool

# Alertas activas
curl -s http://localhost:18000/alerts | python3 -m json.tool

# Predicción IA para machine-004 (la que suele superar 80°C)
curl -s "http://localhost:18000/machines/machine-004/predict?temperature_c=85.0" | python3 -m json.tool

# Ver DLQ — mensajes rechazados
docker exec $(docker ps -qf "label=com.docker.compose.service=redpanda") \
  rpk topic consume sensors_invalid -n 5
```

### Paso 8 — Abrir el dashboard

En la pestaña **Ports** de Codespaces, abre el puerto **18501** (Streamlit).

Navega por las pestañas en orden:
1. **📡 Tiempo Real** → ver gauges de las 5 máquinas
2. **📈 Historial** → ver serie temporal de temperatura
3. **⚠️ Alertas** → verificar que machine-004 genera alertas
4. **🗄️ Lambda Query** → clic en "Ejecutar Query Federada" y ver unión hot+cold
5. **🤖 IA Anomalías** → entrenar modelo y hacer predicciones

---

## Hito 1 — El Puente de Ingesta

**Archivo:** `src/01_ingestion/mqtt_to_redpanda_bridge.py`

### Objetivo
Construir un cliente que actúe de puente entre el broker MQTT y Redpanda (Kafka), con validación de mensajes.

### Schema del mensaje (topic `sensors/telemetry`)

Con hash-chaining activo (el simulador añade `prev_hash` y `hash`):

```json
{
  "device_id":   "machine-001",
  "temperature": 176.5,
  "unit":        "F",
  "ts":          "2026-03-11T09:00:00Z",
  "prev_hash":   "a3f8c2...",
  "hash":        "d7e91b..."
}
```

Campos obligatorios validados por el bridge: `device_id` (str no vacío), `temperature` (numérico), `unit` ∈ {C, F, K}, `ts` (presente).

### Funcionamiento

1. **Conexión MQTT** con `CallbackAPIVersion.VERSION2` (paho-mqtt ≥ 2.0) y QoS 1.
2. **Validación** en `on_message`: rechaza mensajes con JSON malformado, campos faltantes o unidades no reconocidas (log warning, no crash).
3. **Enriquecimiento**: añade `_mqtt_topic`, `_mqtt_qos`, `_ingested_at` (ms).
4. **Producción a Kafka** con `enable.idempotence=True`. La clave Kafka es `device_id` para garantizar orden por dispositivo.

### Cómo probar

```bash
# Publicar mensaje válido con hash
mosquitto_pub -h localhost -p 11883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-001","temperature":176.0,"unit":"F","ts":"2026-03-11T00:00:00Z","prev_hash":"0000000000000000000000000000000000000000000000000000000000000000","hash":"abc123"}'

# Publicar mensaje corrupto (sin temperature)
mosquitto_pub -h localhost -p 11883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-001","unit":"C","ts":"2026-03-11T00:00:00Z"}'
# → WARNING | Mensaje rechazado — campo 'temperature' ausente

# Ver lo que llegó a Redpanda
docker exec $(docker ps -qf "label=com.docker.compose.service=redpanda") \
  rpk topic consume sensors_raw -n 1
```

---

## Hito 2 — Normalización con Flink y UDF

**Archivo:** `jobs/flink_normalization_job.py`

### Objetivo
Usar Apache Flink Table API con una UDF Python para limpiar, normalizar y enrutar los datos.

### UDF `to_celsius`

```python
@udf(result_type=DataTypes.DOUBLE())
def to_celsius(temperature: float, unit: str) -> float:
    unit = unit.strip().upper()
    if unit == "F":
        return (temperature - 32.0) * 5.0 / 9.0
    if unit == "K":
        return temperature - 273.15
    return float(temperature)  # "C" → pass-through
```

### Conversiones de referencia

| Unidad | Fórmula → Celsius    | Ejemplo          |
|--------|----------------------|------------------|
| F      | `(F - 32) × 5 / 9`  | 176°F → 80.0°C   |
| K      | `K - 273.15`         | 353.15K → 80.0°C |
| C      | pass-through         | 80°C → 80.0°C    |

### Dead Letter Queue (DLQ)

En lugar de descartar silenciosamente los mensajes inválidos, el job los enruta al topic `sensors_invalid` con un campo `reason` que explica el motivo de rechazo. Esto permite auditoría y reprocess posterior.

El job usa `StatementSet` para ejecutar **dos INSERT INTO en el mismo job Flink** de forma atómica:

```python
stmt_set = t_env.create_statement_set()
stmt_set.add_insert_sql("INSERT INTO sensors_clean  SELECT ... FROM sensors_raw WHERE <válidos>")
stmt_set.add_insert_sql("INSERT INTO sensors_invalid SELECT ... FROM sensors_raw WHERE <inválidos>")
stmt_set.execute()
```

Cada mensaje va a **exactamente uno** de los dos sinks.

Ejemplos de mensajes en `sensors_invalid`:
```json
{"device_id": null,       "temperature": 75.0, "unit": "C",       "reason": "device_id ausente o vacío"}
{"device_id": "m-001",   "temperature": null,  "unit": "C",       "reason": "temperature ausente o no numérico"}
{"device_id": "m-fault", "temperature": 75.0,  "unit": "RANKINE", "reason": "unidad no reconocida: RANKINE"}
{"device_id": "m-001",   "temperature": 99999, "unit": "C",       "reason": "temperatura sobre máximo: 99999.00C"}
```

### Filtros para `sensors_clean`

- `device_id IS NOT NULL AND device_id <> ''`
- `temperature IS NOT NULL`
- `UPPER(TRIM(unit)) IN ('C', 'F', 'K')`
- `to_celsius(temperature, unit) >= -50.0`
- `to_celsius(temperature, unit) <= 1000.0`

### Schema de salida (topic `sensors_clean`)

```json
{
  "device_id":      "machine-001",
  "temperature_c":  80.28,
  "unit_original":  "F",
  "ts":             "2026-03-11T09:00:00Z",
  "_ingested_at":   1741687200000
}
```

### Cómo ejecutar y verificar

```bash
JM=$(docker ps -qf "label=com.docker.compose.service=jobmanager")

# Lanzar
docker exec $JM flink run -py /opt/flink/jobs/flink_normalization_job.py

# Verificar en Flink UI
curl -s http://localhost:18081/jobs | python3 -m json.tool

# Ver mensajes válidos normalizados
docker exec $(docker ps -qf "label=com.docker.compose.service=redpanda") \
  rpk topic consume sensors_clean -n 3

# Ver mensajes rechazados (DLQ)
docker exec $(docker ps -qf "label=com.docker.compose.service=redpanda") \
  rpk topic consume sensors_invalid -n 3
```

---

## Hito 3 — Inteligencia y Alertas

**Archivo:** `jobs/flink_analytics_job.py`

### Objetivo
Calcular estadísticas por ventana de tiempo y generar alertas cuando la temperatura media supera 80°C.

### Tumble Window de 1 minuto

```sql
SELECT
    device_id,
    TUMBLE_START(event_time, INTERVAL '1' MINUTE) AS window_start,
    TUMBLE_END(event_time,   INTERVAL '1' MINUTE) AS window_end,
    AVG(temperature_c)                             AS avg_temp_c,
    MAX(temperature_c)                             AS max_temp_c,
    COUNT(*)                                       AS count_readings
FROM sensors_clean
WHERE temperature_c IS NOT NULL
GROUP BY device_id, TUMBLE(event_time, INTERVAL '1' MINUTE)
```

**Watermark**: 10 segundos de tolerancia a mensajes tardíos.

### Lógica de alerta

El campo `alert` en InfluxDB vale `1` si `avg_temp_c > 80.0` (configurable con `ALERT_THRESHOLD`).

### Escritura en InfluxDB (HTTP Line Protocol)

```
machine_stats,device_id=machine-001 avg_temp_c=82.34,max_temp_c=87.12,count=28i,alert=1i 1741687260
```

El job usa `urllib.request` (stdlib Python, sin dependencias externas en el contenedor Flink).

### Cómo verificar

```bash
# Datos en InfluxDB
curl -s -X POST "http://localhost:18086/api/v2/query?org=ilerna" \
  -H "Authorization: Token supersecrettoken" \
  -H "Content-Type: application/vnd.flux" \
  -d 'from(bucket:"sensores") |> range(start:-5m) |> filter(fn:(r) => r._measurement=="machine_stats") |> last()'

# Alertas via FastAPI
curl -s http://localhost:18000/alerts | python3 -m json.tool
```

---

## Hito 4 — API de Servicio y Dashboard

### FastAPI — `src/04_api/main.py`

```bash
uvicorn src.04_api.main:app --host 0.0.0.0 --port 8000 --reload
# Docs interactivos: http://localhost:18000/docs
```

#### Endpoints completos

| Método | Endpoint                      | Descripción                                         |
|--------|-------------------------------|-----------------------------------------------------|
| GET    | `/health`                     | Estado API + conectividad InfluxDB                  |
| GET    | `/machines/status`            | Última temperatura y alerta por máquina             |
| GET    | `/machines/{id}`              | Historial de temperatura de una máquina             |
| GET    | `/machines/{id}/predict`      | Predicción IsolationForest (IA Cloud)               |
| GET    | `/alerts`                     | Ventanas donde avg_temp_c superó el umbral          |
| GET    | `/stats`                      | Estadísticas (media, min, max) por máquina          |
| POST   | `/machines/publish`           | Publica lectura manual vía MQTT                     |
| POST   | `/model/train`                | Entrena IsolationForest con datos de InfluxDB       |
| GET    | `/model/status`               | Estado del modelo ML                                |

#### Ejemplos de uso

```bash
# Ver estado de todas las máquinas
curl -s http://localhost:18000/machines/status | python3 -m json.tool

# Publicar lectura manual (200°F = 93.3°C → generará alerta)
curl -X POST http://localhost:18000/machines/publish \
  -H "Content-Type: application/json" \
  -d '{"device_id":"machine-001","temperature":200.0,"unit":"F"}'

# Entrenar modelo con los últimos 10 minutos
curl -X POST "http://localhost:18000/model/train?range_minutes=10&contamination=0.1"

# Predecir si 85°C es anómalo para machine-004
curl -s "http://localhost:18000/machines/machine-004/predict?temperature_c=85.0"
```

---

### Streamlit Dashboard — `src/05_ui/app.py`

```bash
streamlit run src/05_ui/app.py --server.port 8501
# Abrir: http://localhost:18501
```

#### Pestañas del dashboard

**📡 Tiempo Real**
- Gauges de temperatura por máquina (verde = normal, rojo = alerta)
- Auto-refresco cada 5 segundos

**📈 Historial**
- Línea temporal de temperatura media por minuto
- Línea de umbral a 80°C

**⚠️ Alertas**
- Scatter plot de eventos sobre 80°C con timestamp y máquina

**🗄️ Lambda Query** (Arquitectura Lambda)

Botón que lanza una **query federada con DuckDB**:
1. Consulta InfluxDB (hot path) → `pandas.DataFrame`
2. Lee Parquet de MinIO (cold path) → `pandas.DataFrame`
3. DuckDB registra ambos DataFrames como tablas virtuales y hace `UNION ALL`
4. Muestra gráfica unificada coloreada por fuente y estadísticas cruzadas

```python
conn.register("hot_data",  hot_df)   # InfluxDB → RAM
conn.register("cold_data", cold_df)  # MinIO Parquet → RAM
result = conn.execute("""
    SELECT device_id, value, ts, 'InfluxDB (hot)' AS source FROM hot_data
    UNION ALL
    SELECT device_id, value, ts, 'MinIO (cold)'   AS source FROM cold_data
    ORDER BY ts DESC LIMIT 5000
""").df()
```

**🤖 IA Anomalías**
- Panel de entrenamiento del modelo (selección de ventana + tasa de contaminación)
- Predictor interactivo: introduce temperatura → obtén `is_anomaly` + `failure_prob`
- Análisis masivo: evalúa todo el histórico reciente y colorea por probabilidad de fallo

---

## Aportaciones avanzadas

### A1 — Hash-Chaining SHA256 (Seguridad e Integridad)

**Archivos:** `sensor_simulator.py` + `jobs/flink_hash_verifier_job.py`

#### Concepto

Cada mensaje incluye un hash SHA256 que depende del contenido del mensaje y del hash del mensaje anterior. Esto forma una cadena criptográfica (similar a blockchain) que hace detectables las manipulaciones.

```
msg_1: { ..., prev_hash="0"×64,   hash=SHA256(content_1 + "0"×64) }
msg_2: { ..., prev_hash=hash_1,   hash=SHA256(content_2 + hash_1) }
msg_3: { ..., prev_hash=hash_2,   hash=SHA256(content_3 + hash_2) }
```

Si un atacante modifica `msg_2`, su hash cambia y `msg_3` detecta que `prev_hash` declarado ≠ hash real de `msg_2`.

#### Implementación en el simulador

```python
_chain_state: dict[str, str] = {}   # device_id → último hash

def compute_hash(payload: dict, prev_hash: str) -> str:
    content = {k: v for k, v in payload.items() if k not in ("hash", "prev_hash")}
    raw = json.dumps(content, sort_keys=True) + prev_hash
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def sign_message(payload: dict, device_id: str) -> dict:
    prev_hash = _chain_state.get(device_id, "0" * 64)
    payload["prev_hash"] = prev_hash
    payload["hash"]      = compute_hash(payload, prev_hash)
    _chain_state[device_id] = payload["hash"]
    return payload
```

#### Verificación en Flink (DataStream + estado)

```python
class HashChainVerifier(KeyedProcessFunction):
    def open(self, runtime_context):
        self.last_hash_state = runtime_context.get_state(
            ValueStateDescriptor("last_valid_hash", Types.STRING())
        )

    def process_element(self, raw_msg, ctx, out):
        data         = json.loads(raw_msg)
        state_hash   = self.last_hash_state.value() or "0" * 64
        reported_prev = data.get("prev_hash", state_hash)
        reported_hash = data.get("hash", "")

        # Verificación 1: ¿prev_hash coincide con el estado de Flink?
        if reported_prev != state_hash:
            data["reason"] = "hash_chain_broken: prev_hash no coincide"
            ctx.output(TAMPERED_TAG, json.dumps(data))   # → DLQ
            return

        # Verificación 2: ¿el hash es correcto?
        expected = compute_hash(data, reported_prev)
        if reported_hash != expected:
            data["reason"] = "hash_chain_broken: hash incorrecto"
            ctx.output(TAMPERED_TAG, json.dumps(data))   # → DLQ
            return

        self.last_hash_state.update(reported_hash)
        out.collect(raw_msg)                             # → sensors_verified
```

#### Cómo ejecutar y verificar

```bash
JM=$(docker ps -qf "label=com.docker.compose.service=jobmanager")
docker exec $JM flink run -py /opt/flink/jobs/flink_hash_verifier_job.py

# Simular una rotura deliberada de cadena
python src/01_ingestion/sensor_simulator.py --machines 1 --fault-rate 0.5 --once

# Ver mensajes detectados como manipulados
docker exec $(docker ps -qf "label=com.docker.compose.service=redpanda") \
  rpk topic consume sensors_invalid -n 5

# Los mensajes válidos van a sensors_verified
docker exec $(docker ps -qf "label=com.docker.compose.service=redpanda") \
  rpk topic consume sensors_verified -n 3
```

---

### A2 — Arquitectura Lambda con Flink → MinIO Parquet

**Archivo:** `jobs/flink_to_minio_job.py`

#### Concepto

La Arquitectura Lambda divide el procesamiento en dos caminos paralelos:

| Path | Latencia | Tecnología | Uso |
|------|----------|-----------|-----|
| **Hot** | segundos | Flink → InfluxDB | Alertas, monitorización en tiempo real |
| **Cold** | minutos | Flink → MinIO/Parquet | Análisis histórico, ML, reportes |

Ambos paths se combinan en la capa de consulta (DuckDB) para obtener una vista unificada.

#### Particionado Hive

Flink escribe Parquet particionado por tiempo del evento (no de procesamiento):

```
s3a://datalake/clean/
  year=2026/
    month=03/
      day=11/
        hour=09/
          part-0001.parquet
          part-0002.parquet
```

DuckDB puede aprovechar este particionado para filtros eficientes:
```sql
SELECT * FROM read_parquet('s3://datalake/clean/**/*.parquet', hive_partitioning=true)
WHERE year='2026' AND month='03'   -- Solo lee las particiones necesarias
```

#### Query federada Lambda en Streamlit

```python
# Hot path: últimos 60 min de InfluxDB
hot_df  = query_influxdb(range_minutes=60)

# Cold path: histórico de MinIO (puede ser semanas/meses)
cold_df = query_minio_parquet()

# DuckDB une ambas fuentes en memoria (UNION ALL)
conn = duckdb.connect()
conn.register("hot",  hot_df)
conn.register("cold", cold_df)
unified = conn.execute("""
    SELECT device_id, value, ts, 'InfluxDB (hot)' AS source FROM hot
    UNION ALL
    SELECT device_id, value, ts, 'MinIO (cold)'   AS source FROM cold
    ORDER BY ts DESC
""").df()
```

#### Cómo ejecutar

```bash
JM=$(docker ps -qf "label=com.docker.compose.service=jobmanager")
# Plugin S3 ya incluido en Dockerfile.jobmanager
docker exec $JM flink run -py /opt/flink/jobs/flink_to_minio_job.py

# Verificar que se crean archivos en MinIO
python3 -c "
from minio import Minio
c = Minio('minio:9000', access_key='admin', secret_key='Ilerna_Programaci0n', secure=False)
objects = list(c.list_objects('datalake', prefix='clean/', recursive=True))
print(f'{len(objects)} archivos Parquet en MinIO')
for o in objects[:5]:
    print(' ', o.object_name, f'({o.size/1024:.1f} KB)')
"
```

---

### A3 — IA Cloud: Detección de Anomalías con IsolationForest

**Archivos:** `src/04_api/anomaly_model.py` + `src/04_api/main.py`

#### Edge vs Cloud Intelligence

| Nivel | Dónde | Tecnología | Qué detecta | Latencia |
|-------|-------|-----------|-------------|----------|
| **Edge** | Flink (streaming) | Regla hardcoded | avg_temp > 80°C | ms |
| **Cloud** | FastAPI | IsolationForest (ML) | Comportamiento estadísticamente raro | ~100ms |

La detección Edge es rápida pero rígida. La detección Cloud es flexible: aprende el patrón normal de cada máquina y detecta anomalías aunque no superen el umbral fijo (por ejemplo, una temperatura inusualmente baja en una máquina que siempre estuvo caliente).

#### IsolationForest: cómo funciona

```
1. Entrenamiento (POST /model/train):
   · Lee N temperaturas de InfluxDB (histórico real)
   · Construye 100 árboles de decisión aleatorios
   · Los puntos normales necesitan muchos splits para ser aislados
   · Los puntos anómalos son aislados rápidamente (pocas particiones)

2. Predicción (GET /machines/{id}/predict?temperature_c=85.0):
   · score_samples() → float negativo
   · Más negativo = más anómalo (aislado con pocas particiones)
   · Se normaliza a failure_prob ∈ [0.0, 1.0]
```

#### Flujo completo de predicción

```bash
# 1. Entrenar con 10 minutos de datos reales
curl -X POST "http://localhost:18000/model/train?range_minutes=10"
# → {"trained": true, "samples": 1200, "stats": {"mean": 72.4, "std": 8.1, ...}}

# 2. Predecir temperatura normal (dentro del rango histórico)
curl -s "http://localhost:18000/machines/machine-003/predict?temperature_c=58.0"
# → {"is_anomaly": false, "failure_prob": 0.02, "interpretation": "Temperatura normal..."}

# 3. Predecir temperatura anómala (alta)
curl -s "http://localhost:18000/machines/machine-003/predict?temperature_c=95.0"
# → {"is_anomaly": true, "failure_prob": 0.87, "interpretation": "Temperatura excepcionalmente ALTA..."}
```

---

## Flows de datos completos (schemas por topic)

### `sensors/telemetry` (MQTT)
```json
{
  "device_id":   "machine-001",
  "temperature": 176.5,
  "unit":        "F",
  "ts":          "2026-03-11T09:00:00Z",
  "prev_hash":   "a3f8c2d1...",
  "hash":        "d7e91b3f..."
}
```

### `sensors_raw` (Redpanda — enriquecido por el bridge)
```json
{
  "device_id":    "machine-001",
  "temperature":  176.5,
  "unit":         "F",
  "ts":           "2026-03-11T09:00:00Z",
  "prev_hash":    "a3f8c2d1...",
  "hash":         "d7e91b3f...",
  "_mqtt_topic":  "sensors/telemetry",
  "_mqtt_qos":    1,
  "_ingested_at": 1741687200000
}
```

### `sensors_clean` (Redpanda — normalizado por Flink Hito 2)
```json
{
  "device_id":      "machine-001",
  "temperature_c":  80.28,
  "unit_original":  "F",
  "ts":             "2026-03-11T09:00:00Z",
  "_ingested_at":   1741687200000
}
```

### `sensors_invalid` (Redpanda — DLQ: rechazados + manipulados)
```json
{
  "device_id":   "machine-fault",
  "temperature": null,
  "unit":        "C",
  "ts":          "2026-03-11T09:00:00Z",
  "reason":      "temperature ausente o no numérico"
}
```

### `sensors_verified` (Redpanda — cadena hash verificada por Flink)
Mismo schema que `sensors_raw` + garantía de integridad criptográfica.

### `machine_stats` (InfluxDB — escrito por Flink Hito 3)
```
measurement: machine_stats
tags:    device_id=machine-001
fields:  avg_temp_c=82.34, max_temp_c=87.12, count=28, alert=1
time:    1741687260  (inicio de ventana en segundos Unix)
```

### `datalake/clean/year=2026/month=03/day=11/hour=09/*.parquet` (MinIO)
```
Columnas: device_id, temperature_c, unit_original, ts, _ingested_at, year, month, day, hour
Formato:  Parquet + SNAPPY compression
Escrito por: flink_to_minio_job.py (particionado por event_time)
```

---

## Preguntas frecuentes

**¿Por qué hash-chaining en el simulador y no en el bridge?**
El hash debe generarse en el origen del dato (Edge), no en el transporte. Si el bridge firmara los mensajes, no podría detectar manipulaciones ocurridas entre el sensor y el bridge. La firma en el sensor garantiza autenticidad desde el origen.

**¿Por qué Flink usa `ValueState` keyed por `device_id` para el hash verifier?**
Cada máquina tiene su propia cadena de hashes independiente. Si usáramos estado global, el mensaje 2 de machine-001 dependería del hash de machine-002, lo que es incorrecto. El estado keyed garantiza que cada cadena se verifica de forma aislada.

**¿Por qué dos jobs para hot/cold path en lugar de uno?**
El hot path (InfluxDB) necesita baja latencia y escribe registro por registro. El cold path (MinIO/Parquet) necesita alta eficiencia y escribe por lotes/particiones. Mezclarlos en un job crearía presión entre ambas latencias. La separación permite escalar cada path independientemente.

**¿Por qué IsolationForest y no un umbral fijo en la API?**
Un umbral fijo (>80°C) no captura contexto histórico de cada máquina. machine-003 normalmente va a 55°C; si sube a 70°C, no supera el umbral pero es estadísticamente anómalo para esa máquina específica. IsolationForest aprende el "rango normal" de cada dispositivo, y el modelo entrenado sobre todos los datos lo captura globalmente.

**¿Qué pasa si el JSON está malformado?**
Primera línea de defensa: el bridge lo descarta con `WARNING JSON inválido`.
Segunda línea: Flink normalization tiene `json.ignore-parse-errors=true` → campos NULL → DLQ con `reason`.
Tercera línea: el hash verifier detecta cualquier manipulación post-ingesta.

**¿Por qué DuckDB en lugar de Spark para el análisis histórico?**
DuckDB es un motor OLAP embebido (sin servidor), perfecto para análisis interactivos desde Streamlit. Lee Parquet desde S3/MinIO directamente con `httpfs` sin descargar los ficheros completos. Para datasets > 100 GB o procesamiento distribuido se usaría Spark o Trino.

---

## Dependencias Python

| Módulo | Librería | Versión | Uso |
|--------|---------|---------|-----|
| sensor_simulator | `paho-mqtt` | ≥2.0 | MQTT con CallbackAPIVersion.VERSION2 |
| sensor_simulator | `hashlib` | stdlib | SHA256 hash-chaining |
| mqtt_bridge | `paho-mqtt` | ≥2.0 | Suscripción MQTT |
| mqtt_bridge | `confluent-kafka` | latest | Productor Kafka idempotente |
| Flink jobs | `apache-flink` | 1.18.1 | PyFlink (en contenedor) |
| Flink analytics | `urllib.request` | stdlib | HTTP a InfluxDB sin deps extras |
| kafka_to_influx | `influxdb-client` | latest | Escritura SYNCHRONOUS |
| kafka_to_minio | `minio` | latest | S3 API |
| kafka_to_minio | `pyarrow` | latest | Serialización Parquet |
| FastAPI | `fastapi`, `uvicorn` | latest | API REST asíncrona |
| FastAPI | `influxdb-client` | latest | Consultas Flux |
| FastAPI | `scikit-learn` | latest | IsolationForest |
| FastAPI | `numpy` | ≥1.24 | Arrays para ML |
| Streamlit | `streamlit` | latest | Dashboard interactivo |
| Streamlit | `duckdb` | latest | Query federada Lambda |
| Streamlit | `plotly` | latest | Gráficas interactivas |
| Streamlit | `requests` | stdlib | Llamadas a FastAPI |
| Notebooks | `jupyterlab` | latest | Exploración de datos |
