FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY entities/ ./entities/ 2>/dev/null || true
ENV ENGRAM_DB_PATH=/data/engram.db
ENV ENGRAM_PORT=3456
EXPOSE 3456
VOLUME /data
CMD ["python", "http_server.py"]
