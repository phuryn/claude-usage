FROM python:3.12-slim

WORKDIR /app

COPY scanner.py cli.py dashboard.py ./

# SECURITY [L-4]: Run as a non-root user so that a container escape or
# path-traversal exploit cannot write to system directories.
# The /data volume (mounted by the host) must be writable by appuser (UID 1000).
RUN useradd -m -u 1000 appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser

# SECURITY [C-2]: Bind to localhost (127.0.0.1) by default, not 0.0.0.0.
# In Docker the port is already published to the host via -p HOST_PORT:8080,
# so binding on 0.0.0.0 inside the container only matters if other containers
# on the same Docker network could reach the dashboard directly — which is an
# unnecessary attack surface.  To expose on all interfaces (e.g. for a reverse
# proxy in the same network), override at runtime: -e HOST=0.0.0.0
ENV HOST=127.0.0.1
ENV PORT=8080
# SECURITY [L-3]: Path is validated by _resolve_db_path() in scanner.py;
# must end in .db and stay within the home directory of the running user.
# In the Docker image appuser's home is /home/appuser, so use /data/*.db
# and mount the volume there, not at /root/.
ENV CLAUDE_USAGE_DB=/data/usage.db

EXPOSE 8080

CMD ["python3", "cli.py", "dashboard", "--no-browser"]
