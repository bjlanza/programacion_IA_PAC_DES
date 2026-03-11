# Smoke Test / Checklist — ILERNA Smart-Industry PAC DES

Valida que el entorno Docker y el pipeline de datos están **levantados y funcionales**.
Completa en orden: primero infraestructura, luego pipeline.

> **Puertos externos** (los que usa el host/Codespaces):
> MQTT=11883 · Kafka=19092 · Redpanda Console=18080 · Flink UI=18081
> InfluxDB=18086 · MinIO S3=19000 · MinIO UI=19001 · Grafana=13000
> FastAPI=18000 · Streamlit=18501 · Jupyter=18888

---

## Fase 0 — Requisitos previos

- [ ] Estoy dentro del Codespace (terminal integrada de VS Code).
- [ ] El script `start.sh` ha terminado sin errores (ver output del terminal al abrir).
- [ ] Existe `.devcontainer/docker-compose.yml`.
- [ ] Existe `config/mosquitto.conf` con `listener 1883 0.0.0.0` y `allow_anonymous true`.

---

## Fase 1 — Estado de contenedores Docker

```bash
docker ps --filter "label=com.docker.compose.project" \
  --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

Comprobar que están **Up** (healthy donde aplica):

- [ ] `mosquitto` — Up
- [ ] `redpanda` — Up, **healthy**
- [ ] `redpanda-console` — Up
- [ ] `jobmanager` — Up, **healthy**
- [ ] `taskmanager` — Up
- [ ] `influxdb` — Up, **healthy**
- [ ] `minio` — Up, **healthy**
- [ ] `grafana` — Up

Si alguno está `Exited` o `unhealthy`:
```bash
docker logs $(docker ps -qf "label=com.docker.compose.service=<SERVICIO>") --tail=50
```

---

## Fase 2 — Test automático de conectividad

```bash
bash tests/test_connectivity.sh
```

- [ ] Todos los checks Docker Health pasan ✅
- [ ] Todos los checks HTTP pasan ✅
- [ ] Todos los checks TCP pasan ✅
- [ ] Salida final: `Todos los checks pasaron (N/N)`

---

## Fase 3 — Verificación de UIs (navegador)

En Codespaces: pestaña **Ports** → abrir cada puerto en el navegador.

- [ ] **Redpanda Console** (18080) — se carga la lista de tópicos
- [ ] **Flink UI** (18081) — se ve el Overview y al menos 1 TaskManager registrado
- [ ] **InfluxDB** (18086) — pantalla de login, entrar con `admin` / `adminpassword`
- [ ] **MinIO Console** (19001) — pantalla de login, entrar con `admin` / `adminpassword`
- [ ] **Grafana** (13000) — pantalla de login, entrar con `admin` / `admin`

---

## Fase 4 — MQTT (Mosquitto)

### Instalar clientes CLI (una vez)
```bash
sudo apt-get update && sudo apt-get install -y mosquitto-clients
```

### Publish / Subscribe round-trip
Terminal A (suscriptor):
```bash
mosquitto_sub -h localhost -p 11883 -t "sensors/telemetry" -v
```

Terminal B (publicador):
```bash
mosquitto_pub -h localhost -p 11883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-001","temperature":75.0,"unit":"C","ts":"2026-01-01T00:00:00Z"}'
```

- [ ] En terminal A aparece el JSON publicado ✅

---

## Fase 5 — Redpanda / Kafka

### Crear tópicos del pipeline
```bash
RP=$(docker ps -qf "label=com.docker.compose.service=redpanda")
docker exec $RP rpk topic create sensors_raw      -p 1 -r 1
docker exec $RP rpk topic create sensors_clean    -p 1 -r 1
docker exec $RP rpk topic create sensors_invalid  -p 1 -r 1
docker exec $RP rpk topic create sensors_verified -p 1 -r 1
```

- [ ] `sensors_raw` creado (o ya existe) ✅
- [ ] `sensors_clean` creado (o ya existe) ✅
- [ ] `sensors_invalid` creado (o ya existe) ✅  ← DLQ normalización
- [ ] `sensors_verified` creado (o ya existe) ✅  ← Hash-Chain verificado

### Produce → Consume en sensors_raw
```bash
echo '{"device_id":"machine-test","temperature":176.0,"unit":"F","ts":"2026-01-01T00:00:00Z"}' | \
  docker exec -i $RP rpk topic produce sensors_raw

docker exec $RP rpk topic consume sensors_raw -n 1
```

- [ ] El JSON aparece en el consume ✅

### Verificar tópicos existentes
```bash
docker exec $RP rpk topic list
```

- [ ] `sensors_raw` en la lista ✅
- [ ] `sensors_clean` en la lista ✅

---

## Fase 6 — Flink (preparación)

### Verificar TaskManager registrado
```bash
curl -s http://localhost:18081/taskmanagers | python3 -m json.tool | grep -i "id\|slots"
```

- [ ] Aparece al menos 1 TaskManager con slots disponibles ✅

### Descargar JARs y configurar plugin S3 (una vez)
```bash
docker exec -it $(docker ps -qf "label=com.docker.compose.service=jobmanager") \
  bash /opt/flink/jobs/download_flink_jars.sh
