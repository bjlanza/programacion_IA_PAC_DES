# Grafana — Datasources y Endpoints

## Qué es Infinity Datasource

**Infinity** es un plugin de Grafana que permite consultar **cualquier API HTTP** como fuente de datos: JSON, CSV, XML o GraphQL. No requiere que el servicio tenga un datasource oficial en Grafana.

En este proyecto lo usamos para los servicios que no tienen datasource nativo:

| Servicio   | Datasource usado | Por qué                                      |
|------------|-----------------|----------------------------------------------|
| InfluxDB   | InfluxDB nativo | Plugin oficial, soporta Flux directamente    |
| Redpanda   | Infinity        | No hay datasource Kafka/Redpanda en Grafana  |
| Flink      | Infinity        | No hay datasource Flink en Grafana           |
| FastAPI    | Infinity        | API REST propia, se consulta como JSON       |

---

## Endpoints disponibles por servicio

Todos los hostnames son internos a la red Docker (`ilerna_ia_big_data`). Grafana accede a ellos desde dentro del contenedor.

### Redpanda — Admin API (puerto 9644)

| Endpoint | Descripción | Formato respuesta |
|---|---|---|
| `http://redpanda:9644/v1/brokers` | Lista de brokers del cluster | Array de objetos |
| `http://redpanda:9644/v1/cluster/health_overview` | Estado de salud del cluster (`is_healthy`, nodos caídos, particiones sin líder) | Objeto |
| `http://redpanda:9644/v1/topics/{topic}/partitions/0/replicas` | High watermark de una partición | Array con campo `high_watermark` |

> **Notas:**
> - `GET /v1/cluster` devuelve **404 en Redpanda v25** — usar `/v1/cluster/health_overview`.
> - `GET /v1/topics` (listado de todos los topics) también devuelve 404. Los paneles de topics usan datos inline estáticos.

### Redpanda — Pandaproxy HTTP (puerto 8082)

| Endpoint | Descripción | Formato respuesta |
|---|---|---|
| `http://redpanda:8082/topics` | Lista de nombres de topics | Array de strings |
| `http://redpanda:8082/topics/{topic}/offsets` | Offsets por topic | Objeto con offsets |

### Flink — REST API (puerto 8081)

| Endpoint | Descripción | Formato respuesta |
|---|---|---|
| `http://jobmanager:8081/jobs/overview` | Resumen de todos los jobs con estado | Objeto con array `jobs` |
| `http://jobmanager:8081/jobs` | Lista de job IDs | Objeto con array `jobs` |
| `http://jobmanager:8081/taskmanagers` | Info de los task managers | Objeto con array `taskmanagers` |

### FastAPI (puerto 8000)

| Endpoint | Descripción | Formato respuesta |
|---|---|---|
| `http://workspace:8000/machines/status` | Estado actual de todas las máquinas | Array de objetos |
| `http://workspace:8000/machines/{id}` | Detalle de una máquina concreta | Objeto |
| `http://workspace:8000/alerts` | Alertas activas | Array de objetos |

> **Nota:** El hostname `workspace` puede variar. Si no resuelve, prueba `api` o `localhost`.

### InfluxDB — API Flux (puerto 8086)

Aunque se usa el datasource nativo, el endpoint subyacente es:

```
POST http://influxdb:8086/api/v2/query?org=ilerna
Authorization: Token supersecrettoken
Content-Type: application/vnd.flux
```

---

## Dashboards provisionados

Los dashboards están definidos en `config/grafana/provisioning/dashboards/` y Grafana los recarga automáticamente cada 30 segundos. **No se pueden guardar desde la UI** — cualquier cambio debe hacerse en el archivo JSON directamente.

| Archivo | UID | Título |
|---|---|---|
| `pipeline.json` | `ilerna-smartindustry` | ILERNA Smart-Industry — Telemetría IoT |
| `redpanda.json` | `ilerna-redpanda` | ILERNA Smart-Industry — Redpanda / Kafka |
| `fastapi.json` | *(ver archivo)* | ILERNA Smart-Industry — FastAPI |

Para ver el JSON actual de un dashboard desde la API de Grafana:

```
http://localhost:13000/api/dashboards/uid/ilerna-redpanda
http://localhost:13000/api/dashboards/uid/ilerna-smartindustry
```

### Otros endpoints de la API de Grafana

| Endpoint | Descripción |
|---|---|
| `http://localhost:13000/api/health` | Estado de Grafana |
| `http://localhost:13000/api/datasources` | Datasources configurados |
| `http://localhost:13000/api/search` | Lista de dashboards |
| `http://localhost:13000/api/alerts` | Alertas activas |

---

## Configuración de un panel Infinity — referencia rápida

Para añadir un nuevo panel que consuma una API REST:

1. Datasource: **Infinity**
2. Type: **JSON**
3. Parser: **Backend**
4. Source: **URL**
5. Method: **GET**
6. URL: el endpoint deseado (ver tablas arriba)
7. En _Parsing options_: añadir columnas con `selector` = nombre del campo JSON

Para datos estáticos (sin llamada HTTP):
- Source: **Inline**
- Data: JSON array como string, p.ej. `[{"topic":"sensors_raw"},...]`
