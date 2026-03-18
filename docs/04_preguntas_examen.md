# Preguntas de Examen — ILERNA Smart-Industry PAC DES

Preguntas y respuestas organizadas por bloque temático, cubriendo los conceptos clave del pipeline.

---

## 1. Arquitectura general

**¿Qué es la Arquitectura Lambda y cómo se aplica en este proyecto?**

La Arquitectura Lambda combina dos paths de procesamiento:
- **Hot path** (baja latencia): Flink → InfluxDB → consultas en segundos. Sirve para monitorización en tiempo real y alertas.
- **Cold path** (alta escala): Flink/Python → MinIO JSON → consultas históricas. Sirve para análisis mensual y entrenamiento ML.
- **Serving layer**: DuckDB ejecuta un `UNION ALL` en memoria combinando ambas fuentes sin mover datos.

---

**¿Por qué se usa Redpanda en lugar de Kafka?**

Redpanda es compatible con la API de Kafka pero es más ligero (binario único en C++, sin JVM ni ZooKeeper). Para un entorno de desarrollo como Codespaces reduce el consumo de memoria y simplifica el despliegue, manteniendo la misma interfaz (`rpk`, `confluent_kafka`).

---

**¿Qué rol cumple cada topic Kafka?**

| Topic | Contenido |
|---|---|
| `sensors_raw` | Mensajes crudos validados por el bridge (temperature, unit, ts) |
| `sensors_clean` | Datos normalizados a Celsius por Flink (temperature_c, unit_original) |
| `sensors_invalid` | DLQ: mensajes con schema inválido o hash roto |
| `sensors_verified` | Mensajes con hash-chain verificado (integridad confirmada) |

---

## 2. Ingesta (Hito 1)

**¿Qué validaciones hace `mqtt_to_redpanda_bridge.py` antes de publicar en Kafka?**

1. Parsea JSON del payload MQTT.
2. Verifica que existan los campos `device_id`, `temperature`, `unit`, `ts`.
3. Valida que `unit` sea uno de `{C, F, K}`.
4. Enriquece el mensaje con `_mqtt_topic` e `_ingested_at`.
5. Publica en `sensors_raw` con `enable.idempotence=True` y clave Kafka = `device_id`.

---

**¿Para qué sirve `enable.idempotence=True` en el productor Kafka?**

Garantiza que en caso de reintento por fallo de red el broker no registre el mismo mensaje dos veces. El productor asigna un ID de secuencia; si el broker ya recibió ese ID, descarta el duplicado. Evita que un reintento ante un ACK perdido genere mensajes duplicados en el topic.

---

**¿Por qué se usa QoS 1 en MQTT y cómo se relaciona con la idempotencia de Kafka?**

MQTT QoS 1 garantiza entrega *at-least-once*: el broker confirma la recepción pero puede haber reenvíos. Combinado con `enable.idempotence=True` en el lado Kafka se consigue un pipeline *effectively-once*: MQTT puede reenviar pero Kafka no duplica.

---

## 3. Normalización con Flink (Hito 2)

**¿Qué hace `flink_normalization_job.py`?**

Consume `sensors_raw`, aplica una UDF `to_celsius` para convertir F→C y K→C, añade el campo `unit_original` y envía los resultados a `sensors_clean`. Los mensajes con campos inválidos se desvían al DLQ `sensors_invalid` mediante `StatementSet` (dos sinks en el mismo job).

---

**¿Cuáles son las fórmulas de conversión de temperatura?**

- Fahrenheit a Celsius: `(F - 32) × 5/9`
- Kelvin a Celsius: `K - 273.15`
- Celsius: sin cambio.

---

**¿Qué es una UDF en Flink Table API?**

Una *User-Defined Function* que extiende el SQL de Flink con lógica personalizada. Se registra con `table_env.create_temporary_function("nombre", ClasePython)`. En este proyecto `to_celsius` es un `ScalarFunction` que recibe `temperature` y `unit` y devuelve `temperature_c`.

---

**¿Qué es un `StatementSet` y para qué se usa aquí?**

Permite ejecutar varios `INSERT INTO` en el mismo pipeline Flink. En lugar de abrir dos jobs separados, el normalizador envía datos válidos a `sensors_clean` e inválidos a `sensors_invalid` en un único job con un solo plan de ejecución, reduciendo recursos.

---

## 4. Analítica con Flink (Hito 3)

**¿Qué es una Tumble Window y por qué se usa una de 1 minuto?**

Una ventana de tiempo fija no solapada. Todos los eventos que llegan entre `T` y `T+60s` se agrupan y se emite un resultado al cerrar la ventana. Se usa 1 minuto porque es un compromiso entre latencia (suficientemente rápido para detectar alertas) y agregación (suficientes muestras para calcular una media fiable).

---

**¿Qué es un watermark y para qué sirve el de 10 segundos?**

Un watermark es una marca de progreso temporal que indica al motor de streaming hasta qué punto del tiempo de evento puede considerar que ya han llegado todos los datos. Un watermark de 10s significa que Flink espera hasta 10 segundos de retraso antes de cerrar una ventana, tolerando mensajes desordenados hasta ese margen.

