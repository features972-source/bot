#!/bin/bash
# Update VICIDIAL_TEST_NUMBERS on Render p1-bot and redeploy.
set -euo pipefail
: "${RENDER_API_KEY:?Set RENDER_API_KEY}"
SVC="${P1_RENDER_SERVICE_ID:-srv-d92bpksm0tmc73dt2fj0}"
NUMBERS="447769799593"
API="https://api.render.com/v1/services/${SVC}"

curl -fsS -H "Authorization: Bearer ${RENDER_API_KEY}" -H "Accept: application/json" \
  "${API}/env-vars" | python3 -c "
import json, sys
nums = '${NUMBERS}'
data = json.load(sys.stdin)
out = []
for row in data:
    ev = row.get('envVar') or row
    if ev.get('key') == 'VICIDIAL_TEST_NUMBERS':
        ev['value'] = nums
    out.append({'key': ev['key'], 'value': ev['value']})
json.dump(out, open('/tmp/p1-env.json','w'))
"

curl -fsS -X PUT "${API}/env-vars" \
  -H "Authorization: Bearer ${RENDER_API_KEY}" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/p1-env.json

curl -fsS -X POST "${API}/deploys" \
  -H "Authorization: Bearer ${RENDER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"clearCache":"do_not_clear"}'

echo "Updated VICIDIAL_TEST_NUMBERS and triggered deploy on ${SVC}"
