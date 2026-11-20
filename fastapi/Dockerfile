FROM python:3.11-slim

# HF_HOME / NLTK_DATA: models live in the image at fixed paths (not in /root, the service
# does not run as root)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/huggingface \
    NLTK_DATA=/usr/share/nltk_data

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

# torch first: it is the biggest download and does not change with requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Models are downloaded at build time, so the service needs no internet when it starts
RUN python -m spacy download en_core_web_sm \
 && python -c "import nltk; nltk.download('stopwords', download_dir='/usr/share/nltk_data')" \
 && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

# Non-root user; /data is the job folder volume (docker-compose.yml)
RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data && chown -R app /data /models
COPY --chown=app . .
USER app

EXPOSE 8000

# /v1 API. The worker uses the same image: python -m service.worker
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]
