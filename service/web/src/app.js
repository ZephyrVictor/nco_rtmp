import "./app.css";
import mpegts from "mpegts.js";
import { Upload } from "tus-js-client";
import { createSHA256 } from "hash-wasm";
import {
  createElement,
  LogOut,
  RefreshCw,
  Plus,
  KeyRound,
  Download,
  Maximize2,
  X,
  ChevronLeft,
  ChevronRight,
  UploadCloud,
  Check,
  AlertTriangle,
  FileCheck2,
  Pause,
  Play,
  UserRound,
  Eye,
  RotateCcw,
  Pencil,
  ShieldCheck,
} from "lucide";

const icons = {
  LogOut,
  RefreshCw,
  Plus,
  KeyRound,
  Download,
  Maximize2,
  X,
  ChevronLeft,
  ChevronRight,
  UploadCloud,
  Check,
  AlertTriangle,
  FileCheck2,
  Pause,
  Play,
  UserRound,
  Eye,
  RotateCcw,
  Pencil,
  ShieldCheck,
};
const root = document.querySelector("#app"),
  modal = document.querySelector("#modal");
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icon = (name, title, attrs = "") =>
  `<button type="button" class="icon" title="${esc(title)}" aria-label="${esc(title)}" ${attrs}><i data-icon="${name}"></i></button>`;
const bytes = (n) =>
  n >= 1024 ** 4
    ? (n / 1024 ** 4).toFixed(2) + " TiB"
    : n >= 1024 ** 3
      ? (n / 1024 ** 3).toFixed(2) + " GiB"
      : n >= 1024 ** 2
        ? (n / 1024 ** 2).toFixed(1) + " MiB"
        : n + " B";
const date = (n) =>
  n ? new Date(n * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
const labels = {
  admin: "管理员",
  proctor: "监考员",
  student: "选手",
  online: "在线",
  offline: "离线",
  unknown: "状态未知",
  original: "本地原件",
  dvr: "辅助录像",
  queued: "排队中",
  checking: "校验中",
  checked: "已校验",
  failed: "校验失败",
  accepted: "审核通过",
  rejected: "审核不通过",
  pending: "待人工审核",
  uploading: "上传中",
  received: "已接收",
  cancelled: "已取消",
};
let user = null,
  tab = "wall",
  candidates = [],
  rounds = [],
  overview = {},
  page = 0,
  mediaPage = 0,
  mediaKind = "original",
  mediaContestant = "",
  mediaRound = "",
  showDisabled = false,
  filter = "",
  group = "",
  liveOnly = false,
  players = new Map(),
  transfers = [],
  timer,
  busy = false;
let restoreModal = null;
function restoreModalContent() {
  const restore = restoreModal;
  restoreModal = null;
  restore?.();
}
window.addEventListener("online", () => {
  players.forEach((player) => player.reconnect?.());
});
function decorate(scope = document) {
  scope
    .querySelectorAll("[data-icon]")
    .forEach((el) => el.replaceWith(createElement(icons[el.dataset.icon])));
}
function toast(message) {
  const el = document.querySelector("#toast");
  el.textContent = message;
  el.style.display = "block";
  clearTimeout(timer);
  timer = setTimeout(() => (el.style.display = "none"), 6000);
}
async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(options.method && options.method !== "GET"
      ? { "X-CSRF-Token": user?.csrf || "" }
      : {}),
    ...options.headers,
  };
  const response = await fetch("/api/" + path, {
    ...options,
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) {
    let error;
    try {
      error = await response.json();
    } catch {}
    if (response.status === 401 && user) {
      user = null;
      destroyPlayers();
      loginPage();
    }
    throw Error(
      typeof error?.detail === "string"
        ? error.detail
        : JSON.stringify(error?.detail || `请求失败 (${response.status})`),
    );
  }
  return response.json();
}
const mutate = (path, body, method = "POST") => api(path, { method, body });
const status = (c) =>
  `<span class="status ${c.status}"><span class="dot"></span>${labels[c.status]}</span>`;
