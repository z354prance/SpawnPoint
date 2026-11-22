#!/usr/bin/env bash
# Opens TCP 25565 on a Pangolin VPS for SpawnPoint, then tests the route.
#
#   bash pangolin-open-25565.sh atm10.mc.example.com
#
# What it does (each step is skipped if already done):
#   1. docker-compose.yml: publish 25565:25565 on the gerbil service
#   2. config/traefik/traefik_config.yml: add the tcp-25565 entry point
#   3. config/config.yml: flags.allow_raw_resources: true
#   4. back up all three files first; if Docker Compose rejects the result, restore them
#   5. restart Pangolin (sites are down for ~30 s), open ufw if it's active
#   6. ask SpawnPoint for the server's status through the new port
set -euo pipefail

TEST_HOST="${1:-}"
DIR="${PANGOLIN_DIR:-}"
if [ -z "$DIR" ]; then
  DIR=$(docker inspect gerbil --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' 2>/dev/null || true)
fi
if [ -z "$DIR" ] || [ ! -f "$DIR/docker-compose.yml" ]; then
  echo "Couldn't find Pangolin's folder. Run it as: PANGOLIN_DIR=/path/to/pangolin bash $0 <server address>"
  exit 1
fi
command -v python3 >/dev/null || { echo "python3 is needed (apt install -y python3)"; exit 1; }
cd "$DIR"
echo "Pangolin folder: $DIR"

STAMP=$(date +%Y%m%d-%H%M%S)
FILES=(docker-compose.yml config/traefik/traefik_config.yml config/config.yml)
for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "Missing $f in $DIR"; exit 1; }
  cp -p "$f" "$f.bak-$STAMP"
done
echo "Backups saved with suffix .bak-$STAMP"
restore() {
  echo "Something went wrong; restoring the backups."
  for f in "${FILES[@]}"; do cp -p "$f.bak-$STAMP" "$f"; done
}

python3 - <<'PY' || { restore; exit 1; }
import re

def indent(line):
    return len(line) - len(line.lstrip(" "))

def block_end(lines, start, base):
    """Index after the block that starts at lines[start] (indent `base`)."""
    i = start + 1
    while i < len(lines):
        l = lines[i]
        if l.strip() and not l.lstrip().startswith("#") and indent(l) <= base:
            break
        i += 1
    return i

def child_indent(lines, start, end, fallback):
    for l in lines[start + 1:end]:
        if l.strip() and not l.lstrip().startswith("#"):
            return indent(l)
    return fallback

def load(p):
    with open(p) as f:
        return f.read().split("\n")

def save(p, lines):
    with open(p, "w") as f:
        f.write("\n".join(lines))

# 1. gerbil port ------------------------------------------------------------
p = "docker-compose.yml"
L = load(p)
g = next((i for i, l in enumerate(L) if re.match(r"^\s*gerbil:\s*(#.*)?$", l)), None)
if g is None:
    raise SystemExit("No 'gerbil:' service in docker-compose.yml")
gend = block_end(L, g, indent(L[g]))
if any(re.search(r"['\"]?25565:25565", l) for l in L[g:gend]):
    print("1. gerbil already publishes 25565")
else:
    pi = next((i for i in range(g, gend) if re.match(r"^\s*ports:\s*(#.*)?$", L[i])), None)
    if pi is None:
        raise SystemExit("gerbil has no 'ports:' list; add '- 25565:25565' by hand")
    item = next((L[i] for i in range(pi + 1, gend) if L[i].lstrip().startswith("- ")), None)
    ind = indent(item) if item else indent(L[pi]) + 2
    quote = '"' if item and item.lstrip()[2:3] == '"' else ""
    L.insert(pi + 1, " " * ind + f"- {quote}25565:25565{quote}")
    save(p, L)
    print("1. added 25565:25565 to gerbil's ports")

# 2. traefik entry point -----------------------------------------------------
p = "config/traefik/traefik_config.yml"
L = load(p)
if any(re.match(r"^\s*tcp-25565:", l) for l in L):
    print("2. traefik already has the tcp-25565 entry point")
