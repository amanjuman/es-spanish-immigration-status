FROM python:3.12-slim-bookworm

# Tesseract for the free OCR captcha solver.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # Chromium + its system libraries for Playwright.
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data
VOLUME /data
EXPOSE 8000

CMD ["python", "-m", "app.web.main"]
