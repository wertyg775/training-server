# Explicit baseline; verify CUDA compatibility against the host driver before use.
ARG BASE_IMAGE=pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime
FROM ${BASE_IMAGE}
WORKDIR /app
COPY train.py /app/train.py
ENV PYTHONUNBUFFERED=1 GPU_JOB_OUTPUT_DIR=/output
CMD ["python", "/app/train.py", "--epochs", "3", "--pause", "0"]