else:
    e = next((i for i, l in enumerate(L) if re.match(r"^entryPoints:\s*(#.*)?$", l)), None)
    if e is None:
        L += ["", "entryPoints:", "  tcp-25565:", '    address: ":25565/tcp"']
    else:
        end = block_end(L, e, 0)
        ci = child_indent(L, e, end, 2)
        # grandchild indent: copy from an existing entry point if there is one
        gi = next((indent(l) for l in L[e + 1:end] if l.strip() and indent(l) > ci), ci * 2)
        while end > e + 1 and not L[end - 1].strip():
            end -= 1
        L[end:end] = [" " * ci + "tcp-25565:", " " * gi + 'address: ":25565/tcp"']
    save(p, L)
    print("2. added the tcp-25565 entry point to traefik")

# 3. raw resources flag ------------------------------------------------------
p = "config/config.yml"
L = load(p)
r = next((i for i, l in enumerate(L) if re.match(r"^\s*allow_raw_resources:", l)), None)
if r is not None:
    if re.search(r":\s*true\b", L[r]):
        print("3. allow_raw_resources is already true")
    else:
        L[r] = re.sub(r":\s*\S+", ": true", L[r], count=1)
        save(p, L)
        print("3. set allow_raw_resources: true")
else:
    f = next((i for i, l in enumerate(L) if re.match(r"^flags:\s*(#.*)?$", l)), None)
    if f is None:
        while L and not L[-1].strip():
            L.pop()
        L += ["", "flags:", "  allow_raw_resources: true", ""]
    else:
        end = block_end(L, f, 0)
        ci = child_indent(L, f, end, 2)
        L.insert(f + 1, " " * ci + "allow_raw_resources: true")
    save(p, L)
    print("3. added flags.allow_raw_resources: true")

# sanity-check the two YAML files Compose doesn't validate, if PyYAML is around
try:
    import yaml
    for p in ("config/traefik/traefik_config.yml", "config/config.yml"):
        yaml.safe_load(open(p))
    print("   YAML check passed")
except ImportError:
    pass
PY

if ! docker compose config -q; then
  restore
  exit 1
fi
echo "   docker compose config is valid"

if [ "${SKIP_RESTART:-0}" != "1" ]; then
  echo "4. Restarting Pangolin (about 30 seconds)..."
  docker compose down && docker compose up -d || { restore; docker compose up -d; exit 1; }
  sleep 8
  if ss -tln | grep -q ':25565 '; then echo "   port 25565 is listening"; else echo "   WARNING: nothing is listening on 25565 yet"; fi
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw status | grep -q "25565" || { ufw allow 25565/tcp >/dev/null && echo "   opened 25565/tcp in ufw"; }
  fi
  echo "   Also make sure your VPS provider's firewall allows TCP 25565."
fi

if [ -n "$TEST_HOST" ]; then
  echo "5. Testing $TEST_HOST ..."
  python3 - "$TEST_HOST" <<'PY'
import json, socket, struct, sys
host = sys.argv[1]
def vi(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n: return out
def pkt(pid, data):
    body = vi(pid) + data
    return vi(len(body)) + body
h = host.encode()
try:
    s = socket.create_connection((host, 25565), timeout=8)
except OSError as e:
    print(f"   FAILED to connect: {e}. Check the VPS provider firewall."); sys.exit(1)
s.sendall(pkt(0, vi(767) + vi(len(h)) + h + struct.pack(">H", 25565) + vi(1)) + pkt(0, b""))
data = b""
s.settimeout(8)
try:
    while b"}" not in data:
        c = s.recv(4096)
        if not c: break
        data += c
except socket.timeout:
    pass
if not data:
    print("   Connected, but no reply. Check the Pangolin resource targets UnraidIP:25565,")
    print("   that SpawnPoint is running, and that this name is one of your SpawnPoint servers.")
    sys.exit(1)
j = json.loads(data[data.index(b"{"):data.rindex(b"}") + 1])
d = j.get("description", {})
import re
text = d.get("text", "") if isinstance(d, dict) else str(d)
print("   OK! SpawnPoint answered:", re.sub(r"\u00a7.", "", text))
PY
fi
