#!/usr/bin/env bash
# Temporal dev server with the flags Nexus and Activity-backed Operations need.
# activity.enableCallbacks=true lets a Standalone Activity complete a Nexus Operation.
# nexusoperation.enableStandalone=true lets a Client start a Nexus Operation with no caller Workflow.
exec temporal server start-dev \
  --dynamic-config-value history.enableUpdateCallbacks=true \
  --dynamic-config-value history.enableCHASMSignalBacklinks=true \
  --dynamic-config-value history.enableSignalWithStartFromWorkflow=true \
  --dynamic-config-value activity.enableCallbacks=true \
  --dynamic-config-value nexusoperation.enableStandalone=true \
  --log-level error "$@"
