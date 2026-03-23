"""
validators.py — Validación de esquema JSON para mensajes del pipeline.

El bridge MQTT→Redpanda (mqtt_to_redpanda_bridge.py) valida los mensajes
antes de publicarlos en sensors_raw. Este módulo expone esas mismas
validaciones como funciones reutilizables.

Uso:
    from src.processing.validators import validate_raw_message, ValidationError

    ok, err = validate_raw_message(msg)
    if not ok:
        print(f"Rechazado: {err}")
"""

from __future__ import annotations

from typing import Any

# Campos obligatorios en un mensaje de sensors_raw
REQUIRED_FIELDS_RAW = {"device_id", "temperature", "unit", "ts", "hash", "prev_hash"}

# Unidades de temperatura aceptadas
VALID_UNITS = {"C", "F", "K"}

# Límites físicos de temperatura (en la unidad original) para detección de datos corruptos
TEMP_LIMITS: dict[str, tuple[float, float]] = {
    "C": (-50.0,  200.0),
    "F": (-58.0,  392.0),
    "K": (223.15, 473.15),
}


class ValidationError(Exception):
    """Excepción para mensajes que no superan la validación de esquema."""


def validate_raw_message(msg: dict[str, Any]) -> tuple[bool, str]:
    """Valida un mensaje de sensors_raw.

    Comprueba:
    1. Que todos los campos obligatorios están presentes.
    2. Que 'unit' es C, F o K.
    3. Que 'temperature' es un número dentro del rango físico.
    4. Que 'device_id' y 'ts' no están vacíos.

    Args:
        msg: Diccionario Python representando el mensaje JSON.

    Returns:
        (True, "") si el mensaje es válido.
        (False, reason) si el mensaje es inválido.
    """
    # 1. Campos obligatorios
    missing = REQUIRED_FIELDS_RAW - set(msg.keys())
    if missing:
        return False, f"missing_fields:{','.join(sorted(missing))}"

    # 2. device_id y ts no vacíos
    if not str(msg.get("device_id", "")).strip():
        return False, "empty_device_id"
    if not str(msg.get("ts", "")).strip():
        return False, "empty_ts"

    # 3. Unidad válida
    unit = str(msg.get("unit", "")).strip().upper()
    if unit not in VALID_UNITS:
        return False, f"invalid_unit:{unit}"

    # 4. Temperatura numérica y dentro de rango
    try:
        temp = float(msg["temperature"])
    except (TypeError, ValueError):
        return False, "temperature_not_numeric"

    lo, hi = TEMP_LIMITS[unit]
    if not (lo <= temp <= hi):
        return False, f"temperature_out_of_range:{temp}{unit}"

    return True, ""


def validate_clean_message(msg: dict[str, Any]) -> tuple[bool, str]:
    """Valida un mensaje de sensors_clean (post-normalización).

    Comprueba que tiene device_id, temperature_c (float, -50…200) y ts.
    """
    required = {"device_id", "temperature_c", "ts"}
    missing = required - set(msg.keys())
    if missing:
        return False, f"missing_fields:{','.join(sorted(missing))}"

    try:
        temp_c = float(msg["temperature_c"])
    except (TypeError, ValueError):
        return False, "temperature_c_not_numeric"

    if not (-50.0 <= temp_c <= 200.0):
        return False, f"temperature_c_out_of_range:{temp_c}"

    return True, ""


def filter_valid(records: list[dict[str, Any]], schema: str = "raw") -> tuple[list[dict], list[dict]]:
    """Filtra una lista de registros aplicando la validación de esquema.

    Args:
        records: Lista de mensajes a validar.
        schema:  "raw" para sensors_raw, "clean" para sensors_clean.

    Returns:
        (valid_records, invalid_records)
    """
    validate_fn = validate_raw_message if schema == "raw" else validate_clean_message
    valid, invalid = [], []
    for rec in records:
        ok, reason = validate_fn(rec)
        if ok:
            valid.append(rec)
        else:
            invalid.append({**rec, "_validation_error": reason})
    return valid, invalid
