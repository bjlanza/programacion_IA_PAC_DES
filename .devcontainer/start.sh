#!/bin/bash
set -e

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║     ILERNA - Programación IA PAC DES                 ║"
echo "║     Arrancando entorno de práctica...                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Permisos Docker ──────────────────────────────────────
echo ">>> [1/4] Ajustando permisos del socket Docker..."
if sudo chmod 666 /var/run/docker.sock 2>/dev/null; then
  echo "    ✅ Permisos ajustados correctamente"
else
  echo "    ⚠️  No se pudieron ajustar permisos del socket Docker"
  echo "    Intentando continuar de todas formas..."
fi

# ── 2. Levantar contenedores ────────────────────────────────
# Al estar en .devcontainer/, el archivo compose está en el mismo directorio (.)
echo ">>> [2/4] Levantando contenedores (docker compose up -d)..."
docker compose -f docker-compose.yml up -d

# ── 3. Esperar a servicios con healthcheck ──────────────────
echo ">>> [3/4] Esperando a que los servicios críticos estén healthy..."

# Lista de servicios con healthcheck definido en el compose
SERVICES=("redpanda" "jobmanager" "influxdb" "minio")
TIMEOUT=120

for SERVICE in "${SERVICES[@]}"; do
  echo -n "    Esperando $SERVICE "
  ELAPSED=0
  # Obtenemos el ID del contenedor para inspeccionarlo
  CONTAINER_ID=$(docker compose -f docker-compose.yml ps -q $SERVICE)
  
  until [ "$(docker inspect --format='{{.State.Health.Status}}' $CONTAINER_ID 2>/dev/null)" = "healthy" ]; do
    if [ $ELAPSED -ge $TIMEOUT ]; then
      echo -e "\n${YELLOW}    ⚠️  TIMEOUT: $SERVICE no alcanzó estado healthy en ${TIMEOUT}s${NC}"
      echo "    Revisa los logs con: docker compose logs $SERVICE"
      exit 1
    fi
    echo -n "."
    sleep 3
    ELAPSED=$((ELAPSED + 3))
  done
  echo -e " ${GREEN}✅ healthy${NC}"
done

# ── 4. Estado final ─────────────────────────────────────────
echo ""
echo ">>> [4/4] Estado final de los contenedores:"
docker compose -f docker-compose.yml ps

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ✅ Entorno listo                                    ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  🔴 Redpanda Console  → http://localhost:8080        ║"
echo "║  🟠 Flink UI          → http://localhost:8081        ║"
echo "║  🟡 InfluxDB UI       → http://localhost:8086        ║"
echo "║  🟢 MinIO Console     → http://localhost:9001        ║"
echo "║  🔵 Grafana           → http://localhost:3000        ║"
echo "║  ⚪ FastAPI           → http://localhost:8000        ║"
echo "║  ⚪ Streamlit         → http://localhost:8501        ║"
echo "║  ⚪ Jupyter           → http://localhost:8888        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""