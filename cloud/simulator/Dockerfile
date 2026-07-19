FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fleet_simulator.py .

RUN useradd --uid 1000 --create-home fleet
USER fleet

CMD ["python3", "fleet_simulator.py"]
