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
- [ ] El script `start.sh` ha terminado mostrando `Infraestructura lista` (postStartCommand automático).
- [ ] He ejecutado `bash .devcontainer/init_pipeline.sh` y muestra `Pipeline listo`.
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
- [ ] **InfluxDB** (18086) — pantalla de login, entrar con `admin` / `Ilerna_Programaci0n`
- [ ] **MinIO Console** (19001) — pantalla de login, entrar con `admin` / `Ilerna_Programaci0n`
- [ ] **Grafana** (13000) — pantalla de login, entrar con `admin` / `Ilerna_Programaci0n`

---

## Fase 4 — MQTT (Mosquitto)

### Instalar clientes CLI (una vez)
```bash
sudo apt-get update && sudo apt-get install -y mosquitto-clients
```

### 4.1 — Publish / Subscribe round-trip básico
Terminal A (suscriptor):
```bash
mosquitto_sub -h mosquitto -p 1883 -t "sensors/telemetry" -v
```

Terminal B (publicador — mensaje válido en Celsius):
```bash
mosquitto_pub -h mosquitto -p 1883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-001","temperature":75.0,"unit":"C","ts":"2026-01-01T00:00:00Z"}'
```

- [ ] En terminal A aparece el JSON publicado ✅

### 4.2 — Mensaje válido en Fahrenheit
```bash
mosquitto_pub -h mosquitto -p 1883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-002","temperature":167.0,"unit":"F","ts":"2026-01-01T00:01:00Z"}'
```

- [ ] Mensaje aparece en suscriptor ✅
- [ ] Bridge lo acepta y publica en `sensors_raw` (ver logs del bridge) ✅

### 4.3 — Mensaje válido en Kelvin
```bash
mosquitto_pub -h mosquitto -p 1883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-003","temperature":348.15,"unit":"K","ts":"2026-01-01T00:02:00Z"}'
```

- [ ] Mensaje aparece en suscriptor ✅
- [ ] Bridge lo acepta y publica en `sensors_raw` ✅

### 4.4 — Mensaje con campo faltante (debe ser rechazado por el bridge)
```bash
mosquitto_pub -h mosquitto -p 1883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-001","temperature":75.0,"ts":"2026-01-01T00:03:00Z"}'
```

- [ ] El bridge muestra `WARNING Mensaje rechazado` (falta campo `unit`) ✅
- [ ] El mensaje NO llega a `sensors_raw` ✅

### 4.5 — JSON malformado (debe ser rechazado por el bridge)
```bash
mosquitto_pub -h mosquitto -p 1883 -t "sensors/telemetry" \
  -m 'esto no es json'
```

- [ ] El bridge muestra `WARNING` o error de parseo ✅
- [ ] El mensaje NO llega a `sensors_raw` ✅

### 4.6 — Unidad inválida (debe ser rechazado por el bridge)
```bash
mosquitto_pub -h mosquitto -p 1883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-001","temperature":75.0,"unit":"X","ts":"2026-01-01T00:04:00Z"}'
```

- [ ] El bridge rechaza el mensaje (unit no está en {C,F,K}) ✅

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
curl -s http://jobmanager:8081/taskmanagers | python3 -m json.tool | grep -i "id\|slots"
```

- [ ] Aparece al menos 1 TaskManager con slots disponibles ✅

### Verificar JARs y configuración S3
```bash
JM=$(docker ps -qf "label=com.docker.compose.service=jobmanager")
TM=$(docker ps -qf "label=com.docker.compose.service=taskmanager")

# JAR Kafka en jobmanager y taskmanager
docker exec $JM ls /opt/flink/lib/ | grep kafka
docker exec $TM ls /opt/flink/lib/ | grep kafka

# Plugin S3
docker exec $JM ls /opt/flink/plugins/flink-s3-fs-hadoop/

# Configuración S3/MinIO
docker exec $JM grep -i "s3\|minio" /opt/flink/conf/flink-conf.yaml
```

- [ ] JAR `flink-sql-connector-kafka-3.1.0-1.18.jar` en `/opt/flink/lib/` (jobmanager y taskmanager) ✅
- [ ] Plugin S3 en `/opt/flink/plugins/flink-s3-fs-hadoop/` ✅
- [ ] Configuración S3/MinIO en `flink-conf.yaml` ✅

---

## Fase 7 — InfluxDB

### Verificar health
```bash
curl -f http://influxdb:8086/ping && echo "OK"
```

- [ ] Responde `204 No Content` ✅

### Verificar bucket
```bash
curl -s -H "Authorization: Token supersecrettoken" \
  "http://influxdb:8086/api/v2/buckets?org=ilerna" | python3 -m json.tool | grep '"name"'
