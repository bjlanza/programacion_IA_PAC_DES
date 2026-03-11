"""
main.py - FastAPI REST API para el dashboard de sensores (ILERNA Smart-Industry)

Endpoints:
  GET  /health                      → Estado de la API
  GET  /machines/status             → Estado actual de todas las máquinas (Hito 4)
  GET  /machines/{device_id}        → Historial de una máquina
  GET  /machines/{device_id}/predict→ Predicción de anomalía con IsolationForest (IA Cloud)
  GET  /alerts                      → Alertas recientes (avg > 80°C, detección Flink)
  GET  /stats                       → Estadísticas agregadas
  POST /machines/publish            → Publica lectura manual vía MQTT
  POST /model/train                 → Entrena el modelo ML con datos de InfluxDB
  GET  /model/status                → Estado del modelo ML

Uso:
  uvicorn src.04_api.main:app --host 0.0.0.0 --port 8000 --reload
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from influxdb_client import InfluxDBClient
from pydantic import BaseModel

from anomaly_model import detector

# ── Configuración ─────────────────────────────────────────────
INFLUX_URL    = os.getenv("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "supersecrettoken")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "ilerna")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "sensores")

MQTT_HOST  = os.getenv("MQTT_HOST",  "mosquitto")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "sensors/telemetry")

ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "80.0"))

# ── App ───────────────────────────────────────────────────────
app = FastAPI(
    title="ILERNA Smart-Industry — Sensor API",
    description=(
        "API REST para monitorización de temperatura de máquinas industriales. "
        "Datos normalizados a Celsius por Apache Flink."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Modelos ───────────────────────────────────────────────────

class MachineReading(BaseModel):
    """Lectura de temperatura de una máquina para publicar vía MQTT."""
    device_id:   str
    temperature: float
    unit:        str = "C"   # "C", "F" o "K"


# ── InfluxDB helper ───────────────────────────────────────────

def get_influx_client() -> InfluxDBClient:
    return InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/health", tags=["Sistema"])
def health():
    """Estado de la API y conectividad con InfluxDB."""
    try:
        client = get_influx_client()
        client.ping()
        client.close()
        influx_ok = True
    except Exception:
        influx_ok = False

    return {
        "status":   "ok",
        "influxdb": "ok" if influx_ok else "error",
        "ts":       datetime.now(timezone.utc).isoformat(),
    }


@app.get("/machines/status", tags=["Máquinas"])
def machines_status(
    range_minutes: int = Query(5, ge=1, le=60, description="Ventana para última lectura")
):
    """
    Estado actual de todas las máquinas.
    Devuelve la última temperatura normalizada (°C) por dispositivo,
    indicando si está en alerta (avg_temp_c > umbral).
    """
    client = get_influx_client()
    query_api = client.query_api()

    # Obtener última lectura de cada dispositivo desde machine_stats (escritas por Flink)
    query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{range_minutes}m)
      |> filter(fn: (r) => r._measurement == "machine_stats")
      |> filter(fn: (r) => r._field == "avg_temp_c" or r._field == "alert")
      |> group(columns: ["device_id", "_field"])
      |> last()
      |> pivot(rowKey: ["device_id"], columnKey: ["_field"], valueColumn: "_value")
    """
    try:
        tables = query_api.query(query, org=INFLUX_ORG)
        machines = []
        for table in tables:
            for r in table.records:
                avg = r.values.get("avg_temp_c")
                alert_flag = r.values.get("alert", 0)
                machines.append({
                    "device_id":   r.values.get("device_id"),
                    "avg_temp_c":  round(avg, 2) if avg is not None else None,
                    "alert":       bool(alert_flag),
                    "ts":          r.get_time().isoformat() if r.get_time() else None,
                })

        return {
            "threshold_c":  ALERT_THRESHOLD,
            "range_minutes": range_minutes,
            "count":        len(machines),
            "machines":     sorted(machines, key=lambda x: x["device_id"] or ""),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.get("/machines/{device_id}", tags=["Máquinas"])
def get_machine_history(
    device_id: str,
    range_minutes: int = Query(30, ge=1, le=1440),
    limit: int = Query(200, ge=1, le=2000),
):
    """Historial de temperatura normalizada (°C) de una máquina."""
    client = get_influx_client()
    query_api = client.query_api()
    query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{range_minutes}m)
      |> filter(fn: (r) => r._measurement == "machine_stats")
      |> filter(fn: (r) => r.device_id == "{device_id}")
      |> filter(fn: (r) => r._field == "avg_temp_c")
      |> sort(columns: ["_time"], desc: false)
      |> limit(n: {limit})
    """
    try:
        tables = query_api.query(query, org=INFLUX_ORG)
        records = [
            {"ts": r.get_time().isoformat(), "avg_temp_c": round(r.get_value(), 3)}
            for table in tables
            for r in table.records
        ]
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"Sin datos para '{device_id}' en los últimos {range_minutes}min"
            )
        return {"device_id": device_id, "count": len(records), "history": records}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.get("/alerts", tags=["Alertas"])
def get_alerts(
    range_minutes: int = Query(60, ge=1, le=1440),
):
    """Ventanas de tiempo donde avg_temp_c superó el umbral de alerta."""
    client = get_influx_client()
    query_api = client.query_api()
    query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{range_minutes}m)
      |> filter(fn: (r) => r._measurement == "machine_stats")
      |> filter(fn: (r) => r._field == "avg_temp_c")
      |> filter(fn: (r) => r._value > {ALERT_THRESHOLD})
      |> sort(columns: ["_time"], desc: true)
    """
    try:
        tables = query_api.query(query, org=INFLUX_ORG)
        alerts = [
            {
                "ts":         r.get_time().isoformat(),
                "device_id":  r.values.get("device_id"),
                "avg_temp_c": round(r.get_value(), 2),
            }
            for table in tables
            for r in table.records
        ]
        return {
            "threshold_c":   ALERT_THRESHOLD,
            "range_minutes": range_minutes,
            "count":         len(alerts),
            "alerts":        alerts,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.get("/stats", tags=["Estadísticas"])
def get_stats(range_minutes: int = Query(60, ge=1, le=1440)):
    """Estadísticas globales (media, min, max) por máquina en la ventana indicada."""
    client = get_influx_client()
    query_api = client.query_api()
    query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{range_minutes}m)
      |> filter(fn: (r) => r._measurement == "machine_stats")
      |> filter(fn: (r) => r._field == "avg_temp_c")
      |> group(columns: ["device_id"])
      |> reduce(
          identity: {{count: 0, sum: 0.0, min: 9999.0, max: -9999.0}},
          fn: (r, accumulator) => ({{
              count: accumulator.count + 1,
              sum:   accumulator.sum + r._value,
              min:   if r._value < accumulator.min then r._value else accumulator.min,
              max:   if r._value > accumulator.max then r._value else accumulator.max,
          }})
      )
    """
    try:
        tables = query_api.query(query, org=INFLUX_ORG)
        stats = []
        for table in tables:
            for r in table.records:
                count = r.values.get("count", 0)
                stats.append({
                    "device_id": r.values.get("device_id"),
                    "count":     count,
                    "mean_c":    round(r.values.get("sum", 0) / count, 2) if count else None,
                    "min_c":     round(r.values.get("min", 0), 2),
                    "max_c":     round(r.values.get("max", 0), 2),
                })
        return {"range_minutes": range_minutes, "stats": sorted(stats, key=lambda x: x["device_id"] or "")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        client.close()


@app.get("/machines/{device_id}/predict", tags=["IA — Detección de Anomalías"])
def predict_anomaly(
    device_id: str,
    temperature_c: float = Query(..., description="Temperatura en °C a evaluar"),
):
    """
    Predice si una temperatura es anómala usando IsolationForest.

    **IA Edge vs Cloud:**
    - **Flink** detecta anomalías por umbral fijo (avg > 80°C) — rápido, sin modelo.
    - **Este endpoint** usa un modelo ML estadístico entrenado con datos históricos,
      capaz de detectar comportamientos inusuales aunque no superen el umbral fijo.

    Entrena el modelo primero con `POST /model/train`.
    """
    result = detector.predict(temperature_c)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return {"device_id": device_id, **result}


@app.post("/model/train", tags=["IA — Detección de Anomalías"])
def train_model(
    range_minutes: int = Query(120, ge=10, le=10080,
                               description="Minutos de datos históricos para entrenar"),
    contamination: float = Query(0.1, ge=0.01, le=0.5,
                                 description="Fracción esperada de anomalías (0.01-0.5)"),
):
    """
    Entrena el modelo IsolationForest con temperaturas reales de InfluxDB.

    El modelo aprende la distribución "normal" de temperatura y luego puede
    detectar puntos estadísticamente raros, sin necesidad de etiquetas.
    """
    client = get_influx_client()
    query_api = client.query_api()
    query = f"""
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{range_minutes}m)
      |> filter(fn: (r) => r._measurement == "machine_stats")
      |> filter(fn: (r) => r._field == "avg_temp_c")
    """
    try:
        tables = query_api.query(query, org=INFLUX_ORG)
        temperatures = [r.get_value() for table in tables for r in table.records
                        if r.get_value() is not None]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando InfluxDB: {e}")
    finally:
        client.close()

    if len(temperatures) < 10:
        raise HTTPException(
            status_code=422,
            detail=f"Datos insuficientes: {len(temperatures)} muestras (mínimo 10). "
                   f"¿Está el pipeline corriendo?"
        )

    detector.contamination = contamination
    try:
        result = detector.train(temperatures)
        return {"status": "trained", "range_minutes": range_minutes, **result}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/model/status", tags=["IA — Detección de Anomalías"])
def model_status():
    """Estado actual del modelo de detección de anomalías."""
    if not detector.is_trained:
        return {
            "trained":  False,
            "message":  "Modelo no entrenado. Ejecuta POST /model/train primero.",
        }
    return {
        "trained":       True,
        "samples":       detector._n_samples,
        "contamination": detector.contamination,
        "stats":         detector._feature_stats,
    }


@app.post("/machines/publish", tags=["Ingesta"], status_code=202)
def publish_reading(reading: MachineReading):
    """Publica manualmente una lectura de temperatura vía MQTT."""
    if reading.unit.upper() not in ("C", "F", "K"):
        raise HTTPException(status_code=422, detail="unit debe ser C, F o K")

    payload = {
        "device_id":   reading.device_id,
        "temperature": reading.temperature,
        "unit":        reading.unit.upper(),
        "ts":          datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"api-pub-{int(time.time())}",
        )
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=5)
        result = client.publish(MQTT_TOPIC, json.dumps(payload), qos=1)
        client.disconnect()

        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise HTTPException(status_code=503, detail=f"Error MQTT rc={result.rc}")

        return {"status": "published", "topic": MQTT_TOPIC, "payload": payload}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"No se pudo conectar a MQTT: {e}")
