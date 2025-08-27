FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 YTDLP_NO_UPDATE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libopus0 ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . /app

# Expose port 8990 (listener di dalam container)
EXPOSE 8111

CMD ["python", "bot.py"]
