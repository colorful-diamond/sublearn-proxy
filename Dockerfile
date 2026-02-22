FROM python:3.11-slim
WORKDIR /app
COPY proxy_server.py .
EXPOSE 10000
CMD ["python3", "proxy_server.py"]
