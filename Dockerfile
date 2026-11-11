# ==============================================================================
# Dockerfile for Building Analytics Backend & Migration Runner
# ==============================================================================

# 1. Base Image: Lightweight Python 3.11 Linux container
FROM python:3.11-slim

# Prevent Python from writing .pyc files to disk and enable instant unbuffered stdout/stderr logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MPLBACKEND=Agg
ENV MPLCONFIGDIR=/tmp/matplotlib

# Cloud Run injects $PORT (defaults to 8080)
ENV PORT=8080

# 2. Set working directory
WORKDIR /app

# 3. Install dependencies first (leverages Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 4. Copy backend application code, profile configs, and alembic
COPY . /app

# 5. Create symlink so 'from backend.xxx import' resolves cleanly inside /app
RUN ln -s /app /app/backend

# 6. Set PYTHONPATH so Python finds backend modules
ENV PYTHONPATH=/app

# 7. Expose default port
EXPOSE 8080

# 8. Default CMD: Run FastAPI web server
# When executed as a Cloud Run Job, the command is overridden to: ["alembic", "upgrade", "head"]
CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT}"]
