FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

WORKDIR /app

# Switch to immutable Debian snapshot corresponding to base image timestamp (20261104T212339Z)
# while preserving cryptographic signature validation via debian-archive-keyring
RUN set -eux; \
    sed -i 's|http://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/20261104T212339Z|g' /etc/apt/sources.list.d/debian.sources; \
    sed -i 's|http://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/20261104T212339Z|g' /etc/apt/sources.list.d/debian.sources; \
    printf 'Acquire::Check-Valid-Until "false";\nAcquire::http::Pipeline-Depth "0";\nAcquire::Retries "3";\n' > /etc/apt/apt.conf.d/99snapshot; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        curl=8.14.1-2+deb13u7 \
        libcurl4t64=8.14.1-2+deb13u7; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --default-timeout=100 --retries=5 --only-binary=:all: --require-hashes -r requirements.lock

RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/uploads /data && \
    chown -R appuser:appuser /app /data

COPY --chown=appuser:appuser . .

USER appuser

ENV PYTHONUNBUFFERED=1
ENV DATABASE_PATH=/data/orders.db

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
