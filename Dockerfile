FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Jerusalem
WORKDIR /app

COPY pyproject.toml ./
COPY metro ./metro
COPY web ./web
COPY data/sp500.csv data/themes.json data/relations.json ./seed/
COPY scripts ./scripts
# editable install keeps the code next to web/ and data/, where the app looks for them
RUN pip install --no-cache-dir -e ".[ibkr]" && python scripts/build_web.py

# the database, documents and transcripts live in the /app/data volume. `metro boot` copies the seed files in,
# and on an empty disk restores the latest backup from GitHub (BACKUP_GITHUB_REPO) or starts a new database;
# with keys set, the first fill then starts by itself in the background.
ENV DATA_DIR=/app/data HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "metro boot && metro run"]
