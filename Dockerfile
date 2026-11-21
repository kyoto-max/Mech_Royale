FROM python:3.13.9-slim
WORKDIR /bot
COPY brain.pyc world.pyc mechfight.pyc ./
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/bot/brain.pyc"]
