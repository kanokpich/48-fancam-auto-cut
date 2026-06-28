// ── State ──────────────────────────────────────────────────────────────────
const S = {
  movPaths: [],
  wavPath: null,
  outDir: null,
  syncJsonPath: null,
  syncClips: null,
  songsJsonPath: null,
  songs: [],           // {index, start, end, label}
  wavDuration: 0,
  wavPeaks: null,
  watermarkPath: null,
  endscreenPath: null,
};

// ── API helpers ────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch("/api" + path, opts);
  if (!r.ok) throw new Error(`${method} /api${path} → ${r.status}`);
  return r.json();
}

function connectWS(jobId, handlers) {
  const ws = new WebSocket(`ws://127.0.0.1:8000/api/jobs/${jobId}/ws`);
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    (handlers[ev.type] || handlers["*"])?.(ev.payload);
  };
  ws.onerror = () => handlers["failed"]?.("WebSocket error");
  return ws;
}

function waitJob(jobId, handlers) {
  return new Promise((resolve, reject) => {
    connectWS(jobId, {
      ...handlers,
      done: (p) => { handlers.done?.(p); resolve(p); },
      failed: (p) => { handlers.failed?.(p); reject(new Error(p)); },
    });
  });
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmtTime(t) {
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = Math.floor(t % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function parseTime(str) {
  if (!str || !str.trim()) return 0;
  const s = str.trim();
  if (!s.includes(":")) return parseFloat(s) || 0;
  const parts = s.split(":").map(Number);
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] * 3600 + parts[1] * 60 + parts[2];
}

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)}KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)}MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)}GB`;
}

// ── Navigation ─────────────────────────────────────────────────────────────
function showSection(name) {
  document.querySelectorAll(".section").forEach((s) => s.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
  document.getElementById(`s-${name}`)?.classList.add("active");
  document.querySelector(`.nav-btn[data-sec="${name}"]`)?.classList.add("active");
}

// ── Native macOS picker — calls osascript via backend ─────────────────────
const NativePick = (() => {
  function qs(params) {
    return Object.entries(params)
      .filter(([, v]) => v != null && v !== "")
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
      .join("&");
  }

  async function file(prompt, location = "") {
    const r = await api("GET", `/pick/file?${qs({ prompt, location })}`);
    return r.cancelled ? null : r.path;
  }

  async function files(prompt, location = "") {
    const r = await api("GET", `/pick/files?${qs({ prompt, location })}`);
    return r.cancelled ? [] : (r.paths || []);
  }

  async function folder(prompt, location = "") {
    const r = await api("GET", `/pick/folder?${qs({ prompt, location })}`);
    return r.cancelled ? null : r.path;
  }

  return { file, files, folder };
})();

// ── Waveform canvas ─────────────────────────────────────────────────────────
function initWaveform(canvas, regionsLayer, peaks, duration, songs, onSongsChange) {
  const COLORS = [
    "#2d7dd2", "#e76f51", "#06d6a0", "#ffd166",
    "#ef476f", "#118ab2", "#9b5de5", "#f15bb5",
  ];

  let W = 0, H = 0;
  let _songs = songs.map((s) => ({ ...s }));
  let _peaks = peaks;
  let _duration = duration;

  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas.parentElement);

  function resize() {
    const parent = canvas.parentElement;
    W = parent.clientWidth;
    H = parent.clientHeight;
    canvas.width = W;
    canvas.height = H;
    draw();
    drawRegions();
  }

  function draw() {
    if (!_peaks || !W) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, W, H);
    const mid = H / 2;
    const n = _peaks.length;
    const bw = Math.max(1, W / n);
    ctx.fillStyle = "#2d5f8a";
    for (let i = 0; i < n; i++) {
      const x = (i / n) * W;
      const amp = _peaks[i] * mid * 0.92;
      ctx.fillRect(x, mid - amp, bw, amp * 2);
    }
    // center line
    ctx.fillStyle = "#2d5f8a";
    ctx.fillRect(0, mid - 0.5, W, 1);
  }

  function drawRegions() {
    regionsLayer.innerHTML = "";
    _songs.forEach((s, i) => {
      if (!_duration) return;
      const x1 = (s.start / _duration) * W;
      const x2 = (s.end / _duration) * W;
      const color = COLORS[i % COLORS.length];
      const div = document.createElement("div");
      div.className = "song-region";
      div.style.left = x1 + "px";
      div.style.width = Math.max(4, x2 - x1) + "px";
      div.style.backgroundColor = color + "33";
      div.style.borderColor = color;

      const lbl = document.createElement("span");
      lbl.className = "rlabel";
      lbl.textContent = s.label;

      const lh = document.createElement("div");
      lh.className = "rhandle left";
      const rh = document.createElement("div");
      rh.className = "rhandle right";

      div.appendChild(lh);
      div.appendChild(lbl);
      div.appendChild(rh);
      regionsLayer.appendChild(div);

      bindHandle(lh, i, div, "left");
      bindHandle(rh, i, div, "right");
    });
  }

  function bindHandle(handle, idx, regionDiv, side) {
    handle.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      handle.setPointerCapture(e.pointerId);

      const onMove = (me) => {
        const rect = regionsLayer.getBoundingClientRect();
        const x = Math.max(0, Math.min(W, me.clientX - rect.left));
        const t = (x / W) * _duration;
        if (side === "left") {
          _songs[idx].start = Math.max(0, Math.min(t, _songs[idx].end - 1));
        } else {
          _songs[idx].end = Math.max(_songs[idx].start + 1, Math.min(_duration, t));
        }
        // update div CSS without full redraw
        const x1 = (_songs[idx].start / _duration) * W;
        const x2 = (_songs[idx].end / _duration) * W;
        regionDiv.style.left = x1 + "px";
        regionDiv.style.width = Math.max(4, x2 - x1) + "px";
      };

      const onUp = () => {
        handle.releasePointerCapture(e.pointerId);
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        onSongsChange(_songs);
      };

      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
    });
  }

  function setSongs(newSongs) {
    _songs = newSongs.map((s) => ({ ...s }));
    drawRegions();
  }

  // initial draw
  resize();

  return { setSongs };
}

// ── Song table ──────────────────────────────────────────────────────────────
let _wf = null;

function renderSongTable(songs, tbody, onUpdate) {
  tbody.innerHTML = "";
  songs.forEach((s, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td style="color:var(--text-dim)">${s.index}</td>
      <td><input type="text" value="${s.label}" data-field="label" data-i="${i}"></td>
      <td><input type="text" value="${fmtTime(s.start)}" data-field="start" data-i="${i}" style="width:70px"></td>
      <td><input type="text" value="${fmtTime(s.end)}" data-field="end" data-i="${i}" style="width:70px"></td>
      <td style="color:var(--text-dim)">${fmtTime(s.end - s.start)}</td>
      <td><button class="del-song-btn" data-i="${i}" title="ลบ">✕</button></td>
    `;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("change", () => {
      const i = parseInt(inp.dataset.i);
      const field = inp.dataset.field;
      if (field === "start" || field === "end") {
        songs[i][field] = parseTime(inp.value);
        // refresh duration cell
        const cells = tbody.querySelectorAll("tr")[i].querySelectorAll("td");
        cells[4].textContent = fmtTime(songs[i].end - songs[i].start);
      } else {
        songs[i][field] = inp.value;
      }
      onUpdate(songs);
    });
  });

  tbody.querySelectorAll(".del-song-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = parseInt(btn.dataset.i);
      songs.splice(i, 1);
      songs.forEach((s, idx) => { s.index = idx + 1; });
      renderSongTable(songs, tbody, onUpdate);
      onUpdate(songs);
    });
  });
}

