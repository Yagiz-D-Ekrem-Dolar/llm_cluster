"""Otomatik ölçekleme, güç yönetimi ve onarım kararları.

Her döngüde (varsayılan 5 sn):
  1. Her model için talebe (işlenen + bekleyen istek) göre istenen replika sayısını hesaplar,
     gerekirse yeni replika yerleştirir (boş kapasite -> boşta düşük öncelikli modeli tahliye -> uyuyan düğümü uyandır),
     boşta kalan replikaları kaldırır.
  2. Uzun süredir boş düğümleri uykuya alır (en az `auto.min_awake_nodes` düğüm uyanık kalır).
  3. Kayıp GPU'lu düğümleri yeniden başlatır, beklenmedik şekilde kapanan düğümleri Wake-on-LAN ile kurtarmaya çalışır.
  4. Aşırı ısınan düğümlerdeki replikaları başka yere taşır.

`auto.dry_run` açıkken kararlar yalnızca olay günlüğüne yazılır, uygulanmaz.
"""
from __future__ import annotations

import asyncio
import math
from collections import deque

from .util import log, now


class Autoscaler:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        self.c = ctx.cluster
        self.kick = asyncio.Event()
        self.cold: dict[str, float] = {}
        self.last_up: dict[int, float] = {}
        self.unmet: dict[int, dict] = {}
        self.decisions: deque = deque(maxlen=150)
        self.per_model: dict[int, dict] = {}
        self.last_tick = 0.0
        self._seen: dict[str, float] = {}
        self._gpu_lost: dict[int, float] = {}
        self._task: asyncio.Task | None = None

    @property
    def gw(self):
        return self.ctx.gateway

    # ---- yaşam döngüsü ------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def request_capacity(self, m: dict) -> None:
        """Ağ geçidi: hazır replikası olmayan bir model için istek geldi."""
        self.cold[m["name"]] = now()
        self.kick.set()

    async def _loop(self) -> None:
        while True:
            interval = max(2.0, float(self.db.get("auto.interval_s")))
            try:
                await asyncio.wait_for(self.kick.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self.kick.clear()
            try:
                if self.db.get("auto.enabled"):
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("autoscaler")
            await asyncio.sleep(0.5)

    # ---- karar kaydı --------------------------------------------------------
    async def act(self, kind: str, label: str, reason: str, fn=None, node_id: int | None = None) -> bool:
        dry = bool(self.db.get("auto.dry_run"))
        key = f"{kind}:{label}"
        t = now()
        if dry and t - self._seen.get(key, 0) < 60:
            return True
        self._seen[key] = t
        rec = {"ts": t, "kind": kind, "label": label, "reason": reason, "executed": not dry, "error": ""}
        self.decisions.appendleft(rec)
        self.db.event("info", "autoscaler", f"{'[öneri] ' if dry else ''}{label} — {reason}", node_id)
        if dry or fn is None:
            return True
        try:
            await fn()
            return True
        except Exception as e:
            rec["error"] = str(e)
            self.db.event("warn", "autoscaler", f"{label} başarısız: {e}", node_id)
            return False

    def _set_unmet(self, m: dict, reason: str) -> None:
        cur = self.unmet.get(m["id"])
        t = now()
        if cur is None:
            self.unmet[m["id"]] = {"model": m["name"], "reason": reason, "since": t, "logged": t}
            self.db.event("warn", "autoscaler", f"{m['name']} için yer açılamadı: {reason}")
        elif t - cur["logged"] > 300:
            cur["logged"] = t
            self.db.event("warn", "autoscaler", f"{m['name']} hâlâ yerleştirilemiyor: {reason}")

    # ---- ana döngü adımı ----------------------------------------------------
    async def tick(self) -> None:
        t = now()
        self.last_tick = t
        for m in sorted(self.c.models.values(), key=lambda x: -x["priority"]):
            await self._scale_model(m, t)
        await self._thermal(t)
        await self._power(t)

    async def _scale_model(self, m: dict, t: float) -> None:
        c = self.c
        # bakımdaki ya da beklenmedik şekilde çevrimdışı düğümlerdeki replikalar "var" sayılmaz
        deps = [d for d in c.deps_of(m["id"]) if d.desired == "running"
                and c.nodes[d.node_id].enabled and c.nodes[d.node_id].status != "offline"]
        have = len(deps)
        if not m["enabled"]:
            for d in deps:
                if d.origin == "autoscaler" and not d.pinned:
                    await self.act("scale_down", f"{m['name']}@{c.nodes[d.node_id].name}", "model devre dışı",
                                   lambda d=d: c.remove_deployment(d.id, "model devre dışı"), d.node_id)
            return
        dm = self.gw.demand(m["name"])
        cold_recent = t - self.cold.get(m["name"], 0) < 20
        load = dm["inflight"] + dm["waiting"]
        target = max(1, min(int(m["max_concurrency"]), int(self.db.get("auto.target_concurrency"))))
        need = math.ceil(load / target) if load > 0 else 0
        if (dm["cold"] > 0 or cold_recent) and need < 1:
            need = 1
        recent = bool(dm["last_ts"]) and (t - dm["last_ts"] < m["idle_unload_min"] * 60)
        if need == 0 and recent:
            need = have
        wanted = max(int(m["min_replicas"]), min(need, int(m["max_replicas"])))
        self.per_model[m["id"]] = {"model_id": m["id"], "name": m["name"], "wanted": wanted, "have": have,
                                   "ready": sum(1 for d in deps if d.state == "ready"), "load": load, "rpm": dm["rpm"]}
        if wanted > have:
            cooldown = float(self.db.get("auto.scale_up_cooldown_s"))
            if have == 0 or t - self.last_up.get(m["id"], 0) >= cooldown:
                why = "soğuk başlatma" if (have == 0 and (dm["cold"] > 0 or cold_recent)) else \
                      f"yük {load} / hedef {target} → {wanted} replika" if load else f"en az {m['min_replicas']} replika"
                for _ in range(min(wanted - have, 2)):
                    if not await self._add_replica(m, why):
                        break
                    self.last_up[m["id"]] = t
        else:
            self.unmet.pop(m["id"], None)
            if wanted < have:
                idle = sorted([d for d in deps if d.inflight == 0 and not d.pinned and d.origin == "autoscaler"
                               and t - d.last_used >= m["idle_unload_min"] * 60], key=lambda d: d.last_used)
                for d in idle[: have - wanted]:
                    n = c.nodes[d.node_id]
                    await self.act("scale_down", f"{m['name']}@{n.name}",
                                   f"{int((t - d.last_used) / 60)} dk boşta",
                                   lambda d=d: c.remove_deployment(d.id, "boşta"), n.id)

    async def _add_replica(self, m: dict, why: str) -> bool:
        c = self.c
        p = c.plan_placement(m, allow_wake=bool(self.db.get("auto.allow_wake")), allow_evict=True)
        if p is None:
            rt = c.runtimes.get(m["runtime"])
            why_not = "uygun düğüm / yeterli kapasite yok"
            if rt:
                ok_nodes, _ = c.runtime_nodes(rt)
                if not [n for n in ok_nodes if n.enabled]:
                    why_not = f"'{rt['label']}' çalıştırılabilir dosyası hiçbir sunucuda kullanılabilir değil (yol ya da ajan izni)"
            self._set_unmet(m, why_not)
            return False
        self.unmet.pop(m["id"], None)
        extra = []
        if p.evict:
            extra.append("tahliye: " + ", ".join(f"{c.models[d.model_id]['name']}@{p.node.name}" for d in p.evict))
        if p.needs_wake:
            extra.append("düğüm uyandırılacak")
        if p.cpu:
            extra.append("CPU modunda")
        return await self.act("scale_up", f"{m['name']} → {p.node.name}", why + (" · " + " · ".join(extra) if extra else ""),
                              lambda: c.deploy(m, p, "autoscaler", False), p.node.id)

    async def _thermal(self, t: float) -> None:
        c = self.c
        for n in list(c.nodes.values()):
            if n.status != "online" or n.thermal != "crit":
                continue
            for d in list(c.deps_on(n.id)):
                if d.origin == "autoscaler" and not d.pinned:
                    name = c.models[d.model_id]["name"]
                    await self.act("thermal", f"{name}@{n.name}", f"aşırı sıcaklık {n.max_temp:.0f}°C — replika başka düğüme taşınacak",
                                   lambda d=d: c.remove_deployment(d.id, "aşırı sıcaklık"), n.id)

    async def _power(self, t: float) -> None:
        c, db = self.c, self.db
        allow_sleep = bool(db.get("auto.allow_sleep"))
        min_awake = int(db.get("auto.min_awake_nodes"))
        sleep_after = float(db.get("auto.sleep_after_idle_min")) * 60
        for n in c.nodes.values():
            if n.status != "online":
                n.idle_since = None
                continue
            busy = n.intent is not None or any(d.desired == "running" for d in c.deps_on(n.id))
            if busy:
                n.idle_since = None
            elif n.idle_since is None:
                n.idle_since = t
        awake = [n for n in c.nodes.values() if n.status == "online" and n.enabled]
        # 1) uykuya alma
        if allow_sleep:
            cands = [n for n in awake if n.allow_sleep and not n.always_on and not n.draining and not n.intent
                     and not n.is_self and n.idle_since and t - n.idle_since >= sleep_after]
            cands.sort(key=lambda n: n.idle_since)
            count = len(awake)
            for n in cands:
                if count <= min_awake:
                    break
                if any(self.cold.get(m["name"], 0) > t - 30 for m in c.models.values()):
                    break  # talep var, bekle
                await self.act("sleep", n.name, f"{int((t - n.idle_since) / 60)} dk boşta → uyku",
                               lambda n=n: c.power(n, "sleep", "otomatik: boşta"), n.id)
                count -= 1
        # 2) GPU kaybı -> yeniden başlat
        for n in awake:
            if n.gpu_lost:
                since = self._gpu_lost.setdefault(n.id, t)
                if (t - since >= 30 and n.allow_restart and db.get("auto.allow_reboot") and not n.intent
                        and c.reboots_last_hour(n) < int(db.get("auto.max_reboots_per_hour"))):
                    self._gpu_lost.pop(n.id, None)
                    await self.act("reboot", n.name, "GPU sürücüden düştü → yeniden başlatma",
                                   lambda n=n: c.power(n, "reboot", "GPU kayıp", force=True), n.id)
            else:
                self._gpu_lost.pop(n.id, None)
        # 3) beklenmedik çevrimdışı -> WoL ile kurtarma
        if db.get("auto.allow_wake"):
            for n in c.nodes.values():
                if n.status == "offline" and n.enabled and n.mac and t - n.last_recover_wake > 600:
                    n.last_recover_wake = t
                    await self.act("recover", n.name, "çevrimdışı → Wake-on-LAN ile kurtarma denemesi",
                                   lambda n=n: c.wake(n, "kurtarma"), n.id)

    # ---- arayüz için durum --------------------------------------------------
    def status(self) -> dict:
        return {
            "enabled": bool(self.db.get("auto.enabled")),
            "dry_run": bool(self.db.get("auto.dry_run")),
            "mode": self.db.get("auto.mode"),
            "last_tick": self.last_tick,
            "decisions": list(self.decisions)[:60],
            "unmet": list(self.unmet.values()),
            "models": list(self.per_model.values()),
        }
