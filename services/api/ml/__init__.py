"""Batch ML jobs: anomaly detection, forecasting and right-sizing.

Training runs as a CLI inside the API image and writes artifacts to
ML_ARTIFACT_DIR; the API loads them at startup and on POST /ml/reload.
"""
