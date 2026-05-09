FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl-dev \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY lib /app/lib
ENV LD_LIBRARY_PATH=/app/lib

COPY static /app/static
COPY app ./app

EXPOSE 8098

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8098"]
