# Build from the repository root:  docker build -f safety-engine/Dockerfile -t safety-engine .
FROM python:3.12-slim
WORKDIR /app
COPY safety-engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY safety-engine/ ./safety-engine/
COPY src/main/resources/static/ ./ui/
WORKDIR /app/safety-engine
# Train the hazard model at build time if the bundled one does not match this scikit-learn version
RUN python -c "from safe_engine.train import load_or_train; load_or_train()"
ENV UI_DIR=/app/ui PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn safe_engine.api:app --host 0.0.0.0 --port ${PORT}"]
