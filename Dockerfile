# Dry-runs and tests only: there is no GPIB/VISA in the container, and the app refuses --allow-live off localhost.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends make && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY measure measure
COPY webapp webapp
COPY configs configs
COPY layouts layouts
COPY Makefile .
ENV MPLBACKEND=Agg
EXPOSE 8300
CMD ["python", "-B", "-m", "webapp", "--host", "0.0.0.0", "--port", "8300"]
