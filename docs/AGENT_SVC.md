# agent-svc: lokal Codex ishga tushirish xizmati

agent-svc **AI emas** — bu netcup serverida ishlaydigan oddiy systemd xizmati. Uning
vazifasi: Task Manager API'dan navbatdagi taskni lease qilish, Codex CLI child
jarayonini alohida `agent-codex` hisobida `sudo` orqali ishga tushirish, natijani
(patch, xabar, review) API'ga qaytarish. Qaysi model ishlatilishi, kod qanday
yozilishi yoki qaror qabul qilish — bularning barchasini Codex (`gpt-6-luna`,
`gpt-6-sol`) qiladi. agent-svc faqat lease/dispetcherlash, sandbox, kredensial va
git mirror infratuzilmasini boshqaradi; u o'zi hech qanday model chaqirmaydi va
hech qanday matnni "o'ylab" javob yozmaydi.

Codex hech qachon `codex-runner` hisobida ishlamaydi: shu hisob bir vaqtning o'zida
tirik GitHub Actions self-hosted runner ham bo'lgani uchun, uning uid'i bilan
o'qiladigan har qanday fayl (runner credential'lari, checkout tokenlari, runnerning
o'z Codex `auth.json`si) Codex sandboxi uchun ham ko'rinadi. Shu sabab Codex uchun
alohida, faqat shu maqsad uchun yaratilgan `agent-codex` hisobi ishlatiladi.

## Arxitektura (matnli diagramma)

