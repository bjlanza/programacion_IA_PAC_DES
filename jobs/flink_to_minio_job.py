"""
flink_to_minio_job.py — Flink FileSystem Connector → MinIO Parquet (particionado)

Escribe los mensajes de 'sensors_clean' directamente en MinIO como archivos
Parquet particionados por año/mes/día/hora:

  s3a://datalake/clean/year=2026/month=03/day=11/hour=09/<part>.parquet

Ventajas sobre el writer Python (kafka_to_minio.py):
  · Flink gestiona checkpointing → escritura exactamente una vez (exactly-once).
  · Particionado automático por tiempo del evento (no de procesamiento).
  · DuckDB puede leer directamente con hive_partitioning=true, lo que
    permite filtros eficientes: WHERE year='2026' AND month='03'.

Requisitos previos (ejecutar una vez en el contenedor jobmanager):
  bash /opt/flink/jobs/download_flink_jars.sh
  # El script también configura el plugin S3 de Flink para MinIO.

Ejecutar:
  flink run -py /opt/flink/jobs/flink_to_minio_job.py

Variables de entorno:
  KAFKA_BROKER    (default: redpanda:29092)
  KAFKA_INPUT     (default: sensors_clean)
  KAFKA_GROUP     (default: flink-minio-writer)
  S3_ENDPOINT     (default: http://minio:9000)
  S3_ACCESS       (default: admin)
  S3_SECRET       (default: adminpassword)
  S3_BUCKET       (default: datalake)
  S3_PATH         (default: clean)
"""

import logging
import os

from pyflink.datastream import StreamExecutionEnvironment
from pyflink.table import StreamTableEnvironment, DataTypes

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("flink-minio-writer")

# ── Configuración ─────────────────────────────────────────────
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:29092")
KAFKA_INPUT  = os.getenv("KAFKA_INPUT",  "sensors_clean")
KAFKA_GROUP  = os.getenv("KAFKA_GROUP",  "flink-minio-writer")

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_ACCESS   = os.getenv("S3_ACCESS",   "admin")
S3_SECRET   = os.getenv("S3_SECRET",   "adminpassword")
S3_BUCKET   = os.getenv("S3_BUCKET",   "datalake")
S3_PATH     = os.getenv("S3_PATH",     "clean")


def main():
    env   = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)

    # ── Configurar S3A (MinIO) en tiempo de ejecución ────────
    # Alternativa a flink-conf.yaml: pasar propiedades vía código.
    config = t_env.get_config().get_configuration()
    config.set_string("s3.endpoint",            S3_ENDPOINT)
    config.set_string("s3.path.style.access",   "true")
    config.set_string("s3.access-key",          S3_ACCESS)
    config.set_string("s3.secret-key",          S3_SECRET)

    log.info("Broker: %s | topic: %s → s3a://%s/%s/", KAFKA_BROKER, KAFKA_INPUT, S3_BUCKET, S3_PATH)

    # ── Tabla fuente: sensors_clean (Kafka) ───────────────────
    t_env.execute_sql(f"""
    CREATE TABLE sensors_clean (
        device_id     STRING,
        temperature_c DOUBLE,
        unit_original STRING,
        ts            STRING,
        _ingested_at  BIGINT,
        event_time    AS TO_TIMESTAMP(ts, 'yyyy-MM-dd''T''HH:mm:ss''Z'''),
        WATERMARK FOR event_time AS event_time - INTERVAL '15' SECOND
    ) WITH (
        'connector'                    = 'kafka',
        'topic'                        = '{KAFKA_INPUT}',
        'properties.bootstrap.servers' = '{KAFKA_BROKER}',
        'properties.group.id'          = '{KAFKA_GROUP}',
        'scan.startup.mode'            = 'earliest-offset',
        'format'                       = 'json',
        'json.ignore-parse-errors'     = 'true'
    )
    """)

    # ── Tabla destino: MinIO Parquet particionada ─────────────
    # Particionamos por año/mes/día/hora derivados del event_time.
    # DuckDB puede usar hive_partitioning=true para filtrar eficientemente.
    t_env.execute_sql(f"""
    CREATE TABLE sensors_parquet (
        device_id     STRING,
        temperature_c DOUBLE,
        unit_original STRING,
        ts            STRING,
        _ingested_at  BIGINT,
        year          STRING,
        month         STRING,
        day           STRING,
        hour          STRING
    ) PARTITIONED BY (year, month, day, hour)
    WITH (
        'connector'   = 'filesystem',
        'path'        = 's3a://{S3_BUCKET}/{S3_PATH}/',
        'format'      = 'parquet',
        'parquet.compression' = 'SNAPPY',
        'sink.partition-commit.trigger'               = 'watermark',
        'sink.partition-commit.delay'                 = '1 min',
        'sink.partition-commit.policy.kind'           = 'success-file',
        'sink.rolling-policy.rollover-interval'       = '10 min',
        'sink.rolling-policy.check-interval'          = '1 min'
    )
    """)

    # ── Insertar con columnas de partición calculadas ─────────
    stmt = t_env.execute_sql("""
    INSERT INTO sensors_parquet
    SELECT
        device_id,
        temperature_c,
        unit_original,
        ts,
        _ingested_at,
        DATE_FORMAT(event_time, 'yyyy') AS year,
        DATE_FORMAT(event_time, 'MM')   AS month,
        DATE_FORMAT(event_time, 'dd')   AS day,
        DATE_FORMAT(event_time, 'HH')   AS hour
    FROM sensors_clean
    WHERE temperature_c IS NOT NULL
    """)

    log.info("Job iniciado. Escritura en: s3a://%s/%s/year=.../month=.../day=.../hour=.../",
             S3_BUCKET, S3_PATH)
    stmt.get_job_client().get_job_execution_result().result()


if __name__ == "__main__":
    main()
