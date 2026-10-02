const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
const clips = []; // { id, name, el, uploading }
const assets = { music: null, logo: null, broll: [] };

const STORE = "raw-to-reel-options";
const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
if (!("auto_reframe" in saved)) delete saved.caption_position; // settings saved before smart placement existed

function applyOptions(values) {
  document.querySelectorAll("[data-opt]").forEach((el) => {
    const key = el.dataset.opt;
    if (!(key in values)) return;
    if (el.type === "checkbox") el.checked = values[key];
    else if (el.type === "radio") el.checked = el.value === values[key];
    else el.value = values[key];
  });
  syncSubs();
}

function readOptions() {
  const opts = {};
  document.querySelectorAll("[data-opt]").forEach((el) => {
    if (el.type === "radio") { if (el.checked) opts[el.dataset.opt] = el.value; }
    else opts[el.dataset.opt] = el.type === "checkbox" ? el.checked : el.value;
  });
  for (const k of ["target_length", "reel_length", "speed"]) opts[k] = Number(opts[k] || 0);
  return opts;
}

function saveOptions() {
  localStorage.setItem(STORE, JSON.stringify(readOptions()));
  $("saved").style.opacity = 1;
  setTimeout(() => ($("saved").style.opacity = 0.4), 900);
}

function syncSubs() {
  document.querySelectorAll("[data-sub]").forEach((el) => $(el.dataset.sub).classList.toggle("show", el.checked));
  const src = document.querySelector("[data-opt=music_source]");
  if (src) $("musicPicker").parentElement.style.display = src.value === "upload" ? "" : "none";
}

document.querySelectorAll("[data-opt]").forEach((el) => el.addEventListener("change", () => { syncSubs(); saveOptions(); }));
applyOptions(saved);

const api = (url, body, method) =>
  fetch(url, body === undefined && !method ? {} : {
    method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}),
  }).then((r) => r.json());

// ---------------------------------------------------------------- uploads

function upload(file, kind, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/upload?kind=${kind}&name=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) {}
      xhr.status === 200 ? resolve(data) : reject(data.error || `فشل الرفع (${xhr.status})`);
    };
    xhr.onerror = () => reject("فشل الرفع: الاتصال بالسيرفر انقطع. تأكد إن النافذة السوداء مفتوحة");
    xhr.send(file);
  });
}

function addClips(files) {
  for (const file of files) {
    if (!file.type.startsWith("video/")) continue;
    const el = document.createElement("div");
    el.className = "clip";
    el.innerHTML = `<video muted src="${URL.createObjectURL(file)}"></video><div></div><span class="bar"></span><button title="حذف">×</button>`;
    el.querySelector("div").textContent = file.name;
    const clip = { id: null, name: file.name, el, uploading: true };
    current = null; // new upload: the log goes back to upload status
    clips.push(clip);
    el.querySelector("button").onclick = () => { clips.splice(clips.indexOf(clip), 1); el.remove(); refresh(); };
    $("clips").appendChild(el);
    upload(file, "video", (p) => (el.querySelector(".bar").style.width = `${p * 100}%`))
      .then((res) => { clip.id = res.id; clip.uploading = false; el.querySelector(".bar").style.width = "0"; refresh(); })
      .catch((err) => {
        el.querySelector("div").textContent = "❌ فشل الرفع";
        clip.uploading = false;
        refresh();
        $("log").textContent = `❌ ${file.name}: ${err}`;
      });
  }
  refresh();
}

let busy = false;
function refresh() {
  const ready = clips.filter((c) => c.id);
  const uploading = clips.some((c) => c.uploading);
  $("go").disabled = $("quick").disabled = !ready.length || uploading || busy;
  if (busy || (current && !uploading)) return; // keep the finished job's log on screen
  if (uploading) $("log").textContent = "جارٍ رفع الفيديو...";
  else if (ready.length) $("log").textContent = `${ready.length} فيديو جاهز. اختر التعديلات واضغط "ابدأ المونتاج".`;
}
function setBusy(b) {
  busy = b;
  $("rvRender").disabled = $("rvPreviewBtn").disabled = b;
  refresh();
}

