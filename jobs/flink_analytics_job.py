"""
flink_analytics_job.py — Hito 3: Inteligencia y Alertas con Flink

Consume de 'sensors_clean', calcula medias por minuto (Tumble Window)
por dispositivo, detecta alertas (avg > 80°C) y escribe en InfluxDB.

Flujo:
  sensors_clean (Kafka)
    → Tumble Window 1 min por device_id
    → avg_temp_c, max_temp_c, count
    → si avg_temp_c > 80°C → ALERT
    → InfluxDB measurement 'machine_stats'

Schema sensors_clean:
  device_id STRING, temperature_c DOUBLE, ts STRING (ISO-8601)

Ejecutar en el contenedor jobmanager:
  flink run -py /opt/flink/jobs/flink_analytics_job.py \\
    --jarfile /opt/flink/lib/flink-sql-connector-kafka-3.1.0-1.18.jar

Variables de entorno:
  KAFKA_BROKER, INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET
  ALERT_THRESHOLD (default: 80.0)
"""

import logging
import os
import urllib.error
import urllib.request

from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.functions import MapFunction, SinkFunction
from pyflink.table import StreamTableEnvironment, DataTypes, Row
from pyflink.table.udf import udf

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("flink-analytics")

# ── Configuración ─────────────────────────────────────────────
KAFKA_BROKER  = os.getenv("KAFKA_BROKER",  "redpanda:29092")
KAFKA_INPUT   = os.getenv("KAFKA_INPUT",   "sensors_clean")
KAFKA_GROUP   = os.getenv("KAFKA_GROUP",   "flink-analytics")

INFLUX_URL    = os.getenv("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "supersecrettoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "ilerna")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "sensores")

ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "80.0"))


# ── Sink: escribe en InfluxDB vía HTTP Line Protocol ─────────

class InfluxDBSink(SinkFunction):
    """
    Escribe resultados de ventana en InfluxDB usando el Line Protocol
    a través de urllib (sin dependencias externas en el contenedor Flink).
    """

    def __init__(self, url: str, token: str, org: str, bucket: str, threshold: float):
        self._url       = f"{url}/api/v2/write?org={org}&bucket={bucket}&precision=s"
        self._token     = token
        self._threshold = threshold

    def invoke(self, value, context):
        """
        value es una Row con:
          device_id, window_start, window_end, avg_temp_c, max_temp_c, count_readings
        """
        device_id    = value[0]
        window_start = value[1]   # datetime
        avg_temp     = value[3]
        max_temp     = value[4]
        count        = value[5]
        alert        = 1 if avg_temp > self._threshold else 0

        ts_seconds = int(window_start.timestamp())

        # InfluxDB Line Protocol:
        # measurement,tag=value field=value timestamp
        line = (
            f"machine_stats,"
            f"device_id={device_id} "
            f"avg_temp_c={avg_temp:.4f},"
            f"max_temp_c={max_temp:.4f},"
            f"count={count}i,"
            f"alert={alert}i "
            f"{ts_seconds}"
        )

        try:
            req = urllib.request.Request(
                self._url,
                data=line.encode("utf-8"),
                headers={
                    "Authorization": f"Token {self._token}",
                    "Content-Type":  "text/plain; charset=utf-8",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status not in (200, 204):
                    log.error("InfluxDB HTTP %s para %s", resp.status, device_id)
                else:
                    status = "⚠ ALERTA" if alert else "✓"
                    log.info("%s %s | avg=%.2f°C max=%.2f°C n=%d",
                             status, device_id, avg_temp, max_temp, count)
        except urllib.error.HTTPError as e:
            log.error("InfluxDB error HTTP %s: %s", e.code, e.read().decode()[:200])
        except Exception as e:
            log.error("Error escribiendo en InfluxDB: %s", e)


# ── Main ──────────────────────────────────────────────────────

def main():
    env   = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)

    log.info("Job analytics: %s → InfluxDB | umbral_alerta=%.1f°C",
             KAFKA_INPUT, ALERT_THRESHOLD)

    # ── Tabla fuente con event time ───────────────────────────
    # ts viene como STRING ISO-8601; lo convertimos a TIMESTAMP para windowing
    t_env.execute_sql(f"""
    CREATE TABLE sensors_clean (
        device_id      STRING,
        temperature_c  DOUBLE,
        unit_original  STRING,
        ts             STRING,
        _ingested_at   BIGINT,
        event_time     AS TO_TIMESTAMP(ts, 'yyyy-MM-dd''T''HH:mm:ss''Z'''),
        WATERMARK FOR event_time AS event_time - INTERVAL '10' SECOND
    ) WITH (
        'connector'                     = 'kafka',
        'topic'                         = '{KAFKA_INPUT}',
        'properties.bootstrap.servers'  = '{KAFKA_BROKER}',
        'properties.group.id'           = '{KAFKA_GROUP}',
        'scan.startup.mode'             = 'earliest-offset',
        'format'                        = 'json',
        'json.ignore-parse-errors'      = 'true'
    )
    """)

    # ── Ventana Tumble de 1 minuto por dispositivo ────────────
    result_table = t_env.sql_query(f"""
    SELECT
        device_id,
        TUMBLE_START(event_time, INTERVAL '1' MINUTE)   AS window_start,
        TUMBLE_END(event_time,   INTERVAL '1' MINUTE)   AS window_end,
        AVG(temperature_c)                               AS avg_temp_c,
        MAX(temperature_c)                               AS max_temp_c,
        COUNT(*)                                         AS count_readings
    FROM sensors_clean
    WHERE temperature_c IS NOT NULL
    GROUP BY
        device_id,
        TUMBLE(event_time, INTERVAL '1' MINUTE)
    """)

    # Convertir Table → DataStream para usar sink personalizado
    ds = t_env.to_append_stream(
        result_table,
        DataTypes.ROW([
            DataTypes.FIELD("device_id",      DataTypes.STRING()),
            DataTypes.FIELD("window_start",   DataTypes.TIMESTAMP(3)),
            DataTypes.FIELD("window_end",     DataTypes.TIMESTAMP(3)),
            DataTypes.FIELD("avg_temp_c",     DataTypes.DOUBLE()),
            DataTypes.FIELD("max_temp_c",     DataTypes.DOUBLE()),
            DataTypes.FIELD("count_readings", DataTypes.BIGINT()),
        ])
    )

    # ── Sink: InfluxDB ────────────────────────────────────────
    ds.add_sink(InfluxDBSink(
        url=INFLUX_URL,
        token=INFLUX_TOKEN,
        org=INFLUX_ORG,
        bucket=INFLUX_BUCKET,
        threshold=ALERT_THRESHOLD,
    ))

    log.info("Ejecutando job de analítica...")
    env.execute("sensor-analytics-tumble-1min")


if __name__ == "__main__":
    main()
