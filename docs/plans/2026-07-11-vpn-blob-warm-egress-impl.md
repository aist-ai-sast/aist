# Plan: Implementation — blob fetch за VPN через тёплый per-VPN egress (вариант A)

**Date:** 2026-07-11
**Context:** Blob-эндпоинт (`aist/api/files.py:92`) и список `/findings` тянут
исходники синхронным `requests.get` без прокси в web-процессе → для SCM за VPN
не работает. Web без docker.sock, cold-start OpenVPN ~30с. Решение: долгоживущий
per-VPN egress-контейнер (переиспользуем образ `aist-vpn-sidecar`), поднимается
prewarm-таском из celeryworker, web ходит через него по детерминированному имени
`aist-vpn-egress-<vpn_integration_id>`. Ничего не хранится, источник истины —
Docker. Риск-зоны: изоляция (ключ по vpn_integration, tinyproxy Allow),
неблокирование пайплайна (отдельный пул), регресс публичных SCM (ветка без VPN).
Архитектура: `docs/plans/2026-07-11-vpn-blob-warm-egress.md`.
**Estimated tasks:** 14

## Tasks

### Task 1: детерминированные имя/URL egress
**Test first:** в `aist/test/test_vpn_integration.py` новый класс
`EgressNamingTests`: `egress_container_name(42) == "aist-vpn-egress-42"`,
`egress_proxy_url(42) == "http://aist-vpn-egress-42:1080"`.
Expected failure: `ImportError` (функций нет).
**Implementation:** две чистые функции в `aist/utils/vpn.py`.
**Verify:** `run-rest-framework-tests.zsh -k EgressNamingTests`.
**Commit:** `Add deterministic egress container name/URL helpers`

### Task 2: entrypoint — список Allow + connect-лог для idle-детекта
**Test first:** shell-проверка в `docker-compose.integration.yml`-сценарии
(или bats, если появится) не гоняется локально — покрывается T5/T9 на Python-
уровне (Allow-список формируется в vpn.py) + ручной integration-note.
**Implementation:** `sast-combinator/vpn-sidecar/entrypoint.sh`:
- `AIST_ALLOWED_IP` парсить как список (разделители `, ` / пробел) → несколько
  строк `Allow <ip>`.
- Добавить `LogFile /tmp/tinyproxy-access.log` + `LogLevel Connect` (одна строка
  на CONNECT → mtime = last-use). Приватность: лог внутри эфемерного контейнера,
  наружу не отдаётся (reaper читает только mtime).
**Verify:** integration-прогон в Docker; отдельного unit нет (shell).
**Commit:** `vpn-sidecar: multi-IP Allow list + tinyproxy connect access log`

### Task 3: resolve web/worker IP для Allow
**Test first:** `EgressAllowedIpsTests`: при заданном `AIST_EGRESS_ALLOWED_IPS`
возвращает его split; иначе резолвит `AIST_EGRESS_WEB_SERVICE` (default `uwsgi`)
через `socket.gethostbyname_ex` (мок) + добавляет own eth0 IP; на ошибке —
только own IP. Expected failure: `ImportError`.
**Implementation:** `resolve_egress_allowed_ips() -> list[str]` в `aist/utils/vpn.py`
(reuse `_get_own_eth0_ip`).
**Verify:** `-k EgressAllowedIpsTests`.
**Commit:** `Resolve tinyproxy Allow IPs (web service + own) for warm egress`

### Task 4: `ensure_warm_egress` (поднять/переиспользовать)
**Test first:** `EnsureWarmEgressTests` (мок `subprocess.run`/`_find_executable`):
(a) `docker inspect` показывает Running → возвращает proxy_url, `docker run` НЕ
зовётся; (b) не запущен → `docker run` c `--name aist-vpn-egress-<id>`, без
`--rm`, с `-e AIST_ALLOWED_IP=<web,worker>`, затем readiness-wait. Expected
failure: `ImportError`.
**Implementation:** `ensure_warm_egress(vpn_resolved, vpn_id) -> str` в
`aist/utils/vpn.py`; вынести общий cmd-билд из `vpn_sidecar_context` в
`_build_run_cmd(...)` (не менять поведение эфемерного пути — INV-5).
**Verify:** `-k EnsureWarmEgressTests`.
**Commit:** `Add ensure_warm_egress: long-lived per-VPN egress container`

