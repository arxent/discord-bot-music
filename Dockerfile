FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 YTDLP_NO_UPDATE=1

# ffmpeg & libopus wajib untuk voice
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libopus0 ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependensi lebih dulu untuk cache build optimal
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy source code
COPY . /app

# (opsional) healthcheck sederhana: pastikan proses python hidup
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD pgrep -f "python .*bot.py" || exit 1

# Jalankan bot (ubah kalau file utamamu bukan bot.py)
CMD ["python", "bot.py"]
