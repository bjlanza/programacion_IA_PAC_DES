"""
transformations.py — Funciones de transformación de temperatura del pipeline.

Estas son las mismas conversiones que usa el job Flink (flink_normalization_job.py)
expuestas como funciones Python puras para uso en notebooks, tests y FastAPI.

Uso:
    from src.processing.transformations import to_celsius, normalize_record

    record = {"temperature": 98.6, "unit": "F", "device_id": "machine-001", "ts": "..."}
    celsius = to_celsius(98.6, "F")   # → 37.0
    clean   = normalize_record(record) # → {"device_id": ..., "temperature_c": 37.0, ...}
"""

from __future__ import annotations

from typing import Any


def to_celsius(value: float, unit: str) -> float:
    """Convierte una temperatura a grados Celsius.

    Args:
        value: Valor numérico de temperatura.
        unit:  Unidad de origen: "C", "F" o "K" (insensible a mayúsculas).

    Returns:
        Temperatura en grados Celsius.

    Raises:
        ValueError: Si la unidad no es reconocida.
    """
    unit = unit.strip().upper()
    if unit == "C":
        return float(value)
    if unit == "F":
        return (float(value) - 32.0) * 5.0 / 9.0
    if unit == "K":
        return float(value) - 273.15
    raise ValueError(f"Unidad desconocida: '{unit}'. Se esperaba C, F o K.")


def normalize_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normaliza un registro de sensors_raw al formato sensors_clean.

    Aplica to_celsius y descarta registros con temperatura fuera de rango físico
    (-50°C … 200°C) o con campos obligatorios ausentes.

    Args:
        record: Diccionario con al menos: device_id, temperature, unit, ts.

    Returns:
        Diccionario normalizado con temperature_c, unit_original y los campos
        originales; o None si el registro es inválido.
    """
    try:
        device_id   = record["device_id"]
        temperature = float(record["temperature"])
        unit        = str(record["unit"])
        ts          = record["ts"]
    except (KeyError, TypeError, ValueError):
        return None

    try:
        temp_c = to_celsius(temperature, unit)
    except ValueError:
        return None

    # Descartar valores físicamente imposibles
    if not (-50.0 <= temp_c <= 200.0):
        return None

    return {
        "device_id":      device_id,
        "temperature_c":  round(temp_c, 4),
        "unit_original":  unit,
        "ts":             ts,
        "_ingested_at":   record.get("_ingested_at", ""),
        "hash":           record.get("hash", ""),
        "prev_hash":      record.get("prev_hash", ""),
    }


def normalize_batch(records: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Procesa un batch de registros raw.

    Returns:
        (clean_records, invalid_records) — listas de registros válidos e inválidos.
    """
    clean, invalid = [], []
    for rec in records:
        result = normalize_record(rec)
        if result is not None:
            clean.append(result)
        else:
            invalid.append({"raw": rec, "reason": "normalization_failed"})
    return clean, invalid