function destroyPlayers() {
  players.forEach((p) => {
    p.stopped = true;
    clearTimeout(p.retry);
    clearInterval(p.watch);
    try {
      p.instance?.destroy();
    } catch {}
  });
  players.clear();
}
function closeModal() {
  restoreModalContent();
  modal.close();
  modal.innerHTML = "";
}
modal.addEventListener("close", restoreModalContent);
modal.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) closeModal();
});
function showModal(title, html) {
  modal.innerHTML = `<div class="modal-top"><h2>${esc(title)}</h2>${icon("X", "关闭", "data-close")}</div>${html}`;
  decorate(modal);
  modal.showModal();
}
function formModal(title, fields, submit, onSubmit) {
  showModal(
    title,
    `<form id="dialog-form">${fields}<div class="error"></div><div class="dialog-actions"><button type="button" data-close>取消</button><button class="primary">${esc(submit)}</button></div></form>`,
  );
  modal.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.currentTarget,
      button = form.querySelector("button.primary");
    button.disabled = true;
    try {
      await onSubmit(Object.fromEntries(new FormData(form)), form);
      closeModal();
      await refresh();
    } catch (error) {
      form.querySelector(".error").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
}
const input = (name, label, value = "", type = "text", extra = "") =>
  `<label>${esc(label)}<input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
const candidateSelect = (name = "contestant_id") =>
  `<label>选手<select name="${name}" required>${candidates
    .filter((c) => !c.disabled)
    .map(
      (c) =>
        `<option value="${esc(c.id)}">${esc(c.id + " · " + c.name)}</option>`,
    )
    .join("")}</select></label>`;
const roundSelect = () =>
  `<label>场次<select name="round_id">${rounds.map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join("")}</select></label>`;
function loginPage() {
  root.innerHTML = `<div class="login"><div class="brand"><span class="brandmark">N</span>NCO 监考控制台</div><h1>登录</h1><form id="login">${input("username", "账号", "", "text", 'required autocomplete="username"')}${input("password", "密码", "", "password", 'required autocomplete="current-password"')}<div class="error" role="alert"></div><button class="primary">登录</button></form></div>`;
  document.querySelector("#login").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.currentTarget;
    f.querySelector("button").disabled = true;
    try {
      user = await mutate("auth/login", Object.fromEntries(new FormData(f)));
      tab = user.role === "student" ? "uploads" : "wall";
      await shell();
    } catch (error) {
      f.querySelector(".error").textContent = error.message;
    } finally {
      f.querySelector("button")?.removeAttribute("disabled");
    }
  };
}
async function shell() {
  root.innerHTML = `<header class="top"><div class="brand"><span class="brandmark">N</span>NCO 监考控制台</div><div class="account"><span>${esc(user.username)}</span><span class="tag role">${labels[user.role]}</span>${icon("KeyRound", "修改密码", 'id="password"')}${icon("LogOut", "退出登录", 'id="logout"')}</div></header><nav class="nav"></nav><main><div id="stats"></div><div id="content"></div></main>`;
  const tabs =
    user.role === "student"
      ? [
          ["uploads", "原件提交"],
          ["media", "我的录像"],
          ["settings", "录屏参数"],
        ]
      : [
          ["wall", "实时监考"],
          ["roster", "选手名单"],
          ["media", "录像归档"],
          ["uploads", "原件提交"],
          ["alerts", "告警与日志"],
          ["settings", "设置"],
        ];
  document.querySelector(".nav").innerHTML = tabs
    .map(
      ([id, name]) =>
        `<button data-tab="${id}" class="${tab === id ? "selected" : ""}">${name}</button>`,
    )
    .join("");
  document.querySelector(".nav").onclick = (e) => {
    const b = e.target.closest("[data-tab]");
    if (b) {
      destroyPlayers();
      tab = b.dataset.tab;
      document
        .querySelectorAll("[data-tab]")
        .forEach((n) => n.classList.toggle("selected", n === b));
      if (busy) render().catch((e) => toast(e.message));
      else refresh().catch((e) => toast(e.message));
    }
  };
  document.querySelector("#logout").onclick = async () => {
    if (transfers.some((t) => t.active)) {
      toast("请先暂停当前上传");
      return;
    }
    await mutate("auth/logout");
    user = null;
    destroyPlayers();
    loginPage();
  };
  document.querySelector("#password").onclick = () =>
    formModal(
      "修改密码",
      input(
        "current_password",
        "当前密码",
        "",
        "password",
        'required autocomplete="current-password"',
      ) +
        input(
          "new_password",
          "新密码",
          "",
          "password",
          'required minlength="14" autocomplete="new-password"',
        ),
      "更新",
      async (data) => {
        await mutate("auth/password", data);
        user = null;
        destroyPlayers();
        loginPage();
      },
    );
  decorate();
  await refresh();
}
function stats() {
  const s = overview.storage;
  document.querySelector("#stats").innerHTML =
    `<div class="stats"><div class="stat"><small>选手</small><strong>${overview.contestants ?? "—"}</strong></div><div class="stat"><small>直播在线</small><strong class="good">${overview.online ?? 0}</strong></div><div class="stat"><small>未解除告警</small><strong class="${overview.alerts ? "warn" : ""}">${overview.alerts ?? 0}</strong></div><div class="stat"><small>${s ? "磁盘可用" : "已接收原件"}</small><strong>${s ? bytes(s.available_bytes) : overview.media_counts?.filter((m) => m.kind === "original").reduce((n, m) => n + m.n, 0) || 0}</strong></div><div class="stat"><small>${s ? "磁盘使用率" : "直播服务"}</small><strong class="${s?.used_fraction > 0.85 ? "bad" : ""}">${s ? (s.used_fraction * 100).toFixed(1) + "%" : overview.monitor?.srs_ok ? "正常" : "不可用"}</strong></div></div>`;
}
async function refresh() {
  if (!user || busy) return;
  busy = true;
  try {
    [overview, candidates, rounds] = await Promise.all([
      api("overview"),
      api("contestants"),
      api("rounds"),
    ]);
    stats();
    await render();
  } finally {
    busy = false;
  }
}
async function render() {
  const parent = document.querySelector("#content");
  if (!parent) return;
  const content = document.createElement("div");
  const current = tab;
  parent.replaceChildren(content);
  try {
    if (current === "wall") wall(content);
    if (current === "roster") roster(content);
    if (current === "media") await media(content);
    if (current === "uploads") await uploads(content);
    if (current === "alerts") await alerts(content);
    if (current === "settings") await settings(content);
    decorate(content);
  } catch (error) {
    content.innerHTML = `<div class="notice">${esc(error.message)}</div>`;
  }
}
function attachPlayer(c, el) {
  if (!mpegts.isSupported()) {
    el.querySelector(".placeholder").textContent = "浏览器不支持 HTTP-FLV";
    return;
  }
  const video = el.querySelector("video"), hint = el.querySelector(".placeholder");
  const state = { instance: null, retry: null, attempt: 0, stopped:false,
                  lastProgress:Date.now(), lastTime:0, watch:null };
  players.set(c.id, state);
  function start() {
    if (!video.isConnected || state.stopped || players.get(c.id)!==state) return;
    clearTimeout(state.retry);
    state.retry=null;
    state.lastProgress=Date.now();
    state.lastTime=0;
    try {
      const previous=state.instance;
      state.instance=null;
      previous?.destroy();
      state.instance = mpegts.createPlayer(
        {
          type: "flv",
          url: new URL("/live/" + encodeURIComponent(c.id) + ".flv", location.origin).href,
          withCredentials: true,
          isLive: true,
          hasAudio: true,
          hasVideo: true,
        },
        {
          enableWorker: true,
          enableStashBuffer: false,
          liveBufferLatencyChasing: false,
          lazyLoad: false,
          autoCleanupSourceBuffer: true,
          autoCleanupMaxBackwardDuration: 30,
          autoCleanupMinBackwardDuration: 10,
        },
      );
      state.instance.attachMediaElement(video);
      const instance=state.instance;
      state.instance.on(mpegts.Events.ERROR, () => {
        if(state.stopped || state.instance!==instance)return;
        hint.hidden = false;
        hint.style.display = "flex";
        hint.textContent = "播放中断，正在重连";
        clearTimeout(state.retry);
        state.retry = setTimeout(
          start,
          Math.min(30000, 2000 * 2 ** state.attempt++),
        );
      });
      video.onplaying = () => {
        hint.hidden = true;
        hint.style.display = "none";
        state.attempt = 0;
        state.lastProgress=Date.now();
      };
      state.instance.load();
      video.play().catch(() => {
        hint.textContent = "播放已暂停";
      });
    } catch (error) {
      hint.textContent = "播放失败";
    }
  }
  state.reconnect=start;
  start();
  state.watch=setInterval(()=>{
    if(state.stopped||!video.isConnected)return;
    if(video.readyState>=2&&video.currentTime!==state.lastTime){
      state.lastTime=video.currentTime;
      state.lastProgress=Date.now();
    }else if(Date.now()-state.lastProgress>25000&&!state.retry){
      hint.hidden=false;
      hint.style.display='flex';
      hint.textContent='画面停滞，正在重连';
      state.retry=setTimeout(start,1000);
    }
  },5000);
}
function wall(el) {
  destroyPlayers();
  const groups = [
    ...new Set(candidates.map((c) => c.group_name).filter(Boolean)),
  ];
  let list = candidates.filter(
    (c) =>
      !c.disabled &&
      (!group || c.group_name === group) &&
      (!liveOnly || c.status === "online") &&
      (!filter || (c.id + c.name).toLowerCase().includes(filter.toLowerCase())),
  );
  const max = Math.max(1, Math.ceil(list.length / 16));
  page = Math.min(page, max - 1);
  el.innerHTML = `${!overview.monitor?.srs_ok ? '<div class="notice">直播服务不可用，当前在线状态无法确认</div>' : ""}<div class="toolbar"><h1>实时监考 <small>${list.length} 位选手</small></h1><div class="actions"><input id="search" value="${esc(filter)}" placeholder="编号 / 姓名" aria-label="筛选选手"><select id="group" aria-label="筛选组别"><option value="">全部组别</option>${groups.map((g) => `<option ${g === group ? "selected" : ""}>${esc(g)}</option>`).join("")}</select><label class="check" style="margin:0"><input id="live-only" type="checkbox" ${liveOnly ? "checked" : ""}>仅在线</label>${icon("RefreshCw", "刷新", 'id="refresh"')}</div></div><div class="grid">${list
    .slice(page * 16, page * 16 + 16)
    .map(
      (c) =>
        `<article class="stream" data-cid="${esc(c.id)}"><div class="stream-head"><span class="stream-name" title="${esc(c.id + " · " + c.name)}">${esc(c.id)} · ${esc(c.name)}</span>${icon("Maximize2", "放大画面", `data-zoom="${esc(c.id)}"`)}</div><div class="screen"><video muted autoplay playsinline></video><div class="placeholder">${c.status === "online" ? "正在连接" : labels[c.status]}</div></div><div class="stream-foot">${status(c)}<span>${esc(c.group_name || "未分组")} · 原件 ${c.originals}</span></div></article>`,
    )
    .join(
      "",
    )}</div>${list.length ? "" : '<div class="empty">暂无选手</div>'}<div class="pager">${icon("ChevronLeft", "上一页", `id="prev" ${page === 0 ? "disabled" : ""}`)}<span>${page + 1} / ${max}</span>${icon("ChevronRight", "下一页", `id="next" ${page + 1 >= max ? "disabled" : ""}`)}</div>`;
  el.querySelector("#search").onchange = (e) => {
    filter = e.target.value;
    page = 0;
    wall(el);
    decorate(el);
  };
  el.querySelector("#group").onchange = (e) => {
    group = e.target.value;
    page = 0;
    wall(el);
    decorate(el);
  };
  el.querySelector("#live-only").onchange = (e) => {
    liveOnly = e.target.checked;
    page = 0;
    wall(el);
    decorate(el);
  };
  el.querySelector("#refresh").onclick = () =>
    refresh().catch((e) => toast(e.message));
  el.querySelector("#prev").onclick = () => {
    page--;
    wall(el);
    decorate(el);
  };
  el.querySelector("#next").onclick = () => {
    page++;
    wall(el);
    decorate(el);
  };
  list
    .slice(page * 16, page * 16 + 16)
    .filter((c) => c.status === "online")
    .forEach((c) =>
      attachPlayer(c, el.querySelector(`[data-cid="${CSS.escape(c.id)}"]`)),
    );
  el.querySelectorAll("[data-zoom]").forEach(
    (b) =>
      (b.onclick = () => {
        const screen = b.closest(".stream").querySelector(".screen"),
          parent = screen.parentNode,
          next = screen.nextSibling;
        showModal(
          b.closest(".stream").querySelector(".stream-name").textContent,
          "",
        );
        modal.classList.add("zoom");
        modal.append(screen);
        restoreModal = () => {
          parent.insertBefore(screen, next);
          modal.classList.remove("zoom");
        };
      }),
  );
}
function table(headers, rows) {
  return rows.length
    ? `<div class="table-wrap"><table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : '<div class="empty">暂无记录</div>';
}
function roster(el) {
  const isAdmin = user.role === "admin";
  const visible = candidates.filter(c=>showDisabled||!c.disabled);
  el.innerHTML = `<div class="toolbar"><h1>选手名单 <small>${visible.length} 人</small></h1><div class="actions"><label class="check" style="margin:0"><input id="show-disabled" type="checkbox" ${showDisabled?'checked':''}>包含停用选手</label>${isAdmin ? '<button id="import">导入 CSV</button><button class="primary" id="add"><i data-icon="Plus"></i>添加选手</button>' : ""}</div></div>${table(
    ["编号", "姓名", "组别", "直播", "原件", "状态", "操作"],
    visible.map(
      (c) =>
        `<tr><td>${esc(c.id)}</td><td>${esc(c.name)}</td><td>${esc(c.group_name) || "—"}</td><td>${status(c)}</td><td>${c.originals}</td><td>${c.disabled ? "已停用" : "启用"}</td><td><div class="actions">${isAdmin ? icon("Pencil", "编辑选手", `data-edit="${c.id}"`) + icon("KeyRound", "重置推流凭据", `data-key="${c.id}"`) : "—"}</div></td></tr>`,
    ),
  )}`;
  el.querySelector('#show-disabled').onchange=e=>{showDisabled=e.target.checked;roster(el);decorate(el);};
  if (!isAdmin) return;
  el.querySelector("#add").onclick = () =>
    formModal(
      "添加选手",
      input(
        "id",
        "编号",
        "",
        "text",
        'required pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,63}"',
      ) +
        input("name", "姓名", "", "text", 'required maxlength="100"') +
        input("group_name", "组别"),
      "添加",
      (data) => mutate("contestants", data),
    );
  el.querySelector("#import").onclick = () =>
    formModal(
      "导入选手名单",
      '<label>CSV 文件（id,name,group）<input name="file" type="file" accept=".csv" required></label>',
      "导入",
      async (_, f) =>
        mutate("contestants/import", {
          csv: await f.elements.file.files[0].text(),
        }),
    );
  el.querySelectorAll("[data-edit]").forEach(
    (b) =>
      (b.onclick = () => {
        const c = candidates.find((c) => c.id === b.dataset.edit);
        formModal(
          "编辑 " + c.id,
          input("name", "姓名", c.name, "text", "required") +
            input("group_name", "组别", c.group_name) +
            `<label class="check"><input type="checkbox" name="disabled" ${c.disabled ? "checked" : ""}>停用选手</label>`,
          "保存",
          (data) =>
            mutate(
              "contestants/" + c.id,
              { ...data, disabled: !!data.disabled },
              "PUT",
            ),
        );
      }),
  );
  el.querySelectorAll("[data-key]").forEach(
    (b) => (b.onclick = () => keyDialog(b.dataset.key)),
  );
}
function keyDialog(cid) {
  formModal("重置推流凭据 · " + cid, roundSelect(), "生成", async (data) => {
    const key = await mutate(
      "contestants/" + cid + "/key?round_id=" + data.round_id,
      {},
    );
    setTimeout(
      () =>
        showModal(
          "推流凭据 · " + cid,
          `<label>服务器<input readonly value="${esc(key.server)}"></label><label>串流密钥<input readonly value="${esc(key.stream_key)}"></label>`,
        ),
      0,
    );
  });
}
async function media(el) {
  const query=new URLSearchParams({offset:String(mediaPage*50),limit:'50',kind:mediaKind,contestant:mediaContestant,round_id:mediaRound});
  const data = await api("media?"+query);
  el.innerHTML = `<div class="toolbar"><h1>${user.role === "student" ? "我的录像" : "录像归档"} <small>${data.total} 个文件</small></h1><div class="actions"><input id="media-candidate" aria-label="录像选手编号" placeholder="选手编号" value="${esc(mediaContestant)}"><select id="media-kind" aria-label="录像类型"><option value="original" ${mediaKind==='original'?'selected':''}>本地原件</option><option value="dvr" ${mediaKind==='dvr'?'selected':''}>辅助录像</option><option value="" ${mediaKind===''?'selected':''}>全部类型</option></select><select id="media-round" aria-label="录像场次"><option value="">全部场次</option>${rounds.map(r=>`<option value="${r.id}" ${r.id===mediaRound?'selected':''}>${esc(r.name)}</option>`).join('')}</select>${user.role !== "student" ? '<a href="/api/manifest.csv"><button><i data-icon="Download"></i>导出清单</button></a>' : ""}${icon("RefreshCw", "刷新", 'id="refresh"')}</div></div>${table(
    ["选手 / 场次", "文件", "类型", "大小", "校验", "人工审核", "操作"],
    data.items.map(
      (m) =>
        `<tr><td>${esc(m.contestant_id)}<br><small>${esc(rounds.find((r) => r.id === m.round_id)?.name || m.round_id)}</small></td><td class="long">${esc(m.filename)}<br><small>${date(m.created)}</small></td><td>${labels[m.kind]}</td><td>${bytes(m.size)}</td><td class="${m.state === "failed" || m.report?.errors?.length ? "bad" : m.state === "checked" ? "good" : ""}">${labels[m.state]}${m.report?.errors?.length ? " · 有问题" : ""}</td><td>${m.kind === "original" ? labels[m.review] : "—"}</td><td><div class="actions">${icon("Eye", "查看校验报告", `data-report="${m.id}"`)}<a href="/api/media/${m.id}/download">${icon("Download", "下载录像")}</a>${user.role !== "student" && m.kind === "original" && m.state === "checked" ? icon("FileCheck2", "人工审核", `data-review="${m.id}"`) : ""}${user.role === "admin" && m.state !== "checking" ? icon("RotateCcw", "重新校验", `data-retry="${m.id}"`) : ""}</div></td></tr>`,
    ),
  )}<div class="pager">${icon("ChevronLeft", "上一页", `id="prev" ${mediaPage === 0 ? "disabled" : ""}`)}<span>${mediaPage + 1} / ${Math.max(1, Math.ceil(data.total / 50))}</span>${icon("ChevronRight", "下一页", `id="next" ${(mediaPage + 1) * 50 >= data.total ? "disabled" : ""}`)}</div>`;
  el.querySelector('#media-candidate').onchange=e=>{mediaContestant=e.target.value.trim();mediaPage=0;render();};
  el.querySelector('#media-kind').onchange=e=>{mediaKind=e.target.value;mediaPage=0;render();};
  el.querySelector('#media-round').onchange=e=>{mediaRound=e.target.value;mediaPage=0;render();};
  el.querySelector("#refresh").onclick = () =>
    refresh().catch((e) => toast(e.message));
  el.querySelector("#prev").onclick = () => {
    mediaPage--;
    render();
  };
  el.querySelector("#next").onclick = () => {
    mediaPage++;
    render();
  };
  el.querySelectorAll("[data-report]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          const m = data.items.find((m) => m.id === b.dataset.report),
            r = await api("media/" + m.id + "/report"),
            p = r.report || {},
            v = p.probe?.streams?.find((s) => s.codec_type === "video"),
            a = p.probe?.streams?.find((s) => s.codec_type === "audio");
          showModal(
            "录像校验报告",
            `<dl class="rule-list"><dt>文件</dt><dd>${esc(m.filename)}</dd><dt>处理状态</dt><dd>${labels[r.state]}</dd><dt>完整解码</dt><dd>${p.decode_ok ? "通过" : "未通过 / 未完成"}</dd><dt>时长</dt><dd>${p.duration_seconds?.toFixed(1) || "—"} 秒</dd><dt>视频</dt><dd>${v ? `${esc(v.codec_name)} · ${v.width}×${v.height} · ${esc(v.avg_frame_rate)} FPS · ${esc(v.profile)}` : "—"}</dd><dt>音频</dt><dd>${a ? `${esc(a.codec_name)} · ${a.sample_rate} Hz · ${a.channels} 声道` : "—"}</dd><dt>SHA-256</dt><dd>${esc(r.sha256) || "—"}</dd><dt>人工审核</dt><dd>${labels[r.review]} · ${esc(r.review_note)}</dd></dl>${p.errors?.length ? '<ul class="issues">' + p.errors.map((s) => "<li>" + esc(s) + "</li>").join("") + "</ul>" : ""}${p.warnings?.length ? '<div class="notice">' + p.warnings.map(esc).join("<br>") + "</div>" : ""}<details><summary>编码参数与人工核对项</summary><div class="code">${esc(p.x264_options || "未提取到编码参数")}\n\n${esc(p.manual_checks?.join("\n") || "")}</div></details>`,
          );
        } catch (e) {
          toast(e.message);
        }
      }),
  );
  el.querySelectorAll("[data-review]").forEach(
    (b) =>
      (b.onclick = () =>
        formModal(
          "人工审核",
          '<label>审核结论<select name="review"><option value="pending">待人工审核</option><option value="accepted">审核通过</option><option value="rejected">审核不通过</option></select></label><label>核对记录<textarea name="note" required maxlength="2000"></textarea></label>',
          "保存",
          (data) => mutate("media/" + b.dataset.review + "/review", data),
        )),
  );
  el.querySelectorAll("[data-retry]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          await mutate("media/" + b.dataset.retry + "/retry");
          await refresh();
        } catch (e) {
          toast(e.message);
        }
      }),
  );
}
async function hashFile(file, update) {
  const hasher = await createSHA256();
  hasher.init();
  for (let offset = 0; offset < file.size; offset += 4 * 1024 ** 2) {
    hasher.update(
      new Uint8Array(
        await file.slice(offset, offset + 4 * 1024 ** 2).arrayBuffer(),
      ),
    );
    update(Math.min(100, ((offset + 4 * 1024 ** 2) / file.size) * 100));
  }
  return hasher.digest("hex");
}
async function uploads(el) {
  const list = await api("uploads");
  el.innerHTML = `<div class="toolbar"><h1>原件提交</h1>${user.role === "student" ? '<button id="own-key"><i data-icon="KeyRound"></i>推流凭据</button>' : ""}</div>${user.role !== "proctor" ? `<form id="upload-form" class="upload-form"><div class="fields">${candidateSelect()}${roundSelect()}</div><label>MKV 原始录像<input type="file" name="file" accept=".mkv" required></label><button class="primary"><i data-icon="UploadCloud"></i>上传原件</button><div class="error"></div></form>` : ""}<div id="transfers"></div><h2 style="font-size:16px;margin-top:28px">接收记录</h2>${table(
    ["选手", "场次", "文件", "大小", "进度", "状态"],
    list.map(
      (u) =>
        `<tr><td>${esc(u.contestant_id)}</td><td>${esc(u.round_id)}</td><td class="long">${esc(u.filename)}</td><td>${bytes(u.size)}</td><td>${((u.offset / u.size) * 100).toFixed(1)}%</td><td>${labels[u.state]}</td></tr>`,
    ),
  )}`;
  el.querySelector("#own-key")?.addEventListener("click", () =>
    keyDialog(user.contestant_id),
  );
  renderTransfers();
  el.querySelector("#upload-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget,
      button = form.querySelector("button"),
      file = form.elements.file.files[0],
      cid = form.elements.contestant_id.value,
      rid = form.elements.round_id.value;
    if (transfers.some((t) => t.file === file.name && t.active)) {
      toast("此文件正在上传");
      return;
    }
    button.disabled = true;
    const transfer = {
      id: crypto.randomUUID(),
      file: file.name,
      progress: 0,
      status: "计算 SHA-256",
      active: true,
      upload: null,
    };
    transfers.push(transfer);
    renderTransfers();
    try {
      const hash = await hashFile(file, (n) => {
        transfer.status = "计算 SHA-256 · " + n.toFixed(0) + "%";
        renderTransfers();
      });
      transfer.status = "正在连接";
      transfer.upload = new Upload(file, {
        endpoint: "/uploads/",
        chunkSize: 8 * 1024 ** 2,
        retryDelays: [0, 1000, 3000, 5000, 15000],
        headers: { "X-CSRF-Token": user.csrf },
        withCredentials: true,
        metadata: {
          filename: file.name,
          contestant_id: cid,
          round_id: rid,
          sha256: hash,
        },
        fingerprint: async () =>
          `nco:${user.username}:${cid}:${rid}:${hash}:${location.origin}`,
        removeFingerprintOnSuccess: true,
        onError: (error) => {
          transfer.active = false;
          transfer.status = "上传失败：" + error.message;
          renderTransfers();
        },
        onProgress: (sent, total) => {
          transfer.progress = (sent / total) * 100;
          transfer.status = "正在上传";
          renderTransfers();
        },
        onSuccess: () => {
          transfer.active = false;
          transfer.status = "已接收，等待校验";
          renderTransfers();
          toast("原件接收完成");
        },
      });
      const previous = await transfer.upload.findPreviousUploads();
      if (previous.length)
        transfer.upload.resumeFromPreviousUpload(previous[0]);
      transfer.upload.start();
    } catch (error) {
      transfer.active = false;
      transfer.status = error.message;
      renderTransfers();
    } finally {
      button.disabled = false;
    }
  });
}
function renderTransfers() {
  const el = document.querySelector("#transfers");
  if (!el) return;
  el.innerHTML = transfers
    .map(
      (t) =>
        `<div class="transfer"><strong>${esc(t.file)}</strong><div class="actions"><span>${esc(t.status)} · ${t.progress.toFixed(1)}%</span>${t.upload && t.progress < 100 ? icon(t.active ? "Pause" : "Play", t.active ? "暂停上传" : "继续上传", `data-transfer="${t.id}"`) : ""}</div><progress value="${t.progress}" max="100"></progress></div>`,
    )
    .join("");
  decorate(el);
  el.querySelectorAll("[data-transfer]").forEach(
    (b) =>
      (b.onclick = async () => {
        const t = transfers.find((t) => t.id === b.dataset.transfer);
        if (t.active) {
          await t.upload.abort();
          t.active = false;
          t.status = "已暂停";
        } else {
          t.active = true;
          t.status = "正在连接";
          t.upload.start();
        }
        renderTransfers();
      }),
  );
}
async function alerts(el) {
  const [list, events] = await Promise.all([api("alerts"), api("events")]);
  el.innerHTML = `<div class="toolbar"><h1>告警与日志</h1>${icon("RefreshCw", "刷新", 'id="refresh"')}</div>${table(
    ["时间", "级别", "选手", "告警", "状态", "操作"],
    list.map(
      (a) =>
        `<tr><td>${date(a.created)}</td><td class="${a.severity === "critical" ? "bad" : "warn"}">${a.severity === "critical" ? "紧急" : "警告"}</td><td>${esc(a.contestant_id) || "系统"}</td><td class="long">${esc(a.title)}<br><small>${esc(a.detail)}</small></td><td>${a.resolved ? "已恢复" : a.acknowledged_by ? "已确认 · " + esc(a.acknowledged_by) : "未确认"}</td><td>${!a.resolved && !a.acknowledged_by ? icon("Check", "确认并记录通报", `data-ack="${a.id}"`) : "—"}</td></tr>`,
    ),
  )}<div class="section"><h2>操作日志</h2>${table(
    ["时间", "操作者", "操作", "对象", "记录"],
    events
      .slice(0, 100)
      .map(
        (e) =>
          `<tr><td>${date(e.created)}</td><td>${esc(e.actor)}</td><td>${esc(e.action)}</td><td>${esc(e.target)}</td><td class="long">${esc(e.detail)}</td></tr>`,
      ),
  )}</div>`;
  el.querySelector("#refresh").onclick = () =>
    refresh().catch((e) => toast(e.message));
  el.querySelectorAll("[data-ack]").forEach(
    (b) =>
      (b.onclick = () =>
        formModal(
          "确认告警",
          '<label>通报与处理记录<textarea name="note" required maxlength="2000"></textarea></label>',
          "确认",
          (data) => mutate("alerts/" + b.dataset.ack + "/ack", data),
        )),
  );
}
async function settings(el) {
  const rules = await api("rules"),
    isAdmin = user.role === "admin";
  const users = isAdmin ? await api("users") : [];
  el.innerHTML = `<div class="settings"><div class="toolbar"><h1>录屏参数与系统设置</h1></div><dl class="rule-list"><dt>规则来源</dt><dd>ICO 2026 · §11.3 / Appendix A</dd><dt>录制容器</dt><dd>MKV</dd><dt>视频编码</dt><dd>x264 · CRF 24 · High</dd><dt>帧率 / 输出</dt><dd>整数 1 FPS · 原屏幕比例 · 至少 1280×720</dd><dt>音频</dt><dd>FFmpeg AAC · 44100 Hz · Mono</dd><dt>采集 / 时长</dt><dd>宿主机完整显示器 · 每场连续 7 小时</dd></dl><div class="section"><h2>OBS 配置</h2><form id="profile" class="upload-form"><div class="fields">${input("width", "宿主机屏幕宽度", 2560, "number", 'required min="1280" max="8192"')}${input("height", "宿主机屏幕高度", 1664, "number", 'required min="720" max="8192"')}</div><button><i data-icon="Download"></i>下载配置</button></form></div>${
    user.role !== "student"
      ? `<div class="section"><h2>服务状态</h2><dl class="rule-list"><dt>直播服务</dt><dd>${overview.monitor?.srs_ok ? "正常" : "不可用"}</dd><dt>校验进程心跳</dt><dd>${date(overview.worker_at)}</dd><dt>元数据快照</dt><dd>${date(overview.backup?.metadata_at)}</dd><dt>独立存储备份</dt><dd class="warn">${overview.backup?.independent_backup ? "已配置" : "未配置"}</dd><dt>录像预算</dt><dd>${bytes(overview.storage?.committed_bytes || 0)} / ${bytes(overview.storage?.budget_bytes || 0)}</dd><dt>入站规划预算</dt><dd>400 Mbps</dd></dl></div><div class="section"><h2>比赛场次</h2>${table(
          ["场次", "开始时间（本地时区）", "结束时间", "时长", "操作"],
          rounds.map(
            (r) =>
              `<tr><td>${esc(r.name)}</td><td>${date(r.start_at)}</td><td>${date(r.end_at)}</td><td>7 小时</td><td>${isAdmin ? icon("Pencil", "编辑场次", `data-round="${r.id}"`) : "—"}</td></tr>`,
          ),
        )}</div>`
      : ""
  }${
    isAdmin
      ? `<div class="section"><div class="toolbar"><h2>用户与权限</h2><button id="add-user"><i data-icon="Plus"></i>添加账号</button></div>${table(
          ["账号", "角色", "选手", "状态", "操作"],
          users.map(
            (u) =>
              `<tr><td>${esc(u.username)}</td><td>${labels[u.role]}</td><td>${esc(u.contestant_id) || "—"}</td><td>${u.disabled ? "停用" : "启用"}</td><td>${u.username !== user.username ? `<button data-user="${esc(u.username)}" data-disabled="${u.disabled}">${u.disabled ? "启用" : "停用"}</button>` : "—"}</td></tr>`,
          ),
        )}</div>`
      : ""
  }</div>`;
  el.querySelector("#profile").onsubmit = (e) => {
    e.preventDefault();
    const d = new FormData(e.currentTarget);
    location.href =
      "/api/obs-profile.zip?width=" +
      d.get("width") +
      "&height=" +
      d.get("height");
  };
  el.querySelectorAll("[data-round]").forEach(
    (b) =>
      (b.onclick = () => {
        const r = rounds.find((r) => r.id === b.dataset.round);
        const dt = r.start_at
          ? new Date(
              r.start_at * 1000 -
                new Date(r.start_at * 1000).getTimezoneOffset() * 60000,
            )
              .toISOString()
              .slice(0, 16)
          : "";
        formModal(
          "编辑场次",
          input("name", "名称", r.name, "text", "required") +
            input("start", "开始时间（本地时区）", dt, "datetime-local") +
            `<label class="check"><input type="checkbox" name="enabled" ${r.enabled ? "checked" : ""}>允许推流</label>`,
          "保存",
          (data) =>
            mutate(
              "rounds/" + r.id,
              {
                name: data.name,
                start_at: data.start
                  ? new Date(data.start).getTime() / 1000
                  : null,
                enabled: !!data.enabled,
              },
              "PUT",
            ),
        );
      }),
  );
  el.querySelector("#add-user")?.addEventListener("click", () =>
    formModal(
      "添加账号",
      input(
        "username",
        "账号",
        "",
        "text",
        'required pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,63}"',
      ) +
        input(
          "password",
          "初始密码",
          "",
          "password",
          'required minlength="14" autocomplete="new-password"',
        ) +
        '<label>角色<select name="role"><option value="proctor">监考员</option><option value="student">选手</option><option value="admin">管理员</option></select></label>' +
        candidateSelect(),
      "添加",
      (data) => mutate("users", data),
    ),
  );
  el.querySelectorAll("[data-user]").forEach(
    (b) =>
      (b.onclick = () =>
        formModal(
          "更改账号状态",
          `<p>${esc(b.dataset.user)} · ${b.dataset.disabled === "1" ? "启用" : "停用"}</p>`,
          "确认",
          () =>
            mutate(
              "users/" + b.dataset.user,
              { disabled: b.dataset.disabled !== "1" },
              "PUT",
            ),
        )),
  );
}
setInterval(async () => {
  if (!user || busy || modal.open) return;
  try {
    const [o, cs] = await Promise.all([api("overview"), api("contestants")]);
    const changed =
      cs.some(
        (c) => candidates.find((p) => p.id === c.id)?.status !== c.status,
      ) || cs.length !== candidates.length;
    overview = o;
    candidates = cs;
    stats();
    if (tab === "wall" && changed) {
      wall(document.querySelector("#content"));
      decorate();
    }
  } catch (error) {
    if (tab === "wall") {
      document
        .querySelectorAll(".status")
        .forEach((el) => (el.textContent = "状态未知"));
    }
    toast(error.message);
  }
}, 5000);
window.addEventListener("beforeunload", (e) => {
  if (transfers.some((t) => t.active)) {
    e.preventDefault();
    e.returnValue = "";
  }
});
try {
  user = await api("auth/me");
  tab = user.role === "student" ? "uploads" : "wall";
  await shell();
} catch {
  loginPage();
}
