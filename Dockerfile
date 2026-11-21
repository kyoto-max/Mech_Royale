# Build from this folder: docker build -t survivor-bot .
# Matches run it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, uid 65534, map at /arena.
FROM python:3.13.9-slim
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /bot
COPY mechfight.py pilot.py THIRD_PARTY_LICENSES ./
ENTRYPOINT ["python3", "-u", "/bot/pilot.py"]
