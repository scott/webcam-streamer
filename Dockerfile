FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py *.sh *.yaml ./
COPY music ./music

RUN mkdir -p /app/stream_buffers && \
    chmod +x stream_processor.sh

ENV PATH="/root/.local/bin:${PATH}"

CMD ["python", "stream_manager.py"]
