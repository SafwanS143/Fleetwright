FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY incident_store.py .

# Runs unprivileged like the other services. /data is created HERE (owned by fleet) so that when the
# named volume first mounts over it, docker seeds the volume with this ownership — otherwise the
# volume comes up root-owned and the unprivileged process can't create the SQLite file.
RUN useradd --uid 1000 --create-home fleet && mkdir -p /data && chown fleet /data
USER fleet

EXPOSE 9096
CMD ["python3", "incident_store.py"]
