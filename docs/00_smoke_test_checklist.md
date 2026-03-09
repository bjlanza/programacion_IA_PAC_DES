# Smoke Test / Checklist — ILERNA IA + Big Data (Codespaces)

Este checklist valida que el entorno Docker (Mosquitto + Redpanda + Flink + InfluxDB + MinIO + Grafana) está **levantado y funcional** antes de ejecutar scripts de Python.

> Nota: en Docker Compose, solo verás estado `healthy` en servicios que tengan `healthcheck`. El resto deben estar al menos `Up`.

## 0) Requisitos previos (1 min)
- [ ] Estoy dentro del Codespace (terminal integrada de VS Code).
- [ ] Existe `.devcontainer/docker-compose.yml`.
- [ ] Existe `config/mosquitto.conf` con:
  - [ ] `listener 1883 0.0.0.0`
  - [ ] `allow_anonymous true`

## 1) Estado general de contenedores (1–2 min)
Ejecuta:

- [ ] ```bash
      docker compose -f .devcontainer/docker-compose.yml ps
      ```

Comprueba:
- [ ] `mosquitto` está `Up`
- [ ] `redpanda` está `Up` (idealmente `healthy`)
- [ ] `redpanda-console` está `Up` (idealmente `healthy`)
- [ ] `jobmanager` está `Up` (idealmente `healthy`)
- [ ] `taskmanager` está `Up`
- [ ] `influxdb` está `Up` (idealmente `healthy`)
- [ ] `minio` está `Up` (idealmente `healthy`)
- [ ] `grafana` está `Up`

Si alguno está `Exited` o `unhealthy`:
- [ ] ```bash
      docker compose -f .devcontainer/docker-compose.yml logs --tail=200 <servicio>
      ```

## 2) Verificación por UI (abrir en “Ports” de Codespaces)
Abre estas UIs (en Codespaces: pestaña **Ports** → abrir en navegador):

- [ ] **Redpanda Console** (`8080`) abre sin error
- [ ] **Flink UI** (`8081`) abre y carga el overview
- [ ] **InfluxDB UI** (`8086`) responde (pantalla inicial/setup o login)
- [ ] **MinIO Console** (`9001`) responde (login)
- [ ] **Grafana** (`3000`) responde (login)

## 3) Verificación por HTTP (sin navegador) (2 min)
Ejecuta:

- [ ] ```bash
      curl -fsS http://localhost:8080 >/dev/null && echo "OK: Redpanda Console" || echo "FAIL: Redpanda Console"
      ```
- [ ] ```bash
      curl -fsS http://localhost:8081/overview >/dev/null && echo "OK: Flink UI" || echo "FAIL: Flink UI"
      ```
- [ ] ```bash
      curl -fsS http://localhost:8086/ping >/dev/null && echo "OK: InfluxDB" || echo "FAIL: InfluxDB"
      ```
- [ ] ```bash
      curl -fsS http://localhost:9000/minio/health/live >/dev/null && echo "OK: MinIO" || echo "FAIL: MinIO"
      ```
- [ ] ```bash
      curl -fsS http://localhost:3000/api/health >/dev/null && echo "OK: Grafana" || echo "FAIL: Grafana"
      ```

> Si `curl` no está instalado en tu `workspace`, instala con:
> `sudo apt-get update && sudo apt-get install -y curl`

## 4) Verificación MQTT (Mosquitto) (2 min)
### 4.1 Instalar clientes (una vez)
- [ ] ```bash
      sudo apt-get update && sudo apt-get install -y mosquitto-clients
      ```

### 4.2 Test publish/subscribe
En terminal A:
- [ ] ```bash
      mosquitto_sub -h localhost -p 1883 -t "test/ping" -v
      ```

En terminal B:
- [ ] ```bash
      mosquitto_pub -h localhost -p 1883 -t "test/ping" -m "hello"
      ```

Resultado esperado:
- [ ] En terminal A aparece: `test/ping hello`

## 5) Verificación Kafka/Redpanda (3–5 min)
### 5.1 Crear topic (desde el contenedor redpanda)
- [ ] ```bash
      docker exec -it $(docker compose -f .devcontainer/docker-compose.yml ps -q redpanda) rpk topic create sensores_raw -p 1 -r 1
      ```

Resultado esperado:
- [ ] Topic creado (o mensaje de que ya existe)

### 5.2 Producir un mensaje
- [ ] ```bash
      echo '{"device_id":"dev-001","ts":"2026-01-01T00:00:00Z","temp":25.0}' | \
      docker exec -i $(docker compose -f .devcontainer/docker-compose.yml ps -q redpanda) \
      rpk topic produce sensores_raw
      ```

### 5.3 Consumir el mensaje
- [ ] ```bash
      docker exec -it $(docker compose -f .devcontainer/docker-compose.yml ps -q redpanda) \
      rpk topic consume sensores_raw -n 1
      ```

Resultado esperado:
- [ ] Ves el JSON que acabas de producir

## 6) Verificación Flink (JobManager + TaskManager) (2–3 min)
En Flink UI (`8081`):
- [ ] En **TaskManagers** aparece al menos **1** TaskManager registrado
- [ ] En **Overview** el cluster se ve “running” (sin errores)

(Después, cuando subamos jobs, deben aparecer en la lista de Jobs.)

## 7) Verificación MinIO (2–3 min)
En MinIO Console (`9001`):
- [ ] Puedo iniciar sesión con las credenciales configuradas en `docker-compose.yml`
- [ ] (Opcional) Creo un bucket `datalake` sin error

## 8) Verificación InfluxDB (2–5 min)
En InfluxDB UI (`8086`):
- [ ] Puedo entrar (setup o login)
- [ ] Existe el bucket configurado (por ejemplo `sensores`) o puedo crearlo

## 9) Verificación Grafana (2–5 min)
En Grafana (`3000`):
- [ ] Puedo iniciar sesión (admin / password configurada)
- [ ] (Opcional) Añadir datasource de InfluxDB sin errores

## 10) “Todo OK” (criterio de aceptación)
Marca esto solo si se cumple:
- [ ] Docker Compose no muestra `Exited` en ningún servicio
- [ ] Flink UI responde
- [ ] Redpanda Console responde
- [ ] InfluxDB y MinIO responden a health
- [ ] MQTT pub/sub funciona
- [ ] Redpanda produce/consume funciona

---

## Apéndice A — Comandos de recuperación rápida
Recrear todo:
- ```bash
  docker compose -f .devcontainer/docker-compose.yml up -d --force-recreate