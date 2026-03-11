"""
anomaly_detector.py - Job PyFlink: detección de anomalías en sensores

Lee del topic Kafka 'sensores_raw', detecta valores fuera de rango
y publica alertas al topic 'sensores_alertas'.

Ejecutar dentro del contenedor jobmanager:
  flink run -py /opt/flink/jobs/anomaly_detector.py

Requiere PyFlink (incluido en la imagen flink:1.18.1-java11).
"""

import json
import os

from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import (
    FlinkKafkaConsumer,
    FlinkKafkaProducer,
)
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.typeinfo import Types

# ── Configuración ─────────────────────────────────────────────
KAFKA_BROKER  = os.getenv("KAFKA_BROKER",  "redpanda:29092")
KAFKA_INPUT   = os.getenv("KAFKA_INPUT",   "sensores_raw")
KAFKA_OUTPUT  = os.getenv("KAFKA_OUTPUT",  "sensores_alertas")

# Umbrales de anomalía por tipo de sensor
THRESHOLDS = {
    "temperatura": {"min": 0.0,   "max": 50.0},
    "humedad":     {"min": 10.0,  "max": 95.0},
    "presion":     {"min": 950.0, "max": 1080.0},
}


def detect_anomaly(msg: str) -> str | None:
    """
    Evalúa si un mensaje de sensor es una anomalía.
    Devuelve JSON de alerta o None si es normal.
    """
    try:
        data = json.loads(msg)
        sensor = data.get("sensor", "")
        value  = float(data.get("value", 0))
        limits = THRESHOLDS.get(sensor)

        if limits and (value < limits["min"] or value > limits["max"]):
            alert = {
                "type":      "ANOMALY",
                "device_id": data.get("device_id"),
                "sensor":    sensor,
                "value":     value,
                "min":       limits["min"],
                "max":       limits["max"],
                "ts":        data.get("ts"),
                "source":    data,
            }
            return json.dumps(alert)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def main():
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)

    kafka_props = {
        "bootstrap.servers": KAFKA_BROKER,
        "group.id":          "flink-anomaly-detector",
        "auto.offset.reset": "earliest",
    }

    # Source: leer de sensores_raw
    consumer = FlinkKafkaConsumer(
        topics=KAFKA_INPUT,
        deserialization_schema=SimpleStringSchema(),
        properties=kafka_props,
    )
    consumer.set_start_from_earliest()

    # Sink: escribir alertas a sensores_alertas
    producer = FlinkKafkaProducer(
        topic=KAFKA_OUTPUT,
        serialization_schema=SimpleStringSchema(),
        producer_config={"bootstrap.servers": KAFKA_BROKER},
    )

    stream = env.add_source(consumer)

    # Filtrar y transformar anomalías
    alerts = (
        stream
        .map(detect_anomaly, output_type=Types.STRING())
        .filter(lambda x: x is not None)
    )

    alerts.add_sink(producer)

    print(f"[flink] Iniciando job: {KAFKA_INPUT} → anomaly detector → {KAFKA_OUTPUT}")
    env.execute("sensor-anomaly-detector")


if __name__ == "__main__":
    main()
