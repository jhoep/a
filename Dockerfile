FROM python:3.11-slim

# Instalar ffmpeg, yt-dlp y dependencias de audio/compilación
RUN apt-get update && apt-get install -y \
    ffmpeg \
    build-essential \
    curl \
    libtool \
    autoconf \
    automake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt \
    && pip install --no-cache-dir --break-system-packages yt-dlp

COPY . .

CMD ["python3", "bot.py"]
