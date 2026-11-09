# Apollo as one container: the built React UI is served by FastAPI on the same origin as /api.
# Made for Hugging Face Spaces (Docker, free CPU): port 7860, runs as user 1000.
# Secrets (API keys, JWT_SECRET, DATABASE_URL) come from the host's environment, never from this image.

# 1) Build the frontend
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# 2) Python app
FROM python:3.11-slim
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/home/user/.cache/huggingface \
    OUTPUT_DIR=/tmp/apollo-output
WORKDIR /home/user/app

# CPU-only torch first (the default Linux wheel pulls GPU libraries worth several GB), then the rest.
COPY --chown=user backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir --user torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir --user -r backend/requirements.txt

COPY --chown=user . .
COPY --chown=user --from=web /web/dist ./frontend/dist

# Fetch the embedding model at build time so the first research run does not wait for a download.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

EXPOSE 7860
# The Python code lives in backend/; --app-dir puts it on the import path.
CMD ["uvicorn", "api:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "7860"]
