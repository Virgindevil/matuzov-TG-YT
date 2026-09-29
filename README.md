# Telegram YouTube → Cloudflare R2 bot

The bot inspects YouTube formats (480p–2160p), downloads the selected video with yt-dlp/FFmpeg, uploads it to a private Cloudflare R2 bucket, and returns a temporary presigned download URL.

## Render environment variables

Required:
- `BOT_TOKEN`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET`
- `R2_ENDPOINT` — `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

Optional:
- `R2_LINK_TTL=21600` (6 hours; R2 presigned URLs max 604800 seconds / 7 days)
- `DOWNLOAD_DIR=downloads`

Do not commit `.env` or credentials.

## Render
Deploy as a Docker Web Service. `/health` returns service status. `PORT` is supplied by Render.

## Important limitation
Cloudflare R2 can store multi-GB objects, but yt-dlp + FFmpeg still need local working space before this version uploads the final file. Render Free uses ephemeral local storage and is not suitable for guaranteed 10–20 GB processing. For truly large jobs, move the downloader/FFmpeg worker to compute with sufficient disk or implement a different streaming/transcoding architecture.
