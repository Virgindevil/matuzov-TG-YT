const url = document.querySelector("#url");
const find = document.querySelector("#find");
const message = document.querySelector("#msg");
const card = document.querySelector("#card");
const qualities = document.querySelector("#qualities");

document.querySelector("#search").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = url.value.trim();
    if (!input || find.disabled) return;

    message.textContent = "";
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
        card.classList.remove("hidden");
    } catch (error) {
        message.textContent = error.message;
    } finally {
        find.disabled = false;
        find.textContent = "Найти";
    }
});
