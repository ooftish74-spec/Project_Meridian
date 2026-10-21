# =================================================================
# Project Meridian — Production Dockerfile (Immutable Infrastructure)
# =================================================================
FROM python:3.12-slim-bookworm as base

# 1. Environment & Timezone (Seoul)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Seoul \
    ENVIRONMENT=production \
    PYTHONPATH=/app

WORKDIR /app

# 2. System Dependencies (GCC, OpenBLAS, GFortran, OpenMP for LightGBM/CatBoost)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    build-essential \
    gfortran \
    libopenblas-dev \
    libgomp1 \
    curl \
    git \
    ca-certificates \
    && ln -fs /usr/share/zoneinfo/Asia/Seoul /etc/localtime \
    && dpkg-reconfigure --frontend noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# 3. Python Dependencies Installation
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip wheel \
    && pip install --no-cache-dir -r requirements.txt

# 4. Copy Codebase (Excludes specified in .dockerignore)
COPY src/ /app/src/
COPY config/ /app/config/
COPY scripts/ /app/scripts/
COPY dashboard/ /app/dashboard/
COPY pytest.ini /app/pytest.ini
COPY Makefile /app/Makefile
COPY README.md /app/README.md

# 5. Create Persistent Directories & Set Permissions
RUN mkdir -p /app/data /app/results /app/logs /app/reports \
    && chmod -R 755 /app

# 6. Expose Ports (8501 for Streamlit Dashboard, 8080 for API)
EXPOSE 8501 8080

# 7. Healthcheck for Production Container
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os; exit(0 if os.path.exists('/app/src/streams/base_stream.py') else 1)"

# 8. Entrypoint / Default Command
ENTRYPOINT ["python"]
CMD ["scripts/stream_orchestrator.py"]
