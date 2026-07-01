FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLOUD_DEPLOYED=true

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY press1_cloud.py press1_bot.py press1_utils.py vicidial_client.py ./

EXPOSE 10000

CMD ["python", "-u", "press1_cloud.py"]
