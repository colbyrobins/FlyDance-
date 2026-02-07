FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DANCECOMP_DB_PATH=/data/dancecomp.db

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/
RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "app.py"]
