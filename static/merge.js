export const MAX_INPUT_BYTES = 200 * 1024 * 1024;

export async function fetchTrack(url, maxBytes) {
    const response = await fetch(url, {credentials: "omit", referrerPolicy: "no-referrer"});
    if (!response.ok || !response.body) throw new Error("CDN не отдал дорожку.");

    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > maxBytes) throw new Error("Дорожки превышают лимит 200 МБ.");
            chunks.push(value);
        }
        return new Blob(chunks);
    } finally {
        await reader.cancel().catch(() => {});
    }
}

export async function mergeTracks(videoFile, audioFile, {writable, onWrite} = {}) {
    if (!writable && videoFile.size + audioFile.size > MAX_INPUT_BYTES) {
        throw new Error("Дорожки превышают лимит 200 МБ. Выберите меньшее разрешение.");
    }
    const {
        Input, MP4, BlobSource, EncodedPacketSink, EncodedVideoPacketSource,
        EncodedAudioPacketSource, Output, Mp4OutputFormat, BufferTarget, StreamTarget, UrlSource,
    } = await import("https://cdn.jsdelivr.net/npm/mediabunny@1.65.2/+esm");

    const videoInput = openInput(videoFile);
    const audioInput = openInput(audioFile);
    const target = writable ? new StreamTarget(writable) : new BufferTarget();
    if (onWrite) target.onwrite = onWrite;
    const format = writable ? new Mp4OutputFormat({fastStart: false}) : new Mp4OutputFormat();
    const output = new Output({format, target});
    try {
        const video = await videoInput.getPrimaryVideoTrack();
        const audio = await audioInput.getPrimaryAudioTrack();
        const videoCodec = await video?.getCodec();
        const audioCodec = await audio?.getCodec();
        if (!videoCodec || !audioCodec) throw new Error("Проверьте, что выбраны видео MP4 и аудио M4A.");

        const videoSource = new EncodedVideoPacketSource(videoCodec);
        const audioSource = new EncodedAudioPacketSource(audioCodec);
        output.addVideoTrack(videoSource);
        output.addAudioTrack(audioSource);
        await output.start();
        await Promise.all([copyTrack(video, videoSource), copyTrack(audio, audioSource)]);
        await output.finalize();
        if (!writable) return new Blob([target.buffer], {type: "video/mp4"});
    } catch (error) {
        await output.cancel();
        throw error;
    } finally {
        videoInput.dispose();
        audioInput.dispose();
    }

    function openInput(input) {
        const source = typeof input === "string"
            ? new UrlSource(input, {
                requestInit: {credentials: "omit", referrerPolicy: "no-referrer"},
                maxCacheSize: 16 * 1024 * 1024,
                getRetryDelay: (attempt, error) => attempt < 1 && !(error instanceof TypeError) ? 1 : null,
            })
            : new BlobSource(input);
        return new Input({formats: [MP4], source});
    }

    async function copyTrack(track, source) {
        const config = await track.getDecoderConfig();
        if (!config) throw new Error("Формат дорожки не поддерживается.");
        let metadata = {decoderConfig: config};
        for await (const packet of new EncodedPacketSink(track).packets()) {
            await source.add(packet, metadata);
            metadata = undefined;
        }
        source.close();
    }
}
