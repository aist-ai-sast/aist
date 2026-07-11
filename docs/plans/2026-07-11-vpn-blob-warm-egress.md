# Plan: On-demand blob fetch за VPN через тёплый per-VPN egress-gateway

**Date:** 2026-07-11
**Estimated tasks:** 12

## Context

### Симптом
`GET /api/v2/aist/projects_version/<id>/files/blob/<path>` не отдаёт исходники
для проектов, чья SCM-интеграция ходит через VPN. Пример из прод:
`.../projects_version/108/files/blob/apps/backend/Dockerfile`. Ломается не только
code-view одного файла, но и **список `/findings`**: каждая строка рисует
3-строчный превью, скачивая целый файл через тот же blob-эндпоинт.

### Root cause
Blob-эндпоинт (`aist/api/files.py:120`) для git-версий тянет файл так:

```python
# aist/api/files.py:92 — исполняется в ПОТОКЕ web-запроса (uwsgi), не в Celery
response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)  # без proxies
# aist/models.py:374 — Gerrit fetch_raw_bytes, та же проблема
```

Три структурных факта:

1. **Blob исполняется в web-процессе.** VPN-sidecar управляется только из
   celeryworker, где смонтирован `/var/run/docker.sock` (`aist/utils/vpn.py`;
   `OrgIntegration.scoped_session` `aist/models.py:637` явно требует worker).
   Web не может и не должен поднимать контейнеры.
2. **Cold-start OpenVPN ≈ 30 сек** (`_TUN_WAIT_SECS = 35`, `aist/utils/vpn.py`).
   «Обернуть blob в `vpn_sidecar_context`» невозможно и по месту исполнения, и
   по латентности (требование ≤5 сек нарушается на порядок).
3. **`proxy_url` не проброшен в fetch-путь.** Прецедент есть: `get_project_info`
   у GitLab/Gerrit/Gitea уже принимает `proxy_url` (`aist/models.py:255/383/463`);
   `fetch_raw_bytes` и `_return_remote_bytes` — нет. GitHub идёт через async
   App-client и proxy не поддерживает вовсе.

### Что именно делает список найдингов (подтверждено кодом)
- `FindingSnippetPreview.tsx:19` → `useFileSnippet` → `fetchFileContent(sourceFileLink)`
  (`client-ui/src/lib/api.ts:206`) → blob-эндпоинт. Скачивается **целый файл**,
  режется до 3 строк на клиенте (`snippetCache.ts:30`).
- Detail/code-view (`CodeSnippet.tsx:20`) — **тот же хук**, тот же полный файл,
  просто показывает больше строк.
- Сниппет нигде не хранится: `sourcefile_link` — это URL из `finding_meta`
  (`queries.ts:229`). Серверный сериализатор списка
  (`AISTFindingListItemSerializer`, `aist/api/findings.py:102`) кода не содержит.
- Page size по умолчанию **50**, серверного `max_limit` нет → до **~50 закачек
  целых файлов на рендер** (клиент дедупит по URL файла — React Query key
  `["file", sourceFileLink]`, `snippetCache.ts:15`).

### Жёсткие ограничения (подтверждены заказчиком)
- **Отображение исходников — всегда live из GIT по ref/commit, НИКОГДА из данных
  скана.** Скан ежедневный, прогонов много, сорцы двигаются → любой снятый на
  скане сниппет протухает. Хранить/замораживать сорцы или сниппеты **нельзя**
  (место, версионирование, регресс, немасштабируемо).
- Следствие: чиним скорость/масштаб **live-фетча**, а не подменяем его
  хранилищем. На web-пути не персистится ничего.

### Ключевые инсайты по масштабируемости
- Тёплых туннелей нужно столько, сколько **VPN-интеграций, чьи найдинги смотрят
  прямо сейчас** — не сколько прогонов/версий/пользователей. Десять юзеров одной
  орг. делят один туннель.
- Узкое место списка — **число upstream-фетчей** (по одному на уникальный файл),
  а не размер файла. Значит бьём по: (а) тёплый туннель один раз на VPN,
  (б) меньше одновременных фетчей (ленивая подгрузка по вьюпорту + дедуп).

### Опровержение опасения «VPN орг. A мешает орг. B»
Пересечения нет by design и мы обязаны это сохранить:
- Каждый sidecar — отдельный контейнер, свой network namespace; `tun0` и
  default-route живут **внутри** контейнера, маршруты хоста не трогаются.
- Имя уникально; `NET_ADMIN`/`/dev/net/tun` действуют только в netns контейнера.
- tinyproxy `Allow` ограничивает клиентов по IP → чужой контейнер не пропивотит.

## Целевая архитектура

