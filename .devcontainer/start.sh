#!/usr/bin/env bash
set -euo pipefail

# Colores
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
NC="\033[0m"

COMPOSE_FILE=".devcontainer/docker-compose.yml"
TIMEOUT=180

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║     ILERNA - Programación IA PAC DES                 ║"
echo "║     Arrancando entorno de práctica...                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Permisos Docker ──────────────────────────────────────
echo ">>> [1/4] Ajustando permisos del socket Docker..."
if sudo chmod 666 /var/run/docker.sock 2>/dev/null; then
  echo -e "    ${GREEN}✅ Permisos ajustados correctamente${NC}"
else
  echo -e "    ${YELLOW}⚠️  No se pudieron ajustar permisos del socket Docker${NC}"
  echo    "    Intentando continuar de todas formas..."
fi

# ── 2. Levantar contenedores ────────────────────────────────
echo ">>> [2/4] Levantando contenedores (docker compose up -d)..."


# Si los contenedores ya están corriendo (gestionados por Codespaces), no relanzar
if docker ps --filter "name=redpanda" --filter "status=running" -q 2>/dev/null | grep -q .; then
  echo "    ℹ️  Contenedores ya activos (gestionados por Codespaces), saltando start..."
else
  echo ">>> Limpiando estado anterior..."
  docker compose -f "${COMPOSE_FILE}" down --remove-orphans --timeout 10 2>/dev/null || true
  docker container prune -f 2>/dev/null || true
  echo "    ✅ Entorno limpio"
  docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans
fi

# ── 3. Esperar a servicios con healthcheck ──────────────────
echo ">>> [3/4] Esperando a que los servicios críticos estén healthy..."

SERVICES=("redpanda" "jobmanager" "influxdb" "minio")

