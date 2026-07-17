FROM python:3.11.9-slim-bookworm

# Set working directory
WORKDIR /app

# Install system dependencies (e.g. tzdata for timezone)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl && \
    ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . /app
RUN mkdir -p /opt/gmr && cp -a /app/quant-strategy /opt/gmr/quant-strategy

# Ensure correct PYTHONPATH so that internal modules can find each other
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV XDG_CACHE_HOME=/tmp/.cache
ENV MPLCONFIGDIR=/tmp/matplotlib

RUN groupadd --gid 10001 radar && \
    useradd --create-home --uid 10001 --gid 10001 radar && \
    mkdir -p /data /app/logs /app/industry-radar/reports && \
    chmod 0555 /app/docker-entrypoint.sh && \
    chown -R radar:radar /app /opt/gmr /data
USER radar

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python /app/scheduler.py --healthcheck

# Populate the writable quant runtime, then start the fail-closed scheduler.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