VPN убирается с потока построения per-request-туннеля: для интерактивного fetch
используется **долгоживущий per-VPN egress-gateway**, поднятый заранее «по
намерению» и переиспользуемый между запросами. Ничего не хранится — каждый файл
тянется on-demand через raw-API поверх уже поднятого туннеля.

Источник истины — **сам Docker** (детерминированное имя контейнера), без внешнего
реестра. proxy_url выводится из `vpn_integration_id`, liveness — по факту коннекта.

```
┌── WEB (uwsgi) ─ blob endpoint ─────────────────────────────────┐
│ resolve (repo, commit_sha, path) + vpn_integration_id | None   │
│  ├─ VPN не нужен ─▶ requests.get(raw_url)          (как сейчас) │
│  └─ VPN нужен (vpn_id):                                        │
│      proxy = f"http://aist-vpn-egress-{vpn_id}:1080"          │
│      try: requests.get(raw_url, proxies=proxy,                 │
│               connect_timeout~2с, total<5с)                    │
│      ├─ ok        ─▶ stream байты (ничего не храним)           │
│      └─ conn err  ─▶ enqueue prewarm(vpn_id) + HTTP 202        │
│         (контейнера нет → Docker DNS падает мгновенно;          │
│          tun0 не готов → refused. Быстрый фейл, не 30с вис)     │
└────────────────────────────────────────────────────────────────┘
                                                ▲ manage (docker.sock)
┌── CELERYWORKER ────────────────────────────────────────────────┐
│ • PIPELINE: свой эфемерный aist-vpn-<exec_id> (НЕ ТРОГАЕМ)      │
│ • EGRESS-SUPERVISOR (новое):                                   │
│    ensure_warm_egress(vpn_id): docker inspect → reuse :         │
│      docker run -d --name aist-vpn-egress-<vpn_id> (тот образ)  │
│      single-flight бесплатно: уникальность --name               │
│    idle reaper (celery-beat): last-use = mtime tinyproxy-лога  │
│      (docker exec stat), гасит простаивающие; LRU при cap       │
└────────────────────────────────────────────────────────────────┘
```

Pre-warm «по намерению»: при открытии страницы найдингов / code-view фронт для
каждой **уникальной VPN-интеграции** в видимых строках дёргает лёгкий idempotent
prewarm-эндпоинт. Пока грузится список — туннели поднимаются фоном; к моменту
отрисовки превью они тёплые → fetch ≤5 сек. Idle-TTL гасит после бездействия.

## Архитектурные инварианты

- **INV-1 (изоляция):** ровно один egress-контейнер на VPN-интеграцию
  (`aist-vpn-egress-<vpn_integration_id>`), свой netns, tinyproxy `Allow` =
  {web IP, worker IP}. `vpn_integration` принадлежит одной организации →
  cross-org невозможен. vpn_id берётся из авторизованного объекта, не из ввода.
- **INV-2 (неблокирование анализатора):** egress-пул для UI **физически отделён**
  от эфемерного `aist-vpn-<execution_id>` пайплайна. Blob-операции никогда не
  разделяют контейнер/лок с анализатором.
- **INV-3 (no source storage / no external state):** web-путь не пишет байты
  исходников никуда. Нет внешнего реестра — источник истины сам Docker.
- **INV-4 (web без docker.sock):** контейнерами управляет только celeryworker;
  web выводит proxy_url из `vpn_integration_id` и ходит на прокси по имени
  контейнера; liveness — по факту коннекта (try → 202 при ошибке).
- **INV-5 (нет регрессий пайплайна):** `vpn_sidecar_context` и путь
  `aist/tasks/pipeline.py` для анализаторов остаются как есть; warm-mode —
  аддитивный.
- **INV-6 (иммутабельный ref):** для GIT_BRANCH файл тянется по
  `last_resolved_commit` (SHA), не по имени ветки — воспроизводимость.
- **INV-7 (live GIT):** контент всегда резолвится из GIT по ref/commit в момент
  показа. Никаких снятых на скане снапшотов/сниппетов — они протухают.

## Tasks

### Task 1: детерминированное имя/URL egress (без внешнего реестра)
Единая точка вывода имени и прокси-URL — web и worker считают одинаково:
- `egress_container_name(vpn_id) -> "aist-vpn-egress-<vpn_id>"`,
  `egress_proxy_url(vpn_id) -> "http://aist-vpn-egress-<vpn_id>:1080"`.
- Источник истины — Docker (наличие контейнера). **Нет** Valkey/DB-реестра,
  нет `last_used_ts` в общем стейте (INV-3). Discovery/liveness — из имени +
  факта коннекта.
- Тест: чистая функция, стабильный вывод.

