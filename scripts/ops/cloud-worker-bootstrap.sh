#!/bin/bash
# Vendor-neutral Linux worker bootstrap template. Does not start workers or
# call a cloud vendor CLI. No services or runs authorized.
set -euo pipefail

echo "continuous-operator cloud-worker-bootstrap"
echo "role=unprivileged-adapter"
echo "inputs=lease_bundle (attempt_id, lease_epoch, fencing_token)"
echo "work=sandbox execute -> sanitize -> local PAX -> return cas_uri"
echo "canonical_cas=host-authority-only"
echo "canonical_catalog=host-authority-only"
echo "started=no"
echo "dispatch=no"
exit 0
