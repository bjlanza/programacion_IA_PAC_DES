# 02_processing — Apache Flink Jobs

Los jobs de Flink se ejecutan **dentro del contenedor `jobmanager`**, no en el workspace local.

## Estructura

```
02_processing/
  anomaly_detector.py   ← Job PyFlink: detecta anomalías en sensores
```

## Enviar un job al cluster Flink

```bash
# 1. Copiar el job al volumen compartido (ya montado en /opt/flink/jobs)
#    El volumen '../jobs' ya se monta en jobmanager y taskmanager.

# 2. Ejecutar el job desde el contenedor jobmanager
docker exec -it $(docker ps -qf "label=com.docker.compose.service=jobmanager") \
  flink run -py /opt/flink/jobs/anomaly_detector.py

# 3. Ver el estado en Flink UI → http://localhost:18081
```

## Variables de entorno del job (dentro del contenedor)

| Variable       | Default               |
|----------------|-----------------------|
| KAFKA_BROKER   | redpanda:29092        |
| KAFKA_INPUT    | sensores_raw          |
| KAFKA_OUTPUT   | sensores_alertas      |