### Task 2: warm-mode в `aist/utils/vpn.py`
Добавить (не ломая `vpn_sidecar_context`) управление долгоживущим контейнером:
- `ensure_warm_egress(vpn_resolved, vpn_id) -> proxy_url`: если
  `aist-vpn-egress-<vpn_id>` уже жив (`docker inspect`) — вернуть proxy_url;
  иначе `docker run -d --name <...> --restart=no` (тот же образ, те же env-креды,
  тот же выбор network) + `_wait_for_sidecar_ready`.
- tinyproxy `Allow` расширить до {worker IP, web IP} — прокинуть `AIST_ALLOWED_IP`
  списком (правка `entrypoint.sh`/tinyproxy `Allow`).
- `stop_warm_egress(vpn_id)`, `list_warm_egress()` (по префиксу имени).
- Single-flight **бесплатно**: уникальность `--name` (второй `docker run` с
  занятым именем падает → «уже поднят»). Без external lock, без race.

### Task 3: Celery-таск pre-warm (`aist/tasks/egress.py`)
- `prewarm_egress(vpn_integration_id)`: загрузить `OrgIntegration`(VPN) с секретом,
  проверить `is_active`, `ensure_warm_egress(...)`. Идемпотентно, дёшево если тёпл.
- Хелпер `vpn_integration_for_project_version(pv) -> OrgIntegration|None`:
  цепочка `pv.project.repository.get_binding().org_integration.vpn_integration`
  (`aist/models.py:709→62→org_integration→596`) + учёт `ProjectIntegrationOverride`
  через `resolve_integration` (`aist/integrations/resolver.py`) для VPN-дефолта.
- Тест: не поднимает контейнер, если VPN не сконфигурирован/не активен.

### Task 4: idle-TTL reaper + pool cap (celery-beat)
- `reap_idle_egress()`: пройти `list_warm_egress()`. last-use — **worker-side без
  общего стейта**: mtime лога tinyproxy внутри контейнера
  (`docker exec <c> stat -c %Y /var/log/tinyproxy/tinyproxy.log`; строка на каждый
  CONNECT). `now - last_use > AIST_EGRESS_IDLE_TTL` (default 900с) → stop+rm.
- Pool cap `AIST_EGRESS_MAX_WARM` (default 10): при превышении — LRU-эвикция по
  тому же last-use. Расширить паттерн `cleanup_orphaned_vpn_containers`
  (`aist/utils/vpn.py`), не дублировать.
- Beat-расписание рядом с прочими AIST beat-задачами.

### Task 5: проброс `proxy_url` в fetch-путь (`aist/api/files.py`, `aist/models.py`)
- `_return_remote_bytes(self, url, filename, extra_headers=None, *, proxy_url=None)`
  → `requests.get(url, ..., proxies={"http":proxy_url,"https":proxy_url} if proxy_url else None)`.
- `ScmGerritBinding.fetch_raw_bytes(self, scm, ref, path, *, proxy_url=None)`
  (`aist/models.py:367`) — прокинуть в `requests.get`.
- Никакой логики поднятия VPN в web. proxy_url вычисляется из `vpn_id` (Task 1).

### Task 6: логика выбора пути в blob-эндпоинте (`aist/api/files.py:120`)
Cache-free ветвление:
1. `FILE_HASH` → как сейчас (`_return_local_file`).
2. Git-версия: `vpn = vpn_integration_for_project_version(pv)`.
   - `vpn is None` → текущий путь без прокси (нулевой регресс публичных SCM).
   - иначе: `proxy = egress_proxy_url(vpn.id)` (Task 1),
     `requests.get(raw_url, proxies=proxy, connect_timeout≈2с, total<5с)`:
     - успех → отдать байты (touch происходит естественно — запись в лог tinyproxy).
     - `ConnectionError`/timeout коннекта → `prewarm_egress.delay(vpn.id)` и
       `202 {"status":"warming","retry_after":3}`. UI перезапросит (Task 9).
3. Нет repository → `404` (как сейчас).

### Task 7: (ОТЛОЖЕНО — выбран вариант A) snippet-mode на blob-эндпоинте
Не реализуем сейчас. Зафиксировано решение: список тянет превью **вариантом A**
(live-фетч целого файла через тёплый туннель + viewport-lazy + prewarm + дедуп).
За VPN дорогое — round-trip на запрос, а не байты, поэтому payload-оптимизация
(`?lines=a-b`) второстепенна. Оставлено как задел на будущее, если понадобится
резать трафик: параметр `?lines=<a>-<b>` — backend режет файл на сервере, отдаёт
нужные строки, всё ещё live-GIT без хранения.

