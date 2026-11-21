FROM python:3.13.9-slim
WORKDIR /bot
COPY pilot.pyc mechfight.pyc NOTICE.txt ./
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/bot/pilot.pyc"]
