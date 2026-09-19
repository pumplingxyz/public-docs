# Reproducible VRF verifier image.
#
# Build context MUST be the repo root (so the canonical vrf.py is reachable):
#   docker build -f vrf/Dockerfile -t vrf-verifier .
#
# Base image pinned by digest for reproducibility. To bump, get the new
# digest with `docker manifest inspect python:<version>-slim` (look at the
# top-level manifest list digest) and update both the tag and the sha256
# together. The tag is informational; the digest is what locks the bytes.

FROM python:3.12.13-slim@sha256:401f6e1a67dad31a1bd78e9ad22d0ee0a3b52154e6bd30e90be696bb6a3d7461

WORKDIR /app

# stdlib only — no pip install. Keeps the image trivially reproducible.

COPY vrf.py            /app/vrf.py
COPY vrf/verify.py     /app/verify.py
COPY vrf/verify.sh     /app/verify.sh
RUN chmod +x /app/verify.sh

ENTRYPOINT ["/app/verify.sh"]
