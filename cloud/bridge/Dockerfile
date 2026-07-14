FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mqtt_prometheus_bridge.py .

# Unlike the gateway, the bridge touches no host devices — it's a plain network service, so run
# unprivileged with no device-cgroup or dialout-gid gymnastics.
RUN useradd --uid 1000 --create-home fleet
USER fleet

# Documents the scrape port; publishing it to the host is the compose file's job, not the image's.
EXPOSE 8000
CMD ["python3", "mqtt_prometheus_bridge.py"]
