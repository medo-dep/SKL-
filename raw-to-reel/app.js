const $ = (id) => document.getElementById(id);
const clips = []; // { id, name, el, uploading }
const assets = { music: null, logo: null, broll: [] };

const STORE = "raw-to-reel-options";
const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
if (!("auto_reframe" in saved)) delete saved.caption_position; // settings saved before smart placement existed

document.querySelectorAll("[data-opt]").forEach((el) => {
  const key = el.dataset.opt;
  if (key in saved) {
    if (el.type === "checkbox") el.checked = saved[key];
    else if (el.type === "radio") el.checked = el.value === saved[key];
    else el.value = saved[key];
  }
  el.addEventListener("change", () => { syncSubs(); saveOptions(); });
});

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
}
syncSubs();

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

$("go").onclick = async () => {
  $("go").disabled = true;
  $("result").innerHTML = "";
  $("log").textContent = "";
  const res = await fetch("/api/edit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ videos: clips.filter((c) => c.id).map((c) => c.id), ...assets, options: readOptions() }),
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
