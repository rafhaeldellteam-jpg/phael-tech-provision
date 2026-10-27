"""Phael Tech Provision 2 — ponto de entrada.

Interface em HTML/CSS (pasta ui) dentro de uma janela nativa (pywebview + Edge WebView2).
Toda a lógica roda em Python (pasta core) e conversa com a interface pela classe Api.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from core import driverpacks, drivers, maintenance, programs, provision, seal, sysinfo, updater
from core.provision import NeedsReboot
from core.util import (APP_NAME, APP_VERSION, IS_WIN, bundle_dir, find_resource, fmt_bytes, is_admin, log,
                       relaunch_as_admin, run, work_dir)

STEP_ORDER = ["power", "office", "wu", "vendor", "finalize"]
# Etapas do mesmo grupo rodam ao mesmo tempo: o Office instala junto com o Windows Update;
# os drivers do fabricante só começam depois que o Windows Update terminar.
STEP_GROUPS = [["power"], ["office", "wu"], ["vendor"], ["finalize"]]
MAX_REBOOTS = 6          # no máximo 6 reinícios por provisionamento (proteção contra loop)
MAX_VENDOR_REBOOTS = {"hp": 1}   # HP Image Assistant: 1 reinício (ele reinstala o mesmo SoftPaq com frequência)
MAX_VENDOR_REBOOTS_DEFAULT = 2   # Dell / Lenovo / outros: no máximo 2 reinícios pela etapa do fabricante
STEP_TITLES = {"power": "Energia", "office": "Microsoft Office", "wu": "Windows Update",
               "vendor": "Drivers do fabricante", "finalize": "Finalização"}


class Api:
    """Métodos chamados pelo JavaScript (window.pywebview.api.*). Cada chamada roda em thread própria."""

    def __init__(self) -> None:
        self._window = None
        self._events: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._reboot_cancel = threading.Event()
        self._busy = threading.Lock()          # provisionamento / programas / drivers (não rodam juntos)
        self._maint_busy = threading.Lock()
        self._inventory: dict | None = None
        self._inv_lock = threading.Lock()
        self._sensors = maintenance.SensorReader()
        self._continuing = "--continuar" in sys.argv
        self._provisioning = False        # liga a vigia da área de trabalho só durante o provisionamento
        log.add_sink(lambda ts, level, msg: self._emit({"t": "log", "ts": ts, "level": level, "msg": msg}))
        threading.Thread(target=self._shell_watchdog, daemon=True).start()

    def _shell_watchdog(self) -> None:
        """Enquanto o provisionamento roda, garante que a área de trabalho (explorer.exe) está aberta.
        Ao instalar driver de vídeo o Windows reinicia a tela e às vezes o explorer não volta:
        ficava tudo preto, sem barra de tarefas e sem pesquisa, só com o programa aberto."""
        if not IS_WIN:
            return
        misses = 0
        while True:
            time.sleep(10)
            if not self._provisioning:
                misses = 0
                continue
            if maintenance_shell_running():
                misses = 0
                continue
            misses += 1
            if misses >= 2:             # 20 s sem explorer (ele mesmo costuma voltar em poucos segundos)
                misses = 0
                log("A área de trabalho do Windows (explorer) não estava aberta — reabrindo.", "warn")
                try:
                    subprocess.Popen(["explorer.exe"], cwd=os.environ.get("SystemRoot", r"C:\Windows"))
                except OSError as e:
                    log(f"Não consegui reabrir a área de trabalho: {e}", "error")

    # ------------------------------------------------------------------ eventos -> interface
    def _attach(self, window) -> None:
        self._window = window
        threading.Thread(target=self._pump, daemon=True).start()

    def _emit(self, ev: dict) -> None:
        self._events.put(ev)

    def _pump(self) -> None:
        while True:
            batch = [self._events.get()]
            time.sleep(0.12)
            while not self._events.empty() and len(batch) < 300:
                batch.append(self._events.get_nowait())
            if self._window is None:
                continue
            try:
                payload = json.dumps(batch, ensure_ascii=False)
                self._window.evaluate_js(f"window.PT && window.PT.onEvents({payload})")
            except Exception:
                pass

    # ------------------------------------------------------------------ inicialização
    def boot(self) -> dict:
        state = provision.load_state()
        if not state.get("pending"):
            state = {}
            if self._continuing:
                provision.clear_resume_on_boot()   # nada pendente: a reabertura automática não serve mais
        return {
            "app": APP_NAME, "version": APP_VERSION, "admin": is_admin(), "continuing": bool(state),
            "state": state,
            "programs": [{k: v for k, v in p.items() if k in ("key", "title", "desc", "icon", "default", "note")}
                         | {"available": programs.is_available(p)} for p in programs.PROGRAMS],
            "offline": [{k: v for k, v in p.items() if k in ("key", "vendor", "title", "desc")}
                        | {"available": drivers.offline_available(p)} for p in drivers.OFFLINE_PACKS],
            "repair": [{k: s[k] for k in ("key", "title", "desc")} for s in maintenance.REPAIR_STEPS],
            "log_path": str(log.path),
            "updated_from": self._version_changed(),
        }

    def _version_changed(self) -> str:
        """Versão anterior se o programa acabou de ser atualizado ('' caso contrário)."""
        marker = work_dir() / "versao.txt"
        try:
            before = marker.read_text(encoding="utf-8").strip()
        except OSError:
            before = ""
        try:
            marker.write_text(APP_VERSION, encoding="utf-8")
        except OSError:
            pass
        if before and before != APP_VERSION:
            log(f"Programa atualizado: {before} → {APP_VERSION}.", "ok")
            return before
        return ""

    def inventory(self, refresh: bool = False) -> dict:
        with self._inv_lock:
            if self._inventory is None or refresh:
                try:
                    self._inventory = sysinfo.collect_inventory()
                except Exception as e:
                    log(f"Falha ao ler o hardware: {e}", "error")
                    return {"error": str(e)}
            return self._inventory

    def live(self) -> dict:
        try:
            return sysinfo.live_metrics()
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------ utilidades
    def open_url(self, url: str) -> None:
        if url:
            webbrowser.open(url)

    def open_path(self, path: str) -> None:
        if IS_WIN and path and os.path.exists(path):
            os.startfile(path)

    def open_logs(self) -> None:
        self.open_path(str(log.path.parent))

    def copy_text(self, text: str) -> bool:
        if not IS_WIN:
            return False
        try:
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, creationflags=0x08000000)
            p.communicate(text.encode("utf-16-le"))
            return True
        except Exception:
            return False

    def reboot_now(self) -> None:
        log("Reiniciando o computador…")
        provision.reboot(5)

    def cancel(self) -> None:
        self._cancel.set()
        log("Cancelamento solicitado — a etapa atual será interrompida.", "warn")

    def save_report(self) -> str:
        try:
            path = provision.write_report(self.inventory())
            log(f"Relatório salvo em {path}", "ok")
            return path
        except Exception as e:
            log(f"Falha ao salvar relatório: {e}", "error")
            return ""

    # ------------------------------------------------------------------ provisionamento
    def start_provision(self, opts: dict) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "msg": "Já existe uma tarefa em andamento."}
        if self._maint_busy.locked():
            self._busy.release()
            return {"ok": False, "msg": "Aguarde a limpeza/reparo terminar antes de provisionar."}
        self._cancel.clear()
        steps = [s for s in STEP_ORDER if opts.get("steps", {}).get(s)]
        if not steps:
            self._busy.release()
            return {"ok": False, "msg": "Nenhuma etapa marcada."}
        if not opts.get("resumed"):
            provision.clear_state()          # provisionamento novo: zera contadores de tentativas/reinícios
        threading.Thread(target=self._provision_worker, args=(steps, opts), daemon=True).start()
        return {"ok": True}

    def resume_provision(self) -> dict:
        state = provision.load_state()
        if not state.get("pending"):
            return {"ok": False, "msg": "Não há provisionamento pendente."}
        opts = state.get("opts", {})
        opts["steps"] = {s: True for s in state["pending"]}
        opts["resumed"] = True
        log("Continuando o provisionamento após a reinicialização…")
        return self.start_provision(opts)

    def discard_resume(self) -> None:
        provision.clear_state()
        provision.clear_resume_on_boot()
        log("Continuação automática descartada.")

    def _step_progress(self, key: str):
        def cb(pct: float, text: str):
            self._emit({"t": "step", "key": key, "status": "run", "pct": pct, "text": text})
        return cb

    def _provision_worker(self, steps: list[str], opts: dict) -> None:
        results: dict[str, str] = {}      # ok | err | reboot
        reboot_msgs: list[str] = []
        self._provisioning = True
        try:
            log(f"Provisionamento iniciado: {', '.join(STEP_TITLES[s] for s in steps)}.")
            for s in STEP_ORDER:
                if s not in steps and not opts.get("resumed"):
                    self._emit({"t": "step", "key": s, "status": "skip", "pct": 0, "text": "Não selecionado"})
            # _plan = etapas do provisionamento ORIGINAL (continua igual depois de reiniciar)
            opts = dict(opts, _parallel=steps, _plan=opts.get("_plan") or steps)
            for group in STEP_GROUPS:
                keys = [k for k in group if k in steps]
                if not keys or self._cancel.is_set():
                    continue
                if len(keys) > 1:
                    log("Rodando ao mesmo tempo: " + ", ".join(STEP_TITLES[k] for k in keys) + ".")
                threads = [threading.Thread(target=self._run_one, args=(k, opts, results, reboot_msgs), daemon=True)
                           for k in keys]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                if reboot_msgs:
                    st = provision.load_state()
                    count = int(st.get("reboots", 0)) + 1
                    if count > MAX_REBOOTS:
                        # trava de segurança: nunca fica reiniciando sem parar
                        log(f"Limite de {MAX_REBOOTS} reinicializações neste provisionamento atingido — "
                            "seguindo sem reiniciar de novo. Reinicie ao final.", "warn")
                        for k in keys:
                            if results.get(k) == "reboot":
                                results[k] = "ok"
                                self._emit({"t": "step", "key": k, "status": "ok", "pct": 100,
                                            "text": "Concluído (reinicie ao final)"})
                        reboot_msgs.clear()
                        continue
                    st["reboots"] = count
                    provision.save_state(st)
                    # o que pediu reinício continua depois; o que ainda não rodou também
                    pending = [k for k in steps if results.get(k) in (None, "reboot")]
                    self._schedule_reboot(pending, opts, reboot_msgs[0])
                    return
            all_ok = all(results.get(k) == "ok" for k in steps)
            provision.clear_state()
            provision.clear_resume_on_boot()
            done_msg = ("Provisionamento cancelado." if self._cancel.is_set() else
                        "Provisionamento concluído." if all_ok else "Provisionamento concluído com erros.")
            log(done_msg, "ok" if all_ok and not self._cancel.is_set() else "warn")
            self._emit({"t": "done", "ok": all_ok and not self._cancel.is_set(), "msg": done_msg})
            if all_ok and opts.get("reboot_end") and not self._cancel.is_set():
                self._countdown_reboot(30, "Reiniciando para concluir o provisionamento")
        except Exception:
            log("Erro inesperado: " + traceback.format_exc(), "error")
            self._emit({"t": "done", "ok": False, "msg": "Erro inesperado — veja o log."})
        finally:
            self._provisioning = False
            self._busy.release()

    def _run_one(self, key: str, opts: dict, results: dict, reboot_msgs: list) -> None:
        self._emit({"t": "step", "key": key, "status": "run", "pct": -1, "text": "Iniciando…"})
        try:
            msg = self._run_step(key, self._step_progress(key), opts)
            results[key] = "ok"
            self._emit({"t": "step", "key": key, "status": "ok", "pct": 100, "text": msg})
        except NeedsReboot as nr:
            results[key] = "reboot"
            reboot_msgs.append(str(nr))
            self._emit({"t": "step", "key": key, "status": "wait", "pct": 100, "text": "Continua após reiniciar"})
        except Exception as e:
            results[key] = "err"
            if self._cancel.is_set():
                self._emit({"t": "step", "key": key, "status": "err", "pct": 0, "text": "Cancelado"})
                return
            log(f"{STEP_TITLES[key]}: {e}", "error")
            self._emit({"t": "step", "key": key, "status": "err", "pct": 0, "text": str(e)[:160]})

    def _run_step(self, key: str, prog, opts: dict) -> str:
        inv = self.inventory()
        vendor = (inv or {}).get("vendor", "other")
        if key == "power":
            msg = provision.disable_fast_startup(prog, opts.get("power_button", True), opts.get("no_sleep", True))
            try:   # sem rede não há Windows Update: instala os drivers de rede embutidos já na 1ª etapa
                net = drivers.ensure_network_drivers(vendor, prog, self._cancel)
                if net:
                    msg += " " + net
            except Exception as e:
                log(f"Drivers de rede embutidos: {e}", "warn")
            if opts.get("trust_cert"):
                try:
                    provision.trust_certificate()
                except Exception as e:
                    log(f"Certificado: {e}", "warn")
            return msg
        if key == "office":
            return provision.install_office(prog, self._cancel)
        if key == "wu":
            return provision.windows_update(prog, self._cancel)
        if key == "vendor":
            # se o Windows Update fez parte do provisionamento, os drivers do catálogo Microsoft já entraram
            wu_together = "wu" in opts.get("_plan", opts.get("_parallel", []))
            try:
                return drivers.vendor_drivers(vendor, prog, self._cancel, opts.get("include_bios", False),
                                              wu_fallback=not wu_together, inv=inv)
            except NeedsReboot:
                n = int(provision.load_state().get("vendor_reboots", 0)) + 1
                provision.update_state(vendor_reboots=n)
                if n > MAX_VENDOR_REBOOTS.get(vendor, MAX_VENDOR_REBOOTS_DEFAULT):
                    log("O fabricante pediu reinício de novo; para não entrar em loop, a etapa termina aqui.", "warn")
                    return "Drivers do fabricante aplicados (reinicie ao final)."
                raise
        if key == "finalize":
            self._inventory = None
            return provision.finalize(prog, self.inventory())
        raise ValueError(key)

    def _schedule_reboot(self, pending: list[str], opts: dict, reason: str) -> None:
        state = provision.load_state()
        state.update({"pending": pending, "opts": opts, "saved": time.strftime("%d/%m/%Y %H:%M")})
        provision.save_state(state)
        if provision.set_resume_on_boot():
            log("Programa agendado para abrir sozinho após reiniciar e continuar de onde parou.", "ok")
        else:
            log("Não consegui agendar a reabertura automática — abra o programa após reiniciar.", "warn")
        log(reason, "warn")
        if opts.get("auto_reboot", True):
            self._countdown_reboot(20, "Reiniciando para continuar o provisionamento")
        else:
            self._emit({"t": "reboot_prompt", "msg": reason})

    def cancel_reboot(self) -> None:
        self._reboot_cancel.set()

    def _countdown_reboot(self, seconds: int, msg: str) -> None:
        self._reboot_cancel.clear()
        for s in range(seconds, 0, -1):
            self._emit({"t": "countdown", "s": s, "msg": msg})
            if self._reboot_cancel.wait(1):
                self._emit({"t": "countdown", "s": 0, "msg": "Reinicialização cancelada."})
                log("Reinicialização automática cancelada. O provisionamento continua quando você reiniciar.", "warn")
                return
        self._emit({"t": "countdown", "s": 0, "msg": "Reiniciando…"})
        self._provisioning = False     # o explorer fecha ao reiniciar: a vigia não deve reabri-lo
        provision.reboot(3, msg)

    # ------------------------------------------------------------------ aplicativos
    def install_programs(self, keys: list[str]) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "msg": "Já existe uma tarefa em andamento."}
        self._cancel.clear()

        def worker():
            try:
                fails = self._install_programs(keys, None)
                self._emit({"t": "apps_done", "ok": fails == 0})
            finally:
                self._busy.release()
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def _install_programs(self, keys: list[str], step_prog) -> int:
        catalog = {p["key"]: p for p in programs.PROGRAMS}
        fails = 0
        for n, key in enumerate(keys, 1):
            entry = catalog.get(key)
            if not entry or self._cancel.is_set():
                continue
            if step_prog:
                step_prog(100 * (n - 1) / len(keys), f"{n}/{len(keys)}: {entry['title']}")

            def cb(pct, text, key=key):
                self._emit({"t": "app", "key": key, "status": "run", "pct": pct, "text": text})
            self._emit({"t": "app", "key": key, "status": "run", "pct": -1, "text": "Iniciando…"})
            try:
                msg = programs.install_program(entry, cb, self._cancel)
                self._emit({"t": "app", "key": key, "status": "ok", "pct": 100, "text": msg})
            except Exception as e:
                fails += 1
                log(f"{entry['title']}: {e}", "error")
                self._emit({"t": "app", "key": key, "status": "err", "pct": 0, "text": str(e)[:140]})
        return fails

    def app_icons(self) -> dict:
        try:
            return programs.program_icons()
        except Exception as e:
            log(f"Ícones dos aplicativos: {e}", "warn")
            return {}

    def program_status(self) -> dict:
        return {p["key"]: programs.is_installed(p) for p in programs.PROGRAMS}

    # ------------------------------------------------------------------ drivers
    def run_drivers(self, mode: str, include_bios: bool = False) -> dict:
        """mode: 'vendor' | 'wu' | 'offline:<key>'"""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "msg": "Já existe uma tarefa em andamento."}
        self._cancel.clear()

        def cb(pct, text):
            self._emit({"t": "drv", "mode": mode, "status": "run", "pct": pct, "text": text})

        def worker():
            try:
                self._emit({"t": "drv", "mode": mode, "status": "run", "pct": -1, "text": "Iniciando…"})
                if mode == "vendor":
                    inv = self.inventory()
                    msg = drivers.vendor_drivers(inv.get("vendor", "other"), cb, self._cancel, include_bios, inv=inv)
                elif mode == "wu":
                    msg = drivers.windows_update_drivers(cb, self._cancel)
                else:
                    pack = next(p for p in drivers.OFFLINE_PACKS if p["key"] == mode.split(":", 1)[1])
                    msg = drivers.install_offline(pack, cb, self._cancel)
                self._emit({"t": "drv", "mode": mode, "status": "ok", "pct": 100, "text": msg})
            except NeedsReboot as nr:
                self._emit({"t": "drv", "mode": mode, "status": "ok", "pct": 100, "text": "Instalado — reinicie"})
                self._emit({"t": "reboot_prompt", "msg": str(nr)})
            except Exception as e:
                log(f"Drivers: {e}", "error")
                self._emit({"t": "drv", "mode": mode, "status": "err", "pct": 0, "text": str(e)[:160]})
            finally:
                self._inventory = None
                self._busy.release()
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------------ rede
    def network(self) -> dict:
        try:
            return maintenance.network_info()
        except Exception as e:
            return {"error": str(e)}

    def connectivity(self, gateway: str = "") -> dict:
        return maintenance.connectivity_test(lambda l: self._emit({"t": "out", "ch": "net", "line": l}), gateway)

    # ------------------------------------------------------------------ manutenção
    def maintenance(self, action: str) -> dict:
        if action in ("temp", "wu", "components", "all") and self._provisioning:
            # parar o Windows Update / apagar temporários no meio da instalação quebrava as atualizações
            return {"ok": False, "msg": "Aguarde o provisionamento terminar para fazer a limpeza."}
        if not self._maint_busy.acquire(blocking=False):
            return {"ok": False, "msg": "Aguarde a tarefa de manutenção atual terminar."}

        def out(line):
            self._emit({"t": "out", "ch": "clean", "line": line})

        def worker():
            before = maintenance.free_space()
            try:
                if action == "temp":
                    maintenance.clean_temp(out)
                elif action == "wu":
                    maintenance.clean_windows_update(out)
                elif action == "components":
                    maintenance.clean_component_store(out, self._cancel)
                elif action == "bin":
                    maintenance.empty_recycle_bin(out)
                elif action == "all":
                    maintenance.clean_temp(out)
                    maintenance.clean_windows_update(out)
                    maintenance.empty_recycle_bin(out)
                elif action == "analyze":
                    maintenance.analyze_space(out)
                elif action == "startup":
                    items = maintenance.startup_programs()
                    out(f"{len(items)} programa(s) iniciam com o Windows:")
                    for it in items:
                        out(f"• {it.get('Name')} — {it.get('Command')}")
            except Exception as e:
                out(f"Erro: {e}")
            finally:
                freed = max(0, maintenance.free_space() - before)
                self._emit({"t": "clean_done", "freed": fmt_bytes(freed), "free": fmt_bytes(maintenance.free_space())})
                self._maint_busy.release()
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def free_space(self) -> str:
        return fmt_bytes(maintenance.free_space())

    def repair(self, keys: list[str]) -> dict:
        if self._provisioning:
            return {"ok": False, "msg": "Aguarde o provisionamento terminar para rodar o reparo (DISM/SFC)."}
        if not self._maint_busy.acquire(blocking=False):
            return {"ok": False, "msg": "Aguarde a tarefa de manutenção atual terminar."}
        self._cancel.clear()

        def worker():
            try:
                for st in maintenance.REPAIR_STEPS:
                    if st["key"] not in keys or self._cancel.is_set():
                        continue
                    self._emit({"t": "rep", "key": st["key"], "status": "run", "pct": -1})
                    self._emit({"t": "out", "ch": "repair", "line": f"▶ {st['title']}"})
                    rc = maintenance.run_repair_step(
                        st, lambda l: self._emit({"t": "out", "ch": "repair", "line": l}),
                        lambda p, k=st["key"]: self._emit({"t": "rep", "key": k, "status": "run", "pct": p}),
                        self._cancel)
                    ok = rc == 0
                    self._emit({"t": "rep", "key": st["key"], "status": "ok" if ok else "err", "pct": 100 if ok else 0})
                    log(f"{st['title']}: {'concluído' if ok else f'falhou (código {rc})'}", "ok" if ok else "error")
                self._emit({"t": "rep_done"})
            finally:
                self._maint_busy.release()
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------------ pacotes offline por modelo
    def pack_status(self) -> dict:
        inv = self.inventory()
        if inv.get("error"):
            return {"error": inv["error"]}
        return driverpacks.local_status(inv)

    def pack_run(self, mode: str) -> dict:
        """mode: 'download' (baixar para a pasta ao lado do .exe) | 'install' (instalar do pendrive)."""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "msg": "Já existe uma tarefa em andamento."}
        self._cancel.clear()

        def cb(pct, text):
            self._emit({"t": "pack", "status": "run", "pct": pct, "text": text})

        def worker():
            try:
                inv = self.inventory()
                fn = driverpacks.download_pack if mode == "download" else driverpacks.install_pack
                msg = fn(inv, cb, self._cancel)
                self._emit({"t": "pack", "status": "ok", "pct": 100, "text": msg})
            except Exception as e:
                log(f"Pacote por modelo: {e}", "error")
                self._emit({"t": "pack", "status": "err", "pct": 0, "text": str(e)[:200]})
            finally:
                self._busy.release()
                self._emit({"t": "pack_done"})
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------------ finalizar e selar
    def seal_check(self) -> dict:
        try:
            return seal.precheck()
        except Exception as e:
            return {"items": [], "error": str(e)}

    def seal_fix_appx(self) -> dict:
        def worker():
            seal.remove_user_appx(lambda l: self._emit({"t": "out", "ch": "seal", "line": l}))
            self._emit({"t": "seal_checked"})
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def seal_open_gui(self) -> dict:
        try:
            return {"ok": True, "msg": seal.open_gui()}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def seal_run(self, generalize: bool, action: str) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "msg": "Aguarde a tarefa atual terminar."}
        action = "reboot" if action == "reboot" else "shutdown"

        def worker():
            try:
                seal.seal(bool(generalize), action, self.inventory(),
                          lambda l: self._emit({"t": "out", "ch": "seal", "line": l}))
            except Exception as e:
                log(f"Finalizar e selar: {e}", "error")
                self._emit({"t": "seal_err", "msg": str(e)})
            finally:
                self._busy.release()
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------------ atualização (GitHub)
    def update_check(self) -> dict:
        info = updater.check()
        info["can_install"] = updater.can_self_update()
        info["busy"] = self._busy.locked() or bool(provision.load_state().get("pending"))
        if info.get("available"):
            log(f"Nova versão disponível: {info['version']} (atual {info.get('current')}).", "ok")
        return info

    def update_install(self) -> dict:
        if self._busy.locked():
            return {"ok": False, "msg": "Aguarde a tarefa atual terminar para atualizar."}
        info = updater.check()
        if not info.get("available"):
            return {"ok": False, "msg": info.get("error") or "Você já está na versão mais recente."}

        def worker():
            try:
                log(f"Baixando a versão {info['version']}…")
                last = [-10.0]

                def prog(p):
                    if p < 0 or p - last[0] >= 2 or p >= 100:
                        last[0] = p
                        self._emit({"t": "upd", "status": "download", "pct": p})
                path = updater.download(info, prog)
                log("Download conferido (SHA-256). Instalando a atualização…", "ok")
                target = updater.apply(path)          # troca o .exe agora (falha = erro na tela)
                self._emit({"t": "upd", "status": "restart", "pct": 100})
                time.sleep(1.5)
                self._sensors.stop()
                closer = threading.Thread(target=lambda: self._window and self._window.destroy(), daemon=True)
                closer.start()
                closer.join(3)                        # fecha a janela se der…
                updater.kill_own_children()           # …e encerra o navegador interno na hora
                _release_instance()                   # libera a trava para a versão nova abrir
                if not updater.launch(target):        # abre a versão nova e espera ela confirmar
                    _message_box("A atualização foi instalada, mas a versão nova não abriu sozinha.\n\n"
                                 f"Abra de novo o programa:\n{target}")
                os._exit(0)
            except Exception as e:
                log(f"Falha na atualização: {e}", "error")
                self._emit({"t": "upd", "status": "error", "msg": str(e)})
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "version": info["version"]}

    # ------------------------------------------------------------------ sensores
    def sensors(self) -> dict:
        if not self._sensors.start():
            return {"error": self._sensors.error}
        data = self._sensors.read()
        return data or {"waiting": True, "error": self._sensors.error}

    def _shutdown(self) -> None:
        self._sensors.stop()


def maintenance_shell_running() -> bool:
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower() == "explorer.exe":
                return True
        return False
    except Exception:
        return True     # sem como verificar: não mexe


# =========================================================================== janela
def _message_box(text: str, title: str = APP_NAME) -> None:
    if IS_WIN:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(text)


def _ensure_webview2() -> None:
    """Se o runtime do Edge WebView2 não existir, tenta instalar o instalador da pasta Recursos."""
    if not IS_WIN:
        return
    import winreg
    keys = [r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"]
    for k in keys:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, k) as h:
                    if winreg.QueryValueEx(h, "pv")[0] not in ("", "0.0.0.0"):
                        return
            except OSError:
                continue
    setup = find_resource("MicrosoftEdgeWebview2Setup.exe", "MicrosoftEdgeWebView2RuntimeInstallerX64.exe")
    if setup:
        run([str(setup), "/silent", "/install"], timeout=900)


_INSTANCE_MUTEX = None


def _already_running() -> bool:
    """Evita duas janelas do programa ao mesmo tempo (ex.: reabertura automática + clique do usuário)."""
    global _INSTANCE_MUTEX
    if not IS_WIN:
        return False
    _INSTANCE_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\PhaelTechProvision")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def _release_instance() -> None:
    global _INSTANCE_MUTEX
    if IS_WIN and _INSTANCE_MUTEX:
        ctypes.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
        _INSTANCE_MUTEX = None


def main() -> None:
    updater.mark_started(sys.argv)           # avisa a versão antiga (atualização) que esta abriu
    if IS_WIN and not is_admin():
        if relaunch_as_admin():
            return
        _message_box("Execute o Phael Tech Provision como administrador.")
        return
    updater.wait_previous_instance(sys.argv, work_dir() / "webview")
    if _already_running():
        # logo após uma atualização a versão antiga pode levar alguns segundos para sair
        for _ in range(20):
            time.sleep(0.5)
            _release_instance()
            if not _already_running():
                break
        else:
            _message_box("O Phael Tech Provision já está aberto nesta máquina.")
            return
    threading.Thread(target=updater.cleanup_old_copies, daemon=True).start()
    try:
        import webview
    except ImportError:
        _message_box("Biblioteca pywebview ausente. Rode o build.bat para gerar o executável.")
        return

    _ensure_webview2()
    api = Api()
    ui = bundle_dir() / "ui" / "index.html"
    window = webview.create_window(
        f"{APP_NAME} {APP_VERSION}", url=str(ui), js_api=api, width=1440, height=900, min_size=(1100, 700),
        background_color="#071019", maximized=True, text_select=False)
    api._attach(window)
    window.events.closed += api._shutdown
    log(f"{APP_NAME} {APP_VERSION} iniciado (admin={is_admin()}).")
    try:
        webview.start(gui="edgechromium" if IS_WIN else None, private_mode=False,
                      storage_path=str(work_dir() / "webview"), debug="--debug" in sys.argv)
    except Exception as e:
        _message_box("Não foi possível abrir a interface (Microsoft Edge WebView2 ausente?).\n\n"
                     "Coloque o arquivo MicrosoftEdgeWebview2Setup.exe na pasta Recursos ou instale o "
                     f"WebView2 Runtime e abra novamente.\n\nDetalhe: {e}")


if __name__ == "__main__":
    main()
