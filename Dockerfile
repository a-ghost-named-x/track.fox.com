FROM python:3.12-slim-bookworm

# tzdata: ensures the TZ env var (set for local dev in
# docker-compose.override.yml, since bind-mounting /etc/localtime from the
# host doesn't carry the real timezone through Docker Desktop's own Linux
# VM the way it does on a native Linux host like lost-woods) has zoneinfo
# data to resolve against. Harmless in production, which doesn't set TZ and
# keeps using the bind-mounted files as before.
#
# --- ODBC driver for MSSQL (pyodbc needs the system driver, not just the pip package) ---
# Following Microsoft's official instructions for Debian-based images.
# Uses a dedicated keyring file instead of the deprecated/removed `apt-key`,
# and reads the Debian major version from /etc/os-release instead of
# hardcoding it, so this survives the base image moving to a newer Debian
# release (this broke once already going from bookworm/12 to trixie/13).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gnupg \
        unixodbc-dev \
        tzdata \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && . /etc/os-release \
    && curl -sSL "https://packages.microsoft.com/config/debian/${VERSION_ID}/prod.list" \
        | sed 's/\[signed-by=[^]]*\]/[signed-by=\/usr\/share\/keyrings\/microsoft-prod.gpg]/' \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]