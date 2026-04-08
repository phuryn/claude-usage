FROM python:3.9-slim
WORKDIR /app
COPY cli.py scanner.py dashboard.py ./
RUN adduser --disabled-password --gecos '' appuser
USER appuser
ENTRYPOINT ["python", "cli.py"]
CMD ["dashboard"]