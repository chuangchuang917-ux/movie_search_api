# Use the official Python lightweight image
FROM python:3.10-slim

# Set environment variables to optimize Python execution and set defaults
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Set container working directory
WORKDIR /app

# Install system dependencies if required (slim image does not contain all standard tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file first to take advantage of Docker build caching layers
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source files into the container
COPY . .

# Expose the API port (Cloud Run exposes and maps this automatically)
EXPOSE 8080

# Start Uvicorn. Cloud Run sets the $PORT environment variable dynamically.
# Using 'sh -c' allows environment variable expansion inside CMD execution.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
