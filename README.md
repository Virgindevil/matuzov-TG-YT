# YouTube Telegram Bot + Cloudflare R2

## Render environment variables

- `BOT_TOKEN`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET`
- `R2_ENDPOINT`
- `R2_LINK_TTL=21600` (optional, 6 hours by default)

Deploy as a Docker Web Service. `/health` returns service status.

## Format selection

The bot first reads the complete YouTube format table without selecting a download format. It then displays available standard qualities from 480p through 2160p. After the user chooses a quality, yt-dlp downloads the best video stream at or below that resolution plus the best available audio, with fallbacks for videos that only expose combined streams.
