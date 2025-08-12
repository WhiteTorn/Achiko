# Minimal, non-root image
FROM python:3.11-slim AS runtime

# Create non-root user with a fixed UID/GID so you can match host permissions
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd -g ${APP_GID} app && useradd -m -u ${APP_UID} -g ${APP_GID} -s /usr/sbin/nologin app

# Workdir
WORKDIR /app

# System deps (build tools kept minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini && \
    rm -rf /var/lib/apt/lists/*

# Copy and install Python deps
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
# Expect your tree to contain src/bot.py and the rest of your project files
COPY src /app/src

# Use /data as the in-container download path (mount a volume here)
# DOWNLOAD_ROOT should be set by env or compose; default is /data for safety.
ENV DOWNLOAD_ROOT=/data

# Drop privileges
USER app:app

# Use Tini as init for better signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "src/bot.py"]
