FROM python:3.9-slim

WORKDIR /app

# Copier les fichiers Python
COPY cli.py scanner.py dashboard.py ./

# Créer un groupe et utilisateur avec UID/GID spécifiques
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} appuser && \
    useradd -m -u ${UID} -g appuser appuser

# Créer le dossier .claude avec les bons droits
RUN mkdir -p /home/appuser/.claude && \
    chown -R appuser:appuser /home/appuser/.claude

USER appuser

ENTRYPOINT ["python", "cli.py"]
CMD ["dashboard"]