```

- [ ] JAR Kafka connector descargado en `/opt/flink/lib/` ✅
- [ ] Plugin S3 copiado a `/opt/flink/plugins/flink-s3-fs-hadoop/` ✅
- [ ] Configuración S3/MinIO añadida a `flink-conf.yaml` ✅

> Tras ejecutar el script, reinicia el JobManager para cargar el plugin S3:
> ```bash
> docker restart $(docker ps -qf "label=com.docker.compose.service=jobmanager")
> # Espera ~15s y verifica que el UI vuelve a responder en http://localhost:18081
> ```

---

## Fase 7 — InfluxDB

### Verificar health
```bash
curl -f http://localhost:18086/ping && echo "OK"
```

- [ ] Responde `204 No Content` ✅

### Verificar bucket
```bash
curl -s -H "Authorization: Token supersecrettoken" \
  "http://localhost:18086/api/v2/buckets?org=ilerna" | python3 -m json.tool | grep '"name"'
```

- [ ] Aparece el bucket `sensores` ✅

---

## Fase 8 — MinIO (S3)

### Verificar health
```bash
curl -f http://localhost:19000/minio/health/live && echo "OK"
```

- [ ] Responde `200 OK` ✅

### Crear bucket datalake (si no existe)
```bash
python3 -c "
from minio import Minio
c = Minio('localhost:19000', access_key='admin', secret_key='adminpassword', secure=False)
if not c.bucket_exists('datalake'):
    c.make_bucket('datalake')
    print('Bucket datalake creado')
else:
    print('Bucket datalake ya existe')
"
```

- [ ] Bucket `datalake` existe ✅

---

## Fase 9 — Test end-to-end del pipeline Python

```bash
python tests/test_flow.py
```

- [ ] MQTT publish/subscribe ✅
- [ ] Redpanda topic creado ✅
- [ ] Redpanda produce ✅
- [ ] Redpanda consume ✅
- [ ] InfluxDB write ✅
- [ ] InfluxDB query ✅
- [ ] MinIO bucket ✅
- [ ] MinIO put object ✅
- [ ] MinIO list objects ✅
- [ ] Grafana health ✅
- [ ] Salida: `Todos los tests pasaron (N/N)` ✅

---

## Fase 10 — Pipeline completo de la práctica

Arranca el pipeline en este orden:

```bash
JM=$(docker ps -qf "label=com.docker.compose.service=jobmanager")

# 1. Simulador con hash-chaining (Terminal 1)
python src/01_ingestion/sensor_simulator.py --machines 5 --fault-rate 0.1

# 2. Bridge MQTT → Redpanda — Hito 1 (Terminal 2)
python src/01_ingestion/mqtt_to_redpanda_bridge.py

# 3. Flink Hash Verifier — Aportación A1 (lanzar primero, keyed state)
docker exec $JM flink run -py /opt/flink/jobs/flink_hash_verifier_job.py &

# 4. Flink Normalización — Hito 2 (esperar ~15s al job)
docker exec $JM flink run -py /opt/flink/jobs/flink_normalization_job.py &

# 5. Flink Analítica + Alertas — Hito 3
docker exec $JM flink run -py /opt/flink/jobs/flink_analytics_job.py &

# 6. Flink → MinIO Parquet — Aportación A2 Lambda Cold Path
docker exec $JM flink run -py /opt/flink/jobs/flink_to_minio_job.py &

# 7. FastAPI — Hito 4 (Terminal 3)
uvicorn src.04_api.main:app --host 0.0.0.0 --port 18000 --reload

# 8. Dashboard — Hito 4 (Terminal 4)
streamlit run src/05_ui/app.py --server.port 18501
```

### Verificaciones del pipeline activo

- [ ] El bridge muestra mensajes: `↦ machine-XXX | XX.XX C/F/K → sensors_raw`
- [ ] El bridge descarta mensajes corruptos con `WARNING Mensaje rechazado`
- [ ] En Flink UI (18081) → **Jobs** aparecen 4 jobs en estado RUNNING:
  - [ ] `sensor-hash-chain-verifier` (Aportación A1)
  - [ ] `sensor-normalization` (Hito 2)
  - [ ] `sensor-analytics-tumble-1min` (Hito 3)
  - [ ] `sensor-flink-to-minio` (Aportación A2)
- [ ] En Redpanda Console (18080) → tópico `sensors_clean` recibe mensajes
- [ ] En Redpanda Console (18080) → tópico `sensors_invalid` (DLQ) recibe mensajes corruptos
- [ ] En Redpanda Console (18080) → tópico `sensors_verified` recibe mensajes con hash íntegro
- [ ] En InfluxDB (18086) → measurement `machine_stats` tiene datos
- [ ] FastAPI `/health` → `{"status":"ok","influxdb":"ok"}`
- [ ] FastAPI `/machines/status` → lista de máquinas con temperatura y flag de alerta
- [ ] Dashboard Streamlit (18501) → gauges de temperatura visibles
- [ ] Dashboard pestaña "Alertas" → detecta máquinas sobre 80°C

---

## Fase 11 — Verificación de Aportaciones Avanzadas

### A1 — Hash-Chain (Seguridad)

```bash
RP=$(docker ps -qf "label=com.docker.compose.service=redpanda")

