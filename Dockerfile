FROM python:3.11-slim

# Install poppler-utils (pdftoppm) and ghostscript
RUN apt-get update && apt-get install -y poppler-utils ghostscript && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:8000", "--timeout", "300"]
