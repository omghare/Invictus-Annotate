const videoPlayer = document.getElementById('video-player');
const slider = document.getElementById('video-slider');
const btnPlay = document.getElementById('btn-play');
const btnSetStart = document.getElementById('btn-set-start');
const btnSetEnd = document.getElementById('btn-set-end');
const btnExport = document.getElementById('btn-export');
const lblCurrentTime = document.getElementById('lbl-current-time');
const lblTotalTime = document.getElementById('lbl-total-time');
const lblStartTime = document.getElementById('lbl-start-time');
const lblEndTime = document.getElementById('lbl-end-time');

let currentVideoPath = null;
let startTimeSec = 0;
let endTimeSec = 0;
let isVideoLoaded = false;

function formatTime(seconds) {
    if (isNaN(seconds) || !isFinite(seconds)) return "00:00:00.00";
    const date = new Date(seconds * 1000);
    const hh = String(date.getUTCHours()).padStart(2, '0');
    const mm = String(date.getUTCMinutes()).padStart(2, '0');
    const ss = String(date.getUTCSeconds()).padStart(2, '0');
    const ms = String(date.getUTCMilliseconds()).padStart(3, '0').slice(0,2);
    return `${hh}:${mm}:${ss}.${ms}`;
}

async function pickImportFile() {
    try {
        const res = await fetch('/api/video/pick_import');
        if (res.ok) {
            const data = await res.json();
            document.getElementById('import-path').value = data.filepath;
        } else {
            console.log("No file selected or picker failed.");
        }
    } catch (e) {
        console.error("Picker error", e);
    }
}

async function loadVideo() {
    const pathInput = document.getElementById('import-path').value.trim();
    if (!pathInput) {
        alert("Please enter a valid video path.");
        return;
    }

    currentVideoPath = pathInput;
    
    // Set video stream source
    videoPlayer.src = `/api/video/stream?path=${encodeURIComponent(currentVideoPath)}`;
    videoPlayer.load();

    // Enable buttons
    btnPlay.disabled = false;
    btnSetStart.disabled = false;
    btnSetEnd.disabled = false;
    btnExport.disabled = false;
    isVideoLoaded = true;

    // Reset UI
    document.getElementById('lbl-cut-fps').textContent = "N/A";
    document.getElementById('lbl-cut-res').textContent = "N/A";
    document.getElementById('lbl-cut-codec').textContent = "N/A";
    document.getElementById('lbl-cut-frames').textContent = "N/A";
    document.getElementById('lbl-cut-size').textContent = "N/A";

    // Fetch Stats
    try {
        const res = await fetch('/api/video/stats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filepath: currentVideoPath })
        });
        if (res.ok) {
            const stats = await res.json();
            document.getElementById('lbl-src-fps').textContent = `${stats.fps} fps`;
            document.getElementById('lbl-src-res').textContent = stats.resolution;
            document.getElementById('lbl-src-codec').textContent = stats.codec;
            document.getElementById('lbl-src-frames').textContent = `${stats.frames} frames`;
            document.getElementById('lbl-src-size').textContent = `${stats.avg_size_kb} KB`;
        }
    } catch (e) {
        console.error("Failed to load video stats", e);
    }
}

function togglePlay() {
    if (videoPlayer.paused) {
        videoPlayer.play();
    } else {
        videoPlayer.pause();
    }
}

// Video Events
videoPlayer.addEventListener('loadedmetadata', () => {
    slider.max = videoPlayer.duration;
    lblTotalTime.textContent = formatTime(videoPlayer.duration);
    endTimeSec = videoPlayer.duration;
    updateCutLabels();
});

videoPlayer.addEventListener('timeupdate', () => {
    slider.value = videoPlayer.currentTime;
    lblCurrentTime.textContent = formatTime(videoPlayer.currentTime);
});

slider.addEventListener('input', () => {
    videoPlayer.currentTime = slider.value;
});

function setStartTime() {
    startTimeSec = videoPlayer.currentTime;
    updateCutLabels();
}

function setEndTime() {
    endTimeSec = videoPlayer.currentTime;
    updateCutLabels();
}

function updateCutLabels() {
    lblStartTime.textContent = `Start: ${formatTime(startTimeSec)}`;
    lblEndTime.textContent = `End:   ${formatTime(endTimeSec)}`;

    const duration = videoPlayer.duration || 1;
    let startPercent = (startTimeSec / duration) * 100;
    let endPercent = (endTimeSec / duration) * 100;
    
    if (endPercent < startPercent) {
        let temp = startPercent;
        startPercent = endPercent;
        endPercent = temp;
    }

    const highlight = document.getElementById('timeline-highlight');
    if (highlight) {
        highlight.style.left = `${startPercent}%`;
        highlight.style.width = `${endPercent - startPercent}%`;
    }

    const mStart = document.getElementById('marker-start');
    const mEnd = document.getElementById('marker-end');
    if (mStart) {
        mStart.style.display = 'block';
        mStart.style.left = `${(startTimeSec / duration) * 100}%`;
    }
    if (mEnd) {
        mEnd.style.display = 'block';
        mEnd.style.left = `${(endTimeSec / duration) * 100}%`;
    }
}

async function exportVideo() {
    if (!currentVideoPath) return;

    if (startTimeSec >= endTimeSec) {
        alert("Timeline Error: In-Point (Start) must be before Out-Point (End).");
        return;
    }

    // Call backend to open native save picker
    const defaultNameMatch = currentVideoPath.match(/([^\\]+)(\.[^.]+)$/);
    const defaultName = defaultNameMatch ? defaultNameMatch[1] + "_cut" + defaultNameMatch[2] : "cut_video.mp4";
    let savePath = null;
    
    btnExport.disabled = true;
    btnExport.textContent = "⏳ Waiting for location...";

    try {
        const pickerRes = await fetch(`/api/video/pick_export?default_name=${encodeURIComponent(defaultName)}`);
        if (pickerRes.ok) {
            const data = await pickerRes.json();
            savePath = data.filepath;
        }
    } catch (e) {
        console.error("Save picker failed", e);
    }

    if (!savePath) {
        btnExport.disabled = false;
        btnExport.textContent = "Export Lossless Cut";
        return; // User cancelled
    }

    btnExport.textContent = "⏳ Exporting...";

    try {
        const res = await fetch('/api/video/cut', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filepath: currentVideoPath,
                save_path: savePath,
                start_sec: startTimeSec,
                end_sec: endTimeSec
            })
        });

        if (res.ok) {
            alert("Export Complete: Video exported losslessly!");
            
            // Fetch stats for the new video
            const statsRes = await fetch('/api/video/stats', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filepath: savePath })
            });
            if (statsRes.ok) {
                const stats = await statsRes.json();
                document.getElementById('lbl-cut-fps').textContent = `${stats.fps} fps`;
                document.getElementById('lbl-cut-res').textContent = stats.resolution;
                document.getElementById('lbl-cut-codec').textContent = stats.codec;
                document.getElementById('lbl-cut-frames').textContent = `${stats.frames} frames`;
                document.getElementById('lbl-cut-size').textContent = `${stats.avg_size_kb} KB`;
            }
        } else {
            const err = await res.json();
            alert(`Export Failed: ${err.detail}`);
        }
    } catch (e) {
        alert("Export Error: " + e.message);
    } finally {
        btnExport.disabled = false;
        btnExport.textContent = "Export Lossless Cut";
    }
}
