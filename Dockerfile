FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY iespien.py .
COPY template.html .
COPY config.yaml .

# Carpeta para íconos (se puede montar como volumen)
RUN mkdir -p /app/icons

CMD ["python", "iespien.py"]
