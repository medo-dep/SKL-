const $ = (id) => document.getElementById(id);
const clips = []; // { id, name, el, uploading }
let musicId = null;

const STORE = "raw-to-reel-options";
const saved = JSON.parse(localStorage.getItem(STORE) || "{}");

document.querySelectorAll("[data-opt]").forEach((el) => {
  const key = el.dataset.opt;
  if (key in saved) el.type === "checkbox" ? (el.checked = saved[key]) : (el.value = saved[key]);
  el.addEventListener("change", () => { syncSubs(); saveOptions(); });
});

function readOptions() {
  const opts = {};
  document.querySelectorAll("[data-opt]").forEach((el) => {
    opts[el.dataset.opt] = el.type === "checkbox" ? el.checked : el.value;
  });
  opts.target_length = Number(opts.target_length || 0);
  return opts;
}

function saveOptions() {
  localStorage.setItem(STORE, JSON.stringify(readOptions()));
  $("saved").style.opacity = 1;
  setTimeout(() => ($("saved").style.opacity = 0.4), 900);
}

function syncSubs() {
  document.querySelectorAll("[data-sub]").forEach((el) => $(el.dataset.sub).classList.toggle("show", el.checked));
}
syncSubs();

function upload(file, kind, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/upload?kind=${kind}&name=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
    xhr.onload = () => (xhr.status === 200 ? resolve(JSON.parse(xhr.responseText)) : reject(xhr.responseText));
    xhr.onerror = () => reject("فشل الرفع");
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
    clips.push(clip);
    el.querySelector("button").onclick = () => { clips.splice(clips.indexOf(clip), 1); el.remove(); refresh(); };
    $("clips").appendChild(el);
    upload(file, "video", (p) => (el.querySelector(".bar").style.width = `${p * 100}%`))
      .then((res) => { clip.id = res.id; clip.uploading = false; el.querySelector(".bar").style.width = "0"; refresh(); })
      .catch((err) => { el.querySelector("div").textContent = `❌ ${err}`; clip.uploading = false; refresh(); });
  }
  refresh();
}

function refresh() {
  const ready = clips.filter((c) => c.id);
  const busy = clips.some((c) => c.uploading);
  $("go").disabled = !ready.length || busy;
  if (busy) $("log").textContent = "جارٍ رفع الفيديو...";
  else if (ready.length) $("log").textContent = `${ready.length} فيديو جاهز. اختر التعديلات واضغط "ابدأ المونتاج".`;
}

$("drop").onclick = () => $("picker").click();
$("picker").onchange = (e) => addClips(e.target.files);
$("drop").ondragover = (e) => { e.preventDefault(); $("drop").classList.add("over"); };
$("drop").ondragleave = () => $("drop").classList.remove("over");
$("drop").ondrop = (e) => { e.preventDefault(); $("drop").classList.remove("over"); addClips(e.dataTransfer.files); };

$("musicPicker").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  $("musicName").textContent = "جارٍ الرفع...";
  const res = await upload(file, "music");
  musicId = res.id;
  $("musicName").textContent = `✓ ${file.name}`;
};

$("go").onclick = async () => {
  $("go").disabled = true;
  $("result").innerHTML = "";
  $("log").textContent = "";
  const res = await fetch("/api/edit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ videos: clips.filter((c) => c.id).map((c) => c.id), music: musicId, options: readOptions() }),
  }).then((r) => r.json());
  if (res.error) { $("log").textContent = res.error; $("go").disabled = false; return; }
  poll(res.id);
};

async function poll(id) {
  const job = await fetch(`/api/status/${id}`).then((r) => r.json());
  $("bar").style.width = `${Math.round(job.progress * 100)}%`;
  $("log").innerHTML = job.log.map((l) => `<div>${l.replace(/</g, "&lt;")}</div>`).join("");
  $("log").scrollTop = 1e9;
  if (job.status === "running") return setTimeout(() => poll(id), 1000);
  $("go").disabled = false;
  if (job.status === "done") showResult(id, job.result);
}

function showResult(id, r) {
  const base = `/workspace/jobs/${id}/`;
  const link = (file, label) => (file ? `<a href="${base}${file}" download>${label}</a>` : "");
  $("result").innerHTML = `
    <video controls src="${base}${r.final}"></video>
    <div style="font-size:13px;margin-top:8px">من ${r.input_seconds} ث ← ${r.output_seconds} ث (${r.segments} لقطة)</div>
    <div class="downloads">
      ${link(r.final, "⬇️ تحميل الفيديو النهائي MP4")}
      ${link(r.edl, "🎬 Timeline لـ DaVinci Resolve (EDL)")}
      ${link(r.srt, "💬 ملف الترجمة SRT")}
      ${link(r.resolve_script, "🐍 سكربت فتح المشروع في Resolve")}
    </div>`;
}