```

- [ ] Aparece el bucket `sensores` ✅

---

## Fase 8 — MinIO (S3)

### Verificar health
```bash
curl -f http://minio:9000/minio/health/live && echo "OK"
```

- [ ] Responde `200 OK` ✅

### Crear bucket datalake (si no existe)
```bash
python3 -c "
from minio import Minio
c = Minio('minio:9000', access_key='admin', secret_key='Ilerna_Programaci0n', secure=False)
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

# 7. FastAPI — Hito 4 (Terminal 3) — alias: api
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# 8. Dashboard — Hito 4 (Terminal 4) — alias: ui
streamlit run src/05_ui/app.py --server.port 8501
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
```

#### Verificar mensajes íntegros en sensors_verified
```bash
docker exec $RP rpk topic consume sensors_verified -n 3 | python3 -m json.tool
```

- [ ] Contiene campos `hash` y `prev_hash` en cada mensaje ✅
- [ ] El `device_id` coincide con el esperado ✅

#### Verificar mensajes con cadena rota en sensors_invalid (DLQ)
```bash
docker exec $RP rpk topic consume sensors_invalid -n 5 | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        msg = json.loads(line)
        print(f'device={msg.get(\"device_id\")} reason={msg.get(\"reason\")}')
    except: pass
"
```

- [ ] Aparecen mensajes con `reason: hash_chain_broken` ✅
- [ ] Aparecen mensajes con `reason` de validación del bridge (`missing_field`, etc.) ✅

#### Simular mensaje con hash manipulado
```bash
# Publicar un mensaje con hash incorrecto para forzar detección
mosquitto_pub -h mosquitto -p 1883 -t "sensors/telemetry" \
  -m '{"device_id":"machine-001","temperature":72.0,"unit":"C","ts":"2026-01-01T12:00:00Z","hash":"aaaaaa","prev_hash":"bbbbbb"}'
```

```bash
# Verificar que llega a sensors_invalid con reason hash_chain_broken
sleep 3
docker exec $RP rpk topic consume sensors_invalid -n 1 | python3 -m json.tool | grep reason
```

- [ ] El mensaje con hash manipulado aparece en `sensors_invalid` ✅
- [ ] El campo `reason` contiene `hash_chain_broken` ✅

#### Contar mensajes por tópico
```bash
for TOPIC in sensors_raw sensors_clean sensors_invalid sensors_verified; do
  COUNT=$(docker exec $RP rpk topic describe $TOPIC -p | grep -oP 'high-watermark:\s*\K\d+' | awk '{s+=$1} END {print s}')
  echo "$TOPIC: $COUNT mensajes"
done
```

- [ ] `sensors_raw` > 0 ✅
- [ ] `sensors_clean` > 0 ✅
- [ ] `sensors_invalid` > 0 (fallos + hash roto) ✅
- [ ] `sensors_verified` > 0 ✅

### A2 — Lambda Architecture (Cold Path Parquet)

```bash
# Esperar ~10 min para que Flink emita el primer archivo Parquet (rolling policy)
# Verificar objetos en MinIO:
python3 -c "
from minio import Minio
c = Minio('minio:9000', access_key='admin', secret_key='Ilerna_Programaci0n', secure=False)
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
curl -s -X POST http://localhost:8000/model/train | python3 -m json.tool

# Ver estado del modelo
curl -s http://localhost:8000/model/status | python3 -m json.tool

# Predecir para una temperatura alta (posible anomalía)
curl -s "http://localhost:8000/machines/machine-001/predict?temperature_c=95.0" | python3 -m json.tool
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
curl -s http://jobmanager:8081/jobs | python3 -m json.tool

# Cancelar un job Flink
curl -X PATCH http://jobmanager:8081/jobs/<JOB_ID>?mode=cancel

# Reiniciar solo el simulador con más fallos
python src/01_ingestion/sensor_simulator.py --machines 5 --fault-rate 0.3 --interval 1
```
