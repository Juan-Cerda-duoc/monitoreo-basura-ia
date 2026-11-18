FROM python:3.11-slim

WORKDIR /app

# Dependencias del sistema necesarias para OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Instalar PyTorch en versión CPU (imagen mucho más liviana y rápida de compilar)
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código y ambos modelos YOLO locales
COPY main.py .
COPY best.pt .
COPY yolov8n.pt .

RUN mkdir -p /app/data

EXPOSE 5000

CMD ["python", "main.py"]