FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies (e.g. tzdata for timezone)
RUN apt-get update && apt-get install -y tzdata && \
    ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . /app

# Ensure correct PYTHONPATH so that internal modules can find each other
ENV PYTHONPATH=/app

# Start the Python scheduler
ENTRYPOINT ["python", "scheduler.py"]
