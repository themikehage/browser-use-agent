FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    xvfb x11vnc novnc websockify fluxbox wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY . .
RUN chmod +x start.sh

CMD ["./start.sh"]