### Task 5: reaper по idle (mtime tinyproxy-лога) + pool cap
**Test first:** `ReapIdleEgressTests` (мок docker ps/exec): контейнер с mtime
старше `AIST_EGRESS_IDLE_TTL` → stop+rm; свежий → остаётся; при >`AIST_EGRESS_MAX_WARM`
живых — LRU-эвикция самого старого. Expected failure: `ImportError`.
**Implementation:** `list_warm_egress()`, `_egress_last_used(name)` (docker exec
`stat -c %Y /tmp/tinyproxy-access.log`, fallback на CreatedAt), `stop_warm_egress`,
`reap_idle_egress(...)` в `aist/utils/vpn.py`; переиспользовать паттерн
`cleanup_orphaned_vpn_containers`.
**Verify:** `-k ReapIdleEgressTests`.
**Commit:** `Add warm-egress idle reaper + pool cap (LRU)`

### Task 6: резолвер VPN от project_version
**Test first:** `VpnForProjectVersionTests`: pv с binding→org_integration→
vpn_integration(active) → возвращает его; без vpn / inactive → None; учитывает
`ProjectIntegrationOverride`. Expected failure: `ImportError`.
**Implementation:** `vpn_integration_for_project_version(pv) -> OrgIntegration|None`
в `aist/integrations/resolver.py` (рядом с `resolve_integration`).
**Verify:** `-k VpnForProjectVersionTests`.
**Commit:** `Resolve VPN integration for a project version (blob path)`

### Task 7: Celery-таск prewarm + reap
**Test first:** `PrewarmEgressTaskTests`: `prewarm_egress(vpn_id)` при active VPN
зовёт `ensure_warm_egress(...)`; при неактивной/несуществующей — no-op, не падает.
`ReapEgressTaskTests`: таск делегирует в `reap_idle_egress`. Expected failure:
`ModuleNotFoundError`.
**Implementation:** `aist/tasks/egress.py` — `@shared_task prewarm_egress`,
`@shared_task reap_egress`. select_related секрет VPN (как в
`aist/tasks/integrations.py:65`).
**Verify:** `-k EgressTask`.
**Commit:** `Add prewarm_egress and reap_egress Celery tasks`

### Task 8: beat-расписание для reap_egress
**Test first:** `EgressBeatScheduleTests`: в CELERY beat-конфиге есть запись
`reap_egress` с интервалом. Expected failure: KeyError.
**Implementation:** добавить в существующий AIST beat-schedule (там же, где
`cleanup_orphaned_vpn_containers`, если запланирован; иначе settings beat блок).
**Verify:** `-k EgressBeatSchedule`.
**Commit:** `Schedule reap_egress on celery beat`

### Task 9: проброс proxy_url в fetch-путь
**Test first:** `RemoteBytesProxyTests` (мок `requests.get`): `_return_remote_bytes(..., proxy_url="http://p:1080")`
→ `requests.get` вызван с `proxies={"http":..,"https":..}`; без proxy_url →
без `proxies`. Аналогично `ScmGerritBinding.fetch_raw_bytes(..., proxy_url=...)`.
Expected failure: TypeError (нет kwarg).
**Implementation:** добавить `*, proxy_url=None` в `_return_remote_bytes`
(`aist/api/files.py:89`) и `fetch_raw_bytes` (`aist/models.py:367`).
**Verify:** `-k RemoteBytesProxy`.
**Commit:** `Thread proxy_url into blob fetch path (remote bytes + gerrit)`

### Task 10: ветвление blob-эндпоинта (VPN → warm/202)
**Test first:** `BlobVpnRoutingTests`: (a) git-версия за VPN, `requests.get` мок
ok → 200 + `proxies` содержит `aist-vpn-egress-<vpn_id>`; (b) `ConnectionError`
→ 202 `{"status":"warming"}` + `prewarm_egress.delay(vpn_id)` вызван; (c)
публичный SCM (vpn=None) → как раньше, без proxies (регресс-гард). Expected
failure: AssertionError (нет ветки).
**Implementation:** в `ProjectVersionFileBlobAPI.get` (`aist/api/files.py:120`)
вычислить vpn через Task 6, при наличии — `egress_proxy_url(vpn.id)`, fetch с
коротким connect-timeout, на `requests.ConnectionError`/timeout → enqueue prewarm
+ `Response(202)`. proxy_url НЕ из ввода — из авторизованного pv (INV-1).
**Verify:** `-k BlobVpnRouting`.
**Commit:** `Route VPN-gated blob fetch through warm egress, 202 on cold`

