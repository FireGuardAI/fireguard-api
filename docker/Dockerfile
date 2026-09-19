FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install --user -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
RUN groupadd -r fireguard && useradd -r -g fireguard fireguard
COPY --from=builder /root/.local /home/fireguard/.local
COPY app/ ./app/
ENV PATH=/home/fireguard/.local/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN chown -R fireguard:fireguard /app
USER fireguard
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
