FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY maple.py server.py ./
# data/ and teammates.json are mounted at runtime (never baked into the image)
ENV EB_DATA_DIR=/data TEAMMATES_FILE=/config/teammates.json PORT=8095
EXPOSE 8095
HEALTHCHECK --interval=10s --timeout=3s --retries=10 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/health')"
CMD ["python", "server.py"]
