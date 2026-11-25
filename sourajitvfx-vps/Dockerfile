FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl xz-utils ca-certificates procps proot && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --create-home --uid 10001 --shell /bin/bash vpsbot

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=vpsbot:vpsbot . .
RUN mkdir -p bin instances downloads /data && chown -R vpsbot:vpsbot /app /data

ENV PROOT_NO_SECCOMP=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
USER vpsbot

CMD ["python", "-u", "bot.py"]
