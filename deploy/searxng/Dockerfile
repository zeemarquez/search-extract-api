# syntax=docker/dockerfile:1.7
# SearXNG with the search-extract-api configuration baked in (no bind mount needed).
# Rebuild after editing settings.yml: docker compose build searxng
FROM docker.io/searxng/searxng:2026.10.17-1c10abe2d

# The entrypoint only writes to /etc/searxng when settings.yml is missing (it then copies
# the template) and chowns the directory to searxng:searxng when it isn't already, so ship
# the file owned by that user and the container never needs to modify /etc/searxng.
COPY --chown=searxng:searxng --chmod=0644 settings.yml /etc/searxng/settings.yml