$("drop").onclick = () => $("picker").click();
$("picker").onchange = (e) => addClips(e.target.files);
$("drop").ondragover = (e) => { e.preventDefault(); $("drop").classList.add("over"); };
$("drop").ondragleave = () => $("drop").classList.remove("over");
$("drop").ondrop = (e) => { e.preventDefault(); $("drop").classList.remove("over"); addClips(e.dataTransfer.files); };

function picker(input, label, kind, multiple) {
  $(input).onchange = async (e) => {
    const files = [...e.target.files];
    if (!files.length) return;
    $(label).style.color = "";
    $(label).textContent = "جارٍ الرفع...";
    try {
      const ids = [];
      for (const f of files) ids.push((await upload(f, kind)).id);
      if (multiple) assets[kind] = ids; else assets[kind] = ids[0];
      $(label).textContent = `✓ ${files.map((f) => f.name).join("، ")}`;
    } catch (err) {
      $(label).style.color = "#c0392b";
      $(label).textContent = `❌ ${err}`;
    }
  };
}
picker("musicPicker", "musicName", "music", false);
picker("logoPicker", "logoName", "logo", false);
picker("brollPicker", "brollName", "broll", true);

// ---------------------------------------------------------------- presets & dictionary

async function loadPresets(selected) {
  const presets = await api("/api/presets");
  $("presetSel").innerHTML = `<option value="">📁 القوالب المحفوظة…</option>` +
    Object.keys(presets).map((n) => `<option ${n === selected ? "selected" : ""}>${esc(n)}</option>`).join("");
  $("presetSel").onchange = () => {
    const p = presets[$("presetSel").value];
    if (p) { applyOptions(p); saveOptions(); }
  };
}
$("presetSave").onclick = async () => {
  const name = prompt("اسم القالب (مثلاً: ريل ديبو)", $("presetSel").value || "");
  if (!name) return;
  const res = await api("/api/presets", { name, options: readOptions() });
  if (res.error) return alert(res.error);
  loadPresets(name);
};
$("presetDel").onclick = async () => {
  const name = $("presetSel").value;
  if (!name || !confirm(`تحذف القالب «${name}»؟`)) return;
  await api(`/api/presets?name=${encodeURIComponent(name)}`, undefined, "DELETE");
  loadPresets();
};
loadPresets();

async function loadDictionary() {
  const d = await api("/api/dictionary");
  $("dictBox").value = Object.entries(d).map(([k, v]) => `${k} = ${v}`).join("\n");
}
$("dictSave").onclick = async () => {
  const entries = {};
  for (const line of $("dictBox").value.split("\n")) {
    const [k, ...rest] = line.split("=");
    if (k && rest.length && rest.join("=").trim()) entries[k.trim()] = rest.join("=").trim();
  }
  const d = await api("/api/dictionary", entries);
  $("dictMsg").textContent = `✓ ${Object.keys(d).length} كلمة محفوظة`;
  loadDictionary();
};
loadDictionary();

// ---------------------------------------------------------------- jobs

const jobs = {};
let current = null;

async function startJobs(mode) {
  const opts = readOptions();
  setBusy(true);
  $("result").innerHTML = "";
  $("log").textContent = "";
  $("bar").style.width = "0";
  $("review").classList.remove("show");
  const res = await api("/api/edit", {
    videos: clips.filter((c) => c.id).map((c) => c.id), ...assets, options: opts, mode, batch: opts.batch,
  });
  if (res.error) { $("log").textContent = res.error; setBusy(false); return; }
  for (const k of Object.keys(jobs)) delete jobs[k];
  res.ids.forEach((id) => (jobs[id] = { id, status: "queued", progress: 0, name: "" }));
  current = res.ids[0];
  res.ids.forEach(poll);
}
$("go").onclick = () => startJobs(readOptions().review_mode ? "review" : "auto");
$("quick").onclick = () => startJobs("preview");

