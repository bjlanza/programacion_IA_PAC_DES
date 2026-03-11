"""
kafka_to_influx.py - Consumidor Kafka → InfluxDB

Lee mensajes del topic 'sensores_raw' en Redpanda y los escribe
como puntos de serie temporal en InfluxDB (bucket 'sensores').

Uso:
  python src/03_storage/kafka_to_influx.py

Variables de entorno (opcionales):
  KAFKA_BROKER      (default: localhost:19092)
  KAFKA_TOPIC       (default: sensores_raw)
  KAFKA_GROUP       (default: storage-influx)
  INFLUX_URL        (default: http://localhost:18086)
  INFLUX_TOKEN      (default: supersecrettoken)
  INFLUX_ORG        (default: ilerna)
  INFLUX_BUCKET     (default: sensores)
"""

import json
import os
import signal
import sys
import time

from confluent_kafka import Consumer, KafkaError, KafkaException
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# ── Configuración ─────────────────────────────────────────────
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:29092")
KAFKA_TOPIC  = os.getenv("KAFKA_TOPIC",  "sensores_raw")
KAFKA_GROUP  = os.getenv("KAFKA_GROUP",  "storage-influx")

INFLUX_URL    = os.getenv("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "supersecrettoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "ilerna")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "sensores")

BATCH_SIZE    = 50    # Puntos por lote
FLUSH_SECONDS = 5.0   # Forzar flush cada N segundos

_running = True
_stats = {"consumed": 0, "written": 0, "errors": 0}


def signal_handler(sig, frame):
    global _running
    print("\n[influx-writer] Señal recibida, deteniendo...")
    _running = False


def message_to_point(data: dict) -> Point:
    """Convierte un dict de sensor en un InfluxDB Point."""
    point = (
        Point("sensor_reading")
        .tag("device_id", data["device_id"])
        .tag("sensor",    data["sensor"])
        .field("value",   float(data["value"]))
    )
    if "unit" in data:
        point = point.tag("unit", data["unit"])
    return point


def run():
    print("[influx-writer] Iniciando Kafka → InfluxDB writer")
    print(f"  Kafka  : {KAFKA_BROKER}  topic={KAFKA_TOPIC}  group={KAFKA_GROUP}")
    print(f"  InfluxDB: {INFLUX_URL}  bucket={INFLUX_BUCKET}")
    print()

    # InfluxDB client
    influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    # Kafka consumer
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BROKER,
            "group.id": KAFKA_GROUP,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([KAFKA_TOPIC])
    print(f"[influx-writer] Suscrito a '{KAFKA_TOPIC}'. Ctrl+C para detener.\n")

    batch = []
    last_flush = time.monotonic()

    try:
        while _running:
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                pass
            elif msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    pass  # fin de partición, normal
                else:
                    raise KafkaException(msg.error())
            else:
                _stats["consumed"] += 1
                try:
                    data = json.loads(msg.value().decode("utf-8"))
                    point = message_to_point(data)
                    batch.append(point)
                    print(
                        f"[influx-writer] ← {data.get('device_id')}/{data.get('sensor')} "
                        f"= {data.get('value')}"
                    )
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    _stats["errors"] += 1
                    print(f"[influx-writer] ✗ Mensaje inválido: {e}")

            # Flush por tamaño o por tiempo
            elapsed = time.monotonic() - last_flush
            if batch and (len(batch) >= BATCH_SIZE or elapsed >= FLUSH_SECONDS):
                try:
                    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=batch)
                    consumer.commit(asynchronous=False)
                    _stats["written"] += len(batch)
                    print(f"[influx-writer] ✓ {len(batch)} punto(s) escritos en InfluxDB")
                    batch = []
                    last_flush = time.monotonic()
                except Exception as e:
                    _stats["errors"] += 1
                    print(f"[influx-writer] ✗ Error escribiendo en InfluxDB: {e}")

    finally:
        # Flush final
        if batch:
            try:
                write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=batch)
                _stats["written"] += len(batch)
                print(f"[influx-writer] ✓ Flush final: {len(batch)} punto(s)")
            except Exception as e:
                print(f"[influx-writer] ✗ Error en flush final: {e}")

        consumer.close()
        influx.close()
        print(f"\n[influx-writer] Detenido. Stats: {_stats}")


if __name__ == "__main__":
    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    run()
