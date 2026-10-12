#!/bin/sh
set -eu
cd /opt/beresta/current
# Capture counters before the billing query. Recent and uncertain dispatches remain reserved.
counts=$(beresta-core exec -T db psql -XAt -U notes -d notes -c "SELECT reserved_calls, (SELECT count(*) FROM provider_usage WHERE model <> 'mock' AND (status = 'unknown' OR occurred_at > extract(epoch from now()) - 600)) FROM provider_budgets WHERE id = 'cloudru';")
IFS='|' read -r reserved unsettled <<EOF
$counts
EOF
case "$reserved:$unsettled" in *[!0-9:]*|:*) exit 1;; esac
docker run --rm --read-only --user 1000:1000 --memory 128m --pids-limit 32 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount type=bind,src=/opt/beresta/repository/secrets/cloudru_api_key.txt,dst=/run/secrets/model_key,readonly \
  --mount type=bind,src=/opt/beresta/repository/data/billing,dst=/billing \
  --entrypoint python beresta-pilot-api -m app.gateway_budget \
  --key-file /run/secrets/model_key --destination /billing/current.json \
  --reserved-calls "$reserved" --unsettled-calls "$unsettled" --currency RUB
