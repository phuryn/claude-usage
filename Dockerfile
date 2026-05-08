FROM python:3.12-slim

WORKDIR /app
COPY cli.py dashboard.py scanner.py ./

ENV HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Run scan once at startup, then serve. Bypasses cli.py:cmd_dashboard
# to avoid the webbrowser.open call (irrelevant inside a container).
CMD ["python3", "-c", "from scanner import scan; scan(); from dashboard import serve; serve()"]