// ── Render progress ─────────────────────────────────────────────────────────
function makeProgressSection(container) {
  container.innerHTML = "";
  const rows = {};
  let currentTotal = 0;
  let currentLabel = "";

  function addRow(label) {
    if (rows[label]) return rows[label];
    const div = document.createElement("div");
    div.className = "progress-row";
    div.innerHTML = `
      <span class="progress-label">${label}</span>
      <div class="progress-bar"><div class="progress-fill" style="width:0%"></div></div>
      <span class="progress-status">⏳</span>
    `;
    container.appendChild(div);
    rows[label] = div;
    return div;
  }

  function addPhaseLabel(text) {
    const div = document.createElement("div");
    div.className = "phase-label";
    div.textContent = text;
    container.appendChild(div);
  }

  return {
    onPhase(name) {
      const labels = {
        songs: "● Per-song render",
        full_performance: "● Combining → full_performance",
        full_show: "● Rendering → full_show",
        endscreen_performance: "● Endscreen (performance)",
        endscreen_show: "● Endscreen (show)",
      };
      addPhaseLabel(labels[name] || name);
      currentLabel = "";
      currentTotal = 0;
    },
    onSongBegin(label, total) {
      currentLabel = label;
      currentTotal = total;
      addRow(label);
    },
    onTick(done) {
      if (!currentLabel || !currentTotal) return;
      const pct = Math.min(100, (done / currentTotal) * 100);
      const row = rows[currentLabel];
      if (row) row.querySelector(".progress-fill").style.width = pct + "%";
    },
    onSongDone(entry) {
      const row = rows[entry.label];
      if (!row) return;
      row.querySelector(".progress-fill").style.width = "100%";
      const st = row.querySelector(".progress-status");
      if (entry.status === "ok") {
        st.textContent = "✓"; st.className = "progress-status ok";
      } else {
        st.textContent = "✗"; st.className = "progress-status err";
        st.title = entry.error || "";
      }
    },
    onPhaseBegin(total) {
      currentTotal = total;
      currentLabel = "__phase__";
      const label = container.querySelectorAll(".phase-label");
      const last = label[label.length - 1];
      const ph = last?.textContent || "phase";
      const key = ph + "_combine";
      if (!rows[key]) addRow(key).querySelector(".progress-label").textContent = ph;
      currentLabel = key;
    },
  };
}

