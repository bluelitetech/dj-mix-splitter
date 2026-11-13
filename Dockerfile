FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN pip install --no-cache-dir "yt-dlp[default]" shazamio
WORKDIR /app
COPY trackid.py webui.py selftest.py ./
RUN mkdir -p /data /music && chmod 777 /data
ENV MUSIC_LIBRARY=/music DATA_DIR=/data PORT=8765 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8765
CMD ["python", "-u", "webui.py"]
