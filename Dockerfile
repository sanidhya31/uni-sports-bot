# The multi-user app is pure HTTP (httpx) — no browser needed, so a slim base.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# Drop the heavy browser stack the legacy single-user bot used; the new app
# (app.app) only needs httpx + bs4 + cryptography + dotenv + apprise.
RUN grep -ivE '^(playwright|pyee|greenlet)==' requirements.txt > req.slim.txt \
    && pip install --no-cache-dir -r req.slim.txt

COPY app ./app

# users.db and .env are mounted at runtime (see docker-compose.yml).
CMD ["python", "-m", "app.app"]
