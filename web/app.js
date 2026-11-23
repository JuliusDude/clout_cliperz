const get = id => document.getElementById(id);
const form = get("job");
const session = document.querySelector('meta[name="shorts-session"]').content;
const workflowNames = {"clout-cuts": "Clout-Cuts", streamers: "Streamers", podcast: "Podcast", "movie-recap": "Movie-Recap"};
let active = false;
let busy = false;
let clipKey = "";

function controls() {
  const mode = form.elements.mode.value;
  const workflow = form.elements.workflow.value;
  get("sourceLabel").textContent = workflow === "movie-recap" ? "Narrated recap video links" : "Creator video links";
  get("libraryTitle").textContent = (workflow === "movie-recap" ? "Movie-Recap" : "Clout-Cuts") + " library";
  get("lengthLabel").textContent = ["streamers", "podcast"].includes(workflow) ? "Auto length Â· max seconds" : "Max seconds";
  get("connection").hidden = workflow === "movie-recap";
  get("connect").disabled = busy || active;
  get("links").hidden = mode !== "links";
  get("follow").hidden = mode !== "follow";
  get("urls").required = mode === "links";
  get("channel").required = mode === "follow";
  get("start").textContent = mode === "links" ? "Create Shorts" : "Start following";
  get("start").disabled = busy || active;
  get("stop").disabled = busy || !active;
}

function renderClips(clips, workflow) {
  const key = JSON.stringify([workflow, clips]);
  if (key === clipKey) return;
  clipKey = key;
  get("empty").hidden = clips.length > 0;
  get("clips").replaceChildren(...clips.map(clip => {
    const prefix = workflow === "movie-recap" ? "/movie-recap" : "";
    const base = prefix + "/clips/" + encodeURIComponent(clip.id);
    const card = document.createElement("article");
    card.className = "clip";
    const video = document.createElement("video");
    video.controls = true;
    video.preload = "none";
    video.poster = base + "/thumbnail.jpg";
    video.src = base + "/short.mp4";
    const title = document.createElement("h3");
    title.textContent = clip.title;
    const description = document.createElement("p");
    description.textContent = clip.description || "";
    card.append(video, title, description);
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
  const workflow = form.elements.workflow.value;
  const response = await fetch("/api/status?workflow=" + encodeURIComponent(workflow));
  if (!response.ok) throw new Error("Worker connection failed.");
  const state = await response.json();
  if (workflow !== form.elements.workflow.value) return;
  active = state.active;
  get("channelStatus").textContent = state.authorizationSaved ? "YouTube authorization saved." : "YouTube setup required for uploads.";
  controls();
  get("status").textContent = state.status;
  get("activityTitle").textContent = state.jobWorkflow ? state.jobWorkflow + " activity" : "Activity";
  const log = get("log");
  const text = state.lines.join("\n") || "Waiting for worker output.";
  if (log.textContent !== text) {
    log.textContent = text;
    log.scrollTop = log.scrollHeight;
  }
  if (workflow === form.elements.workflow.value) renderClips(state.clips, workflow);
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

form.addEventListener("change", event => {
  controls();
  if (event.target.name === "workflow") {
    const workflow = form.elements.workflow.value;
    get("seconds").value = ({streamers: 60, podcast: 120})[workflow] || 35;
    get("caption").value = ({streamers: "stream-pop", podcast: "classic"})[workflow] || "pop";
    get("visibility").value = workflow === "movie-recap" ? "local" : "private";
    renderClips([], workflow);
    get("error").hidden = true;
  }
});
form.addEventListener("submit", event => {
  event.preventDefault();
  const mode = form.elements.mode.value;
  action("/api/start", {
    mode,
    workflow: form.elements.workflow.value,
    source: get(mode === "links" ? "urls" : "channel").value,
    perVideo: get("perVideo").checked,
    latest: get("latest").checked,
    count: get("count").value,
    seconds: get("seconds").value,
    caption: get("caption").value,
    visibility: get("visibility").value
  });
});
get("connect").addEventListener("click", () => action("/api/connect", {}));
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
