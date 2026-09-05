#!/usr/bin/env bash
# deploy/skywave/wait-and-deploy.sh — detached on the host: wait for passwordless
# sudo (the one privileged step the operator grants by hand), then run deploy.sh.
# Start with:  nohup ~/grid-deploy/wait-and-deploy.sh > ~/grid-deploy/deploy.log 2>&1 &
end=$((SECONDS+7200))
while [ $SECONDS -lt $end ]; do
  if sudo -n true 2>/dev/null; then
    echo "SUDO OK at $(date -u +%FT%TZ) - running deploy.sh"
    bash "$HOME/grid-deploy/deploy.sh"
    echo "deploy exit=$?"
    exit 0
  fi
  sleep 60
done
echo "TIMEOUT: no passwordless sudo after 2h"
exit 1
