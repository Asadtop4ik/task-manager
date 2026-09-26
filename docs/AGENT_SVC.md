# agent-svc: lokal Codex ishga tushirish xizmati

agent-svc **AI emas** — bu netcup serverida ishlaydigan oddiy systemd xizmati. Uning
vazifasi: Task Manager API'dan navbatdagi taskni lease qilish, Codex CLI child
jarayonini cheklangan `codex-runner` hisobida `sudo` orqali ishga tushirish, natijani
(patch, xabar, review) API'ga qaytarish. Qaysi model ishlatilishi, kod qanday
yozilishi yoki qaror qabul qilish — bularning barchasini Codex (`gpt-6-luna`,
`gpt-6-sol`) qiladi. agent-svc faqat lease/dispetcherlash, sandbox, kredensial va
git mirror infratuzilmasini boshqaradi; u o'zi hech qanday model chaqirmaydi va
hech qanday matnni "o'ylab" javob yozmaydi.

## Arxitektura (matnli diagramma)

```
Task Manager API (tasks.standart-eko.uz/api/v1)
      | lease / heartbeat / stage / callback (HTTPS, servis va callback tokenlar)
      v
agent-svc  — user agent-svc, /opt/agent-svc, systemd xizmati
  - CodeLane / ChatLane / WatchLoop (har biri alohida thread, mustaqil xato-tiklanish)
  - GitHub API mijozi, bare git mirror'lar (/var/lib/agent-svc/mirrors)
      | sudo -n -u codex-runner /usr/bin/python3 libexec/codex_child.py {prepare,exec,package,cleanup}
      v
codex-runner (uid 999, bubblewrap + AppArmor sandbox)
  - CODEX_HOME = .codex-code (kod lane) yoki .codex-chat (suhbat lane)
  - Codex CLI -> gpt-6-luna / gpt-6-sol, faqat /srv/agent-svc/work/<run_id> ichida yozadi
      ^
      | sudo -n -u root /usr/bin/python3 libexec/image_state.py <konteyner nomlari>
      v
root — faqat `docker inspect` orqali image/holat/health o'qiydi, boshqa hech narsa qilmaydi
```

## Foydalanuvchilar, yo'llar, kredensiallar

