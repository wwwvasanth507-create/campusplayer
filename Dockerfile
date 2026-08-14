# Campus Video Player - Enhanced Docker Image
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    wget \
    curl \
    gnupg \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome for Headless SMS functionality (optional)
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/google-chrome /usr/bin/chromium-browser 2>/dev/null || true

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create necessary directories
RUN mkdir -p static/uploads static/hls static/subtitles generated_pdfs static/uploads/assignments

# Expose port
EXPOSE 5000

# Run with gunicorn + eventlet for SocketIO support
CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:5000", "app:app"]