# Ver mensajes en sensors_verified (cadena íntegra)
docker exec $RP rpk topic consume sensors_verified -n 3

# Ver mensajes con cadena rota en sensors_invalid
docker exec $RP rpk topic consume sensors_invalid -n 3 | python3 -m json.tool | grep reason
```

- [ ] `sensors_verified` contiene mensajes con campos `hash` y `prev_hash` ✅
- [ ] `sensors_invalid` contiene mensajes con campo `reason: "hash_chain_broken..."` ✅

### A2 — Lambda Architecture (Cold Path Parquet)

```bash
# Esperar ~10 min para que Flink emita el primer archivo Parquet (rolling policy)
# Verificar objetos en MinIO:
python3 -c "
from minio import Minio
c = Minio('localhost:19000', access_key='admin', secret_key='adminpassword', secure=False)
objs = list(c.list_objects('datalake', prefix='clean/', recursive=True))
print(f'Archivos Parquet en MinIO: {len(objs)}')
for o in objs[:5]:
    print(' ', o.object_name)
"
```

- [ ] Bucket `datalake` contiene objetos bajo `clean/year=.../month=.../day=.../hour=.../` ✅
- [ ] En Streamlit pestaña "Lambda Query" → se puede ejecutar la consulta DuckDB ✅

### A3 — IsolationForest (Detección de Anomalías ML)

```bash
# Entrenar el modelo vía FastAPI (necesita datos en InfluxDB)
curl -s -X POST http://localhost:18000/model/train | python3 -m json.tool

# Ver estado del modelo
curl -s http://localhost:18000/model/status | python3 -m json.tool

# Predecir para una temperatura alta (posible anomalía)
curl -s "http://localhost:18000/machines/machine-001/predict?temperature_c=95.0" | python3 -m json.tool
```

- [ ] `/model/train` responde `{"trained": true, "samples": N, "stats": {...}}` ✅
- [ ] `/model/status` muestra `is_trained: true` y estadísticas del modelo ✅
- [ ] `/machines/{id}/predict` devuelve `is_anomaly`, `failure_prob`, `interpretation` ✅
- [ ] En Streamlit pestaña "IA Anomalías" → se puede entrenar y predecir desde la UI ✅

---

## Criterio de aceptación final

Marca solo si **todo lo anterior** está completado:

**Infraestructura base:**
- [ ] Los 8 contenedores Docker están Up/healthy
- [ ] Los 4 jobs Flink están RUNNING

**Pipeline central (Hitos 1-4):**
- [ ] El bridge valida y descarta mensajes corruptos correctamente
- [ ] Los datos fluyen: MQTT → Kafka → Flink → InfluxDB
- [ ] Las alertas se generan cuando `avg_temp_c > 80°C`
- [ ] La API devuelve estado de máquinas en tiempo real
- [ ] El dashboard muestra gauges y series temporales

**Aportaciones avanzadas:**
- [ ] A1 — Hash-Chain: `sensors_verified` recibe mensajes íntegros, `sensors_invalid` detecta cadena rota
- [ ] A2 — Lambda Cold Path: MinIO `datalake/clean/` contiene Parquet particionado, DuckDB puede consultarlo
- [ ] A3 — IsolationForest: modelo entrenado, predicción devuelve `failure_prob` y `interpretation`

---

## Apéndice — Comandos de recuperación

```bash
# Reiniciar todo el entorno Docker
docker compose -f .devcontainer/docker-compose.yml down --remove-orphans
docker compose -f .devcontainer/docker-compose.yml up -d

# Ver logs de un servicio
docker logs $(docker ps -qf "label=com.docker.compose.service=jobmanager") --tail=100 -f

# Listar todos los tópicos Kafka
docker exec $(docker ps -qf "label=com.docker.compose.service=redpanda") rpk topic list

# Ver jobs Flink activos
curl -s http://localhost:18081/jobs | python3 -m json.tool

# Cancelar un job Flink
curl -X PATCH http://localhost:18081/jobs/<JOB_ID>?mode=cancel

# Reiniciar solo el simulador con más fallos
python src/01_ingestion/sensor_simulator.py --machines 5 --fault-rate 0.3 --interval 1
```