// ── Sources section ─────────────────────────────────────────────────────────
function initSources() {
  const volList = document.getElementById("vol-list");
  const selectedList = document.getElementById("selected-movs");
  const outDirInput = document.getElementById("out-dir");
  const wavDisplay = document.getElementById("wav-display");
  const errSpan = document.getElementById("sources-err");

  function renderSelected() {
    selectedList.innerHTML = "";
    S.movPaths.forEach((p) => {
      const chip = document.createElement("div");
      chip.className = "sel-chip";
      const name = p.split("/").pop();
      chip.innerHTML = `<span>${name}</span><button title="ลบ">✕</button>`;
      chip.querySelector("button").addEventListener("click", () => {
        S.movPaths = S.movPaths.filter((x) => x !== p);
        renderSelected();
      });
      selectedList.appendChild(chip);
    });
    if (!S.movPaths.length) {
      selectedList.innerHTML = `<span class="hint">ยังไม่ได้เลือกไฟล์</span>`;
    }
  }

  async function pickMOVs(location = "") {
    const paths = await NativePick.files("เลือกไฟล์วิดีโอ (.mov / .mp4)", location);
    if (!paths.length) return;
    paths.forEach((p) => { if (!S.movPaths.includes(p)) S.movPaths.push(p); });
    renderSelected();
  }

  // volume chips — click to open native picker starting from that drive
  api("GET", "/volumes").then((vols) => {
    volList.innerHTML = "";
    vols.forEach((v) => {
      const chip = document.createElement("div");
      chip.className = "vol-chip";
      chip.textContent = "💾 " + v.name;
      chip.title = v.path;
      chip.addEventListener("click", () => pickMOVs(v.path));
      volList.appendChild(chip);
    });
    // always-present "เพิ่มไฟล์" button
    const addBtn = document.createElement("button");
    addBtn.className = "btn btn-sm";
    addBtn.textContent = "+ เลือกไฟล์วิดีโอ";
    addBtn.addEventListener("click", () => pickMOVs());
    volList.appendChild(addBtn);
  });

  document.getElementById("btn-pick-wav").addEventListener("click", async () => {
    const p = await NativePick.file("เลือกไฟล์ Master Audio (.wav / .aif / .flac)");
    if (p) {
      S.wavPath = p;
      wavDisplay.textContent = p.split("/").pop();
      if (!outDirInput.value) {
        outDirInput.value = p.replace(/\/[^/]+$/, "") + "/output";
        S.outDir = outDirInput.value;
      }
    }
  });

  document.getElementById("btn-pick-out").addEventListener("click", async () => {
    const loc = S.wavPath ? S.wavPath.replace(/\/[^/]+$/, "") : "";
    const p = await NativePick.folder("เลือก Output Folder", loc);
    if (p) { outDirInput.value = p; S.outDir = p; }
  });

  document.getElementById("btn-default-out").addEventListener("click", () => {
    if (S.wavPath) {
      const wavDir = S.wavPath.replace(/\/[^/]+$/, "");
      outDirInput.value = `${wavDir}/output`;
      S.outDir = outDirInput.value;
    }
  });

  outDirInput.addEventListener("input", () => { S.outDir = outDirInput.value.trim(); });

  document.getElementById("btn-go-sync").addEventListener("click", () => {
    errSpan.textContent = "";
    if (!S.movPaths.length) { errSpan.textContent = "เลือก MOV อย่างน้อย 1 ไฟล์"; return; }
    if (!S.wavPath) { errSpan.textContent = "เลือกไฟล์ WAV"; return; }
    if (!outDirInput.value.trim()) { errSpan.textContent = "กำหนด output folder"; return; }
    S.outDir = outDirInput.value.trim();
    refreshSyncView();
    showSection("sync");
  });

  renderSelected();
}

