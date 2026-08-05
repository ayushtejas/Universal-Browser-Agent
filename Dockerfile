# ---- build ----
FROM python:3.12-slim AS builder

WORKDIR /app
COPY pyproject.toml ./
COPY src/ src/

RUN pip install --no-cache-dir -e ".[browser]"

# ---- runtime ----
FROM python:3.12-slim

# Chromium system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# Playwright Chromium in a shared path
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN playwright install chromium

RUN useradd -m worker && mkdir -p /app/data/raw && chown -R worker:worker /app/data
USER worker

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Single container: FastAPI (port 8000) + background worker thread
ENTRYPOINT ["ipr"]
CMD ["api", "--host", "0.0.0.0", "--port", "8000"]