| Nima | Qiymat |
| --- | --- |
| Xizmat foydalanuvchisi | `agent-svc` (tizim hisobi, `/nonexistent`, `nologin`) |
| Guruhlar | `agent-svc` (asosiy), `agentwork` (umumiy; `codex-runner` ham a'zo) |
| Kod (root-owned, read-only) | `/opt/agent-svc/{agent_svc,libexec,trusted,tools,codex}` |
| Holat (`agent-svc`, 0750) | `/var/lib/agent-svc/{mirrors,publish,runs}` |
| Ish katalogi (`agent-svc:agentwork`, 2770) | `/srv/agent-svc/work/<run_id>/{images,wt,tmp,out}` |
| Codex uy kataloglari | `/home/codex-runner/.codex-code`, `/home/codex-runner/.codex-chat` |
| Non-secret config | `/etc/agent-svc/config.json` |
| Kredensiallar (root 0700, systemd `LoadCredential`) | `/etc/agent-svc/credentials/{agent_svc_token,callback_token,intake_worker_token,github_agent_token,github_public_agent_token,github_qa_token}` |
| sudo qoidalari | `/etc/sudoers.d/60-agent-svc` |
| systemd unit | `/etc/systemd/system/agent-svc.service` |

Kredensial nomlari Task Manager'ning mavjud `/srv/stack/env/task-manager.env`
qiymatlaridan olinadi (`AGENT_CALLBACK_TOKEN`, `INTAKE_WORKER_TOKEN`,
`GITHUB_AGENT_TOKEN`, `GITHUB_PUBLIC_AGENT_TOKEN`, `GITHUB_AGENT_QA_TOKEN`);
`AGENT_SVC_TOKEN` esa agent-svc uchun birinchi o'rnatishda generatsiya qilinadi.
Qiymatlarning o'zi hech qachon terminalga yoki logga chiqarilmaydi.

## Bir martalik o'rnatish

Owner'ning Mac'idan, toza va reviewed `main`dan (CI o'tgan, production'ga aynan shu
commit deploy qilingan — tekshiruv `ops/install_project_catalog.sh`dagi bilan bir xil):

```sh
bash ops/install_agent_svc.sh
```

Skript ketma-ket bajaradi:

1. `agentwork` guruhini va `agent-svc` foydalanuvchisini yaratadi (mavjud bo'lmasa),
   `codex-runner`ni `agentwork`ga qo'shadi.
2. `agent_svc/`, `libexec/`, `codex/`, ishonchli skriptlar nusxasi (`trusted/`) —
   har birini staging katalogi orqali **atomik almashtiradi**, shunda yarim
   ko'chirilgan daraxt hech qachon "jonli" bo'lib qolmaydi.
3. Pin qilingan asboblar uchun venv'lar (`ruff-0.7.4`, `ruff-0.16.0`,
   `black-26.5.1`) — faqat mavjud bo'lmasa yoki versiya mos kelmasa qayta yaratadi,
   versiyalarni chop etadi.
4. Codex uy kataloglarini (mavjud bo'lmasa) va `luna_worker.toml` agent faylini
   o'rnatadi; har bir uy uchun `auth.json` bor-yo'qligini (faqat ha/yo'q) chop etadi.
5. `/srv/stack/env/task-manager.env`dagi mos tokenlarni **qiymatlarini
   chiqarmasdan** `/etc/agent-svc/credentials/`ga nusxalaydi
   (`ops/sync_agent_svc_credentials.py`); `AGENT_SVC_TOKEN` yo'q bo'lsa, uni
   generatsiya qilib env faylga atomik qo'shadi va faqat "AGENT_SVC_TOKEN
   created; recreate task-api to load it" yoki "exists" deb chop etadi.
6. `/etc/agent-svc/config.json`ni (faqat mavjud bo'lmasa, hech qachon
   ustidan yozmaydi), systemd unit'ni, sudoers faylini (`visudo -cf` bilan
   tekshirilgach `/etc/sudoers.d/60-agent-svc`ga 0440 bilan) va tmpfiles
   qoidalarini o'rnatadi, so'ng `systemd-analyze verify` va
   `systemctl daemon-reload` qiladi.

Xizmat bu bosqichda **yoqilmaydi va ishga tushmaydi**. Yoqish uchun:

```sh
bash ops/install_agent_svc.sh --start
```

Buni faqat quyidagi "Loyiha bo'yicha yoqish" bo'limidagi ikkala bayroq ham
o'chirilgan holatda ishga tushiring — shunda birinchi ishga tushishda hech qanday
haqiqiy ish bajarilmaydi, faqat xizmat "bo'sh" holatda kutib turadi.

Codex uy kataloglarida `auth.json` yo'q bo'lsa (skript buni faqat xabar qiladi,
o'zi bajarmaydi), har biriga qo'lda login qiling:

```sh
sudo -u codex-runner env CODEX_HOME=/home/codex-runner/.codex-code \
  /home/codex-runner/.local/bin/codex login --device-auth
sudo -u codex-runner env CODEX_HOME=/home/codex-runner/.codex-chat \
  /home/codex-runner/.local/bin/codex login --device-auth
```

## Loyiha bo'yicha yoqish

agent-svc har doim o'rnatilgan bo'lishi mumkin, lekin ishlashi ikki mustaqil
darajada cheklangan, ikkalasi ham yoqilmaguncha eski (agent-svc'siz) yo'l ishlayveradi:

1. **Backend darajasi** — server env'dagi `AGENT_LOCAL_EXECUTOR_PROJECTS`
   (vergul bilan ajratilgan loyiha kalitlari, masalan `task-manager,qurbot`)
   qaysi loyihalar umuman agent-svc orqali dispatch qilinishini belgilaydi.
   O'zgartirgach `task-api`ni sog'lom holda qayta yarating.
2. **agent-svc darajasi** — `/etc/agent-svc/config.json`dagi
   `lanes.code.enabled`, `lanes.chat.enabled`, `lanes.watch.enabled`
   bayroqlari standart bo'yicha hammasi `false`. Kerakli lane'ni `true` qilib,
   `sudo systemctl restart agent-svc` bilan qayta yuklang.

Yangi lane'ni avval bitta kichik haqiqiy taskda sinab ko'ring, keyin boshqalarini oching.

## Loglar

```sh
journalctl -u agent-svc -o cat | jq .
```

Har bir qator JSON: `ts, level, lane, run_id, task_id, stage, event, duration_ms,
error_type, error`. Kredensial qiymatlari, `gh[pousr]_…`, `github_pat_…`,
`Bearer …`, `sk-…` va JWT'lar avtomatik maskalanadi, xato matni 500 belgigacha qisqartiriladi.

## Self-check

```sh
sudo systemctl is-active agent-svc
sudo systemd-analyze verify /etc/systemd/system/agent-svc.service
sudo -u agent-svc env PYTHONPATH=/opt/agent-svc /usr/bin/python3 -m agent_svc self-check
```

`self-check` tarmoq, kredensial va sudo yo'llarini haqiqiy lease olmasdan tekshiradi.

## Bekor qilish (rollback)

1. Muammoli loyihani `AGENT_LOCAL_EXECUTOR_PROJECTS`dan olib tashlang va
   `task-api`ni qayta yarating — yoki faqat mos `lanes.*.enabled`ni
   `config.json`da `false` qilib xizmatni qayta yuklang.
2. Kerak bo'lsa butunlay to'xtating: `sudo systemctl stop agent-svc`
   (qayta o'z-o'zidan ishga tushmasin desangiz `disable` ham qiling).
3. Ikkala qadam ham eski, agent-svc'dan oldingi dispatch yo'lini buzmaydi:
   `AGENT_LOCAL_EXECUTOR_PROJECTS`da qolmagan loyihalar avvalgidek ishlayveradi.

Muammo sandbox yoki sudo darajasida bo'lsa, avval faqat `chat` lane'ni yoqib
kichik suhbat vazifasida tekshiring, `code`/`watch`ni faqat shundan keyin oching.
