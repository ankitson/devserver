# Source revision: ecd48e2f718220a6400ecf49eafd4867a38feb10 (Open WebUI 0.10.2)
# The base is the exact image currently deployed; this derived image replaces
# only the reviewed frontend build and PCM-related backend source files.
FROM ghcr.io/open-webui/open-webui@sha256:a26effeb220e132482bf7e0560b3404843e7bc40d23051144e062960df8df6b0

COPY build /app/build
COPY backend/open_webui/config.py /app/backend/open_webui/config.py
COPY backend/open_webui/main.py /app/backend/open_webui/main.py
COPY backend/open_webui/routers/audio.py /app/backend/open_webui/routers/audio.py