// ── Sync section ────────────────────────────────────────────────────────────
function refreshSyncView() {
  const clipList = document.getElementById("sync-clips");
  clipList.innerHTML = "";
  S.movPaths.forEach((p) => {
    const div = document.createElement("div");
    div.className = "clip-card";
    div.innerHTML = `<div class="clip-name">${p.split("/").pop()}</div>
      <div class="clip-meta">${p}</div>`;
    clipList.appendChild(div);
  });
}

function initSync() {
  const statusEl = document.getElementById("sync-status");

  document.getElementById("btn-run-sync").addEventListener("click", async () => {
    statusEl.innerHTML = '<span class="spinner">⟳</span> กำลัง sync...';
    try {
      const { job_id } = await api("POST", "/sync", {
        movs: S.movPaths,
        wav: S.wavPath,
        out_dir: S.outDir,
      });

      await waitJob(job_id, {
        clip_done(p) {
          const clipCards = document.querySelectorAll(".clip-card");
          const idx = p.n - 1;
          if (clipCards[idx]) {
            clipCards[idx].querySelector(".clip-meta").textContent =
              `offset ${p.offset > 0 ? "+" : ""}${p.offset}s · conf ${p.confidence} · covers ${fmtTime(p.wav_start)}–${fmtTime(p.wav_end)}`;
          }
        },
        done(result) {
          S.syncJsonPath = result.sync_json;
          S.syncClips = result.clips;
          statusEl.innerHTML = `<span style="color:var(--success)">✓ sync.json บันทึกที่ ${result.sync_json}</span>`;
        },
      });
    } catch (e) {
      statusEl.innerHTML = `<span style="color:var(--error)">✗ ${e.message}</span>`;
    }
  });

  document.getElementById("btn-load-sync").addEventListener("click", () => {
    const p = document.getElementById("sync-json-path").value.trim();
    if (p) {
      S.syncJsonPath = p;
      statusEl.innerHTML = `<span style="color:var(--success)">✓ โหลด ${p}</span>`;
    }
  });

  document.getElementById("btn-go-songs").addEventListener("click", () => {
    if (!S.syncJsonPath) {
      alert("ต้องรัน sync หรือโหลด sync.json ก่อน");
      return;
    }
    // pre-fill songs save path
    const songsJsonInput = document.getElementById("songs-save-path");
    if (!songsJsonInput.value) {
      songsJsonInput.value = S.outDir + "/songs.json";
    }
    S.songsJsonPath = songsJsonInput.value;
    showSection("songs");
  });
}