async function poll(id) {
  const job = await fetch(`/api/status/${id}`).then((r) => r.json());
  jobs[id] = job;
  renderJobs();
  if (id === current) showJob(job);
  if (job.status === "running" || job.status === "queued") return setTimeout(() => poll(id), 1000);
  if (!Object.values(jobs).some((j) => j.status === "running" || j.status === "queued")) setBusy(false);
}

const STATUS = { queued: "⏳ بالانتظار", running: "⚙️ شغّال", review: "✎ جاهز للمراجعة", done: "✅ جاهز", error: "❌ خطأ" };
function renderJobs() {
  const list = Object.values(jobs);
  if (list.length < 2) { $("jobs").innerHTML = ""; return; }
  $("jobs").innerHTML = list.map((j) => `
    <div class="job ${j.id === current ? "active" : ""}" data-id="${j.id}">
      <b>${esc(j.name || "فيديو")}</b> — ${STATUS[j.status] || ""}
      <div class="jbar"><div style="width:${Math.round((j.progress || 0) * 100)}%"></div></div>
    </div>`).join("");
  $("jobs").querySelectorAll(".job").forEach((el) => (el.onclick = () => { current = el.dataset.id; renderJobs(); showJob(jobs[current]); }));
}

function showJob(job) {
  $("bar").style.width = `${Math.round((job.progress || 0) * 100)}%`;
  const lines = (job.log || []).map((l) => `<div>${esc(l)}</div>`);
  if (job.status === "queued") lines.push(`<div>⏳ بالانتظار${job.ahead ? ` (قبله ${job.ahead})` : ""}...</div>`);
  $("log").innerHTML = lines.join("");
  $("log").scrollTop = 1e9;
  if (job.status === "done") { $("review").classList.remove("show"); showResult(job.id, job.result); }
  else $("result").innerHTML = "";
  if (job.status === "review") showReview(job);
}

// ---------------------------------------------------------------- review page

let review = null; // { id, segs, removed:Set, cutSegs:Set, fixes:{} }

function showReview(job) {
  if (!review || review.id !== job.id) {
    review = { id: job.id, segs: job.review, removed: new Set(), cutSegs: new Set(), fixes: {} };
    buildTranscript();
  }
  $("review").classList.add("show");
  $("rvPreview").innerHTML = job.preview ? `
    <div class="reel"><h3>👁️ المعاينة (أول 15 ثانية)</h3>
      <video controls autoplay src="/workspace/jobs/${job.id}/${job.preview.final}?t=${Date.now()}"></video></div>` : "";
  updateStats();
}

function buildTranscript() {
  $("transcript").innerHTML = review.segs.map((s) => `
    <div class="seg" data-seg="${s.seg}">
      <span class="tools"><button class="mini" data-play="${s.seg}" title="شغّل الجملة">▶</button>
        <button class="mini" data-cut="${s.seg}" title="احذف الجملة كاملة">✕</button></span>
      ${s.words.length ? s.words.map((w) => `<span class="w" data-id="${w.id}" data-seg="${s.seg}">${esc(w.text)}</span>`).join(" ")
        : `<i style="color:#8a8580">(مقطع بدون كلام، ${Math.round(s.end - s.start)} ث)</i>`}
    </div>`).join("");
  const first = review.segs[0];
  if (first) setSource(first.file, first.start);
  $("transcript").onclick = (e) => {
    const play = e.target.dataset.play, cut = e.target.dataset.cut, w = e.target.closest(".w");
    if (play !== undefined) { const s = review.segs[play]; setSource(s.file, s.start, true); return; }
    if (cut !== undefined) {
      const n = Number(cut);
      review.cutSegs.has(n) ? review.cutSegs.delete(n) : review.cutSegs.add(n);
      $("transcript").querySelector(`.seg[data-seg="${n}"]`).classList.toggle("cut", review.cutSegs.has(n));
      return updateStats();
    }
    if (!w) return;
    const id = Number(w.dataset.id);
    if (e.altKey) {
      const s = review.segs[w.dataset.seg], word = s.words.find((x) => x.id === id);
      return setSource(s.file, word.start, true);
    }
    review.removed.has(id) ? review.removed.delete(id) : review.removed.add(id);
    w.classList.toggle("del", review.removed.has(id));
    updateStats();
  };
  $("transcript").ondblclick = (e) => {
    const w = e.target.closest(".w");
    if (!w) return;
    const id = Number(w.dataset.id);
    review.removed.delete(id); // a double click is two clicks; editing means the word stays
    w.classList.remove("del");
    const text = prompt("صحّح الكلمة:", w.textContent);
    if (text && text.trim() && text.trim() !== w.textContent) {
      review.fixes[id] = text.trim();
      w.textContent = text.trim();
      w.classList.add("fixed");
    }
    updateStats();
  };
}

