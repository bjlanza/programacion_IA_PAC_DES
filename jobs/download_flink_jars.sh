#!/usr/bin/env bash
# download_flink_jars.sh
# Descarga dependencias Flink y configura el plugin S3 para MinIO.
# Ejecutar UNA VEZ dentro del contenedor jobmanager:
#
#   docker exec -it $(docker ps -qf "label=com.docker.compose.service=jobmanager") \
#     bash /opt/flink/jobs/download_flink_jars.sh

set -euo pipefail

FLINK_VERSION="1.18.1"
SCALA_VERSION="2.12"
TARGET_LIB="/opt/flink/lib"
TARGET_PLUGINS="/opt/flink/plugins"

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
NC="\033[0m"

download_if_missing() {
  local name="$1"
  local url="$2"
  local dest="$3"
  if [ -f "${dest}" ]; then
    echo -e "  ${YELLOW}⏭  Ya existe: $(basename ${dest})${NC}"
  else
    echo -n "  Descargando ${name}..."
    curl -fsSL "${url}" -o "${dest}"
    echo -e " ${GREEN}✅${NC}"
  fi
}

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Flink JAR Setup — ILERNA Smart-Industry            ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Kafka SQL Connector ────────────────────────────────────
echo ">>> [1/3] Kafka SQL Connector (para sensors_raw, sensors_clean, etc.)"
KAFKA_VERSION="3.1.0-${FLINK_VERSION}"
KAFKA_JAR="flink-sql-connector-kafka-${KAFKA_VERSION}.jar"
download_if_missing "Kafka connector" \
  "https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/${KAFKA_VERSION}/${KAFKA_JAR}" \
  "${TARGET_LIB}/${KAFKA_JAR}"

# ── 2. S3 FileSystem Plugin (para flink_to_minio_job.py) ─────
echo ""
echo ">>> [2/3] S3 Hadoop Plugin (para escribir Parquet en MinIO)"

S3_JAR="flink-s3-fs-hadoop-${FLINK_VERSION}.jar"
S3_PLUGIN_DIR="${TARGET_PLUGINS}/flink-s3-fs-hadoop"
mkdir -p "${S3_PLUGIN_DIR}"

# En la imagen oficial de Flink, el JAR ya viene en /opt/flink/opt/
OPT_JAR="/opt/flink/opt/${S3_JAR}"
if [ -f "${OPT_JAR}" ]; then
  if [ ! -f "${S3_PLUGIN_DIR}/${S3_JAR}" ]; then
    cp "${OPT_JAR}" "${S3_PLUGIN_DIR}/"
    echo -e "  ${GREEN}✅ Plugin S3 copiado desde /opt/flink/opt/${NC}"
  else
    echo -e "  ${YELLOW}⏭  Plugin S3 ya en plugins/${NC}"
  fi
else
  # Fallback: descargar desde Maven
  download_if_missing "S3 Hadoop plugin" \
    "https://repo1.maven.org/maven2/org/apache/flink/flink-s3-fs-hadoop/${FLINK_VERSION}/${S3_JAR}" \
    "${S3_PLUGIN_DIR}/${S3_JAR}"
fi

# ── 3. Configurar MinIO (S3-compatible) en flink-conf.yaml ────
echo ""
echo ">>> [3/3] Configurando S3 endpoint para MinIO en flink-conf.yaml"

FLINK_CONF="/opt/flink/conf/flink-conf.yaml"
S3_CONF_MARKER="# MinIO S3 config (ILERNA)"

if grep -q "${S3_CONF_MARKER}" "${FLINK_CONF}" 2>/dev/null; then
  echo -e "  ${YELLOW}⏭  Configuración S3 ya presente en flink-conf.yaml${NC}"
else
  cat >> "${FLINK_CONF}" << 'EOF'

# MinIO S3 config (ILERNA)
s3.endpoint: http://minio:9000
s3.path.style.access: true
s3.access-key: admin
s3.secret-key: adminpassword
EOF
  echo -e "  ${GREEN}✅ Configuración S3 añadida a flink-conf.yaml${NC}"
fi

# ── Resumen ───────────────────────────────────────────────────
echo ""
echo ">>> JARs en ${TARGET_LIB}:"
ls -lh "${TARGET_LIB}"/*.jar 2>/dev/null | awk '{print "  "$NF"\t"$5}'

echo ""
echo ">>> Plugin S3 en ${S3_PLUGIN_DIR}:"
ls -lh "${S3_PLUGIN_DIR}"/*.jar 2>/dev/null | awk '{print "  "$NF"\t"$5}' || echo "  (ninguno)"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo -e "║  ${GREEN}✅ Setup completado${NC}                                   ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Jobs disponibles:                                   ║"
echo "║    flink run -py /opt/flink/jobs/flink_normalization_job.py   ║"
echo "║    flink run -py /opt/flink/jobs/flink_analytics_job.py       ║"
echo "║    flink run -py /opt/flink/jobs/flink_hash_verifier_job.py   ║"
echo "║    flink run -py /opt/flink/jobs/flink_to_minio_job.py        ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  NOTA: Reinicia el JobManager para cargar el plugin S3:"
echo "  docker restart \$(docker ps -qf 'label=com.docker.compose.service=jobmanager')"
echo ""
