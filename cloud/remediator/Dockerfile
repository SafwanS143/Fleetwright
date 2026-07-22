FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY remediator.py .

# Runs as root: restarting a sibling container needs the Docker socket, which is root-owned on the host.
# The blast radius is the reason service-healing is env-gated and k8s liveness probes replace it later.
EXPOSE 9098
CMD ["python3", "remediator.py"]
