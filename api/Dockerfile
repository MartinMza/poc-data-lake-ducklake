FROM python:3.12-slim-bookworm

WORKDIR /app

# Instalar librerías del sistema: descompresión (.tar, .gz, .bz2, .xz, .zst, .zip, .7z),
# cliente Postgres (psql/pg_dump/pg_restore), ODBC y herramientas de red/salud.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    jq \
    tar \
    gzip \
    pigz \
    bzip2 \
    xz-utils \
    zstd \
    lz4 \
    unzip \
    p7zip-full \
    postgresql-client \
    unixodbc \
    unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Pre-instalar extensiones de DuckDB (ducklake, postgres, httpfs) en la imagen
RUN python -c "import duckdb; con = duckdb.connect(); con.execute('INSTALL ducklake; INSTALL postgres; INSTALL httpfs;')"

COPY api /app/api

ENV PYTHONUNBUFFERED=1 \
    API_BIND_HOST=0.0.0.0 \
    API_INTERNAL_PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn api.main:app --host ${API_BIND_HOST:-0.0.0.0} --port ${API_INTERNAL_PORT:-8000}"]