```
Task Manager API (tasks.standart-eko.uz/api/v1)
      | lease / heartbeat / stage / callback (HTTPS, servis va callback tokenlar)
      v
agent-svc  — user agent-svc, /opt/agent-svc/current (release symlink), systemd xizmati
  - CodeLane / ChatLane / WatchLoop (har biri alohida thread, mustaqil xato-tiklanish)
  - GitHub API mijozi, bare git mirror'lar (/srv/agent-svc/mirrors)
      | sudo -n -u agent-codex /usr/bin/python3 libexec/codex_child.py {prepare,exec,package,cleanup}
      v
agent-codex (faqat shu maqsad uchun, codex-runner EMAS; bubblewrap + AppArmor sandbox)
  - CODEX_HOME = .codex-code (kod lane) yoki .codex-chat (suhbat lane)
  - PATH = /opt/agent-svc/node24/bin:/opt/agent-svc/codex-cli/bin (root-owned, pin qilingan)
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
| Codex ishga tushiruvchi hisob | `agent-codex` (tizim hisobi, `/home/agent-codex` 0700, `nologin`; **`codex-runner` emas**) |
| Guruhlar | `agent-svc`, `agent-codex` (har biri o'z asosiy guruhi), `agentwork` (umumiy; `agent-svc` va `agent-codex` a'zo) |
| Kod (root-owned, read-only) | `/opt/agent-svc/releases/<commit>/{agent_svc,libexec,codex,trusted}`, `/opt/agent-svc/current` shu release'ga simlink, `/opt/agent-svc/{agent_svc,libexec,codex,trusted}` `current/...`ga barqaror simlink |
| Pin qilingan asboblar (root-owned) | `/opt/agent-svc/tools/{ruff-0.7.4,ruff-0.16.0,black-26.5.1}`, `/opt/agent-svc/node24`, `/opt/agent-svc/codex-cli` |
| Holat (`agent-svc`, 0750) | `/var/lib/agent-svc/{runs,publish}` |
| Git mirror'lar va ish katalogi (`agent-svc:agentwork`) | `/srv/agent-svc/mirrors` (2750), `/srv/agent-svc/work/<run_id>` (2770, har birini agent-svc yaratadi) |
| Codex uy kataloglari | `/home/agent-codex/.codex-code`, `/home/agent-codex/.codex-chat` |
| Non-secret config | `/etc/agent-svc/config.json` |
| Kredensiallar (root 0700, systemd `LoadCredential`) | `/etc/agent-svc/credentials/{agent_svc_token,callback_token,intake_worker_token,github_agent_token,github_public_agent_token,github_qa_token}` |
| sudo qoidalari | `/etc/sudoers.d/60-agent-svc` |
| systemd unit | `/etc/systemd/system/agent-svc.service` |

Kredensial nomlari Task Manager'ning mavjud `/srv/stack/env/task-manager.env`
qiymatlaridan olinadi (`AGENT_CALLBACK_TOKEN`, `INTAKE_WORKER_TOKEN`,
`GITHUB_AGENT_TOKEN`, `GITHUB_PUBLIC_AGENT_TOKEN`, `GITHUB_AGENT_QA_TOKEN`);
`AGENT_SVC_TOKEN` esa agent-svc uchun birinchi o'rnatishda generatsiya qilinadi.
`GITHUB_AGENT_QA_TOKEN` ixtiyoriy: uning kredensial fayli baribir yaratiladi
(systemd'ning shu versiyasida `LoadCredential=`ni "yo'q bo'lsa ham mayli" qilib
belgilashning yo'li yo'q), lekin **bo'sh** bo'lishi mumkin — bo'sh qiymat QA
o'chirilganini bildiradi, fayl yo'qligini emas. Qiymatlarning o'zi hech qachon
terminalga yoki logga chiqarilmaydi. Har qanday token `/srv/stack/env/task-manager.env`da
almashtirilgandan (rotate) so'ng `ops/install_agent_svc.sh`ni qayta ishga tushiring
(u kredensial sinxronizatsiyasini qayta bajaradi, 6-qadam) va keyin
`sudo systemctl restart agent-svc` qiling — eskirgan token xotirada saqlanib qolmasin.

## Bir martalik o'rnatish

Owner'ning Mac'idan, toza va reviewed `main`dan (CI o'tgan, production'ga aynan shu
commit deploy qilingan — tekshiruv `ops/install_project_catalog.sh`dagi bilan bir xil):

```sh
bash ops/install_agent_svc.sh
```

Skript ketma-ket bajaradi:

1. `agentwork` guruhini va `agent-svc` foydalanuvchisini yaratadi (mavjud bo'lmasa).
2. Kodni **faqat `git archive $commit`dan** (ishchi katalogdan emas) staging orqali
   `/opt/agent-svc/releases/<commit>/`ga o'rnatadi, so'ng `/opt/agent-svc/current`ni
   shu release'ga **atomik ravishda** (`ln -sfn` + `mv -T`) qayta yo'naltiradi;
   `agent_svc`, `libexec`, `codex`, `trusted` doim `current/...`ga barqaror simlink
   bo'lib qoladi — sudo qoidalari ham shu barqaror yo'llarga qadalgan, hech qachon
   muayyan release yo'liga emas. Agar agent-svc allaqachon ishlab turgan bo'lsa,
   almashtirishdan oldin to'xtatiladi va keyin qayta ishga tushiriladi (buni skript
   o'zi chop etadi). Faqat so'nggi 3 release saqlanadi.
3. Pin qilingan Node 24 va Codex CLI'ni root-owned qilib o'rnatadi: Node — GitHub
   Actions runner'ning `externals/node24`idan nusxa (`v24` bilan boshlanishi
   tekshiriladi), Codex CLI — `@openai/codex@0.156.1` shu node/npm bilan
   `/opt/agent-svc/codex-cli`ga (`codex --version` 0.156.1 ekanini tasdiqlaydi).
4. Pin qilingan asboblar uchun venv'lar (`ruff-0.7.4`, `ruff-0.16.0`, `black-26.5.1`)
   — `ops/agent-svc-tools.lock`dagi hash bilan tasdiqlangan (`pip install
   --require-hashes --only-binary=:all:`) paketlardan, faqat mavjud bo'lmasa yoki
   versiya mos kelmasa qayta yaratadi, versiyalarni chop etadi.
5. `agent-codex` foydalanuvchisini (`/home/agent-codex` 0700) yaratadi, `agentwork`ga
   qo'shadi, Codex uy kataloglarini va `luna_worker.toml`ni **AS agent-codex**
   o'rnatadi (`sudo -u agent-codex install ...` — root hech qachon agent-codex'ning
   uyi ichida yozmaydi) va yo'l komponentlaridan biri simlink bo'lsa rad etadi; har
   bir uy uchun `auth.json` bor-yo'qligini (faqat ha/yo'q) chop etadi.
6. `/srv/stack/env/task-manager.env`dagi mos tokenlarni **qiymatlarini
   chiqarmasdan** `/etc/agent-svc/credentials/`ga nusxalaydi
   (`ops/sync_agent_svc_credentials.py`, fayl qulfini — `flock` — olib); talab
   qilinadigan kalitlardan biri yo'q/bo'sh bo'lsa, faqat **kalit nomini** chop etib
   xato bilan to'xtaydi (qiymatni hech qachon emas). `AGENT_SVC_TOKEN` yo'q bo'lsa,
   uni generatsiya qilib env faylga atomik qo'shadi va faqat "AGENT_SVC_TOKEN
   created; recreate task-api to load it" yoki "exists" deb chop etadi.
7. `/etc/agent-svc/config.json`ni (faqat mavjud bo'lmasa, hech qachon ustidan
   yozmaydi), systemd unit'ni va sudoers faylini — ikkalasini ham avval nom oldiga
   nuqta qo'yilgan vaqtinchalik nusxada (`.agent-svc.service.stage`,
   `.60-agent-svc.tmp` — systemd va sudo bularni "yashirin" deb e'tiborsiz
   qoldiradi) `systemd-analyze verify` / `visudo -cf` bilan tekshirib, shundan
   keyingina asl joyiga ko'chiradi (yana bir bor tasdiqlaydi: `visudo -c`,
   `systemd-analyze verify`) — va tmpfiles qoidalarini o'rnatadi.

Xizmat bu bosqichda **yoqilmaydi va ishga tushmaydi** (agar allaqachon ishlab
turmagan bo'lsa). Yoqish uchun:

```sh
bash ops/install_agent_svc.sh --start
```

Buni faqat quyidagi "Loyiha bo'yicha yoqish" bo'limidagi ikkala bayroq ham
o'chirilgan holatda ishga tushiring — shunda birinchi ishga tushishda hech qanday
haqiqiy ish bajarilmaydi, faqat xizmat "bo'sh" holatda kutib turadi.

Codex uy kataloglarida `auth.json` yo'q bo'lsa (skript buni faqat xabar qiladi,
o'zi bajarmaydi), har biriga qo'lda login qiling:

```sh
sudo -u agent-codex env CODEX_HOME=/home/agent-codex/.codex-code \
  /opt/agent-svc/node24/bin/node /opt/agent-svc/codex-cli/bin/codex login --device-auth