// ── Songs section ───────────────────────────────────────────────────────────
function initSongs() {
  const detectStatus = document.getElementById("detect-status");
  const tbody = document.getElementById("song-tbody");
  const canvas = document.getElementById("waveform-canvas");
  const regionsLayer = document.getElementById("regions-layer");
  const saveStatus = document.getElementById("songs-save-status");
  const savePathInput = document.getElementById("songs-save-path");

  function onSongsUpdate(songs) {
    S.songs = songs;
    if (_wf) _wf.setSongs(songs);
  }

  async function loadWaveform() {
    if (!S.wavPath || !S.wavDuration) {
      detectStatus.textContent = "⟳ กำลังโหลด waveform...";
      try {
        const data = await api("GET", `/waveform?wav=${encodeURIComponent(S.wavPath)}&width=1500`);
        S.wavPeaks = data.peaks;
        S.wavDuration = data.duration;
        detectStatus.textContent = `waveform โหลดแล้ว (${fmtTime(data.duration)})`;
      } catch (e) {
        detectStatus.textContent = `⚠ waveform ล้มเหลว: ${e.message}`;
        return;
      }
    }
    _wf = initWaveform(canvas, regionsLayer, S.wavPeaks, S.wavDuration, S.songs, onSongsUpdate);
    renderSongTable(S.songs, tbody, onSongsUpdate);
  }

  document.getElementById("btn-detect").addEventListener("click", async () => {
    detectStatus.innerHTML = '<span class="spinner">⟳</span> กำลัง detect songs...';
    try {
      const { job_id } = await api("POST", "/detect", {
        wav: S.wavPath,
        out_dir: S.outDir,
      });

      let lastPct = 0;
      await waitJob(job_id, {
        tick(frac) {
          const pct = Math.round(frac * 100);
          if (pct !== lastPct) {
            detectStatus.textContent = `⟳ วิเคราะห์... ${pct}%`;
            lastPct = pct;
          }
        },
        done(result) {
          S.songs = result.songs;
          S.songsJsonPath = result.songs_json;
          savePathInput.value = result.songs_json;
          detectStatus.innerHTML = `<span style="color:var(--success)">✓ พบ ${result.songs.length} เพลง</span>`;
          renderSongTable(S.songs, tbody, onSongsUpdate);
          if (_wf) _wf.setSongs(S.songs);
          else loadWaveform();
        },
      });
    } catch (e) {
      detectStatus.innerHTML = `<span style="color:var(--error)">✗ ${e.message}</span>`;
    }
  });

  document.getElementById("btn-load-songs").addEventListener("click", async () => {
    const p = document.getElementById("songs-json-path").value.trim();
    if (!p) return;
    try {
      const songs = await api("GET", `/songs?songs_json=${encodeURIComponent(p)}`);
      S.songs = songs;
      S.songsJsonPath = p;
      savePathInput.value = p;
      renderSongTable(S.songs, tbody, onSongsUpdate);
      if (_wf) _wf.setSongs(S.songs);
      detectStatus.innerHTML = `<span style="color:var(--success)">✓ โหลด ${songs.length} เพลงจาก ${p}</span>`;
    } catch (e) {
      detectStatus.innerHTML = `<span style="color:var(--error)">✗ ${e.message}</span>`;
    }
  });

  document.getElementById("btn-add-song").addEventListener("click", () => {
    const newSong = {
      index: S.songs.length + 1,
      start: S.songs.length ? S.songs[S.songs.length - 1].end + 10 : 0,
      end: S.songs.length ? S.songs[S.songs.length - 1].end + 70 : 60,
      label: `song${String(S.songs.length + 1).padStart(2, "0")}`,
    };
    S.songs.push(newSong);
    renderSongTable(S.songs, tbody, onSongsUpdate);
    if (_wf) _wf.setSongs(S.songs);
  });

  document.getElementById("btn-save-songs").addEventListener("click", async () => {
    const savePath = savePathInput.value.trim() || S.songsJsonPath || S.outDir + "/songs.json";
    if (!savePath) { alert("กำหนด path สำหรับบันทึก songs.json"); return; }
    try {
      const result = await api("PUT", "/songs", {
        songs_json: savePath,
        songs: S.songs,
      });
      S.songsJsonPath = savePath;
      saveStatus.innerHTML = `<span style="color:var(--success)">✓ บันทึก ${result.saved} เพลง → ${savePath}</span>`;
    } catch (e) {
      saveStatus.innerHTML = `<span style="color:var(--error)">✗ ${e.message}</span>`;
    }
  });

  document.getElementById("btn-go-render").addEventListener("click", () => {
    if (!S.songs.length) { alert("ต้องมีเพลงอย่างน้อย 1 เพลง"); return; }
    if (!S.songsJsonPath) { alert("บันทึก songs.json ก่อน"); return; }
    showSection("render");
  });

  // load waveform whenever this section becomes active
  const observer = new MutationObserver(() => {
    if (document.getElementById("s-songs").classList.contains("active") && S.wavPath && !_wf) {
      loadWaveform();
    }
  });
  observer.observe(document.getElementById("s-songs"), { attributes: true, attributeFilter: ["class"] });
}

