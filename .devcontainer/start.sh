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
docker compose -f "${COMPOSE_FILE}" down --remove-orphans 2>/dev/null || true
docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans

# ── 3. Esperar a servicios con healthcheck ──────────────────
echo ">>> [3/4] Esperando a que los servicios críticos estén healthy..."

SERVICES=("redpanda" "jobmanager" "influxdb" "minio")

for SERVICE in "${SERVICES[@]}"; do
  echo -n "    Esperando ${SERVICE} "
  ELAPSED=0

  # Esperar a que el contenedor exista
  CONTAINER_ID=""
  while [[ -z "${CONTAINER_ID}" && "${ELAPSED}" -lt "${TIMEOUT}" ]]; do
    CONTAINER_ID="$(docker compose -f "${COMPOSE_FILE}" ps -q "${SERVICE}" 2>/dev/null || true)"
    if [[ -z "${CONTAINER_ID}" ]]; then
      echo -n "."
      sleep 2
      ELAPSED=$((ELAPSED + 2))
    fi
  done

  if [[ -z "${CONTAINER_ID}" ]]; then
    echo -e "\n${RED}    ❌ No se encontró el contenedor '${SERVICE}' tras ${TIMEOUT}s${NC}"
    echo    "    Revisa: docker compose -f ${COMPOSE_FILE} logs ${SERVICE}"
    exit 1
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
      echo -e "\n${RED}    ❌ ${SERVICE} está unhealthy${NC}"
      echo    "    Logs: docker compose -f ${COMPOSE_FILE} logs --tail=100 ${SERVICE}"
      exit 1
    fi

    if [[ "${ELAPSED}" -ge "${TIMEOUT}" ]]; then
      echo -e "\n${YELLOW}    ⚠️  TIMEOUT: ${SERVICE} no alcanzó healthy en ${TIMEOUT}s${NC}"
      echo    "    Revisa: docker compose -f ${COMPOSE_FILE} logs ${SERVICE}"
      exit 1
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
echo ">>> [4/4] Estado final de los contenedores:"
docker compose -f "${COMPOSE_FILE}" ps

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo -e "║  ${GREEN}✅ Entorno listo${NC}                                    ║"
echo "╠══════════════════════════════════════════════════════╣"
echo -e "║  ${RED}🔴${NC} Redpanda Console  → http://localhost:8080        ║"
echo -e "║  ${YELLOW}🟠${NC} Flink UI          → http://localhost:18081        ║"
echo -e "║  ${YELLOW}🟡${NC} InfluxDB UI       → http://localhost:8086        ║"
echo -e "║  ${GREEN}🟢${NC} MinIO Console     → http://localhost:9001        ║"
echo -e "║  ${CYAN}🔵${NC} Grafana           → http://localhost:3000        ║"
echo    "║  ⚪ FastAPI           → http://localhost:8000        ║"
echo    "║  ⚪ Streamlit         → http://localhost:8501        ║"
echo    "║  ⚪ Jupyter           → http://localhost:8888        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""