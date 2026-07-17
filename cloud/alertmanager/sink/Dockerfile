FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY notify_sink.py .

# Pure network service on stdlib only — no requirements to install. Run unprivileged.
RUN useradd --uid 1000 --create-home fleet
USER fleet

EXPOSE 9095
CMD ["python3", "notify_sink.py"]
