"""
kafka_to_minio.py - Consumidor Kafka → MinIO (S3)

Lee mensajes del topic 'sensores_raw', los acumula en ventanas
de tiempo y los descarga como archivos JSON/Parquet en MinIO.

Uso:
  python src/03_storage/kafka_to_minio.py

Variables de entorno (opcionales):
  KAFKA_BROKER      (default: localhost:19092)
  KAFKA_TOPIC       (default: sensores_raw)
  KAFKA_GROUP       (default: storage-minio)
  MINIO_ENDPOINT    (default: localhost:19000)
  MINIO_ACCESS      (default: admin)
  MINIO_SECRET      (default: Ilerna_Programaci0n)
  MINIO_BUCKET      (default: datalake)
  WINDOW_SECONDS    (default: 30)  — vuelca a MinIO cada N segundos
"""

import io
import json
import os
import signal
import time
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
from confluent_kafka import Consumer, KafkaError, KafkaException
from minio import Minio
from minio.error import S3Error

# ── Configuración ─────────────────────────────────────────────
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:29092")
KAFKA_TOPIC  = os.getenv("KAFKA_TOPIC",  "sensores_raw")
KAFKA_GROUP  = os.getenv("KAFKA_GROUP",  "storage-minio")

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS   = os.getenv("MINIO_ACCESS",   "admin")
MINIO_SECRET   = os.getenv("MINIO_SECRET",   "Ilerna_Programaci0n")
MINIO_BUCKET   = os.getenv("MINIO_BUCKET",   "datalake")

WINDOW_SECONDS = int(os.getenv("WINDOW_SECONDS", "30"))

_running = True
_stats = {"consumed": 0, "files_written": 0, "errors": 0}


def signal_handler(sig, frame):
    global _running
    print("\n[minio-writer] Señal recibida, deteniendo...")
    _running = False


def ensure_bucket(client: Minio, bucket: str):
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        print(f"[minio-writer] Bucket '{bucket}' creado")
    else:
        print(f"[minio-writer] Bucket '{bucket}' ya existe")


def records_to_parquet(records: list[dict]) -> bytes:
    """Serializa una lista de dicts a Parquet en memoria."""
    # Normalizar campos para PyArrow
    rows = {
        "device_id": [r.get("device_id", "") for r in records],
        "sensor":    [r.get("sensor", "")    for r in records],
        "value":     [float(r.get("value", 0)) for r in records],
        "unit":      [r.get("unit", "")      for r in records],
        "ts":        [r.get("ts", "")        for r in records],
    }
    table = pa.table(rows)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def flush_to_minio(client: Minio, records: list[dict]) -> bool:
    if not records:
        return True
    try:
        now = datetime.now(timezone.utc)
        prefix = now.strftime("%Y/%m/%d/%H")
        filename = now.strftime("%Y%m%d_%H%M%S")
        object_name = f"raw/{prefix}/{filename}.parquet"

        data = records_to_parquet(records)
        client.put_object(
            MINIO_BUCKET,
            object_name,
            io.BytesIO(data),
            length=len(data),
            content_type="application/octet-stream",
        )
        _stats["files_written"] += 1
        print(
            f"[minio-writer] ✓ {len(records)} registros → "
            f"s3://{MINIO_BUCKET}/{object_name} ({len(data)/1024:.1f} KB)"
        )
        return True
    except S3Error as e:
        _stats["errors"] += 1
        print(f"[minio-writer] ✗ Error S3: {e}")
        return False


def run():
    print("[minio-writer] Iniciando Kafka → MinIO writer")
    print(f"  Kafka : {KAFKA_BROKER}  topic={KAFKA_TOPIC}  group={KAFKA_GROUP}")
    print(f"  MinIO : {MINIO_ENDPOINT}  bucket={MINIO_BUCKET}  ventana={WINDOW_SECONDS}s")
    print()

    minio_client = Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS,
        secret_key=MINIO_SECRET,
        secure=False,
    )
    ensure_bucket(minio_client, MINIO_BUCKET)

    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BROKER,
            "group.id": KAFKA_GROUP,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([KAFKA_TOPIC])
    print(f"[minio-writer] Suscrito a '{KAFKA_TOPIC}'. Ctrl+C para detener.\n")

    window: list[dict] = []
    window_start = time.monotonic()

    try:
        while _running:
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                pass
            elif msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    raise KafkaException(msg.error())
            else:
                _stats["consumed"] += 1
                try:
                    data = json.loads(msg.value().decode("utf-8"))
                    window.append(data)
                    print(
                        f"[minio-writer] ← [{len(window)}] "
                        f"{data.get('device_id')}/{data.get('sensor')}"
                    )
                except (json.JSONDecodeError, KeyError) as e:
                    _stats["errors"] += 1
                    print(f"[minio-writer] ✗ Mensaje inválido: {e}")

            # Volcar ventana al cumplir el tiempo
            if time.monotonic() - window_start >= WINDOW_SECONDS:
                if flush_to_minio(minio_client, window):
                    consumer.commit(asynchronous=False)
                    window = []
                window_start = time.monotonic()

    finally:
        if window:
            flush_to_minio(minio_client, window)
        consumer.close()
        print(f"\n[minio-writer] Detenido. Stats: {_stats}")


if __name__ == "__main__":
    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    run()
