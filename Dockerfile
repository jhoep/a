FROM python:3.11-slim

# Instalar dependencias de compilación
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    libtool \
    autoconf \
    automake \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp lo necesita para resolver los desafios JS de YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

CMD ["python3", "bot.py"]
