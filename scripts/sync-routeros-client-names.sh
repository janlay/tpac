#!/usr/bin/env bash
# Name TPAC's Wi-Fi clients after the comments of a MikroTik RouterOS DHCP server.
#
# Usage: ROUTER=<ssh destination> HA=<ssh destination> scripts/sync-routeros-client-names.sh
#   ROUTER    SSH destination of the router, for example admin@192.168.88.1
#   HA        SSH destination of Home Assistant (the "Advanced SSH & Web Terminal" app), for example root@homeassistant.local
#   SSH_OPTS  Optional extra ssh options, for example "-i ~/.ssh/id_ed25519"
#
# The script writes <config>/tpac_client_names.json ({"aa:bb:cc:dd:ee:ff": "name"}).
# TPAC reads the file when it changes. Run it again after you edit lease comments.
set -euo pipefail
: "${ROUTER:?set ROUTER to the router SSH destination}"
: "${HA:?set HA to the Home Assistant SSH destination}"
# shellcheck disable=SC2206
SSH=(ssh -o BatchMode=yes ${SSH_OPTS:-})

PARSE='import json, re, sys
best = {}
for block in re.split(r"\n\s*\n", sys.stdin.read()):
    comment = re.search(r";;;\s*(.+)", block)
    mac = re.search(r"mac-address=([0-9A-Fa-f:]{17})", block)
    if not comment or not mac:
        continue
    key = mac.group(1).lower()
    bound = "active-address=" in block
    if key not in best or (bound and not best[key][0]):
        best[key] = (bound, comment.group(1).strip())
print(json.dumps({key: name for key, (_, name) in sorted(best.items())}, ensure_ascii=False, indent=2))'

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
"${SSH[@]}" "$ROUTER" '/ip dhcp-server lease print detail without-paging' | python3 -c "$PARSE" > "$TMP"
"${SSH[@]}" "$HA" 'cat > /config/tpac_client_names.json' < "$TMP"
printf 'Wrote %s client names\n' "$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$TMP")"
