FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt && useradd --uid 10001 --create-home app
COPY asapify_api ./asapify_api
COPY asapify_engine ./asapify_engine
COPY main.py .
USER app
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
