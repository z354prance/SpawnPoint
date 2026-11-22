#!/bin/sh
# SpawnPoint entrypoint.
# Isolated mode (default): start a private Docker engine inside this container, so the
# Minecraft server containers live there and never show up in the host's container list.
set -u
ENGINE_PID=""

if [ "${ISOLATED_DOCKER:-true}" = "true" ]; then
  # Privileged check: CAP_SYS_ADMIN (bit 21) and CAP_NET_ADMIN (bit 12) must be present.
  cap=$(awk '/^CapEff/ {print $2}' /proc/self/status)
  if [ $(( 0x$cap & (1 << 21) )) -eq 0 ] || [ $(( 0x$cap & (1 << 12) )) -eq 0 ] || [ ! -w /sys/fs/cgroup ]; then
    echo "SpawnPoint: needs privileged mode (privileged: true in docker compose, --privileged with docker run, or Privileged ON in the Unraid template)." >&2
    echo "            (or set ISOLATED_DOCKER=false and map /var/run/docker.sock)" >&2
    exit 1
  fi

  # cgroup v2 nesting (same approach as the official docker:dind image) so per-server
  # CPU sets and memory limits work inside the private engine.
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
    sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers \
      > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
  fi

  fs=$(stat -f -c %T /var/lib/docker 2>/dev/null || echo unknown)
  case "$fs" in
    fuseblk|fuse*) echo "SpawnPoint WARNING: engine storage (/var/lib/docker) is on a FUSE or network filesystem, so Docker falls back to the slow 'vfs' driver. Map it to a local disk (on Unraid: /mnt/cache/..., not /mnt/user/...)." >&2 ;;
  esac

  command -v docker >/dev/null || { echo "SpawnPoint: the docker command is missing from this image (build problem)." >&2; exit 1; }
  rm -f /var/run/docker.pid /var/run/docker.sock
  dockerd --data-root /var/lib/docker --host unix:///var/run/docker.sock \
          --log-level info --shutdown-timeout 150 >/var/log/dockerd.log 2>&1 &
  ENGINE_PID=$!
  i=0
  until docker info >/dev/null 2>&1; do
    i=$((i+1))
    if [ $i -gt 90 ] || ! kill -0 "$ENGINE_PID" 2>/dev/null; then
      echo "SpawnPoint: private Docker engine failed to start. Last engine messages:" >&2
      grep -iE "error|fatal|fail|level=warn" /var/log/dockerd.log | tail -n 25 >&2
      echo "---- end of engine log ----" >&2
      tail -n 15 /var/log/dockerd.log >&2
      exit 1
    fi
    sleep 1
  done
  echo "SpawnPoint: private Docker engine ready ($(docker info -f '{{.Driver}}') storage)"
fi

uvicorn app.main:app --host 0.0.0.0 --port 8090 --proxy-headers --forwarded-allow-ips '127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10' &
APP_PID=$!

shutdown() {
  # SpawnPoint saves and stops every Minecraft server, then the engine stops.
  kill -TERM "$APP_PID" 2>/dev/null
  wait "$APP_PID" 2>/dev/null
  if [ -n "$ENGINE_PID" ]; then
    kill -TERM "$ENGINE_PID" 2>/dev/null
    wait "$ENGINE_PID" 2>/dev/null
  fi
  exit 0
}
trap shutdown TERM INT

wait "$APP_PID"
# App exited on its own (crash): take the engine down cleanly too.
[ -n "$ENGINE_PID" ] && kill -TERM "$ENGINE_PID" 2>/dev/null && wait "$ENGINE_PID" 2>/dev/null
exit 1
