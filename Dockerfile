FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

RUN mkdir -p /app/data && useradd --create-home botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "bot.py"]