---

**¿Qué métricas calcula `flink_analytics_job.py` por ventana?**

Por cada (`device_id`, ventana de 1 min):
- `avg_temp_c` — temperatura media en Celsius
- `max_temp_c` — temperatura máxima
- `count` — número de lecturas
- `alert` — `1` si `avg_temp_c > 80°C`, `0` en caso contrario

Escribe en InfluxDB measurement `machine_stats`.

---

**¿Por qué el Flink UI muestra "Records Received: 0" aunque el job esté procesando datos?**

Es un bug conocido del Beam runner de PyFlink 1.18: las métricas de registros de la UI no se actualizan correctamente para jobs Python. No indica un problema real; para verificar el funcionamiento hay que consultar directamente InfluxDB o el topic `sensors_clean`.

---

## 5. Almacenamiento (InfluxDB y MinIO)

**¿Por qué se usa InfluxDB y no una base de datos relacional para el hot path?**

InfluxDB es una base de datos de series temporales optimizada para escrituras de alta frecuencia con timestamps. Ofrece retención automática, compresión nativa de datos temporales y el lenguaje Flux para consultas sobre rangos de tiempo. Una base relacional requeriría índices especiales y tendría peor rendimiento en consultas tipo `range(start: -5m)`.

---

**¿Qué es el Line Protocol de InfluxDB?**

Formato de texto plano para escribir datos:
```
measurement,tag1=v1,tag2=v2 field1=v1,field2=v2 timestamp_ns
```
Por ejemplo:
```
machine_stats,device_id=machine-001 avg_temp_c=72.3,alert=0i 1710768000000000000
```

---

**¿Qué es el particionado Hive en MinIO y para qué sirve?**

Organizar los ficheros en directorios con nombre `columna=valor`:
```
clean/year=2026/month=03/day=18/hour=14/archivo.json
```
DuckDB (y herramientas como Spark, Athena) pueden leer solo las particiones necesarias sin escanear todos los ficheros, reduciendo I/O. Se usa `hive_partitioning=true` en la query DuckDB.

---

**¿Por qué se eligió JSON (NDJSON) en lugar de Parquet para el cold path Python?**

El conector FileSystem de Flink requiere JARs adicionales que presentaban conflictos de dependencias en el entorno. El writer Python alternativo usa NDJSON (un JSON por línea) que DuckDB lee directamente con `read_json()` y también soporta particionado Hive. Parquet requeriría `pyarrow` y es más complejo de generar desde Python puro.

---

## 6. API REST (Hito 4)

**¿Qué endpoints expone FastAPI y para qué sirve cada uno?**

| Endpoint | Descripción |
|---|---|
| `GET /health` | Estado de la API y conectividad InfluxDB |
| `GET /machines/status` | Última lectura normalizada de cada máquina + alerta |
| `GET /machines/{id}` | Historial de temperatura de una máquina |
| `GET /machines/{id}/predict` | Predicción de anomalía con IsolationForest |
| `GET /alerts` | Ventanas donde avg > umbral |
| `GET /stats` | Estadísticas globales (media, min, max) |
| `POST /model/train` | Entrena IsolationForest con datos históricos |
| `POST /machines/publish` | Publica lectura manual vía MQTT |

---

**¿Cómo consulta FastAPI los datos de InfluxDB?**

Usa `influxdb_client` (SDK oficial) con queries en lenguaje Flux. Cada request crea un cliente, ejecuta la query y cierra la conexión en el bloque `finally`. Ejemplo de query: `from(bucket:"sensores") |> range(start: -5m) |> filter(fn: (r) => r._measurement == "machine_stats")`.

---

## 7. Detección de Anomalías (IA)

**¿Cuál es la diferencia entre la detección de Flink y la del modelo ML?**

| | Flink (Edge) | IsolationForest (Cloud) |
|---|---|---|
| Tipo | Regla fija | Modelo estadístico |
| Criterio | `avg_temp_c > 80°C` | Comportamiento estadísticamente raro |
| Latencia | Segundos (streaming) | Bajo demanda (REST) |
| Entrenamiento | No necesita | Requiere datos históricos |
| Ventaja | Determinista, sin modelo | Detecta anomalías sutiles sin umbral fijo |

---

**¿Qué es IsolationForest y cómo funciona?**

Algoritmo de detección de outliers no supervisado. Construye árboles de decisión aleatorios y mide cuántas particiones necesita para aislar cada punto. Los puntos anómalos se aíslan con pocas particiones (caminos cortos), los normales requieren más. El `score` resultante es más negativo cuanto más anómalo es el punto.

---

**¿Qué significa el parámetro `contamination`?**

La fracción esperada de anomalías en el conjunto de entrenamiento. Con `contamination=0.1` se espera que el 10% de los datos sean anómalos. Ajusta el umbral de decisión del modelo: un valor más alto hace al modelo más permisivo (menos falsos positivos pero más falsos negativos).

---

