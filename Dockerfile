FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# One process keeps the in-memory Socket.IO branch rooms consistent; threads
# allow requests from multiple scanners and phones to run concurrently.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "1", "--threads", "16", "--timeout", "120", "--bind", "0.0.0.0:5000", "wsgi:app"]
