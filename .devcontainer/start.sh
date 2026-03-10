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
echo ">>> [4/4] Estado final de los contenedores:"
docker ps --filter "label=com.docker.compose.project" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
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