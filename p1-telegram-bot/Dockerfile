FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLOUD_DEPLOYED=true

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY press1_cloud.py \
     press1_bot.py \
     dash_api.py \
     vicidial_client.py \
     press1_utils.py \
     press1_settings.py \
     press1_access.py \
     press1_campaign.py \
     press1_schedule.py \
     press1_ui.py \
     ./

EXPOSE 10000

CMD ["python", "-u", "press1_cloud.py"]
