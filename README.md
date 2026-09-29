# YouTube Downloader Web + Cloudflare R2

Веб-версия проекта без Telegram.

## Render Environment

Обязательные переменные:

- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET`
- `R2_ENDPOINT`

Опционально:

- `R2_LINK_TTL=21600`
- `MAX_CONCURRENT_JOBS=1`

`BOT_TOKEN` больше не используется.

## Deploy на Render

Создайте/используйте Web Service с Runtime = Docker и задеплойте репозиторий.
После запуска:

- `/health` — состояние сервиса
- `/` — веб-интерфейс

## Важно

Cloudflare R2 подходит для больших объектов, но текущий worker всё ещё использует локальное временное место Render для yt-dlp/FFmpeg.
Поэтому 10–20 ГБ файлы на бесплатном Render пока не гарантируются. Сначала проверьте полный поток на небольшом видео.
