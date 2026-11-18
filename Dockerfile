FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-inter fonts-dejavu-core curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the speech model and the YuNet face detector into the image so
# containers start without downloading anything.
ARG WHISPER_MODEL=base
ENV CLIPVIRAL_WHISPER_MODEL=${WHISPER_MODEL}
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"
RUN mkdir -p /models && curl -fsSL -o /models/face_detection_yunet_2023mar.onnx \
    https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    || echo "YuNet download failed; dlib HOG detector will be used"
ENV CLIPVIRAL_FACE_MODEL=/models/face_detection_yunet_2023mar.onnx \
    CLIPVIRAL_DATA=/data

COPY clipviral ./clipviral
COPY web ./web
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "clipviral.main:app", "--host", "0.0.0.0", "--port", "8000"]
