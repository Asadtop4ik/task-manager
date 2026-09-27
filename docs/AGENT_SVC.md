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
alohida, faqat shu maqsad uchun yaratilgan `agent-codex` hisobi ishlatiladi. Xuddi
shu sabab bilan Node va Codex CLI ham `codex-runner`ning uyidan **nusxalanmaydi**
(uni istalgan GitHub Actions job yozishi mumkin) — ular rasmiy manbadan yuklab
olinib, o'rnatishdan oldin hash bo'yicha tekshiriladi (`ops/agent-svc-node.lock`,
`ops/agent-svc-codex.lock`).

## Arxitektura (matnli diagramma)

```
Task Manager API (tasks.standart-eko.uz/api/v1)
      | lease / heartbeat / stage / callback (HTTPS, servis va callback tokenlar)
      v
agent-svc  — user agent-svc, /opt/agent-svc/current (release symlink), systemd xizmati
  - CodeLane / ChatLane / WatchLoop (har biri alohida thread, mustaqil xato-tiklanish)
  - GitHub API mijozi, bare git mirror'lar (/srv/agent-svc/mirrors)
      | sudo -n -u agent-codex /usr/bin/python3 -I libexec/codex_child.py {prepare,exec,package,preflight,cleanup,discussion}
      v
agent-codex (faqat shu maqsad uchun, codex-runner EMAS; bubblewrap + AppArmor sandbox)
  - CODEX_HOME = .codex-code (kod lane) yoki .codex-chat (suhbat lane)
  - PATH = /opt/agent-svc/node24/bin:/opt/agent-svc/codex-cli/bin (root-owned, pin qilingan,
    rasmiy manbadan yuklab olingan va hash bilan tasdiqlangan)
  - Codex CLI -> gpt-6-luna / gpt-6-sol, faqat /srv/agent-svc/work/<run_id> ichida yozadi
      ^
      | sudo -n -u root /usr/bin/python3 -I libexec/image_state.py <konteyner nomlari>
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
| Pin qilingan asboblar (root-owned, yuklab olingan + hash tekshirilgan) | `/opt/agent-svc/tools/{ruff-0.7.4,ruff-0.16.0,black-26.5.1}` (`ops/agent-svc-tools.lock`), `/opt/agent-svc/node24` (`ops/agent-svc-node.lock`), `/opt/agent-svc/codex-cli` (`ops/agent-svc-codex.lock`) |
| Holat (`agent-svc`, 0750) | `/var/lib/agent-svc/{runs,publish}` |
| Git mirror'lar va ish katalogi (`agent-svc:agentwork`) | `/srv/agent-svc/mirrors` (2750), `/srv/agent-svc/work/<run_id>` (3770 — setgid + sticky, har birini agent-svc yaratadi) |
| Codex uy kataloglari | `/home/agent-codex/.codex-code`, `/home/agent-codex/.codex-chat` |
| Non-secret config | `/etc/agent-svc/config.json` (tekis kalitlar — quyidagi "Konfiguratsiya" bo'limiga qarang) |
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
terminalga yoki logga chiqarilmaydi. Task Manager'ning ikkinchi skripti,
`ops/update_public_agent_token.py`, ham xuddi shu env faylni o'qiydi/yozadi; ikkalasi
ham bitta `env_file_lock.locked_env_file` qulfini (`task-manager.env.lock`, root 0600)
olib ishlaydi, shu bilan bir-birining o'qish/yozishiga aralashmaydi. Har qanday token
`/srv/stack/env/task-manager.env`da almashtirilgandan (rotate) so'ng
`ops/install_agent_svc.sh`ni qayta ishga tushiring (u kredensial sinxronizatsiyasini
qayta bajaradi, 6-qadam) va keyin `sudo systemctl restart agent-svc` qiling — eskirgan
token xotirada saqlanib qolmasin.

## Bir martalik o'rnatish

Owner'ning Mac'idan, toza va reviewed `main`dan (CI o'tgan, production'ga aynan shu
commit deploy qilingan — tekshiruv `ops/install_project_catalog.sh`dagi bilan bir xil):

```sh
bash ops/install_agent_svc.sh
```

Skript avval agent-svc ishlab turgan-turmaganini tekshiradi; ishlab tursa, **eng
boshida bir marta** to'xtatadi (shunda quyidagi hech bir qadam jonli jarayon ostidan
fayl almashtirmaydi) va **eng oxirida bir marta**, hamma narsa (kod, node, codex-cli,
asboblar, kredensiallar, config, unit, sudoers, tmpfiles) joyiga tushgach, qayta
ishga tushiradi. Keyin ketma-ket bajaradi:

1. `agentwork` guruhini, `agent-svc` va `agent-codex` foydalanuvchilarini yaratadi
   (mavjud bo'lmasa; guruh allaqachon mavjud bo'lsa ham xatosiz). Shu bosqichda
   (agar server'da `codex-runner` guruhi mavjud bo'lsa) `agent-codex` shu guruhga
   ham qo'shiladi — **faqat** `/suhbat` (chat lane) uchun Ketoshop diagnostika
   socket'iga (`/run/task-manager-diagnostics/diagnostics.sock`, rejim 0660,
   `ops/task-manager-diagnostics.service`ning `Group=codex-runner`i) o'qish/yozish
   ruxsati bersin deb. Socket'ning o'z guruhi, rejimi yoki
   `ops/diagnostic_host.py`ning discussion-id/lease-id bo'yicha qayta
   avtorizatsiyasi (haqiqiy xavfsizlik chegarasi — shu joyda hech narsa
   yumshatilmaydi) o'zgarmaydi; bu faqat POSIX guruh a'zoligi.
2. Kodni **faqat `git archive $commit`dan** (ishchi katalogdan emas) staging orqali
   `/opt/agent-svc/releases/<commit>/`ga o'rnatadi (agar shu commit uchun release
   allaqachon to'liq mavjud bo'lsa, qayta qurmaydi va **hech qachon** joriy
   release'ni o'chirmaydi), so'ng `/opt/agent-svc/current`ni shu release'ga
   **atomik ravishda** (`ln -sfn` + `mv -T`) qayta yo'naltiradi; `agent_svc`,
   `libexec`, `codex`, `trusted` doim `current/...`ga barqaror simlink bo'lib
   qoladi — sudo qoidalari ham shu barqaror yo'llarga qadalgan, hech qachon
   muayyan release yo'liga emas. So'nggi 3 release (mtime bo'yicha) plyus joriy
   release saqlanadi, qolgani o'chiriladi.
3. Pin qilingan Node 24 va Codex CLI'ni root-owned qilib o'rnatadi — ikkalasi ham
   **rasmiy manbadan yuklab olinadi va hash tekshiriladi**, `codex-runner`ning
   uyidan hech qachon nusxalanmaydi: Node — nodejs.org'dagi rasmiy tarball,
   `ops/agent-svc-node.lock`dagi SHA-256 bilan tasdiqlanadi; Codex CLI —
   `@openai/codex` va uning `linux-x64` platform paketi, `ops/agent-svc-codex.lock`
   dagi npm sha512 integrity bilan tasdiqlanadi, so'ng ikkalasi ham lokal fayldan
   `npm install --global --prefix` bilan o'rnatiladi (shundagina ikkilik
   `codex-cli/bin/codex`da chiqadi). Versiya tekshiruvlari `agent-codex` nomidan
   (root emas) bajariladi.
4. Pin qilingan asboblar uchun venv'lar (`ruff-0.7.4`, `ruff-0.16.0`, `black-26.5.1`)
   — `ops/agent-svc-tools.lock`dagi hash bilan tasdiqlangan (`pip install
   --require-hashes --only-binary=:all:`) paketlardan, faqat mavjud bo'lmasa yoki
   versiya mos kelmasa qayta yaratadi, versiyalarni chop etadi.
5. Codex uy kataloglarini va `luna_worker.toml`ni **AS agent-codex** o'rnatadi
   (`sudo -u agent-codex install ...` — root hech qachon agent-codex'ning uyi
   ichida yozmaydi) va yo'l komponentlaridan biri simlink bo'lsa rad etadi; har
   bir uy uchun `auth.json` bor-yo'qligini (faqat ha/yo'q) chop etadi.
6. `/srv/stack/env/task-manager.env`dagi mos tokenlarni **qiymatlarini
   chiqarmasdan** `/etc/agent-svc/credentials/`ga nusxalaydi
   (`ops/sync_agent_svc_credentials.py`, `env_file_lock` qulfini olib); talab
   qilinadigan kalitlardan biri yo'q/bo'sh bo'lsa, faqat **kalit nomini** chop etib
   xato bilan to'xtaydi (qiymatni hech qachon emas). `AGENT_SVC_TOKEN` yo'q bo'lsa,
   uni generatsiya qilib env faylga atomik qo'shadi va faqat "AGENT_SVC_TOKEN
   created; recreate task-api to load it" yoki "exists" deb chop etadi.
7. `/etc/agent-svc/config.json`ni (faqat mavjud bo'lmasa, hech qachon ustidan
   yozmaydi), systemd unit'ni va sudoers faylini o'rnatadi. Ikkalasi ham avval
   **to'g'ri nomli nusxada** tekshiriladi: unit uchun alohida, xususiy `mktemp -d`
   katalogida (`systemd-analyze verify` nom oldida nuqta bo'lsa "Invalid argument"
   xatosi bilan yiqiladi — bu sudo'ning `#includedir`dagi nuqta-fayl o'tkazib
   yuborishidan butunlay boshqa narsa), keyingina nom oldiga nuqta qo'yilgan
   vaqtinchalik nusxada (`.agent-svc.service.tmp`, `.60-agent-svc.tmp` — systemd va
   sudo ishga tushganda bularni "yashirin" deb o'tkazib yuboradi) asl joyiga
   `mv -T` bilan atomik o'rnatiladi (unit uchun keyin `daemon-reload`, sudoers uchun
   `visudo -c`) va yana bir bor tasdiqlanadi — va tmpfiles qoidalarini o'rnatadi.
