FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY serial_reader.py .

# Non-root. The device cgroup rule grants the CGROUP permission to open a char
# device on major 166; the ordinary DAC check on the node still runs, and the
# node is root:dialout 0660 on the host. So our user's PRIMARY GROUP must have
# the same NUMERIC gid as the host's `dialout` (20 on Debian / Raspberry Pi OS).
# Debian's base image already ships dialout at gid 20 -- verify on your Pi with:
#   stat -c '%G %g' /dev/ttyACM0
RUN useradd --uid 1000 --gid dialout --create-home fleet
USER fleet
CMD ["python3", "serial_reader.py"]