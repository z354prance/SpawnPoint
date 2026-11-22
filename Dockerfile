FROM python:3.12-slim
# Private Docker engine (isolated mode) so game servers stay out of Unraid's Docker tab.
# Debian splits the engine (docker.io) from the `docker` command (docker-cli); we need both.
RUN apt-get update \
 && apt-get install -y --no-install-recommends docker.io docker-cli iptables kmod ca-certificates procps util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && docker --version && dockerd --version
# restic: incremental, deduplicated backups (see app/backups.py). Downloaded and unpacked
# in one step so the compressed download doesn't stay behind in an image layer.
ARG RESTIC_VERSION=0.18.0
ARG TARGETARCH=amd64
RUN python3 -c "import urllib.request,bz2; d=urllib.request.urlopen('https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${TARGETARCH}.bz2').read(); open('/usr/local/bin/restic','wb').write(bz2.decompress(d))" \
 && chmod +x /usr/local/bin/restic && restic version
WORKDIR /opt/spawnpoint
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY app ./app
# The Unraid template and icon ride along in the image, so installing them only needs
# the GHCR login Unraid already has (see README: "Add the template").
COPY unraid ./unraid
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV PYTHONUNBUFFERED=1 ISOLATED_DOCKER=true
EXPOSE 8090 25565
STOPSIGNAL SIGTERM
ENTRYPOINT ["/entrypoint.sh"]
