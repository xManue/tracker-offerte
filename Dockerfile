FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-ita && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY tracker tracker
RUN pip install --no-cache-dir '.[dataset,documents,test]'
COPY alembic.ini .
COPY migrations migrations
COPY tests tests
ENV PYTHONUNBUFFERED=1