8. Agar agent-svc shu skript boshida ishlab turgan bo'lsa, shu yerda qayta ishga
   tushiriladi (yangi kod + config + unit bilan) va holati chop etiladi.

Xizmat oldin ishlab turmagan bo'lsa, shu bosqichda **yoqilmaydi va ishga
tushmaydi**. Yoqish uchun:

```sh
bash ops/install_agent_svc.sh --start
```

Buni faqat quyidagi "Loyiha bo'yicha yoqish" bo'limidagi barcha bayroqlar
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

## Konfiguratsiya

`/etc/agent-svc/config.json` **tekis kalitlar** bilan yoziladi (ichma-ich
`paths`/`lanes`/`models` obyektlari emas) — aynan `agentsvc/agent_svc/config.py`dagi
`load_config` qabul qiladigan shakl, masalan `code_lane_enabled`, `chat_lane_enabled`,
`watch_enabled`, `mirrors_dir`, `codex_home_code`, `model_matrix`, `timeouts`,
`idle_timeout_s`. Noma'lum kalit butun faylni rad etadi. `agentsvc/config.example.json`
shu shaklda, barcha lane'lar o'chirilgan holda (standart o'rnatish uchun xavfsiz).

## Loyiha bo'yicha yoqish

agent-svc har doim o'rnatilgan bo'lishi mumkin, lekin ishlashi ikki mustaqil
darajada cheklangan, ikkalasi ham yoqilmaguncha eski (agent-svc'siz) yo'l ishlayveradi:

1. **Backend darajasi** — server env'dagi `AGENT_LOCAL_EXECUTOR_PROJECTS`
   (vergul bilan ajratilgan loyiha kalitlari, masalan `task-manager,qurbot`)
   qaysi loyihalar umuman agent-svc orqali dispatch qilinishini belgilaydi.
   O'zgartirgach `task-api`ni sog'lom holda qayta yarating.
2. **agent-svc darajasi** — `/etc/agent-svc/config.json`dagi `code_lane_enabled`,
   `chat_lane_enabled`, `watch_enabled` kalitlari standart bo'yicha hammasi
   `false`. Kerakli bayroqni `true` qilib, `sudo systemctl restart agent-svc`
   bilan qayta yuklang.

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
sudo -l -U agent-svc
```

Oxirgi buyruq (`sudo -n -l`ga teng) sudo qoidasi ko'rinayotganini haqiqatda
`codex_child.py`/`image_state.py`ni ishga tushirmasdan tasdiqlaydi — sudoers
faylida endi alohida `--version` yozuvi yo'q, aynan shuning uchun.

To'liq self-check — kredensiallar va Codex CLI'ni haqiqiy lease olmasdan
tekshirish uchun — bitta martalik (transient) systemd unit orqali ishga
tushiriladi, shunda `agent_svc/config.py` xuddi haqiqiy xizmatdagidek
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
   `task-api`ni qayta yarating — yoki faqat mos `*_lane_enabled`/`watch_enabled`ni
   `config.json`da `false` qilib xizmatni qayta yuklang.
2. Kerak bo'lsa butunlay to'xtating va o'chiring:
   ```sh
   sudo systemctl disable --now agent-svc
   sudo rm -f /etc/sudoers.d/60-agent-svc
   sudo systemctl daemon-reload
   ```
3. Bu qadamlar eski, agent-svc'dan oldingi dispatch yo'lini buzmaydi:
   `AGENT_LOCAL_EXECUTOR_PROJECTS`da qolmagan loyihalar avvalgidek ishlayveradi.

Muammo sandbox yoki sudo darajasida bo'lsa, avval faqat `chat_lane_enabled`ni yoqib
kichik suhbat vazifasida tekshiring, `code_lane_enabled`/`watch_enabled`ni faqat
shundan keyin oching.
