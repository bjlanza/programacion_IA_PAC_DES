#!/usr/bin/env bash
set -euo pipefail

# Colores
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
NC="\033[0m"

COMPOSE_FILE=".devcontainer/docker-compose.yml"
TIMEOUT=180

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║     ILERNA - Programación IA PAC DES                 ║"
echo "║     Arrancando infraestructura Docker...             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Permisos Docker ──────────────────────────────────────
echo ">>> [1/3] Ajustando permisos del socket Docker..."
if sudo chmod 666 /var/run/docker.sock 2>/dev/null; then
  echo -e "    ${GREEN}✅ Permisos ajustados correctamente${NC}"
else
  echo -e "    ${YELLOW}⚠️  No se pudieron ajustar permisos del socket Docker${NC}"
  echo    "    Intentando continuar de todas formas..."
fi

# ── 2. Levantar contenedores ────────────────────────────────
echo ">>> [2/3] Levantando contenedores (docker compose up -d)..."

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
echo ">>> [3/3] Esperando a que los servicios críticos estén healthy..."

SERVICES=("redpanda" "jobmanager" "influxdb" "minio")

for SERVICE in "${SERVICES[@]}"; do
  echo -n "    Esperando ${SERVICE} "
  ELAPSED=0

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
    echo -e "\n${YELLOW}    ⚠️  Contenedor '${SERVICE}' no encontrado tras ${TIMEOUT}s${NC}"
    continue
  fi

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

  [[ "$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${CONTAINER_ID}" 2>/dev/null)" == "healthy" ]] \
    && echo -e " ${GREEN}✅ healthy${NC}"
done

echo ""
echo ">>> Estado de contenedores:"
docker ps --filter "label=com.docker.compose.project" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo -e "${GREEN}✅ Infraestructura lista.${NC}"
echo ""
echo "    Ejecuta el siguiente comando para inicializar el pipeline:"
echo ""
echo "    source .devcontainer/init_pipeline.sh"
echo ""
