"""
mqtt_to_redpanda_bridge.py — Hito 1: Puente MQTT → Redpanda

Suscribe al topic MQTT 'sensors/telemetry', valida el JSON recibido
y publica mensajes válidos en el topic Kafka 'sensors_raw'.

Schema esperado (mínimo obligatorio):
  {
    "device_id":   "machine-001",   # str, no vacío
    "temperature": 75.3,            # float, numérico
    "unit":        "C",             # "C", "F" o "K"
    "ts":          "2026-..."       # ISO-8601
  }

Mensajes inválidos son descartados y registrados como warnings.

Variables de entorno:
  MQTT_HOST, MQTT_PORT, MQTT_TOPIC, MQTT_QOS
  KAFKA_BOOTSTRAP, KAFKA_TOPIC, KAFKA_ACKS, KAFKA_KEY_FIELD, LOG_LEVEL
"""

import json
import logging
import os
import signal
import time
from typing import Optional

import paho.mqtt.client as mqtt
from confluent_kafka import Producer

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("mqtt-bridge")

# ── Configuración ─────────────────────────────────────────────
MQTT_HOST  = os.getenv("MQTT_HOST",  "localhost")
MQTT_PORT  = int(os.getenv("MQTT_PORT",  "11883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "sensors/telemetry")
MQTT_QOS   = int(os.getenv("MQTT_QOS",   "1"))

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:19092")
KAFKA_TOPIC     = os.getenv("KAFKA_TOPIC",     "sensors_raw")
KAFKA_KEY_FIELD = os.getenv("KAFKA_KEY_FIELD", "device_id")
KAFKA_ACKS      = os.getenv("KAFKA_ACKS",      "all")

KAFKA_CONF = {
    "bootstrap.servers":  KAFKA_BOOTSTRAP,
    "acks":               KAFKA_ACKS,
    "enable.idempotence": True,
    "retries":            10,
    "retry.backoff.ms":   500,
    "linger.ms":          20,
    "batch.num.messages": 1000,
    "client.id":          "mqtt_to_redpanda_bridge",
}

# Unidades de temperatura aceptadas
VALID_UNITS = {"C", "F", "K"}

_running = True
_stats = {"received": 0, "forwarded": 0, "invalid": 0, "errors": 0}


# ── Validación de esquema ─────────────────────────────────────

def validate(payload: dict) -> Optional[str]:
    """
    Valida el mensaje de sensor.
    Devuelve None si es válido, o un string con el motivo de rechazo.
    """
    if not isinstance(payload.get("device_id"), str) or not payload["device_id"].strip():
        return "device_id ausente o vacío"

    temp = payload.get("temperature")
    if temp is None:
        return "campo 'temperature' ausente"
    try:
        float(temp)
    except (TypeError, ValueError):
        return f"temperature no es numérico: {temp!r}"

    unit = payload.get("unit", "")
    if str(unit).upper() not in VALID_UNITS:
        return f"unidad no reconocida: {unit!r} (esperado C, F o K)"

    if not payload.get("ts"):
        return "campo 'ts' ausente"

    return None  # válido


# ── Kafka callbacks ───────────────────────────────────────────

def delivery_report(err, msg):
    if err is not None:
        _stats["errors"] += 1
        log.error("Kafka delivery failed: %s", err)
    else:
        log.debug("Delivered → %s [%d] offset=%d",
                  msg.topic(), msg.partition(), msg.offset())


def extract_key(payload: dict) -> Optional[str]:
    if KAFKA_KEY_FIELD and KAFKA_KEY_FIELD in payload:
        return str(payload[KAFKA_KEY_FIELD])
    return None


# ── MQTT callbacks ────────────────────────────────────────────

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        log.info("MQTT conectado a %s:%s", MQTT_HOST, MQTT_PORT)
        client.subscribe(MQTT_TOPIC, qos=MQTT_QOS)
        log.info("Suscrito a topic=%s qos=%s", MQTT_TOPIC, MQTT_QOS)
    else:
        log.error("Conexión MQTT fallida reason_code=%s", reason_code)


def on_message(client, userdata, msg):
    """Cada mensaje MQTT → validar → producir a Kafka."""
    producer: Producer = userdata["producer"]
    _stats["received"] += 1

    # 1. Decodificar bytes
    try:
        raw = msg.payload.decode("utf-8", errors="strict")
    except Exception as e:
        _stats["invalid"] += 1
        log.warning("Payload no es UTF-8 (descartado): %s", e)
        return

    # 2. Parsear JSON
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("El JSON no es un objeto")
    except Exception as e:
        _stats["invalid"] += 1
        log.warning("JSON inválido (descartado): %s | raw=%r", e, raw[:200])
        return

    # 3. Validar esquema de sensor
    error = validate(payload)
    if error:
        _stats["invalid"] += 1
        log.warning("Mensaje rechazado — %s | payload=%r", error, payload)
        return

    # 4. Enriquecer con metadatos de trazabilidad
    payload["unit"] = payload["unit"].upper()  # normalizar a mayúsculas
    payload["_mqtt_topic"]   = msg.topic
    payload["_mqtt_qos"]     = msg.qos
    payload["_ingested_at"]  = int(time.time() * 1000)

    key   = extract_key(payload)
    value = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # 5. Producir a Kafka
    try:
        producer.produce(
            topic=KAFKA_TOPIC,
            key=key.encode("utf-8") if key else None,
            value=value,
            on_delivery=delivery_report,
        )
        producer.poll(0)
        _stats["forwarded"] += 1
        log.info("↦ %s | %.2f%s → %s",
                 payload["device_id"], payload["temperature"], payload["unit"], KAFKA_TOPIC)
    except BufferError:
        log.warning("Buffer Kafka lleno, forzando flush...")
        producer.flush(2)
    except Exception as e:
        _stats["errors"] += 1
        log.exception("Error produciendo a Kafka: %s", e)


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        log.warning("Desconexión inesperada MQTT reason_code=%s", reason_code)


# ── Shutdown ──────────────────────────────────────────────────

def handle_shutdown(signum, frame):
    global _running
    log.info("Señal %s recibida. Cerrando bridge...", signum)
    _running = False


# ── Main ──────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGINT,  handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    log.info("Bridge: MQTT %s:%s (%s) → Kafka %s topic=%s",
             MQTT_HOST, MQTT_PORT, MQTT_TOPIC, KAFKA_BOOTSTRAP, KAFKA_TOPIC)

    producer = Producer(KAFKA_CONF)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="mqtt_to_redpanda_bridge",
    )
    client.user_data_set({"producer": producer})
    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect

    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    log.info("Bridge en ejecución. Ctrl+C para detener.")
    try:
        while _running:
            time.sleep(0.2)
    finally:
        log.info("Deteniendo loop MQTT...")
        client.loop_stop()
        client.disconnect()

        log.info("Flush Kafka producer...")
        producer.flush(10)

        log.info("Bridge detenido. Stats finales: %s", _stats)


if __name__ == "__main__":
    main()
