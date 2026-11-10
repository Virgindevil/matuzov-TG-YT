import {mergeTracks} from "./merge.js";

const key = location.hash.slice(1);
const status = document.querySelector("#status");
const save = document.querySelector("#save");
const job = /^download-[0-9a-f-]{36}$/.test(key) && (await chrome.storage.session.get(key))[key];

if (!job) {
    status.textContent = "Откройте сайт и снова выберите качество.";
} else {
    await chrome.storage.session.remove(key);
    document.querySelector("#filename").textContent = job.name;
    save.disabled = false;
    save.addEventListener("click", async () => {
        save.disabled = true;
        let writable;
        try {
            const file = await window.showSaveFilePicker({
                suggestedName: job.name,
                types: [{description: "Видео MP4", accept: {"video/mp4": [".mp4"]}}],
            });
            writable = await file.createWritable();
            status.textContent = "Скачиваем видео и звук напрямую с CDN…";
            let writtenMiB = 0;
            await mergeTracks(job.video, job.audio, {
                writable,
                onWrite: (_start, end) => {
                    const size = Math.floor(end / (1024 * 1024));
                    if (size <= writtenMiB) return;
                    writtenMiB = size;
                    status.textContent = "Записано " + size + " МБ…";
                },
            });
            status.textContent = "MP4 со звуком сохранён.";
        } catch (error) {
            await writable?.abort().catch(() => {});
            status.textContent = error.name === "AbortError"
                ? "Сохранение отменено."
                : "Не удалось сохранить MP4. Проверьте место на диске. Если CDN отклонил ссылку, найдите видео на сайте снова.";
            save.disabled = false;
        }
    });
}
