FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
# numpy + scikit-learn pull manylinux wheels here, so no compiler toolchain is needed in the image.
RUN pip install --no-cache-dir -r requirements.txt

COPY detectors.py anomaly_service.py ./

# Like the bridge, this is a plain network service — no host devices — so run unprivileged.
RUN useradd --uid 1000 --create-home fleet
USER fleet

# Documents the scrape port; publishing it is the compose file's job, not the image's.
EXPOSE 8001
CMD ["python3", "anomaly_service.py"]
