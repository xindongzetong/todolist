FROM python:3.10-slim

LABEL authors="SHI"

WORKDIR /app

COPY . /app

RUN apt-get update

RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get clean

ENTRYPOINT ["/bin/bash", "./run.sh"]