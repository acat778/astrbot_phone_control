/* 手机操控插件 · 监控与资料管理 */
(() => {
  "use strict";

  const POLL_OVERVIEW_MS = 2000;
  const POLL_SERIES_MS = 2000;
  const SERIES_MINUTES = 30;

  const state = {
    bridge: null,
    view: "dashboard",
    recordTab: "diary",
    overview: null,
    series: { screenshots: [], llm: [], buckets: [] },
    timers: [],
    vectorMode: "list",
    graphView: null,
    vectorsOffset: 0,
    vectorsRequest: 0,
    imagesOffset: 0,
    imagesRequest: 0,
    diaryOffset: 0,
    diaryRequest: 0,
    tasksOffset: 0,
    tasksRequest: 0,
  };

  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function fmtNum(value, digits = 0) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const num = Number(value);
    if (digits > 0) return num.toFixed(digits);
    if (Math.abs(num) >= 10000) return `${(num / 1000).toFixed(1)}k`;
    return String(Math.round(num));
  }

  function fmtTime(ts, withSeconds = true) {
    if (!ts) return "—";
    const ms = Number(ts) > 1e12 ? Number(ts) : Number(ts) * 1000;
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return "—";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return withSeconds ? `${hh}:${mm}:${ss}` : `${hh}:${mm}`;
  }

  function fmtDateTime(ts) {
    if (!ts) return "—";
    const ms = Number(ts) > 1e12 ? Number(ts) : Number(ts) * 1000;
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return "—";
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
  }

  function ageLabel(ts) {
    if (!ts) return "—";
    const ms = Number(ts) > 1e12 ? Number(ts) : Number(ts) * 1000;
    const age = Math.max(0, Date.now() - ms);
    if (age < 1000) return `${Math.round(age)}ms 前`;
    if (age < 60000) return `${(age / 1000).toFixed(1)}s 前`;
    return `${Math.round(age / 60000)}min 前`;
  }

  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;
  }

  function setClass(el, names) {
    if (!el) return;
    el.classList.remove("ok", "warn", "error");
    if (names) el.classList.add(names);
  }

  async function getBridge() {
    for (let i = 0; i < 120; i += 1) {
      if (window.AstrBotPluginView) return window.AstrBotPluginView;
      await sleep(50);
    }
    throw new Error("AstrBot 插件页面 bridge 未就绪");
  }

  async function apiGet(endpoint, params) {
    return state.bridge.apiGet(endpoint, params);
  }

  // ---------------- 视图初始化 ----------------
  function switchView(view) {
    stopPolls();
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === view);
    });
    $("view-dashboard").hidden = view !== "dashboard";
    $("view-relay").hidden = view !== "relay";
    $("view-records").hidden = view !== "records";
    $("view-vectors").hidden = view !== "vectors";
    $("view-images").hidden = view !== "images";
    $("view-mesh").hidden = view !== "mesh";
    $("view-tasks").hidden = view !== "tasks";
    if (view === "dashboard") {
      schedulePolls();
      refreshOverview();
      refreshSeries();
    } else {
      if (view === "relay") {
        loadAppConnection();
        state.timers.push(setInterval(() => { if (!document.hidden) loadAppConnection(); }, 5000));
      }
      if (view === "records") switchRecordTab(state.recordTab);
      if (view === "vectors") loadVectors();
      if (view === "images") loadImages();
      if (view === "mesh") loadMesh();
      if (view === "tasks") {
        loadTasks();
      }
    }
  }

  function switchRecordTab(tab) {
    state.recordTab = tab;
    document.querySelectorAll("[data-record-tab]").forEach(button => {
      const active = button.dataset.recordTab === tab;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    $("view-diary").hidden = tab !== "diary";
    $("view-learning").hidden = tab !== "learning";
    if (tab === "diary") loadDiary(); else loadLearning();
  }

  function schedulePolls() {
    stopPolls();
    state.timers.push(setInterval(refreshOverview, POLL_OVERVIEW_MS));
    state.timers.push(setInterval(refreshSeries, POLL_SERIES_MS));
  }

  function stopPolls() {
    state.timers.forEach((timer) => clearInterval(timer));
    state.timers = [];
  }

  function renderAppConnection(value) {
    const peer = value.peer || {};
    const connected = value.enabled && peer.connected;
    const badge = $("app-state");
    badge.className = `relay-state${connected ? " connected" : ""}`;
    badge.textContent = connected ? "已连接" : value.enabled ? "等待连接" : "直连模式";
    setText("app-connection-title", connected ? "手机已就绪" : value.enabled ? "等待手机连接" : "开启 App 控制");
    setText("app-connection-description", value.enabled
      ? connected ? "连接正常，可以从插件控制这台手机。" : "在手机 App 中填写配对码并启动控制。"
      : value.message || "在插件的 ADB 连接中选择「使用 App 控制」，保存并重载。" );
    setText("app-endpoint", value.port ? `TCP ${value.port}` : "—");
    setText("app-version", peer.version ? `v${peer.version}` : "—");
    const route = value.route || {};
    const capability = value.device_capabilities || {};
    setText("app-route-status", `${route.source || "external"} · ${route.message || "等待选择插件地址"}`);
    setText("app-endpoint", route.host ? `${route.host}:${value.port}` : value.port ? `TCP ${value.port}` : "—");
    setText("app-current-target", `${capability.relay_host ? `${connected ? "App → 插件" : "上次App → 插件"}：${capability.relay_host}:${capability.relay_port}；` : "插件目标等待App报告；"}${capability.endpoint_host ? `本机ADB：${capability.endpoint_host}:${capability.endpoint_port}` : "本机ADB目标等待App报告"}`);
    setText("app-adb-status", capability.adb_uid === 0 ? "本机ADB：Root（UID 0）" : Number.isInteger(capability.adb_uid) ? `本机ADB：UID ${capability.adb_uid}${capability.su_present ? "，检测到su，使用时仍需授权" : "；系统CA需要Root或su授权"}` : connected ? "本机ADB已连接，权限详情等待新版App报告。" : "本机ADB尚未报告，请在手机端确认授权与连接端口。");
    setText("app-capability-status", Number.isInteger(capability.sdk_int) ? `Android API ${capability.sdk_int} · 播放音频${capability.playback_audio ? "支持" : "不可用"}；${capability.frame_options ? "画质参数可用" : "升级App启用画质参数"}；逐帧确认${capability.frame_ack ? "支持" : "需升级App"}；文件流控${capability.stream_credit ? "支持" : "需升级App"}。HTTPS需目标App信任对应CA。` : "音频采集要求Android 11及以上；设备能力等待新版App报告。");
    $("app-pair").disabled = !value.enabled || (value.route && !route.host);
    $("app-reset").disabled = !value.enabled;
  }

  async function loadAppConnection() {
    if (state.view !== "relay") return;
    try {
      const value = await apiGet("dashboard/app-connection", { host: $("app-host").value.trim() });
      if (state.view === "relay") renderAppConnection(value);
    } catch (error) { setText("app-status", `状态查询失败：${error.message || error}`); }
  }

  // ---------------- 概览 ----------------
  async function refreshOverview() {
    if (document.hidden) return;
    try {
      const data = await apiGet("dashboard/overview");
      if (!data) return;
      state.overview = data;
      renderOverview(data);
    } catch (err) {
      setText("page-subtitle", `概览加载失败：${err.message || err}`);
    }
  }

  function renderOverview(data) {
    const backends = data.backends || {};
    const control = backends.control || "—";
    const controlConfig = backends.control_config || "";
    const scrcpyRunning = !!backends.scrcpy_running;
    const appMode = backends.connection_mode === "app";
    // 页面打开后后端会后台懒启动 scrcpy：启动前显示配置值（auto），避免误以为默认只用 ADB。
    setText("ov-control", appMode ? "Relay App" : scrcpyRunning ? "scrcpy" : controlConfig === "adb" ? "adb" : controlConfig || control);
    setClass($("ov-control"), control === "scrcpy" ? "ok" : "");
    setText(
      "ov-control-note",
      appMode ? "手机端本机 ADB 控制"
        : scrcpyRunning
        ? "scrcpy 已连接"
        : controlConfig === "adb"
          ? "仅 ADB"
          : "scrcpy 懒启动 · 失败回退 ADB",
    );

    const perception = backends.perception || "—";
    setText("ov-perception", appMode ? (perception === "app_scrcpy" ? "App scrcpy" : "App 截图") : perception);
    let perceptionNote;
    if (appMode) {
      perceptionNote = perception === "app_scrcpy" ? "手机端 scrcpy 视频取帧" : "手机端按需 screencap";
    } else if (backends.scrcpy_video === true) {
      perceptionNote = "scrcpy 视频帧可用";
    } else if (perception === "adb") {
      perceptionNote = scrcpyRunning ? "ADB 截图（scrcpy 仅控制通道）" : "ADB 截图来源";
    } else if (scrcpyRunning) {
      perceptionNote = backends.reason || "视频帧不可用，回退 ADB";
    } else {
      perceptionNote = "未启动";
    }
    setText("ov-perception-note", perceptionNote);

    const quota = data.quota || {};
    if (quota.enabled === false) {
      setText("ov-quota", "未启用");
      setText("ov-quota-note", "quota.enabled=false");
    } else {
      const used = Number(quota.used || 0);
      const max = Number(quota.max_tokens || 0);
      const pct = max > 0 ? Math.min(100, Math.round((used / max) * 100)) : 0;
      setText("ov-quota", `${fmtNum(used / 1e6, 3)} M`);
      setClass($("ov-quota"), pct >= 90 ? "error" : pct >= 70 ? "warn" : "ok");
      setText(
        "ov-quota-note",
        `上限 ${max > 0 ? fmtNum(max / 1e6, 3) + " M · " + pct + "%" : "不限"} · ${quota.window_hours || 24}h 窗口`,
      );
    }

    const shot = data.last_screenshot;
    if (shot) {
      setText("ov-shot", `${fmtNum(shot.ms, 1)} ms`);
      setText(
        "ov-shot-note",
        `${shot.source || "?"}${shot.frame_age_ms != null ? " · 画面年龄 " + fmtNum(shot.frame_age_ms, 1) + " ms" : ""} · ${fmtTime(shot.ts)} · ${ageLabel(shot.ts)}`,
      );
    } else {
      setText("ov-shot", "—");
      setText("ov-shot-note", "暂无截图");
    }
    if (shot) updateShotLatest(shot);

    const llm = data.last_llm;
    if (llm) {
      setText("ov-llm", `${fmtNum(llm.tps, 1)} tok/s`);
      setText(
        "ov-llm-note",
        `${fmtNum(llm.tokens)} tok / ${fmtNum(llm.ms, 0)}ms · ${ageLabel(llm.ts)}`,
      );
      updateTpsLatest(llm);
    } else {
      setText("ov-llm", "—");
      setText("ov-llm-note", "暂无 API 调用");
    }

    const traffic = data.traffic || {};
    if (traffic.running) {
      setText("ov-traffic", `${fmtNum(traffic.flows || 0)} 条`);
      setText(
        "ov-traffic-note",
        `${traffic.provider === "app" ? "App 抓包" : "mitmproxy"} · ${traffic.last_flow_at || traffic.flows ? "已收到流量" : "等待新请求"}${traffic.failures ? ` · 转发失败 ${traffic.failures} 次` : ""}${traffic.dropped ? ` · 队列丢弃 ${traffic.dropped} 条` : ""}`,
      );
      setClass($("ov-traffic"), "ok");
    } else {
      setText("ov-traffic", traffic.enabled ? "未运行" : "未启用");
      setText(
        "ov-traffic-note",
        traffic.enabled
          ? traffic.error || "traffic.enabled=true，等待启动"
          : "traffic.enabled=false",
      );
      setClass($("ov-traffic"), "warn");
    }
    const previewEl = $("ov-traffic-preview");
    if (previewEl) {
      previewEl.textContent =
        traffic.flows ? traffic.preview : traffic.error || (traffic.running ? "代理已启动，尚未收到请求。打开目标 App 产生新请求；HTTPS 需信任当前抓包 CA。" : "抓包尚未运行。");
      if (traffic.ca_help) previewEl.textContent += "\n\n" + traffic.ca_help;
    }

    const today = data.today || {};
    setText(
      "ov-interactions",
      `${today.comments || 0} / ${today.passive_replies || 0} / ${today.posts || 0}`,
    );
    setText("ov-interactions-note", "今日主动评论 / 被动回复 / 发帖");

    setText(
      "page-subtitle",
      `插件 v${data.version || "?"} · 运行 ${formatUptime(data.uptime_s)}`,
    );
  }

  function formatUptime(seconds) {
    const s = Number(seconds || 0);
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}min`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  // ---------------- 曲线 ----------------
  async function refreshSeries() {
    if (document.hidden || state.view !== "dashboard") return;
    try {
      const data = await apiGet("dashboard/series", { minutes: SERIES_MINUTES });
      if (!data) return;
      state.series = {
        screenshots: Array.isArray(data.screenshots) ? data.screenshots : [],
        llm: Array.isArray(data.llm) ? data.llm : [],
        buckets: Array.isArray(data.buckets) ? data.buckets : [],
      };
      renderCharts();
      if (data.last_screenshot) updateShotLatest(data.last_screenshot);

    } catch (err) {
      setText("shot-foot", `曲线加载失败：${err.message || err}`);
    }
  }

  function updateShotLatest(shot) {
    setText("shot-latest", `${fmtNum(shot.ms, 1)} ms`);
    setText(
      "shot-foot",
      `最近 30 分钟 · 单位 ms · ${shot.source || "?"} ${fmtTime(shot.ts, false)}`,
    );
  }

  function updateTpsLatest(llm) {
    const bucket = state.series.buckets.at(-1);
    setText("tps-latest", `${fmtNum(bucket?.tokens || 0)} tokens/分钟`);
  }

  function renderCharts() {
    const now = Date.now() / 1000;
    const from = now - SERIES_MINUTES * 60;

    const shotPoints = state.series.screenshots
      .map((item) => ({ ts: Number(item.ts), value: Number(item.ms) }))
      .filter((item) => Number.isFinite(item.ts) && Number.isFinite(item.value));
    drawChart($("chart-shot"), shotPoints, {
      color: "#3b82f6",
      from: Math.min(from, shotPoints.length ? shotPoints[0].ts : from),
      to: now,
      unit: "ms",
    });

    const bucketPoints = state.series.buckets
      .map((item) => ({ ts: Number(item.ts), value: Number(item.tokens) }))
      .filter((item) => Number.isFinite(item.ts) && Number.isFinite(item.value));
    drawChart($("chart-tps"), bucketPoints, {
      color: "#8b5cf6", from, to: now, unit: "tokens/分钟",
    });
    updateTpsLatest();
  }

  function niceMax(value) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(value)));
    const normalized = value / pow;
    const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return step * pow;
  }

  function drawChart(canvas, points, opts) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(220, Math.floor(rect.width || canvas.parentElement.clientWidth || 300));
    const height = Math.max(140, Math.floor(rect.height || 180));
    if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const pad = { left: 42, right: 10, top: 12, bottom: 22 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const styles = getComputedStyle(document.documentElement);
    const textColor = styles.getPropertyValue("--muted").trim() || "#6b7280";
    const borderColor = styles.getPropertyValue("--border").trim() || "#e5e7eb";

    const from = opts.from || opts.to - SERIES_MINUTES * 60;
    const to = opts.to || Date.now() / 1000;
    const xOf = (ts) => pad.left + ((ts - from) / Math.max(1, to - from)) * plotW;

    let maxVal = 1;
    points.forEach((p) => {
      maxVal = Math.max(maxVal, p.value);
    });
    maxVal = niceMax(maxVal * 1.15);
    const yOf = (v) => pad.top + plotH - (v / maxVal) * plotH;

    // 网格 + 纵轴
    ctx.strokeStyle = borderColor;
    ctx.globalAlpha = 0.7;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const v = (maxVal / 4) * i;
      const y = yOf(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = textColor;
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(fmtNum(v, v < 10 ? 1 : 0), pad.left - 6, y);
    }
    // 横轴时间
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let i = 0; i <= 3; i += 1) {
      const ts = from + ((to - from) / 3) * i;
      ctx.fillText(fmtTime(ts, false), xOf(ts), height - pad.bottom + 6);
    }

    if (!points.length) {
      ctx.fillStyle = textColor;
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("暂无数据", width / 2, height / 2);
      return;
    }

    const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
    gradient.addColorStop(0, `${opts.color}55`);
    gradient.addColorStop(1, `${opts.color}05`);
    ctx.beginPath();
    points.forEach((p, idx) => {
      const x = xOf(p.ts);
      const y = yOf(p.value);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xOf(points[points.length - 1].ts), pad.top + plotH);
    ctx.lineTo(xOf(points[0].ts), pad.top + plotH);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    points.forEach((p, idx) => {
      const x = xOf(p.ts);
      const y = yOf(p.value);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = opts.color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.stroke();

    const last = points[points.length - 1];
    ctx.beginPath();
    ctx.arc(xOf(last.ts), yOf(last.value), 3, 0, Math.PI * 2);
    ctx.fillStyle = opts.color;
    ctx.fill();
  }

  function redrawCharts() {
    if (state.view === "dashboard") renderCharts();
  }

  // ---------------- 浏览日志 ----------------
  async function loadDiary() {
    if (document.hidden) return;
    const request = ++state.diaryRequest;
    const query = ($("diary-query")?.value || "").trim();
    const limit = Number($("diary-limit")?.value || 20);
    try {
      const data = await apiGet("dashboard/diary", {
        query,
        limit,
        offset: state.diaryOffset,
      });
      if (request !== state.diaryRequest) return;
      if (!data.entries.length && data.total && state.diaryOffset) {
        state.diaryOffset = Math.floor((data.total - 1) / limit) * limit;
        return loadDiary();
      }
      renderDiary(data || {});
    } catch (err) {
      if (request !== state.diaryRequest) return;
      setText("diary-subtitle", `浏览日志加载失败：${err.message || err}`);
    }
  }

  function renderDiary(data) {
    const stats = data.stats || {};
    const entries = data.entries || [];
    const total = Number(data.total || 0);
    const pageTotal = Number(stats.entries || 0);
    const maxEntries = Number(stats.max_entries || 0);
    const keepRounds = Number(stats.keep_rounds || 0);
    const last = Number(stats.last_ts || 0);
    const query = String(data.query || "").trim();
    const offset = Number(data.offset || 0);
    const limit = Number(data.limit || 20);
    const apps = Object.entries(stats.apps || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([name, count]) => `${name} ${count}`)
      .join(" · ");
    setText(
      "diary-subtitle",
      pageTotal
        ? `共 ${pageTotal} 条${maxEntries ? `（上限 ${maxEntries}）` : ""}${
            keepRounds ? `；保护最近 ${keepRounds} 轮任务` : ""
          }；最近记录 ${fmtDateTime(last)}${query ? `；搜索「${query}」` : ""}`
        : "浏览日志还是空的：浏览或自主任务会自动记录。"
    );
    const statBox = $("diary-stats");
    if (statBox) {
      statBox.innerHTML = "";
      const chips = [
        `总数 ${pageTotal}`,
        maxEntries ? `上限 ${maxEntries}` : "",
        keepRounds ? `保护 ${keepRounds} 轮` : "",
        last ? `最近 ${fmtDateTime(last)}` : "",
        apps ? `App ${apps}` : "",
      ].filter(Boolean);
      chips.forEach((text) => {
        const chip = document.createElement("span");
        chip.className = "learning-chip";
        chip.textContent = text;
        statBox.appendChild(chip);
      });
    }
    const list = $("diary-list");
    if (!list) return;
    list.innerHTML = "";
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "learning-empty";
      empty.textContent = query
        ? "没有匹配的浏览日志，换个关键词试试。"
        : "还没有浏览日志。";
      list.appendChild(empty);
    } else {
      entries.forEach((entry) => {
        const item = document.createElement("article");
        item.className = "learning-item";

        const head = document.createElement("div");
        head.className = "learning-item-head";
        const time = document.createElement("span");
        time.className = "learning-time";
        time.textContent = fmtDateTime(entry.ts);
        head.appendChild(time);
        const app = document.createElement("span");
        app.className = "learning-source";
        app.textContent = entry.app || "未知 App";
        head.appendChild(app);
        head.append(button("删除", async () => {
          if (!await confirmAction("删除这条浏览日志及其关联检索索引？截图文件保留。")) return;
          await state.bridge.apiPost("dashboard/diary/delete", { id: entry.id });
          await loadDiary();
        }));
        item.appendChild(head);

        const items = entry.items || [];
        items.slice(0, 5).forEach((feedItem) => {
          const line = document.createElement("p");
          line.className = "learning-content";
          const title = String(feedItem.title || feedItem.text || "（无文本）").trim();
          const author = String(feedItem.author || "").trim();
          line.textContent = author ? `${title} —— ${author}` : title;
          item.appendChild(line);
        });
        if (entry.note) {
          const note = document.createElement("p");
          note.className = "learning-content";
          note.textContent = `备注：${entry.note}`;
          item.appendChild(note);
        }
        list.appendChild(item);
      });
    }
    const pageNo = Math.floor(offset / Math.max(1, limit)) + 1;
    const pageMax = Math.max(1, Math.ceil(total / Math.max(1, limit)));
    setText("diary-page", `第 ${pageNo} / ${pageMax} 页`);
    const prev = $("diary-prev");
    const next = $("diary-next");
    if (prev) prev.disabled = offset <= 0;
    if (next) next.disabled = offset + limit >= total;
  }

  async function loadVectors() {
    const requestId = ++state.vectorsRequest;
    const kind = $("vectors-kind").value;
    try {
      const graph = state.vectorMode === "graph";
      const data = await apiGet(graph ? "dashboard/vectors/graph" : "dashboard/vectors", {
        kind, query: $("vectors-query").value, source: $("vectors-source").value,
        offset: state.vectorsOffset, limit: graph ? Number($("graph-limit").value) : 20,
        min_score: Number($("graph-threshold").value),
      });
      if (requestId !== state.vectorsRequest) return;
      if (!graph && !data.entries.length && data.total && state.vectorsOffset) {
        state.vectorsOffset = Math.floor((data.total - 1) / data.limit) * data.limit;
        return loadVectors();
      }
      const manifest = data.manifest || {};
      const states = { ready: "索引可用", missing: "尚未建立索引", invalid: "索引文件不完整，请检查后台构建日志" };
      setText("vectors-subtitle", `${data.enabled ? "功能已开启" : "功能已关闭"} · ${states[data.state] || data.state}`);
      setText("vectors-stats", data.state === "ready"
        ? `${manifest.count} 条 · ${manifest.dim} 维 · ${manifest.provider_id} / ${manifest.model} · 更新 ${fmtDateTime(manifest.updated_at)}` : "暂无可读取的向量");
      if (graph) { renderMemoryGraph(data, kind); return; }
      const list = $("vectors-list"); list.replaceChildren();
      for (const entry of data.entries || []) {
        const item = document.createElement("article"); item.className = "learning-item";
        const title = document.createElement("div"); title.className = "learning-source";
        title.textContent = `${entry.source} · ${entry.id} · ${fmtDateTime(entry.ts)}`;
        const text = document.createElement("p"); text.className = "learning-content"; text.textContent = entry.text;
        const details = document.createElement("details");
        const summary = document.createElement("summary"); summary.textContent = `查看 ${entry.dim} 维向量 · 范数 ${Number(entry.norm).toFixed(4)}`;
        const values = document.createElement("pre"); values.className = "vector-values";
        values.textContent = JSON.stringify(entry.vector, null, 2);
        const meta = document.createElement("p"); meta.className = "learning-content"; meta.textContent = JSON.stringify(entry.meta);
        const remove = button("删除索引", async () => {
          await deleteVector(kind, entry.id);
        });
        details.append(summary, values); item.append(title, text, meta, details, remove); list.append(item);
      }
      if (!list.children.length) { const empty = document.createElement("p"); empty.className = "learning-empty"; empty.textContent = data.state === "ready" ? "没有符合条件的向量记录" : "开启相应学习功能并记录内容后，后台会建立索引"; list.append(empty); }
      $("vectors-prev").disabled = data.offset <= 0;
      $("vectors-next").disabled = data.offset + data.limit >= data.total;
      setText("vectors-page", `共 ${data.total} 条 · 第 ${Math.floor(data.offset / data.limit) + 1} 页`);
    } catch (err) {
      if (requestId !== state.vectorsRequest) return;
      setText("vectors-subtitle", `向量加载失败：${err.message || err}`);
      $("vectors-list").replaceChildren(); setText("vectors-stats", "");
      state.graphView?.destroy(); state.graphView = null;
      $("memory-graph").replaceChildren(); setText("graph-status", "图谱加载失败");
      $("graph-details").replaceChildren();
      $("vectors-prev").disabled = true; $("vectors-next").disabled = true;
    }
  }

  async function deleteVector(kind, id) {
    if (!await confirmAction("删除这条记忆索引？原始经验、学习记录或浏览日志保留；后台重建会保留删除结果。", "确认删除索引")) return;
    await state.bridge.apiPost("dashboard/vectors/delete", { kind, id });
    await loadVectors();
  }

  function switchVectorMode(mode) {
    state.vectorMode = mode;
    document.querySelectorAll("[data-vector-mode]").forEach(tab => {
      const active = tab.dataset.vectorMode === mode;
      tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1;
    });
    $("vectors-table").hidden = mode !== "list";
    $("vectors-graph-panel").hidden = mode !== "graph";
    loadVectors();
  }

  function renderMemoryGraph(data, kind) {
    state.graphView?.destroy(); state.graphView = null;
    $("graph-details").replaceChildren();
    setText("graph-status", `显示 ${data.nodes.length} / ${data.total} 条记录 · ${data.edges.length} 条相似连线${data.truncated ? " · 按更新时间选取最新记录" : ""}`);
    if (!data.nodes.length) { $("memory-graph").replaceChildren(); setText("graph-details", "暂无符合条件的索引记录。"); return; }
    state.graphView = window.PhoneMemoryGraph.render($("memory-graph"), data, node => {
      const details = $("graph-details"); details.replaceChildren();
      const title = document.createElement("h2"); title.textContent = node.label;
      const text = document.createElement("p"); text.textContent = node.text;
      const info = document.createElement("p"); info.textContent = `${node.source} · ${fmtDateTime(node.ts)}`;
      const links = document.createElement("ul");
      for (const edge of data.edges.filter(e => e.source === node.id || e.target === node.id)) {
        const other = data.nodes.find(n => n.id === (edge.source === node.id ? edge.target : edge.source));
        const line = document.createElement("li"); line.textContent = `${other?.label || "记录"} · 相似度 ${Number(edge.score).toFixed(3)}`; links.append(line);
      }
      details.append(title, info, text, links, button("删除索引", () => deleteVector(kind, node.id)));
    });
  }

  // ---------------- 学习记录 ----------------
  async function loadLearning() {
    if (document.hidden) return;
    const query = ($("learning-query")?.value || "").trim();
    const limit = Number($("learning-limit")?.value || 50);
    try {
      const data = await apiGet("dashboard/learning", { query, limit });
      renderLearning(data || {});
    } catch (err) {
      setText(
        "learning-subtitle",
        `学习记录加载失败：${err.message || err}`
      );
    }
  }

  function renderLearning(data) {
    const stats = data.stats || {};
    const entries = data.entries || [];
    const total = Number(stats.entries || 0);
    const maxEntries = Number(stats.max_entries || 0);
    const last = Number(stats.last_ts || 0);
    const sources = Object.entries(stats.sources || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([name, count]) => `${name} ${count}`)
      .join(" · ");
    const query = String(data.query || "").trim();
    setText(
      "learning-subtitle",
      total
        ? `共 ${total} 条${maxEntries ? `（上限 ${maxEntries}）` : ""}；最近记录 ${fmtDateTime(last)}${query ? `；搜索「${query}」` : ""}`
        : "学习档案还是空的：持续刷手机/自主浏览时 AI 会用 phone_learn 记录理解到的内容。"
    );
    const statBox = $("learning-stats");
    if (statBox) {
      statBox.innerHTML = "";
      const chips = [
        `总数 ${total}`,
        maxEntries ? `上限 ${maxEntries}` : "",
        last ? `最近 ${fmtDateTime(last)}` : "",
        sources ? `来源 ${sources}` : "",
      ].filter(Boolean);
      chips.forEach((text) => {
        const chip = document.createElement("span");
        chip.className = "learning-chip";
        chip.textContent = text;
        statBox.appendChild(chip);
      });
    }
    const list = $("learning-list");
    if (!list) return;
    list.innerHTML = "";
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "learning-empty";
      empty.textContent = query
        ? "没有匹配的学习记录，换个关键词试试。"
        : "还没有学习记录。";
      list.appendChild(empty);
      return;
    }
    entries.forEach((entry) => {
      const item = document.createElement("article");
      item.className = "learning-item";

      const head = document.createElement("div");
      head.className = "learning-item-head";
      const time = document.createElement("span");
      time.className = "learning-time";
      time.textContent = fmtDateTime(entry.ts);
      head.appendChild(time);
      const source = document.createElement("span");
      source.className = "learning-source";
      source.textContent = entry.source || "未标注来源";
      head.appendChild(source);
      item.appendChild(head);

      const content = document.createElement("p");
      content.className = "learning-content";
      content.textContent = entry.content || "";
      item.appendChild(content);

      if (entry.attribution_label) {
        const attr = document.createElement("div");
        attr.className = "learning-tags";
        const attrTag = document.createElement("span");
        attrTag.textContent = `归属：${entry.attribution_label}`;
        attr.appendChild(attrTag);
        item.appendChild(attr);
      }
      const tags = entry.tags || [];
      if (tags.length) {
        const tagBox = document.createElement("div");
        tagBox.className = "learning-tags";
        tags.forEach((tag) => {
          const tagEl = document.createElement("span");
          tagEl.textContent = `#${tag}`;
          tagBox.appendChild(tagEl);
        });
        item.appendChild(tagBox);
      }
      const remove = button("删除记录", async () => {
        if (!await confirmAction("删除这条本地学习记录？已同步到记忆插件的副本由记忆插件管理。")) return;
        await state.bridge.apiPost("dashboard/learning/delete", { id: entry.id });
        await loadLearning();
      });
      item.appendChild(remove);
      list.appendChild(item);
    });
  }

  function button(label, action) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "btn ghost";
    el.textContent = label;
    el.addEventListener("click", async () => {
      el.disabled = true;
      $("library-error").hidden = true;
      try { await action(); }
      catch (err) {
        setText("library-error", err.message || String(err));
        $("library-error").hidden = false;
        $("library-error").focus();
      }
      finally { el.disabled = false; }
    });
    return el;
  }

  function confirmAction(message, confirmLabel = "确认删除") {
    return new Promise(resolve => {
      const previous = document.activeElement;
      const dialog = document.createElement("dialog");
      dialog.className = "confirm-dialog";
      dialog.setAttribute("aria-label", confirmLabel);
      const text = document.createElement("p");
      text.textContent = message;
      const actions = document.createElement("div");
      actions.className = "head-actions";
      const finish = value => {
        dialog.close(); dialog.remove();
        if (previous?.isConnected) previous.focus();
        resolve(value);
      };
      actions.append(button("取消", () => finish(false)), button(confirmLabel, () => finish(true)));
      dialog.append(text, actions);
      dialog.addEventListener("cancel", event => { event.preventDefault(); finish(false); });
      document.body.appendChild(dialog);
      dialog.showModal();
      actions.firstElementChild.focus();
    });
  }

  async function loadMesh() {
    try { renderMesh(await apiGet("dashboard/mesh")); }
    catch (error) { setText("mesh-status", `组网状态读取失败：${error.message || error}`); }
  }
  function renderMesh(data) {
    const components = Object.entries(data.components || {}).map(([name, item]) => `${name}：${item.state}${item.message ? "（" + item.message + "）" : ""}`).join("\n");
    const policy = data.provider === "zerotier" ? `\n传输策略：${data.transport_policy === "tcp_relay" ? "强制 TCP 中继" : "自动"}` : "";
    setText("mesh-status", `${data.provider} · ${data.ready ? "已连接" : data.running ? "等待连接/授权" : "已停止"}\n节点：${data.node_id || "—"}\n组网地址：${(data.addresses || []).join("、") || "—"}\nADB 转发：${data.adb_forward ? "已启动" : "未启动"}\n抓包端口：${data.proxy_port || "未发布"}${policy}${data.error ? "\n" + data.error : ""}${components ? "\n运行组件：\n" + components : ""}`);
    const link = $("mesh-login"); link.hidden = true; link.removeAttribute("href");
    try { const url = new URL(data.auth_url); if (url.protocol === "https:" && ["login.tailscale.com", "controlplane.tailscale.com"].includes(url.hostname)) { link.href = url.href; link.hidden = false; } } catch {}
  }

  // ---------------- 图片库 ----------------
  async function loadImages() {
    const requestId = ++state.imagesRequest;
    try {
      const data = await apiGet("dashboard/images", {
        query: $("images-query").value.trim(), offset: state.imagesOffset,
      });
      if (requestId !== state.imagesRequest) return;
      if (!data.entries.length && data.total && state.imagesOffset) {
        state.imagesOffset = Math.floor((data.total - 1) / 30) * 30;
        return loadImages();
      }
      setText("images-subtitle", `共 ${data.total} 张图片`);
      setText("images-page", `${data.total ? Math.floor(data.offset / 30) + 1 : 0} / ${Math.ceil(data.total / 30)}`);
      $("images-prev").disabled = data.offset === 0;
      $("images-next").disabled = data.offset + data.entries.length >= data.total;
      const list = $("images-list");
      list.replaceChildren();
      if (!data.entries.length) {
        const empty = document.createElement("p");
        empty.className = "learning-empty";
        empty.textContent = data.query ? "没有匹配的图片。" : "还没有保存的图片。";
        list.appendChild(empty);
      }
      data.entries.forEach((entry) => renderImage(entry, list));
    } catch (err) { setText("images-subtitle", `图片加载失败：${err.message || err}`); }
  }

  function renderImage(entry, list) {
    const item = document.createElement("article");
    item.className = "learning-item image-item";
    const visual = document.createElement("div");
    visual.className = "image-preview";
    visual.textContent = entry.server_path ? "点击查看图片" : "原图保存在手机，查看时需连接对应设备";
    if (entry.server_path || entry.phone_path) {
      visual.appendChild(button("查看图片", async () => {
        const data = await apiGet("dashboard/images/preview", { id: entry.id });
        const img = document.createElement("img");
        img.src = data.url;
        img.alt = entry.description || "保存的图片";
        const full = document.createElement("a");
        full.href = data.url;
        full.download = `${entry.id}.${entry.format || "png"}`;
        full.textContent = "下载原图";
        visual.replaceChildren(img, full);
      }));
    }
    item.appendChild(visual);
    const form = document.createElement("form");
    const fields = {};
    function field(key, label, value, multiline = false) {
      const wrapper = document.createElement("label");
      wrapper.textContent = label;
      const input = document.createElement(multiline ? "textarea" : "input");
      input.value = value || "";
      input.name = key;
      input.maxLength = key === "tags" ? 409 : 500;
      input.disabled = true;
      wrapper.appendChild(input);
      form.appendChild(wrapper);
      fields[key] = input;
    }
    field("description", "图片语义", entry.description, true);
    field("reason", "保存理由", entry.reason, true);
    field("tags", "标签（逗号分隔，最多 10 个）", (entry.tags || []).join(", "));
    const kindLabel = document.createElement("label");
    kindLabel.textContent = "类型";
    const kind = document.createElement("select");
    for (const [value, label] of [["image", "图片"], ["sticker", "表情包"]]) {
      const option = document.createElement("option");
      option.value = value; option.textContent = label; kind.appendChild(option);
    }
    kind.value = entry.kind || "image"; kind.disabled = true;
    kindLabel.appendChild(kind); form.appendChild(kindLabel); fields.kind = kind;
    const source = document.createElement("p");
    source.className = "image-meta";
    source.textContent = `保存于 ${fmtDateTime(entry.created_at)} · 来源 ${(entry.sources || []).map(s => s.app + (s.content_id ? " / " + s.content_id : "")).join("、") || "未标注"}\n服务端：${entry.server_path || "未保存"}\n手机：${entry.phone_path || "未保存"}${entry.last_error ? "\n保存错误：" + entry.last_error : ""}`;
    form.appendChild(source);
    const actions = document.createElement("div");
    actions.className = "head-actions";
    const edit = button("编辑语义", () => {
      Object.values(fields).forEach(f => { f.disabled = false; });
      edit.hidden = true; save.hidden = false; cancel.hidden = false;
      fields.description.focus();
    });
    const save = button("保存语义", async () => {
      await state.bridge.apiPost("dashboard/images/update", {
        id: entry.id, description: fields.description.value, reason: fields.reason.value,
        tags: fields.tags.value.split(/[,，]/).map(t => t.trim()).filter(Boolean), kind: kind.value,
      });
      await loadImages();
    });
    const cancel = button("取消", () => {
      fields.description.value = entry.description || "";
      fields.reason.value = entry.reason || "";
      fields.tags.value = (entry.tags || []).join(", "); kind.value = entry.kind || "image";
      Object.values(fields).forEach(f => { f.disabled = true; });
      edit.hidden = false; save.hidden = true; cancel.hidden = true;
    });
    const remove = button("删除图片", async () => {
      if (!await confirmAction("删除图片库记录与服务端原图？此操作不可恢复，手机相册副本保留。")) return;
      await state.bridge.apiPost("dashboard/images/delete", { id: entry.id });
      await loadImages();
    });
    save.hidden = true; cancel.hidden = true;
    actions.append(edit, save, cancel, remove); form.appendChild(actions);
    form.addEventListener("submit", e => e.preventDefault());
    item.appendChild(form); list.appendChild(item);
  }

  async function loadTasks() {
    const request = ++state.tasksRequest;
    try {
      const value = await apiGet('dashboard/tasks', { offset: state.tasksOffset, limit: 20 });
      if (request !== state.tasksRequest) return;
      const list = $('tasks-list'); list.replaceChildren();
      setText('tasks-status', `共 ${value.total} 个任务，显示 ${state.tasksOffset + (value.tasks.length ? 1 : 0)}–${state.tasksOffset + value.tasks.length}`);
      $('tasks-prev').disabled = state.tasksOffset === 0;
      $('tasks-next').disabled = state.tasksOffset + value.tasks.length >= value.total;
      const labels = { queued: '排队', running: '执行中', completed: '已完成', cancelled: '已取消', interrupted: '执行中断', failed: '失败', ended: '已结束，待核对' };
      for (const task of value.tasks) {
        const card = document.createElement('article'); card.className = 'card';
        const title = document.createElement('h2'); title.textContent = `${labels[task.status] || task.status} · ${task.summary}`;
        const info = document.createElement('p'); info.textContent = `${task.job_id} · ${fmtDateTime(task.created_at)} · ${task.steps} 步 · 更新 ${fmtDateTime(task.updated_at)}`;
        const progress = document.createElement('p'); progress.textContent = `进度：${task.progress}`;
        const wait = document.createElement('p'); wait.textContent = `等待原因：${task.wait_reason || '无'}`;
        card.append(title, info, progress, wait);
        const detail = document.createElement('details');
        const summary = document.createElement('summary'); summary.textContent = '查看执行记录与结果证据'; detail.append(summary);
        const result = document.createElement('p'); result.textContent = `结果：${task.result || '尚无确认结果'}`; detail.append(result);
        for (const delivery of task.deliveries) {
          const sent = document.createElement('p'); sent.textContent = `已送达 ${fmtDateTime(delivery.ts)}：${delivery.text} ${delivery.link}`; detail.append(sent);
        }
        for (const entry of task.events) {
          const line = document.createElement('p'); line.textContent = `${fmtTime(entry.ts)} ${entry.tool} ${entry.ok ? '成功' : '未完成'}：${entry.summary}`; detail.append(line);
        }
        for (const shot of task.screenshots) {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'btn ghost';
          button.textContent = `查看截图 ${fmtDateTime(shot.ts)}`;
          button.addEventListener('click', async () => {
            button.disabled = true;
            try {
              const proof = await apiGet('dashboard/tasks/evidence', { job_id: task.job_id, id: shot.id });
              if (!/^data:image\/(png|jpeg);base64,/.test(proof.data_url)) throw new Error('截图响应格式无效');
              const img = document.createElement('img'); img.src = proof.data_url; img.alt = '任务截图证据'; img.style.maxWidth = '100%';
              button.replaceWith(img);
            } catch (err) { button.disabled = false; setText('tasks-status', err.message || String(err)); }
          });
          detail.append(button);
        }
        card.append(detail); list.append(card);
      }
    } catch (err) { if (request === state.tasksRequest) setText('tasks-status', err.message || String(err)); }
  }

  // ---------------- 初始化 ----------------
  async function init() {
    try {
      state.bridge = await getBridge();
      const context = await state.bridge.ready();
      $("plugin-link").href = `#/extension/plugins/${encodeURIComponent(context.pluginName)}`;
      $("app-config-link").href = $("plugin-link").href;
      $("plugin-refresh-link").href = `#/plugin-view/${encodeURIComponent(context.pluginName)}/dashboard`;
    } catch (err) {
      setText("page-subtitle", err.message || String(err));
      return;
    }

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.dataset.view));
    });
    $('tasks-refresh').addEventListener('click', loadTasks);
    $('tasks-prev').addEventListener('click', () => { state.tasksOffset = Math.max(0, state.tasksOffset - 20); loadTasks(); });
    $('tasks-next').addEventListener('click', () => { state.tasksOffset += 20; loadTasks(); });
    $("app-pair").addEventListener("click", async () => {
      $("app-pair").disabled = true;
      try {
        const value = await apiGet("dashboard/app-connection", { host: $("app-host").value.trim() });
        renderAppConnection(value);
        $("app-code").value = value.code || "";
        $("app-code-area").hidden = !value.code;
        setText("app-status", value.code ? "配对码已准备好，请复制到手机 App。" : value.message);
      } catch (error) { $("app-pair").disabled = false; setText("app-status", error.message || String(error)); }
    });
    $("app-refresh").addEventListener("click", loadAppConnection);
    $("app-copy").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("app-code").value);
        setText("app-status", "配对码已复制。");
      } catch (_) {
        $("app-code").focus(); $("app-code").select();
        setText("app-status", "已选中配对码，请复制到手机 App。");
      }
    });
    $("app-reset").addEventListener("click", async () => {
      if (!await confirmAction("断开已配对手机并生成新的配对凭据？", "确认重置配对")) return;
      try {
        const value = await state.bridge.apiPost("dashboard/app-connection", {});
        $("app-code").value = ""; $("app-code-area").hidden = true;
        setText("app-status", value.message); loadAppConnection();
      } catch (error) { setText("app-status", error.message || String(error)); }
    });
    $("app-apk").addEventListener("click", async () => {
      $("app-apk").disabled = true;
      $("app-apk").textContent = "正在准备安装包…";
      try {
        const response = await apiGet("dashboard/app-apk");
        const bytes = Uint8Array.from(atob(response.content_base64), c => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: "application/vnd.android.package-archive" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a"); link.href = url; link.download = "phone-adb-relay.apk"; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) { setText("app-status", `下载失败：${error.message || error}`); }
      finally { $("app-apk").disabled = false; $("app-apk").textContent = "下载 Relay APK"; }
    });
    $("mesh-refresh").addEventListener("click", loadMesh);
    const downloadCaptureCa = async feedback => {
      try {
        const certificate = await apiGet("dashboard/mesh/ca");
        const bytes = Uint8Array.from(atob(certificate.content_base64), c => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], { type: "application/x-x509-ca-cert" }));
        const link = document.createElement("a"); link.href = url; link.download = certificate.name;
        document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) { setText(feedback, `证书下载失败：${error.message || error}`); }
    };
    $("mesh-ca").addEventListener("click", () => downloadCaptureCa("mesh-status"));
    $("traffic-ca").addEventListener("click", () => downloadCaptureCa("traffic-ca-status"));
    for (const action of ["start", "stop"]) {
      $("mesh-" + action).addEventListener("click", async () => {
        const button = $("mesh-" + action); button.disabled = true;
        try { renderMesh(await state.bridge.apiPost("dashboard/mesh/action", { action })); }
        catch (error) { setText("mesh-status", `组网操作失败：${error.message || error}`); }
        finally { button.disabled = false; }
      });
    }
    $("btn-refresh").addEventListener("click", () => {
      refreshOverview();
      refreshSeries();
    });
    const recordTabs = [...document.querySelectorAll("[data-record-tab]")];
    recordTabs.forEach((tab, index) => {
      tab.addEventListener("click", () => switchRecordTab(tab.dataset.recordTab));
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? recordTabs.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + recordTabs.length) % recordTabs.length;
        recordTabs[next].focus(); switchRecordTab(recordTabs[next].dataset.recordTab);
      });
    });
    $("btn-diary-refresh").addEventListener("click", loadDiary);
    $("btn-diary-search").addEventListener("click", () => {
      state.diaryOffset = 0;
      loadDiary();
    });
    $("diary-query").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        state.diaryOffset = 0;
        loadDiary();
      }
    });
    $("diary-limit").addEventListener("change", () => {
      state.diaryOffset = 0;
      loadDiary();
    });
    $("diary-prev").addEventListener("click", () => {
      state.diaryOffset = Math.max(0, state.diaryOffset - Number($("diary-limit")?.value || 20));
      loadDiary();
    });
    $("diary-next").addEventListener("click", () => {
      state.diaryOffset += Number($("diary-limit")?.value || 20);
      loadDiary();
    });
    const vectorTabs = [...document.querySelectorAll("[data-vector-mode]")];
    vectorTabs.forEach((tab, index) => {
      tab.addEventListener("click", () => switchVectorMode(tab.dataset.vectorMode));
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? vectorTabs.length - 1 : 1 - index;
        vectorTabs[next].focus(); switchVectorMode(vectorTabs[next].dataset.vectorMode);
      });
    });
    $("graph-threshold").addEventListener("input", () => setText("graph-threshold-value", Number($("graph-threshold").value).toFixed(2)));
    $("graph-threshold").addEventListener("change", loadVectors);
    $("graph-limit").addEventListener("change", loadVectors);
    $("graph-zoom-in").addEventListener("click", () => state.graphView?.zoom(1.2));
    $("graph-zoom-out").addEventListener("click", () => state.graphView?.zoom(1 / 1.2));
    $("graph-reset").addEventListener("click", () => state.graphView?.reset());
    const searchVectors = () => { state.vectorsOffset = 0; loadVectors(); };
    $("vectors-refresh").addEventListener("click", loadVectors);
    $("vectors-search").addEventListener("click", searchVectors);
    $("vectors-kind").addEventListener("change", searchVectors);
    $("vectors-source").addEventListener("change", searchVectors);
    $("vectors-query").addEventListener("keydown", e => { if (e.key === "Enter") searchVectors(); });
    $("vectors-prev").addEventListener("click", () => { state.vectorsOffset = Math.max(0, state.vectorsOffset - 20); loadVectors(); });
    $("vectors-next").addEventListener("click", () => { state.vectorsOffset += 20; loadVectors(); });
    $("btn-learning-refresh").addEventListener("click", loadLearning);
    $("btn-learning-search").addEventListener("click", loadLearning);
    $("learning-query").addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadLearning();
    });
    $("learning-limit").addEventListener("change", loadLearning);
    $("btn-images-refresh").addEventListener("click", loadImages);
    const searchImages = () => { state.imagesOffset = 0; loadImages(); };
    $("btn-images-search").addEventListener("click", searchImages);
    $("images-query").addEventListener("keydown", e => { if (e.key === "Enter") searchImages(); });
    $("images-prev").addEventListener("click", () => { state.imagesOffset = Math.max(0, state.imagesOffset - 30); loadImages(); });
    $("images-next").addEventListener("click", () => { state.imagesOffset += 30; loadImages(); });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      if (state.view === "dashboard") {
        refreshOverview();
        refreshSeries();
      } else if (state.view === "relay") {
        loadAppConnection();
      } else if (state.view === "records") {
        switchRecordTab(state.recordTab);
      } else if (state.view === "vectors") {
        loadVectors();
      } else if (state.view === "images") {
        loadImages();
      } else if (state.view === "tasks") {
        loadTasks();
      }
    });
    window.addEventListener("resize", redrawCharts);

    switchView("dashboard");
  }

  init();
})();
