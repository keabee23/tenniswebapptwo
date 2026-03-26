FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends     ffmpeg     libgl1     libglib2.0-0     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p uploads runs

CMD ["sh", "-c", "gunicorn -w 1 -k gthread --threads 8 -b 0.0.0.0:${PORT} app:app"]