sudo -u agent-codex env CODEX_HOME=/home/agent-codex/.codex-chat \
  /opt/agent-svc/node24/bin/node /opt/agent-svc/codex-cli/bin/codex login --device-auth
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

Tezkor holat tekshiruvi:

```sh
sudo systemctl is-active agent-svc
sudo systemd-analyze verify /etc/systemd/system/agent-svc.service
```

To'liq self-check — kredensiallar, sudo yo'llari va Codex CLI'ni haqiqiy lease
olmasdan tekshirish uchun — bitta martalik (transient) systemd unit orqali
ishga tushiriladi, shunda `agent_svc/config.py` xuddi haqiqiy xizmatdagidek
`$CREDENTIALS_DIRECTORY` orqali kredensiallarni oladi (oddiy `sudo -u agent-svc`
buni bermaydi — LoadCredential faqat systemd boshqargan unit ichida ishlaydi):

```sh
sudo systemd-run --quiet --wait --pipe --collect \
  --unit=agent-svc-selfcheck \
  --property=User=agent-svc --property=Group=agent-svc \
  --property=SupplementaryGroups=agentwork \
  --property=WorkingDirectory=/opt/agent-svc \
  --property=Environment=PYTHONPATH=/opt/agent-svc \
  --property=LoadCredential=agent_svc_token:/etc/agent-svc/credentials/agent_svc_token \
  --property=LoadCredential=callback_token:/etc/agent-svc/credentials/callback_token \
  --property=LoadCredential=intake_worker_token:/etc/agent-svc/credentials/intake_worker_token \
  --property=LoadCredential=github_agent_token:/etc/agent-svc/credentials/github_agent_token \
  --property=LoadCredential=github_public_agent_token:/etc/agent-svc/credentials/github_public_agent_token \
  --property=LoadCredential=github_qa_token:/etc/agent-svc/credentials/github_qa_token \
  /usr/bin/python3 -m agent_svc self-check
```

`--collect` transient unit'ni tugagach avtomatik tozalaydi; `--wait --pipe`
natijani to'g'ridan-to'g'ri terminalga oqizadi.

## Bekor qilish (rollback)

1. Muammoli loyihani `AGENT_LOCAL_EXECUTOR_PROJECTS`dan olib tashlang va
   `task-api`ni qayta yarating — yoki faqat mos `lanes.*.enabled`ni
   `config.json`da `false` qilib xizmatni qayta yuklang.
2. Kerak bo'lsa butunlay to'xtating va o'chiring:
   ```sh
   sudo systemctl disable --now agent-svc
   sudo rm -f /etc/sudoers.d/60-agent-svc
   sudo systemctl daemon-reload
   ```
3. Bu qadamlar eski, agent-svc'dan oldingi dispatch yo'lini buzmaydi:
   `AGENT_LOCAL_EXECUTOR_PROJECTS`da qolmagan loyihalar avvalgidek ishlayveradi.

Muammo sandbox yoki sudo darajasida bo'lsa, avval faqat `chat` lane'ni yoqib
kichik suhbat vazifasida tekshiring, `code`/`watch`ni faqat shundan keyin oching.
