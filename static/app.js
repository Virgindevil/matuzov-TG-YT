import {fetchTrack, MAX_INPUT_BYTES, mergeTracks} from "./merge.js";

const url = document.querySelector("#url");
const find = document.querySelector("#find");
const message = document.querySelector("#msg");
const card = document.querySelector("#card");
const qualities = document.querySelector("#qualities");
const mergePanel = document.querySelector("#merge-panel");
const manual = document.querySelector("#merge-manual");
const mergeStatus = document.querySelector("#merge-status");
const saveMp4 = document.querySelector("#save-mp4");
const videoFile = document.querySelector("#video-file");
const audioFile = document.querySelector("#audio-file");
const mergeFiles = document.querySelector("#merge-files");
let outputUrl;
let outputName;

function clearOutput() {
    if (outputUrl) URL.revokeObjectURL(outputUrl);
    outputUrl = undefined;
    saveMp4.removeAttribute("href");
    saveMp4.classList.add("hidden");
}

function setMerging(busy) {
    find.disabled = busy;
    mergeFiles.disabled = busy;
    for (const button of qualities.querySelectorAll("button")) button.disabled = busy;
}

async function assemble(video, audio) {
    mergeStatus.textContent = "Объединяем дорожки…";
    const mp4 = await mergeTracks(video, audio);
    outputUrl = URL.createObjectURL(mp4);
    saveMp4.href = outputUrl;
    saveMp4.download = outputName;
    saveMp4.classList.remove("hidden");
    mergeStatus.textContent = "MP4 готов.";
}

async function downloadAndMerge(pair, title) {
    clearOutput();
    mergePanel.classList.remove("hidden");
    manual.classList.add("hidden");
    videoFile.value = "";
    audioFile.value = "";
    document.querySelector("#video-track").href = pair.video_url;
    document.querySelector("#audio-track").href = pair.audio_url;
    outputName = (title.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").trim().slice(0, 120) || "Видео")
        + " - " + pair.label + ".mp4";
    setMerging(true);
    try {
        mergeStatus.textContent = "Загружаем дорожки напрямую с CDN…";
        const video = await fetchTrack(pair.video_url, MAX_INPUT_BYTES);
        const audio = await fetchTrack(pair.audio_url, MAX_INPUT_BYTES - video.size);
        await assemble(video, audio);
    } catch {
        mergeStatus.textContent = "Автоматическая сборка не удалась. Можно сохранить дорожки отдельно. Если ссылки устарели, найдите видео снова.";
        manual.classList.remove("hidden");
    } finally {
        setMerging(false);
    }
}

mergeFiles.addEventListener("click", async () => {
    if (!videoFile.files[0] || !audioFile.files[0]) {
        mergeStatus.textContent = "Выберите оба файла.";
        return;
    }
    clearOutput();
    setMerging(true);
    try {
        await assemble(videoFile.files[0], audioFile.files[0]);
    } catch {
        mergeStatus.textContent = "Не удалось собрать MP4. Проверьте формат файлов и лимит 200 МБ.";
    } finally {
        setMerging(false);
    }
});

document.querySelector("#search").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = url.value.trim();
    if (!input || find.disabled) return;

    message.textContent = "";
    clearOutput();
    mergePanel.classList.add("hidden");
    card.classList.add("hidden");
    qualities.replaceChildren();
    find.disabled = true;
    find.textContent = "Ищем…";
    try {
        const response = await fetch("/api/analyze", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({url: input}),
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(typeof data.detail === "string" ? data.detail : "Не удалось найти видео.");
        }

        document.querySelector("#title").textContent = data.title;
        document.querySelector("#source").textContent = data.source;
        for (const quality of data.qualities) {
            const link = document.createElement("a");
            link.className = "download";
            link.href = quality.url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = quality.label + " · MP4";
            qualities.append(link);
        }
        for (const pair of data.track_pairs ?? []) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "download";
            button.textContent = pair.label + " · Собрать MP4";
            button.addEventListener("click", () => downloadAndMerge(pair, data.title));
            qualities.append(button);
        }
        card.classList.remove("hidden");
    } catch (error) {
        message.textContent = error.message;
    } finally {
        find.disabled = false;
        find.textContent = "Найти";
    }
});
