# src/02_processing — Módulos de Transformación y Validación

Funciones Python puras que replican la lógica de los jobs Flink del pipeline.
Útiles para notebooks, tests, FastAPI y auditorías offline.

## Módulos

| Módulo | Descripción |
|--------|-------------|
| `transformations.py` | Conversiones de temperatura (UDF `to_celsius`) |
| `validators.py` | Validación de esquema JSON de mensajes |
| `hash_chain.py` | Verificación de integridad SHA256 en cadena |

---

## transformations.py

Replica la UDF `to_celsius` del job `flink_normalization_job.py`.

```python
from src.processing.transformations import to_celsius, normalize_record

# Conversión simple
celsius = to_celsius(176.0, "F")   # → 80.0
celsius = to_celsius(353.15, "K")  # → 80.0
celsius = to_celsius(80.0, "C")    # → 80.0

# Normalizar un mensaje completo de sensors_raw al formato sensors_clean
record = {
    "device_id": "machine-001",
    "temperature": 176.0,
    "unit": "F",
    "ts": "2026-03-11T09:00:00Z",
    "hash": "abc...", "prev_hash": "000..."
}
clean = normalize_record(record)
# → {"device_id": "machine-001", "temperature_c": 80.0, "unit_original": "F", ...}
# → None si el registro es inválido (temperatura fuera de rango, unidad desconocida, etc.)

# Procesar un batch
clean_list, invalid_list = normalize_batch(records)
```

---

## validators.py

Replica las validaciones del bridge `mqtt_to_redpanda_bridge.py`.

```python
from src.processing.validators import validate_raw_message, validate_clean_message

ok, reason = validate_raw_message({
    "device_id": "machine-001",
    "temperature": 75.0,
    "unit": "C",
    "ts": "2026-03-11T09:00:00Z",
    "hash": "abc...",
    "prev_hash": "000..."
})
# → (True, "")

ok, reason = validate_raw_message({"device_id": "m-001", "unit": "X", ...})
# → (False, "invalid_unit:X")

# Filtrar un batch
valid, invalid = filter_valid(records, schema="raw")
```

Campos validados en `sensors_raw`: `device_id`, `temperature`, `unit` ∈ {C, F, K}, `ts`, `hash`, `prev_hash`.

---

## hash_chain.py

Replica la lógica del job `flink_hash_verifier_job.py` para auditoría offline.

```python
from src.processing.hash_chain import compute_hash, verify_chain, chain_integrity_report

# Calcular hash de un mensaje
h = compute_hash(msg, prev_hash="0" * 64)

# Verificar una secuencia completa (mensajes del mismo device_id, ordenados por ts)
results = verify_chain(messages)
# → [{"idx": 0, "device_id": "m-001", "ts": "...", "ok": True, "reason": ""}, ...]

# Resumen
report = chain_integrity_report(results)
# → {"total": 50, "valid": 48, "invalid": 2, "integrity_pct": 96.0, "broken_at": [12, 31]}
```

---

## Uso en notebooks

```python
import sys, os
sys.path.insert(0, os.path.abspath('..'))   # desde notebooks/

from src.processing.transformations import to_celsius, normalize_batch
from src.processing.validators import validate_raw_message
from src.processing.hash_chain import verify_chain, chain_integrity_report
```

Ver `notebooks/02_analisis_calidad.ipynb` para ejemplos completos de análisis de calidad de datos.
