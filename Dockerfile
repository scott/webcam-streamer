FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x from NodeSource
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py *.sh ./
COPY configs ./configs
COPY music ./music

RUN mkdir -p /app/stream_buffers && \
    chmod +x stream_processor.sh

ENV PATH="/root/.local/bin:/usr/local/bin:${PATH}"

# Default CMD - requires --config argument
CMD ["python", "stream_manager.py", "--config", "configs/streams/ski-resort.yaml"]
