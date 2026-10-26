/* Phael Tech Provision 2 — interface.
   Conversa com o Python por window.pywebview.api (cada chamada devolve uma Promise)
   e recebe eventos em lote por window.PT.onEvents([...]). */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (id, cls = "ic") => `<svg class="${cls}"><use href="#${id}"/></svg>`;
  const GB = 1024 ** 3;
  const fmtGB = (b, d = 1) => (b / GB).toFixed(b / GB >= 100 ? 0 : d).replace(".", ",") + " GB";
  const fmtSize = (b) => (b >= 1e12 ? (b / 1e12).toFixed(1).replace(".", ",") + " TB" : Math.round(b / 1e9) + " GB");
  let api = null;

  const S = {
    boot: null, inv: null, busy: false, steps: {}, theme: "dark",
    appsSel: new Set(), apps: {}, drvMode: null, freed: 0, sensorsTimer: null,
  };

  /* ------------------------------------------------------------------ etapas */
  const STEPS = [
    { key: "power", phase: 0, icon: "i-bolt", title: "Desativar inicialização rápida",
      desc: "Evita problemas de atualizações e drivers, e garante o desligamento completo do sistema. Botão liga/desliga passa a desligar." },
    { key: "office", phase: 1, icon: "i-office", title: "Instalar Microsoft Office",
      desc: "Instalação do Microsoft 365 (Word, Excel, PowerPoint, etc.). Roda ao mesmo tempo que o Windows Update." },
    { key: "wu", phase: 2, icon: "i-update", title: "Atualizar Windows Update",
      desc: "Abre o Windows Update nas Configurações, procura e instala tudo (inclusive drivers) e repete até não sobrar nenhuma. Se precisar reiniciar, o programa reabre e continua sozinho." },
    { key: "vendor", phase: 2, icon: "i-laptop", title: "Atualizar drivers do fabricante",
      desc: "Depois do Windows Update, detecta a marca (Dell, Lenovo, HP, Positivo) e instala os drivers e utilitários certos para o modelo." },
    { key: "finalize", phase: 3, icon: "i-flag", title: "Finalização",
      desc: "Limpa arquivos temporários e salva o relatório da máquina na Área de Trabalho." },
  ];
  const PHASES = ["Preparação da máquina", "Instalação do Office", "Atualizações (Windows + fabricante)", "Finalização"];
  const STATUS_TXT = { wait: "Aguardando", run: "Em andamento…", ok: "Concluído", err: "Falhou", skip: "Não selecionado" };
  const VENDOR_TOOL = {
    dell: "Dell Command | Update — drivers, BIOS e utilitários Dell para este modelo.",
    lenovo: "Catálogo oficial Lenovo (o mesmo do Lenovo System Update), direto pelo programa.",
    hp: "HP Image Assistant — analisa o modelo e instala os SoftPaqs recomendados.",
    positivo: "Positivo não tem ferramenta automática: os drivers vêm do catálogo do Windows Update.",
    other: "Marca sem ferramenta própria: os drivers vêm do catálogo do Windows Update.",
  };
  const VENDOR_SHORT = { dell: "DELL", lenovo: "LNV", hp: "HP", positivo: "POS", other: "PC" };

  /* ------------------------------------------------------------------ tema */
  function setTheme(t) {
    S.theme = t;
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("pt-theme", t); } catch (e) { /* sem armazenamento */ }
  }
  try { setTheme(localStorage.getItem("pt-theme") || "dark"); } catch (e) { setTheme("dark"); }
  $("#theme-btn").addEventListener("click", () => setTheme(S.theme === "dark" ? "light" : "dark"));

  /* ------------------------------------------------------------------ navegação */
  function go(page) {
    $$("#nav > button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + page));
    $(".content").scrollTop = 0;
    if (page === "temps") startSensors(); else stopSensors();
    if (page === "network" && !S.netLoaded) loadNetwork();
    if (page === "cleanup") api.free_space().then((v) => ($("#free-space").textContent = v));
    if (page === "apps") refreshAppStatus();
    if (page === "drivers") loadPack();
    if (page === "seal") loadSeal();
  }
  $$("#nav > button").forEach((b) => b.addEventListener("click", () => go(b.dataset.page)));
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => go(b.dataset.goto)));

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), 3200);
  }

  /* ------------------------------------------------------------------ provisionamento: render */
  function renderTasks() {
    $("#tasks").innerHTML = STEPS.map((s) => `
      <div class="task st-wait" data-key="${s.key}">
        <label class="check"><input type="checkbox" checked aria-label="${esc(s.title)}">${icon("i-check")}</label>
        ${icon(s.icon, "ic tk-ic")}
        <div><h4>${esc(s.title)}</h4><p>${esc(s.desc)}</p></div>
        <div class="st st-wait">
          <span class="st-ic wait">${icon("i-clock")}</span>
          <b>${STATUS_TXT.wait}<span class="pct"></span></b>
          <span class="st-text">Aguardando o início.</span>
        </div>
      </div>`).join("");
    $$(".task input").forEach((inp) => inp.addEventListener("change", () => {
      inp.closest(".task").classList.toggle("off", !inp.checked);
      updateOverall();
    }));
    STEPS.forEach((s) => (S.steps[s.key] = { status: "wait", pct: 0 }));
  }

  function setStep(key, status, pct, text) {
    const st = S.steps[key];
    if (!st) return;
    st.status = status;
    if (pct >= 0) st.pct = pct;
    const row = $(`.task[data-key="${key}"]`);
    if (!row) return;
    row.className = `task st-${status}` + ($("input", row).checked ? "" : " off");
    const box = $(".st", row);
    box.className = `st st-${status}`;
    const icMap = { ok: ["ok", "i-check"], err: ["err", "i-alert"], run: ["run", ""], wait: ["wait", "i-clock"], skip: ["wait", "i-clock"] };
    const [cls, ic] = icMap[status] || icMap.wait;
    const bar = status === "run"
      ? `<div class="bar ${pct < 0 ? "indet" : ""}"><i style="width:${Math.max(0, pct)}%"></i></div>` : "";
    box.innerHTML = `<span class="st-ic ${cls}">${ic ? icon(ic) : ""}</span>
      <b>${STATUS_TXT[status] || status}<span class="pct">${status === "run" && pct >= 0 ? Math.round(pct) + "%" : ""}</span></b>
      ${bar}<span class="st-text" title="${esc(text)}">${esc(text || "")}</span>`;
    updateOverall();
  }

  function selectedSteps() {
    const o = {};
    $$(".task").forEach((t) => (o[t.dataset.key] = $("input", t).checked));
    return o;
  }

  function updateOverall() {
    const sel = selectedSteps();
    const keys = STEPS.filter((s) => sel[s.key]).map((s) => s.key);
    let total = 0;
    keys.forEach((k) => {
      const st = S.steps[k];
      total += st.status === "ok" || st.status === "err" ? 100 : st.status === "run" ? Math.max(5, st.pct || 0) : 0;
    });
    const pct = keys.length ? Math.round(total / keys.length) : 0;
    $("#overall-ring").style.setProperty("--p", pct);
    $("#overall-pct").textContent = pct + "%";
    $("#overall-bar").style.width = pct + "%";
    const running = STEPS.find((s) => S.steps[s.key].status === "run");
    if (running) $("#overall-label").textContent = "Provisionamento em andamento…";

    // fases
    let activePhase = -1;
    const items = PHASES.map((name, i) => {
      const ks = STEPS.filter((s) => s.phase === i && sel[s.key]).map((s) => s.key);
      if (!ks.length) return { name, pct: null, state: "" };
      let p = 0, anyRun = false, allDone = true;
      ks.forEach((k) => {
        const st = S.steps[k];
        p += st.status === "ok" || st.status === "err" ? 100 : st.status === "run" ? Math.max(3, st.pct || 0) : 0;
        anyRun = anyRun || st.status === "run";
        allDone = allDone && (st.status === "ok" || st.status === "err");
      });
      p = Math.round(p / ks.length);
      if (anyRun && activePhase < 0) activePhase = i;
      return { name, pct: p, state: allDone ? "ok" : anyRun || p > 0 ? "run" : "" };
    });
    $("#phase-list").innerHTML = items.map((it) => `
      <li><span class="pdot ${it.state}">${it.state === "ok" ? icon("i-check") : ""}</span>
      <span>${esc(it.name)}</span><em class="${it.state}">${it.pct === null ? "—" : it.pct + "%"}</em></li>`).join("");
    $$("#stepper li").forEach((li, i) => {
      li.classList.toggle("done", items[i].state === "ok");
      li.classList.toggle("active", items[i].state === "run" || (activePhase < 0 && !S.busy && i === 0 && pct === 0));
    });
  }

  function setBusy(b) {
    S.busy = b;
    $("#prov-start").disabled = b;
    $("#prov-cancel").hidden = !b;
    $$(".task input").forEach((i) => (i.disabled = b));
    $("#apps-install").disabled = b;
    $("#drv-vendor-btn").disabled = b;
    $("#drv-wu-btn").disabled = b;
    $$("[data-offline]").forEach((x) => (x.disabled = b));
  }

  function startProvision() {
    const steps = selectedSteps();
    if (!Object.values(steps).some(Boolean)) return toast("Marque pelo menos uma etapa.");
    STEPS.forEach((s) => setStep(s.key, steps[s.key] ? "wait" : "skip", 0, steps[s.key] ? "Na fila" : "Não selecionado"));
    const opts = {
      steps, apps: [...S.appsSel],
      include_bios: $("#opt-bios").checked, auto_reboot: $("#opt-autoreboot").checked,
      reboot_end: $("#opt-endreboot").checked, power_button: true, no_sleep: true,
      trust_cert: $("#opt-trust").checked,
    };
    api.start_provision(opts).then((r) => {
      if (!r.ok) return toast(r.msg);
      setBusy(true);
      $("#overall-label").textContent = "Provisionamento em andamento…";
    });
  }
  $("#prov-start").addEventListener("click", startProvision);
  $("#prov-cancel").addEventListener("click", () => api.cancel());

  /* ------------------------------------------------------------------ eventos do Python */
  const LOG_MAX = 600;
  function addLog(ev) {
    const li = `<li class="${esc(ev.level)}"><time>${esc(ev.ts)}</time><span>${esc(ev.msg)}</span></li>`;
    for (const id of ["#mini-log", "#full-log"]) {
      const el = $(id);
      const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
      el.insertAdjacentHTML("beforeend", li);
      while (el.children.length > LOG_MAX) el.firstElementChild.remove();
      if (stick) el.scrollTop = el.scrollHeight;
    }
  }
  function outLine(ch, line) {
    const el = $("#out-" + ch);
    if (!el) return;
    if (el.dataset.fresh !== "1") { el.textContent = ""; el.dataset.fresh = "1"; }
    el.textContent += line + "\n";
    el.scrollTop = el.scrollHeight;
  }

  function onEvent(ev) {
    switch (ev.t) {
      case "log": addLog(ev); break;
      case "step": setStep(ev.key, ev.status, ev.pct, ev.text); break;
      case "done":
        setBusy(false);
        $("#overall-label").textContent = ev.msg;
        toast(ev.msg);
        break;
      case "app": setApp(ev.key, ev.status, ev.pct, ev.text); break;
      case "apps_done": setBusy(false); toast(ev.ok ? "Aplicativos instalados." : "Alguns aplicativos falharam — veja o log."); refreshAppStatus(); break;
      case "drv": setDrv(ev); break;
      case "out": outLine(ev.ch, ev.line); break;
      case "clean_done": cleanDone(ev); break;
      case "rep": setRepair(ev.key, ev.status, ev.pct); break;
      case "rep_done": $("#repair-run").disabled = false; toast("Reparo finalizado — reinicie para aplicar as correções."); break;
      case "reboot_prompt": showModal("Reiniciar agora?", ev.msg + "\n\nO programa abre sozinho depois e continua de onde parou."); break;
      case "countdown": countdown(ev); break;
      case "upd": onUpdateEvent(ev); break;
      case "pack": setPack(ev); break;
      case "pack_done": setBusy(false); loadPack(); break;
      case "seal_checked": loadSeal(); break;
      case "seal_err": $("#seal-run").disabled = false; outLine("seal", "✘ " + ev.msg); showModal("O Sysprep não selou a máquina", ev.msg, () => {}, "Entendi"); break;
    }
  }
  window.PT = { onEvents: (list) => list.forEach((ev) => { try { onEvent(ev); } catch (e) { console.error(e); } }) };

  function countdown(ev) {
    const box = $("#countdown");
    if (ev.s <= 0) { $("#cd-msg").textContent = ev.msg; setTimeout(() => (box.hidden = true), 2500); return; }
    box.hidden = false;
    $("#cd-msg").textContent = ev.msg;
    $("#cd-s").textContent = ev.s;
  }
  $("#cd-cancel").addEventListener("click", () => api.cancel_reboot());

  let modalYes = null;
  function showModal(title, text, onYes = () => api.reboot_now(), yesLabel = "Reiniciar agora") {
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text;
    $("#modal-yes").textContent = yesLabel;
    modalYes = onYes;
    const d = $("#modal");
    if (!d.open) d.showModal();
  }
  $("#modal-no").addEventListener("click", () => $("#modal").close());
  $("#modal-yes").addEventListener("click", () => { $("#modal").close(); if (modalYes) modalYes(); });

  /* ------------------------------------------------------------------ hardware */
  function fmtUptime(sec) {
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60), x = sec % 60;
    const p2 = (n) => String(n).padStart(2, "0");
    return (d ? d + (d === 1 ? " dia " : " dias ") : "") + `${p2(h)}h ${p2(m)}min ${p2(x)}s`;
  }
  let upBase = null;
  function tickUptime() {
    if (!upBase) return;
    const sec = upBase.sec + Math.floor((Date.now() - upBase.at) / 1000);
    $("#uptime").textContent = fmtUptime(sec);
  }
  setInterval(tickUptime, 1000);   // conta os segundos ao vivo

  function kv(pairs) {
    return pairs.filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `<div><dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd></div>`).join("");
  }
  const okChip = (s) => {
    const l = String(s || "").toLowerCase();
    if (!l || l === "ok" || l === "healthy") return `<span class="chip ok">OK</span>`;
    if (l === "warning") return `<span class="chip warn">Atenção</span>`;
    if (l === "unhealthy") return `<span class="chip bad">Em risco</span>`;
    return `<span class="chip warn">${esc(s)}</span>`;
  };

  function renderInventory(inv) {
    S.inv = inv;
    if (inv.error) {
      $("#health-title").textContent = "Não foi possível ler o hardware";
      $("#health-sub").textContent = inv.error;
      return;
    }
    const v = inv.vendor || "other";
    // topo
    $("#m-host").textContent = inv.hostname;
    $("#m-os").textContent = `${(inv.os.caption || "Windows").replace("Microsoft ", "")} ${inv.os.display || ""}`;
    $("#m-cpu").textContent = `${shortCpu(inv.cpu.name)} · ${Math.round(inv.ram.total / GB)} GB RAM`;
    $("#m-serial-label").textContent = inv.serial_label;
    $("#m-serial").textContent = inv.serial || "não informado";

    // saúde
    const h = inv.health;
    const ring = $("#health-ring");
    ring.style.setProperty("--p", h.score);
    ring.className = "ring big state-" + (h.bads ? "bad" : h.warns ? "warn" : "ok");
    $("#health-score").textContent = h.score;
    $("#health-title").textContent = h.title;
    $("#health-sub").textContent = h.subtitle;
    $("#health-items").innerHTML = h.items.map((i) =>
      `<li class="${i.status}"><i></i><b>${esc(i.label)}</b><span>${esc(i.detail)}</span></li>`).join("");

    // uptime
    upBase = { sec: inv.os.uptime, at: Date.now() };
    tickUptime();
    const boot = inv.os.boot ? new Date(inv.os.boot) : null;
    $("#uptime-since").textContent = boot ? "Ligado desde " + boot.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : "";
    $("#uptime-hint").hidden = inv.fast_startup !== 1;

    // identificação
    for (const id of ["#vendor-badge", "#drv-vendor"]) {
      const b = $(id);
      b.className = "vendor-badge " + v + (id === "#drv-vendor" ? " xl" : "");
      b.textContent = VENDOR_SHORT[v];
    }
    $("#hw-model").textContent = inv.model || "Modelo não informado";
    $("#hw-maker").textContent = `${inv.manufacturer}${inv.sku ? " · SKU " + inv.sku : ""}`;
    $("#tag-label").textContent = inv.serial_label;
    $("#tag-value").textContent = inv.serial || "Não gravado na BIOS";
    $("#tag-extra").textContent = [inv.express_code ? "Express Service Code: " + inv.express_code : "",
      inv.serial_source ? "lido de: " + inv.serial_source : ""].filter(Boolean).join("   ");
    $("#ident-kv").innerHTML = kv([
      ["Patrimônio (Asset Tag)", inv.asset_tag || "—"], ["Computador", inv.hostname], ["Domínio", inv.domain],
      ["Sistema", `${inv.os.caption.replace("Microsoft ", "")} ${inv.os.display}`], ["Build", inv.os.build],
      ["Instalado em", inv.os.installed],
    ]);
    const sb = $("#support-btn");
    sb.hidden = !inv.support_url;
    sb.onclick = () => api.open_url(inv.support_url);

    // processador
    const c = inv.cpu;
    $("#c-cpu").innerHTML = `<div class="lead">${esc(c.name)}</div>
      <div class="facts"><span class="chip">${c.cores} núcleos</span><span class="chip">${c.threads} threads</span>
      <span class="chip">${(c.clock / 1000).toFixed(2).replace(".", ",")} GHz</span>${c.socket ? `<span class="chip">${esc(c.socket)}</span>` : ""}${okChip(c.status)}</div>
      <div class="live-row"><span>Uso agora</span><div class="bar"><i id="live-cpu-bar"></i></div><b id="live-cpu">—</b></div>`;

    // placa-mãe
    const bd = inv.board;
    $("#c-board").innerHTML = `<div class="lead">${esc([bd.maker, bd.product].filter(Boolean).join(" ") || "Não identificada")}</div>
      <dl class="kv">${kv([["Versão da placa", bd.version], ["Serial da placa", bd.serial], ["BIOS", inv.bios.version],
        ["Data da BIOS", inv.bios.date], ["Fabricante da BIOS", inv.bios.maker]])}</dl>`;

    // memória
    const r = inv.ram;
    const sticks = r.sticks || [];
    const slots = Math.max(r.slots || 0, sticks.length);
    const type = sticks.find((s) => s.type)?.type || "";
    const slotHtml = [];
    for (let i = 0; i < slots; i++) {
      const s = sticks[i];
      slotHtml.push(s
        ? `<div class="slot"><b>${Math.round(s.size / GB)} GB</b><div class="stick"></div>
            <small>${esc(s.slot || "Slot " + (i + 1))}</small><small>${esc([s.type, s.speed ? s.speed + " MHz" : "", s.form].filter(Boolean).join(" · "))}</small>
            <small title="${esc(s.part)}">${esc([s.maker, s.part].filter(Boolean).join(" "))}</small></div>`
        : `<div class="slot empty"><b>Vazio</b><div class="stick"></div><small>Slot ${i + 1}</small><small>disponível para expansão</small></div>`);
    }
    $("#c-ram").innerHTML = `<div class="lead">${Math.round((r.installed || r.total) / GB)} GB ${esc(type)}</div>
      <div class="facts"><span class="chip">${sticks.length} pente(s) instalado(s)</span><span class="chip">${slots} slot(s) no total</span>
      ${slots > sticks.length ? `<span class="chip ok">${slots - sticks.length} slot(s) livre(s)</span>` : ""}</div>
      <div class="slots">${slotHtml.join("")}</div>
      <div class="live-row"><span>Em uso</span><div class="bar"><i id="live-mem-bar"></i></div><b id="live-mem">—</b></div>`;

    // áudio / vídeo
    $("#c-audio").innerHTML = inv.audio.length ? `<ul class="list">${inv.audio.map((a) =>
      `<li><span class="grow" title="${esc(a.name)}">${esc(a.name)}</span>${okChip(a.status)}</li>`).join("")}</ul>`
      : `<p class="muted">Nenhuma placa de áudio detectada — verifique os drivers.</p>`;
    $("#c-gpu").innerHTML = inv.gpus.length ? `<ul class="list">${inv.gpus.map((g) =>
      `<li><div class="grow"><div title="${esc(g.name)}">${esc(g.name)}</div><div class="sub">${g.mem ? fmtGB(g.mem, 0) + " · " : ""}driver ${esc(g.driver)}</div></div>${okChip(g.status)}</li>`).join("")}</ul>`
      : `<p class="muted">Nenhuma placa de vídeo detectada.</p>`;

    // discos
    const diskHtml = inv.disks.map((d) => `<li><div class="grow"><div title="${esc(d.model)}">${esc(d.model)}</div>
      <div class="sub">${fmtSize(d.size)} · ${esc(d.media || "")} ${esc(d.bus || "")}${d.temp ? " · " + d.temp + " °C" : ""}${d.wear != null ? " · desgaste " + d.wear + "%" : ""}${d.hours ? " · " + d.hours + " h ligado" : ""}</div></div>${okChip(d.health)}</li>`).join("");
    const volHtml = inv.volumes.map((vol) => {
      const used = vol.total ? 100 * (1 - vol.free / vol.total) : 0;
      const cls = used > 90 ? "bad" : used > 80 ? "warn" : "";
      return `<div class="vol"><div class="vol-top"><b>${esc(vol.letter)} ${esc(vol.label)}</b><span class="muted">${fmtGB(vol.free)} livres de ${fmtGB(vol.total)}</span></div>
        <div class="bar ${cls}"><i style="width:${used.toFixed(0)}%"></i></div></div>`;
    }).join("");
    $("#c-disk").innerHTML = `<ul class="list">${diskHtml}</ul>${volHtml}`;

    // rede
    $("#c-nic").innerHTML = inv.nics.length ? `<ul class="list">${inv.nics.map((n) =>
      `<li><span class="chip">${esc(n.kind)}</span><div class="grow"><div title="${esc(n.name)}">${esc(n.name)}</div><div class="sub mono">${esc(n.mac)}</div></div>${n.up ? `<span class="chip ok">Conectada</span>` : ""}</li>`).join("")}</ul>`
      : `<p class="muted">Nenhuma placa de rede física detectada — drivers ausentes?</p>`;

    // bateria / segurança
    const b = inv.battery;
    const sec = `<dl class="kv" style="margin-top:12px">${kv([
      ["Secure Boot", inv.secure_boot === null ? "Não suportado / Legacy" : inv.secure_boot ? "Ativado" : "Desativado"],
      ["TPM", inv.tpm ? "Versão " + inv.tpm : "Não encontrado"],
      ["Inicialização rápida", inv.fast_startup === 0 ? "Desativada" : inv.fast_startup === 1 ? "Ativada" : "—"],
    ])}</dl>`;
    $("#c-bat").innerHTML = (b ? `<div class="lead">${b.health ? "Saúde " + b.health + "%" : "Bateria presente"}</div>
      <div class="facts"><span class="chip">Carga ${b.charge}%</span>${b.design ? `<span class="chip">Capacidade ${Math.round(b.full / 1000)} de ${Math.round(b.design / 1000)} Wh</span>` : ""}</div>`
      : `<div class="lead">Sem bateria (desktop)</div>`) + sec;

    renderDriversInfo(inv);
    renderVendorMini(inv);
  }

  function shortCpu(n) {
    return String(n || "").replace(/\(R\)|\(TM\)|CPU|Processor|\d+th Gen|with Radeon.*$/gi, "").replace(/@.*$/, "").replace(/\s+/g, " ").trim();
  }

  function renderVendorMini(inv) {
    const v = inv.vendor || "other";
    $("#vendor-mini").innerHTML = `<div class="vendor-badge ${v}">${VENDOR_SHORT[v]}</div>
      <div><b>${esc(inv.vendor_label)} · ${esc(inv.model)}</b><span>${esc(VENDOR_TOOL[v].split(" — ")[0].split(":")[0])}</span></div>`;
  }

  function loadInventory(refresh) {
    $("#hw-refresh").disabled = true;
    if (refresh) { $("#health-title").textContent = "Analisando o equipamento…"; }
    return api.inventory(!!refresh).then((inv) => { renderInventory(inv); $("#hw-refresh").disabled = false; });
  }
  $("#hw-refresh").addEventListener("click", () => loadInventory(true));
  $("#hw-report").addEventListener("click", () => api.save_report().then((p) => p && toast("Relatório salvo: " + p)));
  $("#tag-copy").addEventListener("click", () => {
    const t = S.inv?.serial;
    if (!t) return;
    api.copy_text(t).then(() => toast(S.inv.serial_label + " copiada: " + t));
  });
  $("#restart-btn").addEventListener("click", () => showModal("Reiniciar o computador?", "Salve o que estiver aberto antes de continuar."));

  function pollLive() {
    api.live().then((m) => {
      if (!m || m.error) return;
      const set = (id, pct, label) => {
        const bar = $("#" + id + "-bar"), txt = $("#" + id);
        if (bar) { bar.style.width = pct + "%"; bar.parentElement.className = "bar" + (pct > 90 ? " bad" : pct > 75 ? " warn" : ""); }
        if (txt) txt.textContent = label;
      };
      set("live-cpu", m.cpu, Math.round(m.cpu) + "%");
      set("live-mem", m.mem_pct, Math.round(m.mem_pct) + "%");
      if (m.uptime) {
        const now = upBase ? upBase.sec + Math.floor((Date.now() - upBase.at) / 1000) : null;
        if (now === null || Math.abs(now - m.uptime) > 5) upBase = { sec: m.uptime, at: Date.now() };
      }
      tickUptime();
    }).catch(() => {});
  }

  /* ------------------------------------------------------------------ drivers */
  function renderDriversInfo(inv) {
    const v = inv.vendor || "other";
    $("#drv-model").textContent = `${inv.vendor_label} ${inv.model || ""}`.trim();
    $("#drv-tool").textContent = VENDOR_TOOL[v];
    $("#drv-vendor-btn").innerHTML = icon("i-download") + (v === "positivo" || v === "other"
      ? "Buscar drivers (Windows Update)" : `Buscar drivers ${esc(inv.vendor_label)}`);
    const p = inv.problems || [];
    $("#problems").innerHTML = p.length
      ? p.map((x) => `<li><b title="${esc(x.id)}">${esc(x.name)}</b><span>${esc(x.reason)}</span></li>`).join("")
      : `<li class="okline">${icon("i-check-circle")} Todos os dispositivos estão com driver.</li>`;
    $$(".row-item[data-vendor]").forEach((r) => {
      const rec = $(".rec", r);
      if (rec) rec.hidden = r.dataset.vendor !== v;
    });
  }

  function renderOffline() {
    const colors = { dell: "#0B6FBF", hp: "#0096D6", lenovo: "#D9262B" };
    $("#offline-list").innerHTML = S.boot.offline.map((p) => `
      <div class="row-item" data-vendor="${p.vendor}" data-key="${p.key}">
        <span></span>
        <div class="tile" style="background:${colors[p.vendor] || "#3A4F6A"}">${icon("i-box")}</div>
        <div><h4>${esc(p.title)} <span class="badge ok rec" hidden>Recomendado para esta máquina</span></h4><p>${esc(p.desc)}</p></div>
        <div class="st-col"><span class="off-text">${p.available ? "Pronto para instalar" : "Arquivos não encontrados em Recursos"}</span></div>
        <button class="btn ghost sm" data-offline="${p.key}" ${p.available ? "" : "disabled"}>Instalar</button>
      </div>`).join("");
    $$("[data-offline]").forEach((b) => b.addEventListener("click", () => runDrivers("offline:" + b.dataset.offline)));
  }

  function runDrivers(mode) {
    api.run_drivers(mode, $("#drv-bios").checked).then((r) => {
      if (!r.ok) return toast(r.msg);
      S.drvMode = mode;
      setBusy(true);
    });
  }
  $("#drv-vendor-btn").addEventListener("click", () => runDrivers("vendor"));
  $("#drv-wu-btn").addEventListener("click", () => runDrivers("wu"));

  function setDrv(ev) {
    if (ev.mode.startsWith("offline:")) {
      const row = $(`.row-item[data-key="${ev.mode.slice(8)}"] .off-text`);
      if (row) row.innerHTML = `<span class="badge ${ev.status}">${STATUS_TXT[ev.status]}</span> ${esc(ev.text)}`;
    } else {
      $("#drv-job").hidden = false;
      const badge = $("#drv-badge");
      badge.className = "badge " + ev.status;
      badge.textContent = STATUS_TXT[ev.status];
      $("#drv-text").textContent = ev.text;
      const bar = $("#drv-bar");
      bar.parentElement.className = "bar" + (ev.pct < 0 ? " indet" : ev.status === "err" ? " bad" : "");
      bar.style.width = Math.max(0, ev.pct) + "%";
    }
    if (ev.status !== "run") { setBusy(false); setTimeout(() => loadInventory(true), 1500); }
  }

  /* ------------------------------------------------------------------ aplicativos */
  const APP_COLORS = { office: "#D24726", chrome: "#1A73E8", anydesk: "#EF443B", winrar: "#7A3FB8", firefox: "#E66000",
    brave: "#FB542B", disk: "#2E7D32", cpu: "#4A5D8F", gpu: "#76881D", toys: "#3A6EA5" };
  function renderApps() {
    $("#app-grid").innerHTML = S.boot.programs.map((p) => {
      if (p.default && p.available) S.appsSel.add(p.key);
      return `<div class="app st-wait ${p.available ? "" : "missing"}" data-key="${p.key}">
        <label class="check"><input type="checkbox" ${S.appsSel.has(p.key) ? "checked" : ""} ${p.available ? "" : "disabled"} aria-label="${esc(p.title)}">${icon("i-check")}</label>
        <div class="tile" style="background:${APP_COLORS[p.icon] || "#3A4F6A"}">${esc(p.title.replace("Microsoft ", "").slice(0, 2))}</div>
        <div><h4>${esc(p.title)} <span class="inst"></span></h4><p>${esc(p.desc)}${p.note ? " · " + esc(p.note) : ""}</p></div>
        <div class="app-st"><span class="app-text">${p.available ? "" : "Instalador não encontrado em Recursos\\Programas"}</span></div>
      </div>`;
    }).join("");
    api.app_icons().then((icons) => Object.entries(icons || {}).forEach(([k, src]) => {
      const tile = $(`.app[data-key="${k}"] .tile`);
      if (tile) { tile.classList.add("has-icon"); tile.innerHTML = `<img src="${src}" alt="">`; }
    })).catch(() => {});
    $$("#app-grid .app input").forEach((inp) => inp.addEventListener("change", () => {
      const k = inp.closest(".app").dataset.key;
      inp.checked ? S.appsSel.add(k) : S.appsSel.delete(k);
      syncAppsToggle();
    }));
    syncAppsToggle();
  }
  function syncAppsToggle() {
    const avail = $$("#app-grid .app input:not(:disabled)");
    $("#apps-toggle").textContent = avail.every((i) => i.checked) ? "Desmarcar todos" : "Marcar todos";
  }
  $("#apps-toggle").addEventListener("click", () => {
    const avail = $$("#app-grid .app input:not(:disabled)");
    const all = avail.every((i) => i.checked);
    avail.forEach((i) => { i.checked = !all; i.dispatchEvent(new Event("change")); });
  });
  $("#apps-install").addEventListener("click", () => {
    if (!S.appsSel.size) return toast("Marque pelo menos um aplicativo.");
    api.install_programs([...S.appsSel]).then((r) => (r.ok ? setBusy(true) : toast(r.msg)));
  });
  function setApp(key, status, pct, text) {
    const el = $(`.app[data-key="${key}"]`);
    if (!el) return;
    el.className = `app st-${status}`;
    $(".app-st", el).innerHTML = `<span><span class="badge ${status}">${STATUS_TXT[status]}</span> ${esc(text)}</span>
      ${status === "run" ? `<div class="bar ${pct < 0 ? "indet" : ""}"><i style="width:${Math.max(0, pct)}%"></i></div>` : ""}`;
  }
  function refreshAppStatus() {
    api.program_status().then((st) => Object.entries(st).forEach(([k, inst]) => {
      const el = $(`.app[data-key="${k}"] .inst`);
      if (el) el.innerHTML = inst ? `<span class="badge ok">Instalado</span>` : "";
    }));
  }

  /* ------------------------------------------------------------------ rede */
  function loadNetwork() {
    S.netLoaded = true;
    $("#net-refresh").disabled = true;
    api.network().then((n) => {
      $("#net-refresh").disabled = false;
      if (n.error) { $("#net-status").innerHTML = `<span class="badge err">Erro</span> ${esc(n.error)}`; return; }
      S.net = n;
      const online = /Internet/i.test(n.internet);
      $("#net-status").innerHTML = `<div class="vendor-badge" style="background:${online ? "var(--ok)" : "var(--warn)"}">${icon("i-wifi")}</div>
        <div><div class="big">${online ? "Conectado à internet" : n.internet === "Disconnected" ? "Sem conexão" : "Rede local, sem internet"}</div>
        <span class="muted">${esc(n.profile || "Nenhuma rede identificada")}</span></div>`;
      $("#net-grid").innerHTML = n.adapters.map((a) => `
        <div class="panel net-card ${a.status === "Up" ? "" : "down"}">
          <h3>${icon(a.kind === "Wi-Fi" ? "i-wifi" : "i-link")} ${esc(a.name)} <span class="badge ${a.status === "Up" ? "ok" : ""}">${a.status === "Up" ? "Conectado" : "Desconectado"}</span></h3>
          <p class="desc">${esc(a.desc)}</p>
          <dl class="kv">${kv([["Endereço IP", a.ip || "—"], ["Gateway", a.gateway || "—"], ["DNS", a.dns || "—"],
            ["MAC", a.mac], ["Velocidade", a.speed], ["DHCP", a.dhcp === "Enabled" ? "Automático" : a.dhcp === "Disabled" ? "IP fixo" : a.dhcp],
            ["Wi-Fi (SSID)", a.ssid]])}</dl>
        </div>`).join("");
    });
  }
  $("#net-refresh").addEventListener("click", loadNetwork);
  $("#net-test").addEventListener("click", () => {
    const gw = (S.net?.adapters || []).find((a) => a.gateway)?.gateway || "";
    $("#out-net").dataset.fresh = "0";
    $("#net-test").disabled = true;
    api.connectivity(gw).then(() => ($("#net-test").disabled = false));
  });

  /* ------------------------------------------------------------------ limpeza */
  $$("[data-clean]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.clean === "bin" && !confirm("Esvaziar a Lixeira definitivamente?")) return;
    api.maintenance(b.dataset.clean).then((r) => {
      if (!r.ok) return toast(r.msg);
      $$("[data-clean]").forEach((x) => (x.disabled = true));
      $("#out-clean").dataset.fresh = "0";
    });
  }));
  function cleanDone(ev) {
    $$("[data-clean]").forEach((x) => (x.disabled = false));
    $("#free-space").textContent = ev.free;
    const n = parseFloat(String(ev.freed).replace(",", "."));
    const mult = { B: 1, KB: 1024, MB: 1024 ** 2, GB: GB, TB: 1024 ** 4 }[String(ev.freed).split(" ")[1]] || 1;
    S.freed += n * mult;
    $("#freed-space").textContent = S.freed >= GB ? fmtGB(S.freed) : Math.round(S.freed / 1024 ** 2) + " MB";
  }

  /* ------------------------------------------------------------------ reparo */
  function renderRepair() {
    $("#repair-list").innerHTML = S.boot.repair.map((r, i) => `
      <div class="row-item" data-rep="${r.key}">
        <label class="check"><input type="checkbox" checked aria-label="${esc(r.title)}">${icon("i-check")}</label>
        <div class="tile" style="background:var(--accent)">${i + 1}</div>
        <div><h4>${esc(r.title)}</h4><p>${esc(r.desc)}</p></div>
        <div class="st-col rep-st"><span class="badge">Aguardando</span></div><span></span>
      </div>`).join("");
  }
  $("#repair-run").addEventListener("click", () => {
    const keys = $$("[data-rep]").filter((r) => $("input", r).checked).map((r) => r.dataset.rep);
    if (!keys.length) return toast("Marque pelo menos uma etapa.");
    api.repair(keys).then((r) => {
      if (!r.ok) return toast(r.msg);
      $("#repair-run").disabled = true;
      $("#out-repair").dataset.fresh = "0";
    });
  });
  function setRepair(key, status, pct) {
    const el = $(`[data-rep="${key}"] .rep-st`);
    if (!el) return;
    el.innerHTML = `<span class="badge ${status}">${STATUS_TXT[status]}${status === "run" && pct >= 0 ? " " + Math.round(pct) + "%" : ""}</span>
      ${status === "run" ? `<div class="bar ${pct < 0 ? "indet" : ""}"><i style="width:${Math.max(0, pct)}%"></i></div>` : ""}`;
  }

  /* ------------------------------------------------------------------ temperatura */
  const GAUGES = [
    { key: "cpu", title: "Processador", t: "cpu_t", l: "cpu_l" },
    { key: "gpu", title: "Placa de vídeo", t: "gpu_t", l: "gpu_l" },
    { key: "board", title: "Placa-mãe", t: "board_t" },
    { key: "mem", title: "Memória", l: "mem_l", loadOnly: true },
  ];
  function renderGauges() {
    $("#gauges").innerHTML = GAUGES.map((g) => `<div class="panel gauge" id="g-${g.key}">
      <div class="ring" style="--p:0"><span>—</span></div><h3>${g.title}</h3><p>Aguardando sensor…</p></div>`).join("");
  }
  function applySensors(d) {
    if (!d || d.waiting) { $("#temp-status").textContent = d?.error || "Iniciando leitura dos sensores…"; return; }
    if (d.error && !d.ts) { $("#temp-status").textContent = d.error; return; }
    $("#temp-status").textContent = `Atualizado às ${d.ts} — a cada 2 s · vermelho a partir de 85 °C`;
    GAUGES.forEach((g) => {
      const el = $("#g-" + g.key), ring = $(".ring", el);
      const t = g.t ? d[g.t] : null, l = g.l ? d[g.l] : null;
      if (g.loadOnly) {
        ring.style.setProperty("--p", l ?? 0);
        ring.className = "ring" + (l > 90 ? " state-bad" : l > 75 ? " state-warn" : "");
        $("span", ring).textContent = l == null ? "—" : l + "%";
        $("p", el).textContent = "Uso de memória";
        return;
      }
      ring.style.setProperty("--p", t == null ? 0 : Math.min(100, t));
      ring.className = "ring " + (t == null ? "" : t >= 85 ? "state-bad" : t >= 70 ? "state-warn" : "state-ok");
      $("span", ring).textContent = t == null ? "—" : Math.round(t) + "°";
      $("p", el).textContent = t == null ? "Sem sensor disponível" : (l != null ? "Uso " + l + "%" : "Temperatura") + (g.key === "cpu" && d.cpu_p ? ` · ${d.cpu_p} W` : "");
    });
    $("#t-disks").innerHTML = d.disks?.length ? `<ul class="list">${d.disks.map((x) =>
      `<li><span class="grow">${esc(x.n)}</span><span class="chip ${x.t >= 60 ? "bad" : x.t >= 50 ? "warn" : "ok"}">${x.t} °C</span></li>`).join("")}</ul>` : "Sem leitura de temperatura dos discos.";
    $("#t-fans").innerHTML = d.fans?.length ? `<ul class="list">${d.fans.map((x) =>
      `<li><span class="grow">${esc(x.n)}</span><span class="chip">${x.r} RPM</span></li>`).join("")}</ul>` : "Nenhuma ventoinha com sensor de rotação (comum em notebooks).";
  }
  function startSensors() {
    if (S.sensorsTimer) return;
    const tick = () => api.sensors().then(applySensors).catch(() => {});
    tick();
    S.sensorsTimer = setInterval(tick, 2000);
  }
  function stopSensors() { clearInterval(S.sensorsTimer); S.sensorsTimer = null; }


  /* ------------------------------------------------------------------ atualização automática (GitHub) */
  const U = { timer: null, info: null };
  function showUpdate(title, sub, { buttons = true, progress = null } = {}) {
    $("#update-bar").hidden = false;
    $("#ub-title").textContent = title;
    $("#ub-sub").textContent = sub || "";
    $("#ub-now").hidden = $("#ub-later").hidden = !buttons;
    $("#ub-bar").hidden = progress === null;
    if (progress !== null) $("#ub-fill").style.width = progress + "%";
  }
  function installUpdate() {
    clearInterval(U.timer);
    api.update_install().then((r) => {
      if (!r.ok) { showUpdate("Não foi possível atualizar", r.msg); $("#ub-now").hidden = true; return; }
      showUpdate(`Baixando a versão ${r.version}…`, "O programa fecha e abre sozinho na versão nova.", { buttons: false, progress: 0 });
    });
  }
  function checkUpdates(manual) {
    api.update_check().then((u) => {
      U.info = u;
      if (!u.available) {
        if (manual) {
          if (u.error) showUpdate("Não foi possível verificar", u.error);
          else showUpdate("Programa atualizado", `Você já está na versão mais recente (v${S.boot.version}).`);
          $("#ub-now").hidden = true; $("#ub-later").hidden = false; $("#ub-later").textContent = "Fechar";
        }
        return;
      }
      $("#ub-later").textContent = "Depois";
      if (!u.can_install) { showUpdate(`Versão ${u.version} disponível`, "Baixe o .exe novo na página de releases do GitHub."); $("#ub-now").hidden = true; return; }
      if (u.busy || S.busy) {
        showUpdate(`Versão ${u.version} disponível`, "Será instalada quando você clicar em Atualizar agora (há uma tarefa em andamento).");
        return;
      }
      let n = manual ? 0 : 15;
      if (!n) return installUpdate();
      const tick = () => {
        showUpdate(`Versão ${u.version} disponível`, `Atualizando sozinho em ${n} s…`);
        if (n-- <= 0) installUpdate();
      };
      tick();
      U.timer = setInterval(tick, 1000);
    }).catch(() => {});
  }
  $("#ub-now").addEventListener("click", installUpdate);
  $("#ub-later").addEventListener("click", () => { clearInterval(U.timer); $("#update-bar").hidden = true; });
  $("#version").addEventListener("click", () => {
    showUpdate("Procurando atualização…", "Consultando a última versão publicada no GitHub.", { buttons: false });
    checkUpdates(true);
  });
  function onUpdateEvent(ev) {
    if (ev.status === "download") {
      showUpdate("Baixando atualização…", ev.pct < 0 ? "Baixando pelo proxy do Windows…" : Math.round(ev.pct) + "%", { buttons: false, progress: Math.max(0, ev.pct) });
      if (ev.pct < 0) $("#ub-bar").classList.add("indet"); else $("#ub-bar").classList.remove("indet");
    }
    else if (ev.status === "restart") showUpdate("Atualização pronta", "Reiniciando o programa na versão nova…", { buttons: false, progress: 100 });
    else if (ev.status === "error") { showUpdate("Falha na atualização", ev.msg); $("#ub-now").hidden = true; $("#ub-later").hidden = false; }
  }


  /* ------------------------------------------------------------------ pacote completo por modelo */
  const fmtBig = (b) => (b >= 1e9 ? (b / 1e9).toFixed(1).replace(".", ",") + " GB" : Math.round(b / 1e6) + " MB");
  function loadPack() {
    api.pack_status().then((p) => {
      if (!p || p.error) { $("#pack-kv").innerHTML = kv([["Situação", p?.error || "Indisponível"]]); return; }
      $("#pack-kv").innerHTML = kv([
        ["Código do modelo", p.model_id || "não identificado"],
        ["Situação", p.available ? `Pacote pronto: ${p.drivers} drivers (${fmtBig(p.size)})` : "Nenhum pacote salvo para este modelo"],
        ["Pasta", p.path], ["Pacote", (p.info || "").split("\n")[0]],
      ]);
      $("#pack-install").disabled = !p.available || S.busy;
      $("#pack-download").disabled = !p.downloadable || S.busy;
      $("#pack-download").title = p.downloadable ? "" : "Este fabricante não publica pacotes: copie os drivers para a pasta manualmente.";
    }).catch(() => {});
  }
  function setPack(ev) {
    $("#pack-job").hidden = false;
    const b = $("#pack-badge");
    b.className = "badge " + ev.status;
    b.textContent = STATUS_TXT[ev.status];
    $("#pack-text").textContent = ev.text || "";
    const bar = $("#pack-bar");
    bar.parentElement.className = "bar" + (ev.pct < 0 ? " indet" : ev.status === "err" ? " bad" : "");
    bar.style.width = Math.max(0, ev.pct) + "%";
  }
  function runPack(mode) {
    api.pack_run(mode).then((r) => {
      if (!r.ok) return toast(r.msg);
      setBusy(true);
      $("#pack-install").disabled = $("#pack-download").disabled = true;
      setPack({ status: "run", pct: -1, text: mode === "download" ? "Consultando o catálogo do fabricante…" : "Preparando…" });
    });
  }
  $("#pack-install").addEventListener("click", () => runPack("install"));
  $("#pack-download").addEventListener("click", () => runPack("download"));

  /* ------------------------------------------------------------------ finalizar e selar */
  function loadSeal() {
    $("#seal-items").innerHTML = '<li class="muted">Verificando a máquina…</li>';
    api.seal_check().then((r) => {
      if (r.error) { $("#seal-items").innerHTML = `<li class="bad"><i></i><b>Erro</b><span>${esc(r.error)}</span></li>`; return; }
      $("#seal-items").innerHTML = r.items.map((i) =>
        `<li class="${i.status}"><i></i><b>${esc(i.label)}</b><span>${esc(i.detail)}</span></li>`).join("");
      $("#seal-fix-appx").hidden = !r.items.some((i) => i.key === "appx" && i.status !== "ok");
      $("#seal-run").disabled = !r.can_seal;
      $("#seal-run").title = r.can_seal ? "" : "Reinicie a máquina antes de selar.";
    });
  }
  $("#seal-recheck").addEventListener("click", loadSeal);
  $("#seal-gui").addEventListener("click", () => api.seal_open_gui().then((r) => toast(r.msg)));
  $("#seal-fix-appx").addEventListener("click", () => { $("#out-seal").dataset.fresh = "0"; api.seal_fix_appx(); });
  $("#seal-generalize").addEventListener("change", (e) => ($("#seal-gen-hint").hidden = !e.target.checked));
  $("#seal-run").addEventListener("click", () => {
    const gen = $("#seal-generalize").checked;
    const action = $$('input[name="seal-action"]').find((x) => x.checked).value;
    showModal("Finalizar e selar esta máquina?",
      `O Windows vai sair do modo de auditoria${gen ? ", ser generalizado" : ""} e ${action === "reboot" ? "reiniciar" : "desligar"}.\n\nNo próximo ligar aparece a configuração inicial do Windows para o colaborador. Esta ação não pode ser desfeita pelo programa.`,
      () => {
        api.seal_run(gen, action).then((r) => {
          if (!r.ok) return toast(r.msg);
          $("#seal-run").disabled = true;
          $("#out-seal").dataset.fresh = "0";
        });
      }, "Selar agora");
  });

  /* ------------------------------------------------------------------ logs */
  $("#log-open").addEventListener("click", () => api.open_logs());
  $("#log-clear").addEventListener("click", () => { $("#full-log").innerHTML = ""; });

  /* ------------------------------------------------------------------ continuação após reiniciar */
  function handleResume(state) {
    const bar = $("#resume-bar");
    bar.hidden = false;
    const pend = (state.pending || []).map((k) => STEPS.find((s) => s.key === k)?.title).filter(Boolean);
    STEPS.forEach((s) => {
      const inp = $(`.task[data-key="${s.key}"] input`);
      inp.checked = true;
      if (!state.pending.includes(s.key)) setStep(s.key, "ok", 100, "Feito antes de reiniciar");
    });
    (state.opts?.apps || []).forEach((k) => S.appsSel.add(k));
    let n = 10;
    const tick = () => {
      $("#resume-text").textContent = `Faltam: ${pend.join(", ")}. Continua sozinho em ${n} s.`;
      if (n-- <= 0) { clearInterval(timer); resumeNow(); }
    };
    const timer = setInterval(tick, 1000);
    tick();
    const resumeNow = () => {
      clearInterval(timer);
      bar.hidden = true;
      api.resume_provision().then((r) => (r.ok ? setBusy(true) : toast(r.msg)));
    };
    $("#resume-now").onclick = resumeNow;
    $("#resume-cancel").onclick = () => { clearInterval(timer); bar.hidden = true; api.discard_resume(); toast("Continuação descartada."); };
  }

  /* ------------------------------------------------------------------ início */
  function init(theApi) {
    api = theApi;
    renderTasks();
    renderGauges();
    api.boot().then((b) => {
      S.boot = b;
      $("#version").textContent = "v" + b.version;
      $("#log-path").textContent = "Arquivo: " + b.log_path;
      renderApps();
      renderOffline();
      renderRepair();
      updateOverall();
      if (!b.admin) toast("Execute como administrador para aplicar as etapas.");
      if (b.updated_from) {
        showUpdate(`Atualizado para a versão ${b.version}`, `Versão anterior: ${b.updated_from}. A atualização automática funcionou.`, { buttons: true });
        $("#ub-now").hidden = true; $("#ub-later").textContent = "Fechar";
      }
      if (b.continuing && b.state?.pending?.length) handleResume(b.state);
      loadInventory(false).then(pollLive);
      setInterval(pollLive, 3000);
      if (!(b.continuing && b.state?.pending?.length)) setTimeout(() => checkUpdates(false), 4000);
    });
  }

  if (window.pywebview && window.pywebview.api && window.pywebview.api.boot) init(window.pywebview.api);
  else window.addEventListener("pywebviewready", () => init(window.pywebview.api));
  window.__ptInit = init; // usado pelo modo de demonstração (mock.js)
})();