for SERVICE in "${SERVICES[@]}"; do
  echo -n "    Esperando ${SERVICE} "
  ELAPSED=0

  # Esperar a que el contenedor exista (busca por label, independiente del project name)
  CONTAINER_ID=""
  while [[ -z "${CONTAINER_ID}" && "${ELAPSED}" -lt "${TIMEOUT}" ]]; do
    CONTAINER_ID="$(docker ps -q --filter "label=com.docker.compose.service=${SERVICE}" 2>/dev/null | head -1 || true)"
    if [[ -z "${CONTAINER_ID}" ]]; then
      echo -n "."
      sleep 2
      ELAPSED=$((ELAPSED + 2))
    fi
  done

  if [[ -z "${CONTAINER_ID}" ]]; then
    echo -e "\n${YELLOW}    ⚠️  Contenedor '${SERVICE}' no encontrado tras ${TIMEOUT}s (Codespaces aún arrancando)${NC}"
    continue
  fi

  # Esperar a healthy
  ELAPSED=0
  until [[ "$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${CONTAINER_ID}" 2>/dev/null)" == "healthy" ]]; do
    HEALTH="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${CONTAINER_ID}" 2>/dev/null || true)"

    if [[ "${HEALTH}" == "no-healthcheck" ]]; then
      echo -e " ${YELLOW}⚠️  sin healthcheck (asumiendo OK)${NC}"
      break
    fi

    if [[ "${HEALTH}" == "unhealthy" ]]; then
      echo -e "\n${YELLOW}    ⚠️  ${SERVICE} está unhealthy — revisa: docker logs \$(docker ps -qf label=com.docker.compose.service=${SERVICE})${NC}"
      break
    fi

    if [[ "${ELAPSED}" -ge "${TIMEOUT}" ]]; then
      echo -e "\n${YELLOW}    ⚠️  TIMEOUT: ${SERVICE} no alcanzó healthy en ${TIMEOUT}s${NC}"
      break
    fi

    echo -n "."
    sleep 3
    ELAPSED=$((ELAPSED + 3))
  done

  # Solo imprime ✅ si no se hizo break por no-healthcheck
  [[ "$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${CONTAINER_ID}" 2>/dev/null)" == "healthy" ]] \
    && echo -e " ${GREEN}✅ healthy${NC}"
done

# ── 4. Estado final ─────────────────────────────────────────
echo ""
echo ">>> [4/6] Estado final de los contenedores:"
docker ps --filter "label=com.docker.compose.project" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# ── 5. Preparar infraestructura de datos ────────────────────
echo ""
echo ">>> [5/6] Preparando topics Kafka y bucket MinIO..."

# Topics Redpanda
RP_ID="$(docker ps -qf 'label=com.docker.compose.service=redpanda' 2>/dev/null | head -1 || true)"
if [[ -n "${RP_ID}" ]]; then
  for TOPIC in sensors_raw sensors_clean sensors_invalid sensors_verified; do
    docker exec "${RP_ID}" rpk topic create "${TOPIC}" -p 1 -r 1 2>/dev/null \
      && echo -e "    ${GREEN}✅ Topic ${TOPIC} creado${NC}" \
      || echo -e "    ${YELLOW}ℹ️  Topic ${TOPIC} ya existe${NC}"
  done
fi

# Bucket MinIO
python3 -c "
from minio import Minio
import sys
try:
    c = Minio('minio:9000', access_key='admin', secret_key='adminpassword', secure=False)
    if not c.bucket_exists('datalake'):
        c.make_bucket('datalake')
        print('    \033[32m✅ Bucket datalake creado\033[0m')
    else:
        print('    \033[33mℹ️  Bucket datalake ya existe\033[0m')
except Exception as e:
    print(f'    \033[33m⚠️  MinIO no disponible aún: {e}\033[0m', file=sys.stderr)
" 2>&1 || true

# ── 6. Auto-lanzar jobs Flink ────────────────────────────────
echo ""
echo ">>> [6/6] Verificando jobs Flink..."

JM_ID="$(docker ps -qf 'label=com.docker.compose.service=jobmanager' 2>/dev/null | head -1 || true)"

if [[ -n "${JM_ID}" ]]; then
  # Contar jobs RUNNING en el cluster
  RUNNING_JOBS=$(docker exec "${JM_ID}" bash -c \
    "curl -s http://localhost:8081/jobs 2>/dev/null \
     | python3 -c \"import sys,json; d=json.load(sys.stdin); print(len([j for j in d.get('jobs',[]) if j['status']=='RUNNING']))\" \
     2>/dev/null || echo 0")

  if [[ "${RUNNING_JOBS}" -gt 0 ]]; then
    echo -e "    ${YELLOW}ℹ️  ${RUNNING_JOBS} job(s) ya corriendo en Flink — saltando lanzamiento${NC}"
  else
    echo "    🚀 Lanzando jobs Flink en segundo plano..."

    FLINK_JOBS=(
      "flink_normalization_job.py"
      "flink_hash_verifier_job.py"
      "flink_analytics_job.py"
    )

    for JOB in "${FLINK_JOBS[@]}"; do
      echo -n "    Enviando ${JOB}..."
      docker exec "${JM_ID}" bash -c \
        "nohup flink run -py /opt/flink/jobs/${JOB} \
         > /tmp/flink_${JOB%.py}.log 2>&1 &" \
        && echo -e " ${GREEN}✅${NC}" \
        || echo -e " ${YELLOW}⚠️  (ver /tmp/flink_${JOB%.py}.log)${NC}"
      sleep 3  # pequeña pausa entre envíos
    done

    echo -e "    ${GREEN}✅ Jobs enviados — revisa: http://localhost:18081${NC}"
  fi
else
  echo -e "    ${YELLOW}⚠️  jobmanager no encontrado, saltando lanzamiento de jobs${NC}"
fi

echo ""
# ── 6. Aliases y helpers de desarrollo ─────────────────────
BASHRC="/home/vscode/.bashrc"
MARKER="# === ILERNA PAC DES helpers ==="

if ! grep -q "${MARKER}" "${BASHRC}" 2>/dev/null; then
  cat >> "${BASHRC}" << 'BASHRC_EOF'

# === ILERNA PAC DES helpers ===
# Funciones para obtener IDs de contenedores por servicio
jm()  { docker ps -qf "label=com.docker.compose.service=jobmanager"; }
rp()  { docker ps -qf "label=com.docker.compose.service=redpanda";   }

# Lanzar jobs Flink sin copiar el ID del contenedor
flink-run()  { docker exec "$(jm)" flink run "$@"; }
flink-list() { curl -s http://jobmanager:8081/jobs | python3 -m json.tool; }

# Pipeline shortcuts
alias sim='python src/01_ingestion/sensor_simulator.py --machines 5 --fault-rate 0.1'
alias bridge='python src/01_ingestion/mqtt_to_redpanda_bridge.py'
alias api='uvicorn src.04_api.main:app --host 0.0.0.0 --port 8000 --reload'
alias ui='streamlit run src/05_ui/app.py --server.port 8501'
# === fin ILERNA PAC DES helpers ===
BASHRC_EOF
  echo ">>> [6/6] Aliases de desarrollo añadidos a ~/.bashrc"
fi

echo "╔══════════════════════════════════════════════════════╗"
echo -e "║  ${GREEN}✅ Entorno listo${NC}                                    ║"
echo "╠══════════════════════════════════════════════════════╣"
echo -e "║  ${RED}🔴${NC} Redpanda Console  → http://localhost:18080       ║"
echo -e "║  ${YELLOW}🟠${NC} Flink UI          → http://localhost:18081       ║"
echo -e "║  ${YELLOW}🟡${NC} InfluxDB UI       → http://localhost:18086       ║"
echo -e "║  ${GREEN}🟢${NC} MinIO Console     → http://localhost:19001       ║"
echo -e "║  ${CYAN}🔵${NC} Grafana           → http://localhost:13000       ║"
echo    "║  ⚪ FastAPI           → http://localhost:18000       ║"
echo    "║  ⚪ Streamlit         → http://localhost:18501       ║"
echo    "║  ⚪ Jupyter           → http://localhost:18888       ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""