## 8. Query Federada (DuckDB)

**¿Qué es DuckDB y qué rol tiene en la Arquitectura Lambda?**

DuckDB es un motor SQL analítico embebido (sin servidor). En este proyecto actúa como capa de consulta unificada: ejecuta un `UNION ALL` entre datos de InfluxDB (hot) y MinIO (cold) en memoria, sin necesidad de copiar datos a ningún lado. Usa la extensión `httpfs` para leer ficheros JSON directamente desde MinIO via S3.

---

**¿Qué ventaja tiene usar DuckDB para la query federada frente a copiar datos a una sola BD?**

- No hay movimiento de datos → menor latencia y sin duplicación.
- Funciona con fuentes heterogéneas (time-series + object storage).
- El `UNION ALL` se ejecuta en memoria en el servidor de la API.
- Escala horizontalmente: cada fuente puede crecer independientemente.

---

## 9. Seguridad e Integridad

**¿Qué es el hash-chaining SHA256 del simulador?**

Cada mensaje lleva un campo `hash` calculado como `SHA256(hash_anterior + payload_actual)`. Forma una cadena: si se modifica cualquier mensaje intermedio, el hash de todos los posteriores es inválido. `flink_hash_verifier_job.py` verifica la cadena por `device_id` y envía mensajes corruptos al DLQ `sensors_invalid`.

---

**¿Qué diferencia hay entre `sensors_invalid` y `sensors_verified`?**

- `sensors_invalid` (DLQ): mensajes rechazados — schema incorrecto o hash roto. Sirve para auditoría.
- `sensors_verified`: mensajes con hash-chain validado — integridad confirmada.

---

## 10. Infraestructura y DevOps

**¿Qué hace `init_pipeline.sh` al ejecutarlo?**

1. Crea los 4 topics Kafka en Redpanda.
2. Crea el bucket `datalake` en MinIO.
3. Configura el alias `mc` para comandos MinIO.
4. Lanza los 4 jobs Flink (si no están ya corriendo).
5. Añade aliases de desarrollo a `~/.bashrc` (`sim`, `bridge`, `api`, `ui`, `flink-jobs`, etc.).

---

**¿Por qué el TaskManager de Flink puede caerse en Codespaces?**

Codespaces de 2 cores / 8GB RAM ejecuta simultáneamente: jobmanager (560MB), taskmanager (400MB), workspace (2GB+), redpanda, influxdb, minio, grafana... Con memoria justa, el sistema puede OOM-killar el TaskManager. La solución es ejecutar `flink-restart` para relanzar los jobs tras el reinicio.

---

**¿Cuáles son los puertos de los servicios y cómo se accede a cada uno?**

| Puerto | Servicio | Uso |
|---|---|---|
| 11883 | Mosquitto (MQTT) | Publicar/suscribir mensajes de sensores |
| 13000 | Grafana | Dashboards de monitorización |
| 18000 | FastAPI | API REST del pipeline |
| 18080 | Redpanda Console | UI del broker Kafka |
| 18081 | Flink UI | Monitorización de jobs Flink |
| 18086 | InfluxDB | Base de datos de series temporales |
| 18501 | Streamlit | Dashboard interactivo |
| 18888 | JupyterLab | Notebooks de análisis |
| 19000 | MinIO S3 API | Acceso programático al datalake |
| 19001 | MinIO Console | UI web de MinIO |
| 19092 | Redpanda Kafka API | Producers/consumers Kafka |

---

## 11. Preguntas de concepto rápido

**¿Qué es un Consumer Group en Kafka?**
Un conjunto de consumidores que comparten la carga de un topic. Cada partición es asignada a un único consumidor del grupo. Permite escalar el consumo horizontalmente y mantiene el offset por grupo independientemente.

**¿Qué es el offset en Kafka?**
Un número entero que identifica la posición de un mensaje dentro de una partición. Los consumidores guardan (`commit`) su offset para saber desde dónde continuar en caso de reinicio.

**¿Qué diferencia hay entre `earliest` y `latest` offset?**
- `earliest`: el consumidor empieza desde el primer mensaje disponible (procesa histórico).
- `latest`: solo procesa mensajes nuevos a partir del momento de suscripción.

**¿Qué es un DLQ (Dead Letter Queue)?**
Topic o cola donde se envían mensajes que no pudieron procesarse correctamente. Permite auditoría, reprocesamiento manual y evita que mensajes corruptos bloqueen el pipeline principal.

**¿Qué es el watermark en streaming y por qué importa en ventanas?**
Una estimación del "tiempo de evento actual" que avanza a medida que llegan datos. Sin watermark Flink no sabría cuándo cerrar una ventana temporal porque los eventos pueden llegar desordenados. El watermark define cuánto retraso se tolera antes de emitir el resultado de una ventana.

**¿Qué ventaja tiene el formato NDJSON frente a JSON array?**
NDJSON (una línea = un JSON) permite leer el fichero en streaming sin cargar todo en memoria. También facilita la lectura parcial y el append atómico. DuckDB lo lee directamente con `read_json()`.
