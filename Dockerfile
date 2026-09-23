FROM python:3.12-slim-bookworm

# tzdata lets the TZ variable set in docker-compose.override.yml resolve for
# local development. Production takes its timezone from the host instead.
#
# pyodbc needs Microsoft's ODBC driver installed at the system level. The
# Debian version is read from /etc/os-release rather than hardcoded so the
# repo URL follows the base image. The base image is pinned to bookworm
# because Microsoft's packages don't install cleanly on Debian 13 yet.
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
