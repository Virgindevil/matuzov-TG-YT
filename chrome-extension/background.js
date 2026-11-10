const site = "https://matuzov-tg-yt.onrender.com";

function isTrackUrl(value) {
    if (typeof value !== "string" || value.length > 16384) return false;
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname.endsWith(".googlevideo.com")
        && !url.username && !url.password && !url.port && url.pathname === "/videoplayback";
}

chrome.runtime.onMessage.addListener((job, sender, respond) => {
    if (sender.origin !== site || sender.frameId !== 0) return;
    openDownload(job).then(() => respond({ok: true}), () => respond({ok: false}));
    return true;
});

async function openDownload(job) {
    if (!isTrackUrl(job?.video) || !isTrackUrl(job?.audio)
        || typeof job.name !== "string" || job.name.length > 150) {
        throw new Error("Invalid download");
    }
    const key = "download-" + crypto.randomUUID();
    await chrome.storage.session.set({[key]: {video: job.video, audio: job.audio, name: job.name}});
    try {
        await chrome.tabs.create({url: chrome.runtime.getURL("save.html") + "#" + key});
    } catch (error) {
        await chrome.storage.session.remove(key);
        throw error;
    }
}
