#!/usr/bin/env bash
# init_pipeline.sh — Inicializa topics Kafka, bucket MinIO, jobs Flink y aliases
# Ejecutar manualmente tras confirmar que la infraestructura está healthy:
#   source .devcontainer/init_pipeline.sh

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
NC="\033[0m"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║     ILERNA - Inicializando pipeline de datos...     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Topics Redpanda ───────────────────────────────────────
echo ">>> [1/3] Creando topics Kafka..."
RP_ID="$(docker ps -qf 'label=com.docker.compose.service=redpanda' 2>/dev/null | head -1 || true)"

if [[ -n "${RP_ID}" ]]; then
  for TOPIC in sensors_raw sensors_clean sensors_invalid sensors_verified; do
    docker exec "${RP_ID}" rpk topic create "${TOPIC}" -p 1 -r 1 2>/dev/null \
      && echo -e "    ${GREEN}✅ Topic ${TOPIC} creado${NC}" \
      || echo -e "    ${YELLOW}ℹ️  Topic ${TOPIC} ya existe${NC}"
  done
else
  echo -e "    ${YELLOW}⚠️  Redpanda no encontrado${NC}"
fi

# ── 2. Bucket MinIO ──────────────────────────────────────────
echo ""
echo ">>> [2/3] Creando bucket MinIO..."
python3 -c "
from minio import Minio
import sys
try:
    c = Minio('minio:9000', access_key='admin', secret_key='Ilerna_Programaci0n', secure=False)
    if not c.bucket_exists('datalake'):
        c.make_bucket('datalake')
        print('    \033[32m✅ Bucket datalake creado\033[0m')
    else:
        print('    \033[33mℹ️  Bucket datalake ya existe\033[0m')
except Exception as e:
    print(f'    \033[33m⚠️  MinIO no disponible: {e}\033[0m', file=sys.stderr)
" 2>&1 || true

# ── 3. Jobs Flink ────────────────────────────────────────────
echo ""
echo ">>> [3/3] Lanzando jobs Flink..."
JM_ID="$(docker ps -qf 'label=com.docker.compose.service=jobmanager' 2>/dev/null | head -1 || true)"

if [[ -n "${JM_ID}" ]]; then
  RUNNING_JOBS=$(docker exec "${JM_ID}" bash -c \
    "curl -s http://localhost:8081/jobs 2>/dev/null \
     | python3 -c \"import sys,json; d=json.load(sys.stdin); print(len([j for j in d.get('jobs',[]) if j['status']=='RUNNING']))\" \
     2>/dev/null || echo 0")

  if [[ "${RUNNING_JOBS}" -gt 0 ]]; then
    echo -e "    ${YELLOW}ℹ️  ${RUNNING_JOBS} job(s) ya corriendo en Flink — saltando lanzamiento${NC}"
  else
    JOBS=(flink_normalization_job.py flink_hash_verifier_job.py flink_analytics_job.py flink_to_minio_job.py)
    TOTAL=${#JOBS[@]}
    IDX=0
    for JOB in "${JOBS[@]}"; do
      IDX=$((IDX + 1))
      echo -n "    [${IDX}/${TOTAL}] Enviando ${JOB}..."
      docker exec "${JM_ID}" bash -c \
        "nohup flink run -py /opt/flink/jobs/${JOB} \
         > /tmp/flink_${JOB%.py}.log 2>&1 &" \
        && echo -e " ${GREEN}✅${NC}" \
        || echo -e " ${YELLOW}⚠️  (ver /tmp/flink_${JOB%.py}.log)${NC}"
      sleep 3
    done
  fi
else
  echo -e "    ${YELLOW}⚠️  jobmanager no encontrado${NC}"
fi

# ── Aliases de desarrollo ────────────────────────────────────
BASHRC="/home/vscode/.bashrc"
MARKER="# === ILERNA PAC DES helpers ==="

if ! grep -q "${MARKER}" "${BASHRC}" 2>/dev/null; then
  cat >> "${BASHRC}" << 'BASHRC_EOF'

# === ILERNA PAC DES helpers ===
jm()  { docker ps -qf "label=com.docker.compose.service=jobmanager"; }
rp()  { docker ps -qf "label=com.docker.compose.service=redpanda";   }

flink-run()  { docker exec "$(jm)" flink run "$@"; }
flink-list() { curl -s http://jobmanager:8081/jobs | python3 -m json.tool; }

alias sim='python src/01_ingestion/sensor_simulator.py --machines 5 --fault-rate 0.1'
alias bridge='python src/01_ingestion/mqtt_to_redpanda_bridge.py'
alias api='uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload'
alias ui='streamlit run src/05_ui/app.py --server.port 8501'
alias nb='jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --ServerApp.token="" --ServerApp.password="" --NotebookApp.token="" --NotebookApp.password=""'
alias mqtt-install='sudo apt-get update -qq && sudo apt-get install -y mosquitto-clients'
alias mqtt-sub='mosquitto_sub -h mosquitto -p 1883 -t "sensors/telemetry" -v'
alias aliases='echo "
  sim          → Simulador de sensores (5 máquinas, fault-rate 0.1)
  bridge       → Puente MQTT → Redpanda
  api          → FastAPI en :8000
  ui           → Streamlit dashboard en :8501
  nb           → JupyterLab en :8888
  flink-run    → flink run dentro del jobmanager
  flink-list   → Lista jobs Flink activos
  mqtt-install → Instala mosquitto-clients
  mqtt-sub     → Suscribe a sensors/telemetry
  aliases      → Muestra esta ayuda
"'
# === fin ILERNA PAC DES helpers ===
BASHRC_EOF
  echo -e "    ${GREEN}✅ Aliases de desarrollo añadidos a ~/.bashrc${NC}"
fi

# Cargar aliases en el shell actual (funciona si el script se ejecuta con 'source')
# shellcheck disable=SC1090
source "${BASHRC}" 2>/dev/null || true

# ── Banner final ─────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo -e "║  ${GREEN}✅ Pipeline listo${NC}                                   ║"
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
echo "    Aliases disponibles: sim | bridge | api | ui | nb | flink-run | flink-list | mqtt-install | mqtt-sub"
echo ""
