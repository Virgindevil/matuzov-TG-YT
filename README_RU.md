# Video Downloader v5.1 Web — Render Secret File

Эта версия рассчитана на Secret File Render:

- Filename: `youtube.txt`
- Runtime path: `/etc/secrets/youtube.txt`

Содержимое `youtube.txt` НЕ нужно добавлять в GitHub.

## Что исправлено относительно v5

Функция `opts(url="")` сохранена в нужном виде, но теперь URL действительно
передаётся в неё в ОБОИХ местах:

- анализ: `yt_dlp.YoutubeDL(opts(u) | {"skip_download": True})`
- скачивание: `o = opts(u) | {...}`

Поэтому YouTube получает `cookiefile=/etc/secrets/youtube.txt`, если Secret File существует.

## Проверка после Deploy

Открой:

`https://ВАШ-СЕРВИС.onrender.com/api/health`

Должно быть примерно:

`{"ok":true,"youtube_cookies":true,"youtube_cookies_size":12345}`

Если `youtube_cookies` = false, Render не видит Secret File.
Если true, приложение нашло файл. Само содержимое cookies API никогда не показывает.

После изменения Secret File или кода сделай новый Deploy/Restart сервиса.

## Безопасность

Не коммить `youtube.txt` в GitHub.
Secret File содержит данные сессии аккаунта; не публикуй его содержимое.
