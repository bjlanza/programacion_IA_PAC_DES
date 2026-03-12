"""
anomaly_model.py — Detección de Anomalías con IsolationForest (scikit-learn)

Diferencia entre detección Edge vs Cloud:
  · Edge (Flink): reglas de negocio hardcoded — avg_temp_c > 80°C.
    Rápido, determinista, sin modelos.
  · Cloud (FastAPI, este módulo): modelo ML estadístico — IsolationForest.
    Aprende el comportamiento "normal" de cada máquina a partir de datos
    históricos y detecta anomalías multivariadas (no solo umbrales fijos).

IsolationForest:
  · Algoritmo de detección de outliers basado en árboles de aislamiento.
  · No necesita datos etiquetados (unsupervised).
  · Parámetro clave: contamination = proporción esperada de anomalías.
  · Score: más negativo → más anómalo.

Uso desde FastAPI:
  from src.api.anomaly_model import detector
  detector.train(temperatures)
  result = detector.predict(current_temp)
"""

import logging
import pickle
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest

log = logging.getLogger("anomaly-model")

# Ruta para persistir el modelo entrenado
MODEL_PATH = Path("/tmp/isolation_forest.pkl")


class AnomalyDetector:
    """
    Wrapper sobre IsolationForest de scikit-learn.
    Mantiene un modelo por instancia, configurable por contaminación esperada.
    """

    def __init__(self, contamination: float = 0.1, n_estimators: int = 100):
        self.contamination  = contamination
        self.n_estimators   = n_estimators
        self._model: Optional[IsolationForest] = None
        self._is_trained    = False
        self._n_samples     = 0
        self._feature_stats: dict = {}

    @property
    def is_trained(self) -> bool:
        return self._is_trained

    def train(self, temperatures: list[float]) -> dict:
        """
        Entrena el modelo con una lista de temperaturas (°C).
        Mínimo recomendado: 50 muestras para un resultado estable.
        """
        if len(temperatures) < 10:
            raise ValueError(f"Se necesitan al menos 10 muestras (recibidas: {len(temperatures)})")

        X = np.array(temperatures, dtype=float).reshape(-1, 1)

        self._model = IsolationForest(
            contamination=self.contamination,
            n_estimators=self.n_estimators,
            random_state=42,
        )
        self._model.fit(X)
        self._is_trained = True
        self._n_samples  = len(temperatures)
        self._feature_stats = {
            "mean":  float(np.mean(X)),
            "std":   float(np.std(X)),
            "min":   float(np.min(X)),
            "max":   float(np.max(X)),
            "p5":    float(np.percentile(X, 5)),
            "p95":   float(np.percentile(X, 95)),
        }

        # Persistir en disco para recargar tras reinicios
        try:
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(self, f)
            log.info("Modelo guardado en %s", MODEL_PATH)
        except Exception as e:
            log.warning("No se pudo guardar el modelo: %s", e)

        log.info("Modelo entrenado con %d muestras | media=%.2f°C std=%.2f°C",
                 self._n_samples, self._feature_stats["mean"], self._feature_stats["std"])
        return {"trained": True, "samples": self._n_samples, "stats": self._feature_stats}

    def predict(self, temperature_c: float) -> dict:
        """
        Predice si una temperatura es anómala.

        Returns:
          is_anomaly        → bool
          anomaly_score     → float (más negativo = más anómalo, rango aprox [-0.5, 0.5])
          failure_prob      → float [0.0, 1.0] — probabilidad normalizada de fallo
          interpretation    → str — explicación legible
        """
        if not self._is_trained or self._model is None:
            return {"error": "Modelo no entrenado. Llama primero a POST /model/train"}

        X = np.array([[temperature_c]])
        prediction   = self._model.predict(X)[0]         # 1 = normal, -1 = anomaly
        score        = float(self._model.score_samples(X)[0])   # más negativo → más raro

        # Normalizar score a probabilidad [0, 1]
        # score típico: [-0.5, 0.1] → invertimos y escalamos
        raw_prob = max(0.0, min(1.0, (-score - 0.1) / 0.4))

        is_anomaly = prediction == -1
        stats      = self._feature_stats

        if is_anomaly:
            if temperature_c > stats.get("p95", 0):
                interpretation = f"Temperatura excepcionalmente ALTA ({temperature_c:.1f}°C > p95={stats['p95']:.1f}°C)"
            elif temperature_c < stats.get("p5", 0):
                interpretation = f"Temperatura excepcionalmente BAJA ({temperature_c:.1f}°C < p5={stats['p5']:.1f}°C)"
            else:
                interpretation = f"Comportamiento anómalo detectado (score={score:.3f})"
        else:
            interpretation = f"Temperatura normal ({temperature_c:.1f}°C, media={stats.get('mean', 0):.1f}°C)"

        return {
            "temperature_c":  temperature_c,
            "is_anomaly":     is_anomaly,
            "anomaly_score":  round(score, 4),
            "failure_prob":   round(raw_prob, 4),
            "interpretation": interpretation,
            "model_stats":    stats,
        }

    def load_from_disk(self) -> bool:
        """Intenta cargar un modelo previamente entrenado desde disco."""
        try:
            if MODEL_PATH.exists():
                with open(MODEL_PATH, "rb") as f:
                    saved: "AnomalyDetector" = pickle.load(f)
                self._model        = saved._model
                self._is_trained   = saved._is_trained
                self._n_samples    = saved._n_samples
                self._feature_stats = saved._feature_stats
                log.info("Modelo cargado desde disco (%d muestras)", self._n_samples)
                return True
        except Exception as e:
            log.warning("No se pudo cargar modelo desde disco: %s", e)
        return False


# Singleton — se comparte entre todos los workers de FastAPI
detector = AnomalyDetector(contamination=0.1)
detector.load_from_disk()
