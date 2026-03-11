"""
flink_hash_verifier_job.py — Seguridad: Verificación de Hash-Chain con Flink

Consume de 'sensors_raw', verifica la integridad de la cadena SHA256 por
dispositivo y enruta los mensajes:

  ✅ sensors_verified  → mensajes con cadena íntegra
  ❌ sensors_invalid   → mensajes con cadena rota (DLQ) + reason="hash_chain_broken"

Algoritmo Hash-Chain:
  · El sensor_simulator mantiene un estado: prev_hash por dispositivo.
  · Cada mensaje incluye: "prev_hash" (hash anterior) y "hash" (hash actual).
  · hash = SHA256(json_del_payload_sin_campos_hash + prev_hash)
  · Flink mantiene el ÚLTIMO hash válido por device_id como estado.
  · Si hash_recibido ≠ SHA256(content + prev_hash_en_flink) → TAMPERING detectado.

Estado Flink:
  · Keyed por device_id → ValueState[str] con el último hash válido.
  · Estado inicializado con GENESIS_HASH ("0"*64) para el primer mensaje.

Ejecutar en el contenedor jobmanager:
  flink run -py /opt/flink/jobs/flink_hash_verifier_job.py

Variables de entorno:
  KAFKA_BROKER    (default: redpanda:29092)
  KAFKA_INPUT     (default: sensors_raw)
  KAFKA_VERIFIED  (default: sensors_verified)
  KAFKA_DLQ       (default: sensors_invalid)
"""

import hashlib
import json
import logging
import os

from pyflink.common import Types, WatermarkStrategy
from pyflink.datastream import StreamExecutionEnvironment, OutputTag
from pyflink.datastream.connectors.kafka import (
    FlinkKafkaConsumer,
    FlinkKafkaProducer,
)
from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream.functions import KeyedProcessFunction
from pyflink.datastream.state import ValueStateDescriptor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("flink-hash-verifier")

KAFKA_BROKER   = os.getenv("KAFKA_BROKER",   "redpanda:29092")
KAFKA_INPUT    = os.getenv("KAFKA_INPUT",    "sensors_raw")
KAFKA_VERIFIED = os.getenv("KAFKA_VERIFIED", "sensors_verified")
KAFKA_DLQ      = os.getenv("KAFKA_DLQ",      "sensors_invalid")

GENESIS_HASH = "0" * 64

# OutputTag para mensajes con cadena rota (side output → DLQ)
TAMPERED_TAG = OutputTag("tampered", Types.STRING())


def compute_hash(payload: dict, prev_hash: str) -> str:
    """Replica el cálculo del simulador: SHA256(payload_sin_hash + prev_hash)."""
    content = {k: v for k, v in payload.items() if k not in ("hash", "prev_hash")}
    raw = json.dumps(content, sort_keys=True) + prev_hash
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class HashChainVerifier(KeyedProcessFunction):
    """
    Función stateful keyed por device_id.
    Para cada mensaje:
      1. Recupera el último hash válido del estado (o GENESIS si es el primero).
      2. Recomputa el hash esperado con SHA256.
      3. Si coincide → emite al stream principal y actualiza el estado.
      4. Si no coincide → emite al side output (DLQ) con motivo de rechazo.
    """

    def open(self, runtime_context):
        descriptor = ValueStateDescriptor("last_valid_hash", Types.STRING())
        self.last_hash_state = runtime_context.get_state(descriptor)

    def process_element(self, raw_msg: str, ctx, out):
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            # JSON inválido: no tiene hash, va al DLQ directamente
            bad = json.dumps({
                "raw":    raw_msg[:500],
                "reason": "JSON inválido en hash verifier",
            })
            ctx.output(TAMPERED_TAG, bad)
            return

        device_id      = data.get("device_id", "unknown")
        reported_hash  = data.get("hash", "")
        reported_prev  = data.get("prev_hash", GENESIS_HASH)

        # Estado actual en Flink (último hash válido que vimos para este device)
        state_hash = self.last_hash_state.value() or GENESIS_HASH

        # Verificación 1: ¿el prev_hash declarado coincide con nuestro estado?
        if reported_prev != state_hash:
            data["reason"]          = "hash_chain_broken: prev_hash no coincide con estado Flink"
            data["expected_prev"]   = state_hash
            ctx.output(TAMPERED_TAG, json.dumps(data))
            return

        # Verificación 2: ¿el hash declarado es correcto?
        expected_hash = compute_hash(data, reported_prev)
        if reported_hash != expected_hash:
            data["reason"]          = "hash_chain_broken: hash incorrecto (posible manipulación)"
            data["expected_hash"]   = expected_hash
            ctx.output(TAMPERED_TAG, json.dumps(data))
            return

        # ✅ Cadena íntegra → actualizar estado y emitir al stream principal
        self.last_hash_state.update(reported_hash)
        out.collect(raw_msg)


def main():
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)

    kafka_props = {
        "bootstrap.servers": KAFKA_BROKER,
        "group.id":          "flink-hash-verifier",
        "auto.offset.reset": "earliest",
    }

    consumer = FlinkKafkaConsumer(
        topics=KAFKA_INPUT,
        deserialization_schema=SimpleStringSchema(),
        properties=kafka_props,
    )
    consumer.set_start_from_earliest()

    producer_verified = FlinkKafkaProducer(
        topic=KAFKA_VERIFIED,
        serialization_schema=SimpleStringSchema(),
        producer_config={"bootstrap.servers": KAFKA_BROKER},
    )

    producer_dlq = FlinkKafkaProducer(
        topic=KAFKA_DLQ,
        serialization_schema=SimpleStringSchema(),
        producer_config={"bootstrap.servers": KAFKA_BROKER},
    )

    stream = env.add_source(consumer)

    # Keyear por device_id para que el estado sea independiente por máquina
    # El campo device_id está dentro del JSON, así que lo extraemos primero
    keyed = stream.key_by(
        lambda msg: json.loads(msg).get("device_id", "unknown")
        if msg.startswith("{") else "unknown"
    )

    verified_stream = keyed.process(HashChainVerifier(), Types.STRING())

    # Stream principal → sensors_verified
    verified_stream.add_sink(producer_verified)

    # Side output (mensajes con cadena rota) → DLQ
    verified_stream.get_side_output(TAMPERED_TAG).add_sink(producer_dlq)

    log.info("Hash verifier: %s → %s (DLQ: %s)", KAFKA_INPUT, KAFKA_VERIFIED, KAFKA_DLQ)
    env.execute("sensor-hash-chain-verifier")


if __name__ == "__main__":
    main()
