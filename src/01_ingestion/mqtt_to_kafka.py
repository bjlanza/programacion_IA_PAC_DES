"""
mqtt_to_kafka.py - Bridge MQTT → Redpanda (Kafka)

Suscribe al topic MQTT 'sensores/raw', valida el mensaje JSON
y lo reenvía al topic Kafka 'sensores_raw'.

Uso:
  python src/01_ingestion/mqtt_to_kafka.py

Variables de entorno (opcionales, sobrescriben defaults):
  MQTT_HOST        (default: mosquitto)
  MQTT_PORT        (default: 1883)
  MQTT_TOPIC       (default: sensores/raw)
  KAFKA_BROKER     (default: redpanda:29092)
  KAFKA_TOPIC      (default: sensores_raw)
"""

import json
import os
import signal
import sys
import time

import paho.mqtt.client as mqtt
from confluent_kafka import Producer, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic

# ── Configuración ─────────────────────────────────────────────
MQTT_HOST   = os.getenv("MQTT_HOST",    "mosquitto")
MQTT_PORT   = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC  = os.getenv("MQTT_TOPIC",   "sensores/raw")

KAFKA_BROKER = os.getenv("KAFKA_BROKER", "redpanda:29092")
KAFKA_TOPIC  = os.getenv("KAFKA_TOPIC",  "sensores_raw")

# Campos obligatorios en cada mensaje
REQUIRED_FIELDS = {"device_id", "sensor", "value", "ts"}

# ── Estado global ─────────────────────────────────────────────
_producer: Producer = None
_stats = {"received": 0, "forwarded": 0, "invalid": 0, "errors": 0}
_running = True


def signal_handler(sig, frame):
    global _running
    print("\n[bridge] Señal recibida, cerrando...")
    _running = False


# ── Kafka helpers ─────────────────────────────────────────────

def ensure_topic(broker: str, topic: str):
    """Crea el topic si no existe."""
    admin = AdminClient({"bootstrap.servers": broker})
    fs = admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    for t, f in fs.items():
        try:
            f.result()
            print(f"[bridge] Topic '{t}' creado")
        except KafkaException as e:
            if "already exists" in str(e).lower() or e.args[0].code() == 36:
                print(f"[bridge] Topic '{t}' ya existe")
            else:
                raise


def on_delivery(err, msg):
    if err:
        _stats["errors"] += 1
        print(f"[bridge] ✗ Error entrega Kafka: {err}")
    else:
        _stats["forwarded"] += 1


# ── MQTT callbacks ────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[bridge] Conectado a MQTT {MQTT_HOST}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC, qos=0)
        print(f"[bridge] Suscrito a '{MQTT_TOPIC}'")
    else:
        print(f"[bridge] Error MQTT rc={rc}")
        sys.exit(1)


def on_message(client, userdata, msg):
    global _stats
    _stats["received"] += 1

    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        _stats["invalid"] += 1
        print(f"[bridge] ✗ JSON inválido: {e}")
        return

    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        _stats["invalid"] += 1
        print(f"[bridge] ✗ Campos faltantes: {missing}")
        return

    # Enriquecer con metadatos de ingesta
    payload["_ingested_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["_source_topic"] = msg.topic

    key = payload["device_id"].encode("utf-8")
    value = json.dumps(payload).encode("utf-8")

    _producer.produce(KAFKA_TOPIC, key=key, value=value, callback=on_delivery)
    _producer.poll(0)  # disparar callbacks pendientes sin bloquear

    print(
        f"[bridge] ↦ {payload['device_id']}/{payload['sensor']} "
        f"= {payload['value']} → {KAFKA_TOPIC}"
    )


def on_disconnect(client, userdata, rc):
    if rc != 0:
        print(f"[bridge] ⚠ Desconexión inesperada de MQTT (rc={rc})")


# ── Main ──────────────────────────────────────────────────────

def run():
    global _producer

    print("[bridge] Iniciando MQTT → Kafka bridge")
    print(f"  MQTT   : {MQTT_HOST}:{MQTT_PORT}  topic={MQTT_TOPIC}")
    print(f"  Kafka  : {KAFKA_BROKER}  topic={KAFKA_TOPIC}")
    print()

    # Kafka Producer
    _producer = Producer(
        {
            "bootstrap.servers": KAFKA_BROKER,
            "linger.ms": 10,
            "acks": "all",
        }
    )
    ensure_topic(KAFKA_BROKER, KAFKA_TOPIC)

    # MQTT Client
    client = mqtt.Client(client_id="mqtt-kafka-bridge")
    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    print("[bridge] En ejecución. Ctrl+C para detener.\n")
    try:
        while _running:
            time.sleep(5)
            _producer.flush(timeout=1)
            print(
                f"[bridge] Stats — recibidos={_stats['received']} "
                f"reenviados={_stats['forwarded']} "
                f"inválidos={_stats['invalid']} "
                f"errores={_stats['errors']}"
            )
    finally:
        client.loop_stop()
        client.disconnect()
        _producer.flush(timeout=5)
        print("\n[bridge] Bridge detenido.")
        print(f"[bridge] Stats finales: {_stats}")


if __name__ == "__main__":
    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    run()