### Task 11: prewarm REST-эндпоинт
**Test first:** `PrewarmEndpointTests`: POST `/projects_version/<id>/files/prewarm`
авторизованным юзером своей орг → 200 `{status}` + `prewarm_egress.delay` вызван;
чужая орг → 404 (org-scoped queryset); аноним → 401. Expected failure: 404 (нет
маршрута).
**Implementation:** класс `ProjectVersionPrewarmAPI(AuthorizedQuerySetMixin, GenericAPIView)`
в `aist/api/files.py` (permission_classes=[IsAuthenticated], authorized_queryset как
у blob, `Permissions.Product_View`); маршрут в `aist/api_urls.py`. Без прямого
`request.data`.
**Verify:** `-k PrewarmEndpoint`.
**Commit:** `Add file-snippet prewarm endpoint (enqueue warm egress)`

### Task 12: фронт — обработка 202 (skeleton + retry)
**Test first:** RTL-тест `FindingSnippetPreview`: fetch → 202 → показывает
skeleton; повторный poll → 200 → показывает 3 строки. Expected failure: рендерит
ошибку/пусто на 202.
**Implementation:** `client-ui/src/lib/snippetCache.ts` + `api.ts` — распознавать
202, возвращать статус `warming`; React Query `retry`/`refetchInterval` с backoff
до успеха/лимита; `FindingSnippetPreview.tsx` skeleton-состояние.
**Verify:** `run-client-ui-tests.zsh -t FindingSnippetPreview`.
**Commit:** `client-ui: handle 202 warming for file snippets with backoff`

### Task 13: фронт — viewport-lazy + prewarm по уникальным pv
**Test first:** RTL-тест `FindingsPage`: строки вне вьюпорта не вызывают
`fetchFileContent`; при появлении — вызывают; prewarm дёргается один раз на
уникальный `project_version_id`. Expected failure: fetch на все 50 сразу.
**Implementation:** `IntersectionObserver` в `FindingCard`/`FindingSnippetPreview`
(fetch только когда видно); dedup prewarm-вызовов по pv на уровне `FindingsPage`.
**Verify:** `run-client-ui-tests.zsh -t FindingsPage`.
**Commit:** `client-ui: lazy snippet load by viewport + prewarm per project version`

### Task 14: обновить `aist/integrations/VPN.md` + GitHub-gap
**Test first:** нет (docs). Проверка — ручное ревью.
**Implementation:** строка таблицы «UI file blob / findings snippet → Yes (warm
per-VPN egress)»; секция про lifecycle egress (keying по vpn_integration_id,
idle-TTL, pool cap, отличие от pipeline-sidecar, live-GIT); явно задокументировать,
что GitHub App-client (`aist/models.py:173`) proxy не поддерживает →
GitHub-Enterprise-за-VPN пока не покрыт.
**Commit:** `docs: document warm egress for UI blob + GitHub-behind-VPN limitation`

## Security и pattern-check (Step 3)
- [x] Новый QuerySet: prewarm-эндпоинт (T11) — `AuthorizedQuerySetMixin` +
  `Permissions.Product_View`, org-scoped. Blob уже так.
- [x] Новый API-эндпоинт (T11) — `permission_classes=[IsAuthenticated]`.
- [x] proxy_url из авторизованного pv, не из ввода (T10) — cross-org невозможен.
- [x] Нет MCP file-reading; нет raw SQL; нет `request.data` напрямую.
- [x] Креды в `docker run` не логируются — сохранить redaction из `vpn.py` (T4).
- [x] Egress отделён от эфемерного pipeline-sidecar (INV-2) — не блокирует анализатор.

## Open questions
- `AIST_EGRESS_IDLE_TTL` (900с) / `AIST_EGRESS_MAX_WARM` (10) — дефолты рабочие,
  подтвердить по проду.
- Порядок реализации: backend (T1–T11) → frontend (T12–T13) → docs (T14).
  Frontend можно вести параллельно после T11 (контракт 202 зафиксирован).
