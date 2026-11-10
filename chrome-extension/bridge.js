document.documentElement.dataset.videoDownloaderHd = "1";
document.addEventListener("video-downloader-hd-save", async (event) => {
    let ok = false;
    try {
        const job = JSON.parse(event.detail);
        const result = await chrome.runtime.sendMessage(job);
        ok = result?.ok === true;
    } catch {
        // The extension may have been reloaded since the page opened.
    }
    document.dispatchEvent(new CustomEvent("video-downloader-hd-result", {detail: ok}));
});