### Task 8: фронт — сократить blast radius списка (`client-ui/`)
- **Ленивая подгрузка по вьюпорту**: превью тянется только для видимых строк
  (виртуализация/`IntersectionObserver`), а не для всех 50 сразу. Ограничивает
  одновременные фетчи ≈ размером вьюпорта.
- Сохранить существующий клиентский дедуп по URL файла (React Query key).
- Не менять источник данных: остаётся live blob (INV-7).

### Task 9: prewarm с фронта + обработка 202
- При открытии списка/детали — собрать уникальные `vpn_integration` видимых строк
  и дёрнуть `prewarm` (fire-and-forget) до/во время загрузки превью.
- `202 warming` → скелетон превью + auto-retry с backoff (`retry_after`). После N
  сек без успеха — понятная ошибка в карточке.
- prewarm-эндпоинт: `POST /projects_version/<id>/files/prewarm`, `IsAuthenticated`
  + `AuthorizedQuerySetMixin` (org-scoped, `Permissions.Product_View`), idempotent,
  ставит `prewarm_egress.delay(vpn_id)`. Без прямого `request.data` мимо сериализатора.
- Стиль/`PermissionGate` — как у существующих контролов.

### Task 10: GitHub-gap (документировать/закрыть)
GitHub-путь (App async client, `aist/models.py:173`) proxy не поддерживает.
GitHub обычно cloud (VPN не нужен) → по умолчанию не блокирует. Для
GitHub-Enterprise-за-VPN: либо прокинуть proxy в httpx-транспорт App-клиента,
либо явно документировать ограничение в `aist/integrations/VPN.md`.

### Task 11: обновить `aist/integrations/VPN.md`
Новая строка в таблице «Which traffic goes through VPN»: **UI file blob / findings
snippet → Yes (via warm per-VPN egress)**. Описать lifecycle egress-gateway,
keying по `vpn_integration_id`, idle-TTL, pool cap, отличие от эфемерного
pipeline-sidecar (INV-2), инвариант live-GIT (INV-7).

### Task 12: тесты (реальные сценарии, не смоук)
- blob VPN-проекта, тёплый egress → 200 + байты через прокси (мок fetch).
- blob VPN-проекта, холодный (коннект падает) → 202 + поставлен prewarm-таск.
- blob публичного SCM → без прокси, поведение не изменилось (регресс-гард).
- изоляция: proxy_url для vpn_id A ≠ B; vpn_id из авторизованного объекта.
- одна орг. с двумя VPN-интеграциями → два разных egress, не смешиваются.
- reaper: простаивающий egress (старый mtime лога) гасится; активный — нет.
- pool cap: N+1-й warm вытесняет LRU.
- список из 50 найдингов за VPN: тёплый туннель один на vpn_id, дедуп по файлу.

## Security checklist (cross-cuts 1,2,5,6,9)
- [ ] proxy_url выводится из `vpn_integration_id` авторизованного объекта; выйти
      на чужой туннель нельзя (INV-1).
- [ ] Egress-контейнер: имя per-VPN, tinyproxy `Allow` только web+worker IP.
- [ ] `docker run` креды не логируются (сохранить redaction из `vpn.py`).
- [ ] blob/prewarm — через `AuthorizedQuerySetMixin` + `Product_View`, без прямого `request.data`.
- [ ] Web не получает доступ к docker.sock (INV-4).
- [ ] Никаких байтов исходников в кэше/на диске web-пути и никакого внешнего
      реестра — источник истины Docker (INV-3).
- [ ] Нет privileged/host-network; egress как текущий sidecar.

## Решённые вопросы
- **Список тянет превью — вариант A** (live-фетч целого файла через тёплый
  туннель + viewport-lazy + prewarm + дедуп). B/C не реализуем (см. Task 7).
- **Web ↔ egress — одна docker-сеть.** В `docker-compose.yml` нет блока
  `networks:` → все сервисы (uwsgi, celeryworker, sidecar) в дефолтной сети
  проекта; `vpn.py` поднимает контейнер в сети воркера = та же сеть → web
  достучится до `aist-vpn-egress-<vpn_id>:1080` по имени. Правок в compose нет.

## Open questions
1. `AIST_EGRESS_IDLE_TTL` (default 900с) и `AIST_EGRESS_MAX_WARM` (default 10) —
   утвердить под нагрузку прод (список может держать несколько vpn_id тёплыми).
   Дефолты рабочие, подкрутить по факту.

## Будущая эволюция (не сейчас)
- Миграция egress на **persistent WireGuard** (модель Semgrep Network Broker /
  Snyk Broker) — идеальный end-state для интерактива: idle-туннель ≈ бесплатен,
  handshake ~100мс → «холодный» первый фетч почти мгновенный (важно для списка).
  Высокий риск для рабочего OpenVPN-пайплайна → отдельным планом.