// ── Render section ──────────────────────────────────────────────────────────
function initRender() {
  const statusText = document.getElementById("render-status-text");
  const progressContainer = document.getElementById("render-progress");
  const resultsCard = document.getElementById("render-results");
  const outputList = document.getElementById("render-output-list");

  // watermark + endscreen pickers — native macOS dialog
  document.getElementById("btn-pick-wm").addEventListener("click", async () => {
    const p = await NativePick.file("เลือก Watermark (.png)");
    if (p) {
      S.watermarkPath = p;
      document.getElementById("wm-display").textContent = p.split("/").pop();
    }
  });
  document.getElementById("btn-no-wm").addEventListener("click", () => {
    S.watermarkPath = null;
    document.getElementById("wm-display").textContent = "(ไม่ใส่)";
  });
  document.getElementById("btn-pick-es").addEventListener("click", async () => {
    const p = await NativePick.file("เลือก Endscreen (วิดีโอหรือรูป)");
    if (p) {
      S.endscreenPath = p;
      document.getElementById("es-display").textContent = p.split("/").pop();
    }
  });
  document.getElementById("btn-no-es").addEventListener("click", () => {
    S.endscreenPath = null;
    document.getElementById("es-display").textContent = "(ไม่ใส่)";
  });

  document.getElementById("btn-start-render").addEventListener("click", async () => {
    if (!S.syncJsonPath) { alert("ต้องมี sync.json ก่อน"); return; }
    if (!S.songsJsonPath) { alert("ต้องมี songs.json ก่อน"); return; }

    progressContainer.innerHTML = "";
    resultsCard.classList.add("hidden");
    statusText.textContent = "⟳ กำลัง render...";
    document.getElementById("btn-start-render").disabled = true;

    const prog = makeProgressSection(progressContainer);

    try {
      const body = {
        sync_json: S.syncJsonPath,
        songs_json: S.songsJsonPath,
        out_dir: S.outDir,
        prefix: document.getElementById("r-prefix").value,
        mode: document.getElementById("r-mode").value,
        encoder: document.getElementById("r-encoder").value,
        fade: parseFloat(document.getElementById("r-fade").value) || 1.0,
        no_fade: document.getElementById("r-no-fade").checked,
        fade_color: document.getElementById("r-fade-color").value || "black",
        watermark: S.watermarkPath || null,
        endscreen: S.endscreenPath || null,
        endscreen_duration: parseFloat(document.getElementById("es-dur").value) || 10,
        full_start: document.getElementById("r-full-start").value.trim() || null,
        full_end: document.getElementById("r-full-end").value.trim() || null,
      };

      const { job_id } = await api("POST", "/render", body);

      await waitJob(job_id, {
        phase: (name) => prog.onPhase(name),
        song_begin: ({ label, total }) => prog.onSongBegin(label, total),
        tick: (d) => prog.onTick(d),
        song_done: (entry) => prog.onSongDone(entry),
        phase_begin: (t) => prog.onPhaseBegin(t),
        done(result) {
          statusText.innerHTML = `<span style="color:var(--success)">✓ เสร็จแล้ว!</span>`;
          renderOutputList(result.manifest, result.out_dir);
        },
      });
    } catch (e) {
      statusText.innerHTML = `<span style="color:var(--error)">✗ ${e.message}</span>`;
    } finally {
      document.getElementById("btn-start-render").disabled = false;
    }
  });

  function renderOutputList(manifest, outDir) {
    resultsCard.classList.remove("hidden");
    outputList.innerHTML = "";
    manifest.forEach((entry) => {
      const div = document.createElement("div");
      div.className = "output-item";
      if (entry.status === "ok") {
        const filename = entry.output.split("/").pop();
        div.innerHTML = `
          <span class="output-label output-ok">✓ ${entry.label}</span>
          <a href="/api/file?path=${encodeURIComponent(entry.output)}" target="_blank"
             class="btn btn-sm">▶ Preview</a>
          <span class="hint">${filename}</span>
        `;
      } else {
        div.innerHTML = `<span class="output-label output-err">✗ ${entry.label}: ${entry.error || "failed"}</span>`;
      }
      outputList.appendChild(div);
    });

    // also link full outputs if they exist
    const fullNames = [`${S.outDir}/full_performance.mp4`, `${S.outDir}/full_show.mp4`];
    fullNames.forEach((p) => {
      const name = p.split("/").pop();
      const div = document.createElement("div");
      div.className = "output-item";
      div.innerHTML = `
        <span class="output-label output-ok">● ${name}</span>
        <a href="/api/file?path=${encodeURIComponent(p)}" target="_blank" class="btn btn-sm">▶ Preview</a>
      `;
      outputList.appendChild(div);
    });
  }
}

// ── Bootstrap ───────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => showSection(btn.dataset.sec));
  });

  initSources();
  initSync();
  initSongs();
  initRender();
});
