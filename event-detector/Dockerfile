FROM python:3.12-slim
WORKDIR /app/event-detector
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY event-detector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY event-detector/ .
COPY ce_definitions/ /app/ce_definitions/
COPY android-app/app/src/main/assets/model.tflite /app/models/model.tflite
COPY android-app/app/src/main/assets/labels.txt /app/models/labels.txt
COPY android-app/app/src/main/assets/yamnet.tflite /app/models/yamnet.tflite
ENV CE_DEFINITION_DIR=/app/ce_definitions
CMD ["python", "-m", "services.visualization", "--host", "0.0.0.0"]