let playingFile = null;
function setSource(file, at, play) {
  const v = $("rvPlayer");
  if (playingFile !== file) { v.src = `/workspace/uploads/video/${encodeURIComponent(file)}`; playingFile = file; }
  const go = () => { v.currentTime = at; if (play) v.play(); };
  v.readyState >= 1 ? go() : v.addEventListener("loadedmetadata", go, { once: true });
}
$("rvPlayer").ontimeupdate = () => {
  if (!review) return;
  const t = $("rvPlayer").currentTime;
  document.querySelectorAll(".w.playing").forEach((el) => el.classList.remove("playing"));
  for (const s of review.segs) {
    if (s.file !== playingFile || t < s.start || t > s.end) continue;
    const word = [...s.words].reverse().find((w) => w.start <= t);
    if (word) document.querySelector(`.w[data-id="${word.id}"]`)?.classList.add("playing");
  }
};

function updateStats() {
  if (!review) return;
  const fixes = Object.keys(review.fixes).length;
  $("rvStats").textContent = `محذوف: ${review.removed.size} كلمة و${review.cutSegs.size} جملة — مصحّح: ${fixes} كلمة`;
}

async function sendReview(preview) {
  setBusy(true);
  const res = await api(`/api/render/${review.id}`, {
    removed: [...review.removed], removed_segments: [...review.cutSegs], corrections: review.fixes,
    save_dictionary: $("saveDict").checked, preview,
  });
  if (res.error) { alert(res.error); setBusy(false); return; }
  if (!preview) $("review").classList.remove("show");
  if ($("saveDict").checked && Object.keys(review.fixes).length) loadDictionary();
  poll(review.id);
}
$("rvRender").onclick = () => sendReview(false);
$("rvPreviewBtn").onclick = () => sendReview(true);

// ---------------------------------------------------------------- results

function showResult(id, r) {
  const base = `/workspace/jobs/${id}/`;
  const link = (file, label) => (file ? `<a href="${base}${file}" download>${label}</a>` : "");
  const many = r.reels.length > 1;
  const reels = r.reels.map((reel, i) => `
    <div class="reel">
      ${many ? `<h3>ريل ${i + 1} (${reel.seconds} ث)</h3>` : ""}
      <video controls preload="metadata" src="${base}${reel.final}"></video>
      <div class="downloads">
        ${link(reel.final, "⬇️ تحميل الفيديو MP4")}
        ${link(reel.thumbnail, "🖼️ صورة الغلاف")}
        ${link(reel.srt, "💬 ملف الترجمة SRT")}
        ${link(reel.srt_en, "🇬🇧 الترجمة الإنجليزية SRT")}
      </div>
    </div>`).join("");
  $("result").innerHTML = `
    <div style="font-size:13px;margin-top:10px">من ${r.input_seconds} ث ← ${r.output_seconds} ث (${r.segments} لقطة${many ? `، ${r.reels.length} ريلز` : ""})</div>
    ${r.captions_missing ? `<div class="warn">⚠️ الترجمة ما انعملت لأن تفريغ الصوت فشل. ارجع فوق في السجل وشوف السطر اللي فيه ⚠️</div>` : ""}
    ${reels}
    <div class="downloads reel">
      ${link(r.edl, "🎬 Timeline لـ DaVinci Resolve (EDL)")}
      ${link(r.resolve_script, "🐍 سكربت فتح المشروع في Resolve")}
    </div>`;
}
