"""
flink_normalization_job.py — Hito 2: Normalización con Flink Table API + UDF + DLQ

Lee de Kafka topic 'sensors_raw', aplica una UDF Python para convertir
la temperatura a Celsius y enruta los mensajes en dos destinos:

  ✅ sensors_clean   → mensajes válidos y normalizados
  ❌ sensors_invalid → Dead Letter Queue (DLQ): mensajes corruptos con motivo de rechazo

Flujo:
  sensors_raw (Kafka)
       │
       ├─[válidos]──→ to_celsius UDF → sensors_clean (Kafka)
       └─[inválidos]─→ + reason     → sensors_invalid (Kafka)  ← DLQ

Criterios de invalidez (cualquiera es suficiente):
  · device_id NULL o vacío
  · temperature NULL (JSON malformado o campo ausente)
  · unit NULL o no reconocida (no es C, F ni K)
  · temperatura convertida a °C < -50°C  (físicamente imposible en industria)
  · temperatura convertida a °C > 1000°C (físicamente imposible en industria)

Ejecutar en el contenedor jobmanager:
  flink run -py /opt/flink/jobs/flink_normalization_job.py

Variables de entorno:
  KAFKA_BROKER   (default: redpanda:29092)
  KAFKA_INPUT    (default: sensors_raw)
  KAFKA_OUTPUT   (default: sensors_clean)
  KAFKA_DLQ      (default: sensors_invalid)
  KAFKA_GROUP    (default: flink-normalization)
"""

import logging
import os

from pyflink.datastream import StreamExecutionEnvironment
from pyflink.table import StreamTableEnvironment, DataTypes
from pyflink.table.udf import udf

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("flink-normalization")

# ── Configuración ─────────────────────────────────────────────
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:29092")
KAFKA_INPUT  = os.getenv("KAFKA_INPUT",  "sensors_raw")
KAFKA_OUTPUT = os.getenv("KAFKA_OUTPUT", "sensors_clean")
KAFKA_DLQ    = os.getenv("KAFKA_DLQ",   "sensors_invalid")
KAFKA_GROUP  = os.getenv("KAFKA_GROUP",  "flink-normalization")

TEMP_MIN_C = -50.0
TEMP_MAX_C = 1000.0


# ── UDF: Conversión de unidades ───────────────────────────────

@udf(result_type=DataTypes.DOUBLE())
def to_celsius(temperature: float, unit: str) -> float:
    """
    Convierte temperatura de F o K a Celsius.
    Devuelve None si los argumentos son None (propagación segura).
    """
    if temperature is None or unit is None:
        return None
    unit = unit.strip().upper()
    if unit == "F":
        return (temperature - 32.0) * 5.0 / 9.0
    if unit == "K":
        return temperature - 273.15
    return float(temperature)  # "C" → pass-through


# ── Main ──────────────────────────────────────────────────────

def main():
    env   = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)

    t_env.create_temporary_function("to_celsius", to_celsius)

    log.info("Broker: %s | %s → %s (DLQ: %s)",
             KAFKA_BROKER, KAFKA_INPUT, KAFKA_OUTPUT, KAFKA_DLQ)

    # ── Tabla fuente ──────────────────────────────────────────
    # json.ignore-parse-errors=true convierte campos inválidos en NULL
    # en lugar de fallar el job completo.
    t_env.execute_sql(f"""
    CREATE TABLE sensors_raw (
        device_id    STRING,
        temperature  DOUBLE,
        unit         STRING,
        ts           STRING,
        _ingested_at BIGINT,
        proc_time    AS PROCTIME()
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

    # ── Tabla destino: mensajes válidos ───────────────────────
    t_env.execute_sql(f"""
    CREATE TABLE sensors_clean (
        device_id     STRING,
        temperature_c DOUBLE,
        unit_original STRING,
        ts            STRING,
        _ingested_at  BIGINT
    ) WITH (
        'connector'                    = 'kafka',
        'topic'                        = '{KAFKA_OUTPUT}',
        'properties.bootstrap.servers' = '{KAFKA_BROKER}',
        'format'                       = 'json'
    )
    """)

    # ── Tabla DLQ: mensajes inválidos con motivo ──────────────
    t_env.execute_sql(f"""
    CREATE TABLE sensors_invalid (
        device_id    STRING,
        temperature  DOUBLE,
        unit         STRING,
        ts           STRING,
        _ingested_at BIGINT,
        reason       STRING
    ) WITH (
        'connector'                    = 'kafka',
        'topic'                        = '{KAFKA_DLQ}',
        'properties.bootstrap.servers' = '{KAFKA_BROKER}',
        'format'                       = 'json'
    )
    """)

    # ── StatementSet: ejecuta ambos INSERTs en un único job ───
    # Flink garantiza que cada mensaje va a exactamente uno de los dos sinks.
    stmt_set = t_env.create_statement_set()

    # INSERT 1: mensajes válidos → sensors_clean
    stmt_set.add_insert_sql(f"""
    INSERT INTO sensors_clean
    SELECT
        device_id,
        to_celsius(temperature, unit) AS temperature_c,
        unit                          AS unit_original,
        ts,
        _ingested_at
    FROM sensors_raw
    WHERE
        device_id   IS NOT NULL
        AND device_id   <> ''
        AND temperature IS NOT NULL
        AND unit        IS NOT NULL
        AND UPPER(TRIM(unit)) IN ('C', 'F', 'K')
        AND to_celsius(temperature, unit) >= {TEMP_MIN_C}
        AND to_celsius(temperature, unit) <= {TEMP_MAX_C}
    """)

    # INSERT 2: mensajes inválidos → sensors_invalid (DLQ)
    # CASE determina el primer motivo de rechazo encontrado.
    stmt_set.add_insert_sql(f"""
    INSERT INTO sensors_invalid
    SELECT
        device_id,
        temperature,
        unit,
        ts,
        _ingested_at,
        CASE
            WHEN device_id IS NULL OR device_id = ''
                THEN 'device_id ausente o vacío'
            WHEN temperature IS NULL
                THEN 'temperature ausente o no numérico'
            WHEN unit IS NULL OR UPPER(TRIM(unit)) NOT IN ('C', 'F', 'K')
                THEN CONCAT('unidad no reconocida: ', COALESCE(unit, 'null'))
            WHEN to_celsius(temperature, unit) < {TEMP_MIN_C}
                THEN CONCAT('temperatura bajo mínimo: ',
                     CAST(CAST(to_celsius(temperature, unit) AS DECIMAL(10,2)) AS STRING), 'C')
            WHEN to_celsius(temperature, unit) > {TEMP_MAX_C}
                THEN CONCAT('temperatura sobre máximo: ',
                     CAST(CAST(to_celsius(temperature, unit) AS DECIMAL(10,2)) AS STRING), 'C')
            ELSE 'motivo desconocido'
        END AS reason
    FROM sensors_raw
    WHERE
        device_id   IS NULL
        OR device_id = ''
        OR temperature IS NULL
        OR unit        IS NULL
        OR UPPER(TRIM(unit)) NOT IN ('C', 'F', 'K')
        OR to_celsius(temperature, unit) < {TEMP_MIN_C}
        OR to_celsius(temperature, unit) > {TEMP_MAX_C}
    """)

    log.info("Lanzando job con DLQ...")
    log.info("  ✅ Válidos   → %s", KAFKA_OUTPUT)
    log.info("  ❌ Inválidos → %s (DLQ)", KAFKA_DLQ)

    result = stmt_set.execute()
    result.get_job_client().get_job_execution_result().result()


if __name__ == "__main__":
    main()
