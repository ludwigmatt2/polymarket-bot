FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Persistent data dirs — overridden by Railway volume mount at /data
RUN mkdir -p /data/logs /data/config

CMD ["python", "telegram_bot.py"]
