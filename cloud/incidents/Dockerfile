FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY incident_store.py .

# Create /data owned by fleet so the mounted volume inherits that ownership; otherwise it comes up
# root-owned and the unprivileged process can't write the SQLite file.
RUN useradd --uid 1000 --create-home fleet && mkdir -p /data && chown fleet /data
USER fleet

EXPOSE 9096
CMD ["python3", "incident_store.py"]
