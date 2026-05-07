FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Logs directory (will be overridden by volume mount)
RUN mkdir -p logs

EXPOSE 8765

CMD ["python", "-m", "uvicorn", "dashboard.server:app", "--host", "0.0.0.0", "--port", "8765", "--log-level", "info"]
