#!/usr/bin/env bash
# One Namespace per team, and one Nexus Endpoint per team the box office calls.
set -euo pipefail

for ns in boxoffice bouncer seats payments; do
  temporal operator namespace create --namespace "$ns" >/dev/null 2>&1 \
    && echo "Created Namespace $ns" || echo "Namespace $ns already exists"
done

for team in bouncer seats payments; do
  temporal operator nexus endpoint create \
    --name "$team-endpoint" \
    --target-namespace "$team" \
    --target-task-queue "$team-tq" >/dev/null 2>&1 \
    && echo "Created Endpoint $team-endpoint" || echo "Endpoint $team-endpoint already exists"
done

echo "Namespaces and Endpoints are ready."
