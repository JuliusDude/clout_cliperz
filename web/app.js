const get = id => document.getElementById(id);
const form = get("job");
const session = document.querySelector('meta[name="shorts-session"]').content;
let active = false;
let busy = false;
let clipKey = "";

function controls() {
  const mode = form.elements.mode.value;
  get("links").hidden = mode !== "links";
  get("follow").hidden = mode !== "follow";
  get("urls").required = mode === "links";
  get("channel").required = mode === "follow";
  get("start").textContent = mode === "links" ? "Create Shorts" : "Start following";
  get("start").disabled = busy || active;
  get("stop").disabled = busy || !active;
}

function renderClips(clips) {
  const key = JSON.stringify(clips);
  if (key === clipKey) return;
  clipKey = key;
  get("empty").hidden = clips.length > 0;
  get("clips").replaceChildren(...clips.map(clip => {
    const base = "/clips/" + encodeURIComponent(clip.id);
    const card = document.createElement("article");
    card.className = "clip";
    const video = document.createElement("video");
    video.controls = true;
    video.preload = "none";
    video.poster = base + "/thumbnail.jpg";
    video.src = base + "/short.mp4";
    const title = document.createElement("h3");
    title.textContent = clip.title;
    card.append(video, title);
    for (const [label, file] of [["Video", "short.mp4"], ["Thumbnail", "thumbnail.jpg"], ["Captions", "captions.srt"]]) {
      const link = document.createElement("a");
      link.textContent = label;
      link.href = base + "/" + file;
      link.download = file;
      card.append(link);
    }
    if (clip.videoId && /^[\w-]{11}$/.test(clip.videoId)) {
      const link = document.createElement("a");
      link.textContent = "YouTube Short";
      link.href = "https://www.youtube.com/shorts/" + clip.videoId;
      card.append(link);
    }
    return card;
  }));
}

async function refresh() {
  const response = await fetch("/api/status");
  if (!response.ok) throw new Error("Worker connection failed.");
  const state = await response.json();
  active = state.active;
  controls();
  get("status").textContent = state.status;
  const log = get("log");
  const text = state.lines.join("\n") || "Waiting for worker output.";
  if (log.textContent !== text) {
    log.textContent = text;
    log.scrollTop = log.scrollHeight;
  }
  renderClips(state.clips);
}

async function action(path, data) {
  busy = true;
  controls();
  get("error").hidden = true;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Shorts-Session": session},
      body: JSON.stringify(data)
    });
    if (!response.ok) throw new Error((await response.json()).error || "Job request failed.");
    await refresh();
  } catch (error) {
    get("error").textContent = error.message;
    get("error").hidden = false;
  } finally {
    busy = false;
    controls();
  }
}

form.addEventListener("change", controls);
form.addEventListener("submit", event => {
  event.preventDefault();
  const mode = form.elements.mode.value;
  action("/api/start", {
    mode,
    source: get(mode === "links" ? "urls" : "channel").value,
    perVideo: get("perVideo").checked,
    latest: get("latest").checked,
    count: get("count").value,
    seconds: get("seconds").value,
    caption: get("caption").value,
    visibility: get("visibility").value
  });
});
get("stop").addEventListener("click", () => action("/api/stop", {}));
controls();
async function poll() {
  try {
    await refresh();
  } catch {
    get("status").textContent = "Disconnected";
  }
  setTimeout(poll, 1500);
}
poll();
