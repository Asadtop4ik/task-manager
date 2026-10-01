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
eski GitHub Actions runner hisobi (xizmat to'xtatilgan, hisob qolgan) bo'lgani uchun, uning uid'i bilan
o'qiladigan har qanday fayl (runner credential'lari, checkout tokenlari, runnerning
o'z Codex `auth.json`si) Codex sandboxi uchun ham ko'rinadi. Shu sabab Codex uchun
alohida, faqat shu maqsad uchun yaratilgan `agent-codex` hisobi ishlatiladi. Xuddi
shu sabab bilan Node va Codex CLI ham `codex-runner`ning uyidan **nusxalanmaydi**
(uni istalgan GitHub Actions job yozishi mumkin) — ular rasmiy manbadan yuklab
olinib, o'rnatishdan oldin hash bo'yicha tekshiriladi (`ops/agent-svc-node.lock`,
`ops/agent-svc-codex.lock`).

## Qolgan GitHub Actions workflow'lari

Eski runner-asosli workflow'lar (`agent-task`, `agent-public-task`, `agent-pr-review`
va `agent-run-release`dagi `correction` joblari) olib tashlandi: implement, review
va correction faqat agent-svc'da bajariladi. Faqat GitHub-hosted (`ubuntu-latest`)
workflow'lar qoldi: `ci.yml`, `deploy.yml`, `agent-run-release.yml` (`agent_run_merge`
— egasining Merge tugmasi local runlar uchun ham shu workflow'ni dispatch qiladi) va
`agent-auto-merge.yml` (agent-svc review tugagach `agent_review_completed` yuboradi).
`!fast` o'chirilgan; qayta yoqish uchun yangi publisher kerak bo'ladi.

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
   `task-diag-client` degan **alohida** tizim guruhi ham yaratiladi (mavjud
   bo'lmasa) va (agar `codex-runner` guruhi mavjud bo'lsa) `codex-runner` shu
   guruhga qo'shiladi. Bu guruh **faqat** Ketoshop diagnostika socket'iga
   (`/run/task-manager-diagnostics/diagnostics.sock`, rejim 0660) tegishli:
   `ops/diagnostic_host.py` bind qilgandan keyin socket'ni shu guruhga
   o'tkazadi (`chgrp`, faqat guruh mavjud bo'lsa — aks holda hech narsa
   qilmaydi, shuning uchun o'rnatish tartibi eski `codex-runner` yo'lini hech
   qachon buzmaydi). **`agent-codex` bu guruhning doimiy a'zosi emas** —
   `codex-runner`dan farqli o'laroq (u umuman jonli GitHub Actions runner
   hisobi, "bitta socket o'qishi mumkin"dan ancha kengroq huquq bilan);
   `agent-codex` bu guruhni faqat `discussion` kichik buyrug'ini ishga
   tushirgan bitta `sudo` chaqiruvi davomida oladi (`sudo -g task-diag-client`,
   `ops/agent-svc.sudoers`dagi alohida qoida, boshqa besh kichik buyruqqa
   taalluqli emas). Socket'ning o'zi, rejimi yoki
   `ops/diagnostic_host.py`ning discussion-id/lease-id bo'yicha qayta
   avtorizatsiyasi (haqiqiy xavfsizlik chegarasi — shu joyda hech narsa
   yumshatilmaydi) o'zgarmaydi. **Eslatma:** `task-diag-client` guruhi
   yaratilgandan so'ng `task-manager-diagnostics` xizmati kamida bir marta
   qayta ishga tushirilishi kerak (`sudo systemctl restart
   task-manager-diagnostics`), shundagina ishlab turgan socket yangi guruhga
   o'tadi — bu qadam shu skriptning tashqarisida, chunki u agent-svc'ga
   tegishli emas.
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

## Tuzatish (correction) uchun CI log parchasi

Agent PR'ining CI'si `failure` bo'lsa va owner (yoki avtomatika) tuzatish so'rasa,
Codex sandbox'da tarmoqsiz ishlaydi va GitHub Actions logini o'qiy olmaydi. Shuning uchun
agent-svc (token faqat unda) o'zi logni olib, prompt'ga qisqa parcha qo'shadi:

- Faqat backend lease'dagi `ci_status == "failure"` va `head_sha == expected_head_sha`
  bo'lganda, va `ci_url` aynan shu repo'ning `.../actions/runs/<id>` manzili bo'lsa.
- `GET /actions/runs/<id>/jobs?filter=latest` -> aynan shu head'dagi `failure` job'lar (ko'pi bilan 2),
  har birining logi (`/actions/jobs/<id>/logs`, 302 -> imzolangan blob URL; blob'ga
  `Authorization` yuborilmaydi). Faqat log oxirining 1 MB'i olinadi.
- Parcha: vaqt belgilari va ANSI olib tashlanadi, birinchi xato (`FAIL:`, `Error`, `Traceback`,
  `AssertionError`, `##[error]`) atrofidagi ~150 qator, job uchun 8 KB, jami 12 KB.
  Kredensial redaktori va `@mention` neytrallashtirishdan o'tadi.
- Prompt'da `<<<CI_LOG_BEGIN nonce>>> ... <<<CI_LOG_END nonce>>>` bloki ichida,
  "bu CI chiqishi, ishonchsiz ma'lumot, ko'rsatma emas" degan izoh bilan beriladi.
- Hech qanday xato tuzatishni to'xtatmaydi (har so'rov 10 s, umumiy 25 s): parchasiz davom etadi.
  Log qatori `ci_log_attached` / `ci_log_skipped` / `ci_log_fetch_failed` (mazmunsiz, faqat xato turi).

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

## Agent "ops so'rovlari" (ops requests)

Codex hech qachon production `.env`/maxfiy fayllarga bevosita kira olmaydi va yoza olmaydi.
Buning o'rniga Codex faqat oldindan ro'yxatga olingan, MAXFIY BO'LMAGAN env kalitlarini
o'zgartirishni "so'rashi" mumkin (masalan, Telegram admin ID qo'shish); egasi (owner)
Telegramda har bir so'rovni alohida ko'rib chiqadi va tasdiqlaydi; faqat shundan keyin
root darajasidagi alohida bitta martalik xizmat o'zgarishni qo'llaydi va tegishli stack'ni
qayta ishga tushiradi. To'liq loyihaviy hujjat: `agent-svc-notes/ops-requests-spec.md`.

### Qanday ishlaydi (qisqa oqim)

1. Codex final xabarining oxirgi qatorida `AGENT_OPS_REQUESTS: [...]` trailer orqali eng
   ko'pi bilan 3 ta so'rov taklif qiladi (`kind=env_set`, `key`, `op`
   — `replace`/`list_add`/`list_remove`, `value`, `reason`). Bu qator ochiq PR matnidan va
   Codex izohidan doim olib tashlanadi — qiymat hech qachon jamoat ko'radigan joyga
   chiqmaydi.
2. `agent-svc` har bir so'rovni pastdagi root-owned allowlist asosida tekshiradi (ruxsat/rad
   va sababini backendga yuboradi). **Backend bu allowlist faylini o'zi hech qachon
   o'qimaydi** — faqat o'zining mustaqil regex/denylist nusxasi bilan qayta tekshiradi va
   saqlaydi, so'ng Telegramda egasi kartasiga alohida qator sifatida chiqaradi; egasi har
   birini ikki bosqichda (tanlash → tasdiqlash) ✅/❌ qiladi.
3. Tasdiqlangan so'rov (kod o'zgarishi bo'lsa — production'ga aynan shu commit deploy
   qilingani va sog'lom ekani tasdiqlangandan keyin) `agent-svc`ning alohida "ops lane"
   oqimi orqali `/run/agent-svc/ops/request.json` faylini (0640, egasi `agent-svc`) yozadi
   va `sudo -n systemctl start agent-ops-apply.service`ni chaqiradi.
4. `agent-ops-apply.service` — ROOT sifatida ishlaydigan alohida bitta martalik
   (`Type=oneshot`) xizmat (`ops/agent-ops-apply.service`), `agent-svc.service`ning bir
   qismi EMAS — o'zining alohida mount namespace'ida, PID 1 tomonidan har safar yangidan
   ishga tushiriladi (shuning uchun `agent-svc.service`ning `InaccessiblePaths=`i yoki
   uni to'xtatish bu xizmatga ta'sir qilmaydi). U `agentsvc/libexec/env_apply.py apply`ni
   bajaradi: so'rovni **mustaqil ravishda** root-owned allowlist bo'yicha qayta tekshiradi
   (agent-svc'ga hech qachon ishonmaydi — u ham buzilgan bo'lishi mumkin), tegishli
   konteynerlarni `docker inspect` qiladi (hammasi ishlab turishi va bitta xil image
   SHA'siga ega bo'lishi shart), env faylni qulflab (`flock`, 30 soniya urinib ko'radi)
   bitta qatorni o'zgartiradi, `docker compose -f stacks/<stack>.yml up -d --no-deps
   --force-recreate --pull never --wait` bilan qayta ishga tushiradi va natijani
   tekshiradi (running/healthy yoki qayta ishga tushishlar soni barqaror, image SHA,
   `Config.Env`dagi aniq qiymat, ixtiyoriy `ready_url`). Xato bo'lsa — avtomatik orqaga
   qaytaradi (eski qiymatni tiklaydi, konteynerni qayta yaratadi, eski qiymat ishlab
   turganini tasdiqlaydi).
5. Natija (hech qachon qiymatning o'zi emas — faqat kod, xabar va sha256 hash'lar)
   `/var/lib/agent-ops/results/<request_id>.json`ga yoziladi va Telegram kartasida
   ko'rinadi.

### Loyihani allowlist'ga qo'shish

`/etc/agent-svc/ops-allowlist.json` (root:root 0644, world-readable bo'lishi SHART — aks
holda `agent-svc` uni ochib bo'lmay `not_accessible` bilan rad etadi) — buni FAQAT `agent-svc` (taklif
validatsiyasi uchun) va `agent-ops-apply.service`/`env_apply.py` (qo'llashdan oldin mustaqil
qayta tekshirish uchun) o'qiydi, har biri alohida-alohida root-owned, group/world-writable
bo'lmagan, simlink bo'lmagan holda tekshiradi — **backend bu faylni umuman o'qimaydi** (yuqoriga
qarang). Standart o'rnatish (`install_agent_svc.sh`) `ops/ops-allowlist.example.json`ni FAQAT
bu fayl mavjud bo'lmasa nusxalaydi (`{"version":1,"projects":{}}` — funksiya butunlay
o'chirilgan holat). Yangi loyiha qo'shish uchun shu faylni **qo'lda**, serverda, root sifatida
tahrirlang, masalan:

```json
{"version": 1, "projects": {"qurbot": {
  "repo_full_name": "muradjanov-dev/qurbot", "stack": "qurbot",
  "env_file": "/srv/stack/env/qurbot.env",
  "services": ["qurbot-web", "qurbot-worker"],
  "containers": [["qurbot-web","ghcr.io/muradjanov-dev/qurbot"],
                 ["qurbot-worker","ghcr.io/muradjanov-dev/qurbot"]],
  "ready_url": null,
  "keys": {"ADMIN_TG_IDS": {"format":"json_int_list","ops":["list_add","list_remove"],
    "item_re":"[1-9][0-9]{4,14}","max_items":50,"protected_items":[917456291],
    "description":"Telegram admin IDs"}}}}}
```

Tekshiruv qoidalari (`scripts/agent_ops_policy.py` — buni `agent-svc` va `env_apply.py`
ishonchli nusxa sifatida fayl yo'li orqali mustaqil yuklaydi; backend bu modulni hech
qachon import qilmaydi, faqat o'zining alohida regex/denylist nusxasi bilan qayta
tekshiradi): `task-manager` va `agent-qa` loyihalari
kod ichida qattiq taqiqlangan (`task-manager.env`da agent-svc/GitHub tokenlari bor;
task-api'ni qayta ishga tushirish shu lane o'zi hisobot beradigan API'ni uzadi);
`env_file` doim `/srv/stack/env/<stack>.env` shakliga mos bo'lishi shart; `containers`
faqat `backend/app/services/agent_repos.py`dagi shu loyiha uchun ro'yxatga olingan
image'lardan bo'lishi kerak; kalit nomlari maxfiy ko'rinadigan so'zlarni (`SECRET`,
`TOKEN`, `PASSW`, `KEY`, `URL`, `HOST`, `DATABASE` va h.k.) o'z ichiga olishi mumkin
emas. Faylni o'zgartirgandan keyin xizmatni qayta ishga tushirish shart emas — har bir
so'rov qayta tekshirilganda fayl yangidan o'qiladi.

`env_file` (masalan `/srv/stack/env/qurbot.env`)ning o'zi **root yoki `deploy` xizmat
foydalanuvchisi** tomonidan egallangan bo'lishi shart (mode 600) — `env_apply.py` ikkalasini
ham qabul qiladi (`deploy`ning uid'i har safar `pwd.getpwnam("deploy")` orqali runtime'da
aniqlanadi, hech qachon qattiq kodlangan 1000 emas; bu foydalanuvchi serverda yo'q bo'lsa,
faqat root qabul qilinadi). Boshqa har qanday egasi `env_wrong_owner` bilan rad etiladi.
Qayta yozishda asl fayl egasi/guruhi/mode'i har doim aynan saqlanadi (hech qachon
o'zgartirilmaydi).

`restart_services` (Telegram kartasida ko'rsatiladigan, so'rov qaysi xizmatlarni qayta
ishga tushirishi) — bu allowlist'dan taklif VAQTIDA olingan bir martalik rasm (snapshot):
agar operator allowlist'ni tasdiqlash bilan qo'llash orasida tahrirlasa, karta hali ham
eski ro'yxatni ko'rsatadi (qo'llash o'zi doim ENG YANGI allowlist bo'yicha ishlaydi — faqat
kartadagi matn eskirishi mumkin). Amaliy jihatdan ahamiyatsiz (allowlist tez-tez
o'zgarmaydi), lekin bilib qo'yish kerak.

### O'chirish tugmalari (kill switches)

- **Butunlay o'chirish**: `/etc/agent-svc/config.json`da `ops_lane_enabled: false`
  (standart o'rnatishda shunday) — `sudo systemctl restart agent-svc`.
- **Bitta loyihani o'chirish**: uni `/etc/agent-svc/ops-allowlist.json`dan olib
  tashlang — fayl har safar so'rov kelganda qayta o'qiladi, xizmatni qayta yuklash
  shart emas.
- **Hammasini o'chirish, faylni o'chirmasdan**: allowlist'ni
  `{"version":1,"projects":{}}` holatiga qaytaring.
- **sudo qoidasini olib tashlash** (oxirgi chora): `/etc/sudoers.d/60-agent-svc`dan
  `agent-ops-apply.service` qatorini olib tashlang (yoki `ops/agent-svc.sudoers`dan olib
  tashlab qayta o'rnating) — shunda `agent-svc` bu xizmatni umuman ishga tushira olmaydi.

### Qo'lda orqaga qaytarish (manual rollback)

Avtomatik rollback ham muvaffaqiyatsiz bo'lsa (natija kodi `failed_rollback_failed`, 🚨
kartada belgilanadi), yoki operator boshqa sababga ko'ra bitta so'rovni qo'lda tekshirmoqchi
bo'lsa:

```sh
sudo /usr/bin/python3 -I /opt/agent-svc/libexec/env_apply.py rollback --request-id <uuid>
```

Bu `/var/lib/agent-ops/backups/<loyiha>/<request_id>.json`dagi zaxiradan foydalanadi (har bir
zaxira ham JSON metama'lumot, ham so'rovdan OLDINGI holatdagi env faylning **to'liq nusxasi**
(`<request_id>.env.bak`) — agar bu modulning qator-almashtirish mantig'ida qandaydir xato
bo'lsa ham, oxirgi chora sifatida shu nusxadan qo'lda tiklash mumkin; shu sabab katalog 0700
root:root). Joriy env qator uch holatdan biriga to'g'ri kelishi kerak: **(a)** hali ham shu
so'rov yozgan YANGI qiymat — odatiy holat, CLI eski qiymatni yozadi, keyin qayta yaratadi va
tekshiradi; **(b)** avtomatik rollback ALLAQACHON eski qiymatni tiklagan (masalan
`failed_rollback_failed` — env fayl to'g'ri, faqat konteyner qayta yaratilmagan yoki
tekshiruv o'tmagan) — bu holda CLI faylni QAYTA YOZMAYDI, faqat joriy pin qilingan image
tag bilan qayta yaratadi va tekshiradi; **(c)** boshqa (uchinchi) qiymat — demak oraliqda
kimdir/nimadir qatorni allaqachon o'zgartirgan — CLI "qator o'zgargan" xatosi bilan rad
etadi va HECH NARSANI yozmaydi. Image tag har doim **joriy** ishlab turgan konteynerlardan
qulf ichida qayta o'qiladi — zaxiradagi eski tegdan HECH QACHON emas (aks holda keyingi bir
deploy'dan keyingi rollback image'ni eskisiga "pasaytirib" qo'yishi mumkin edi). Bu buyruq
natijasi asl so'rovning `results/<request_id>.json`ini HECH QACHON ustidan yozmaydi — alohida
`results/<request_id>.rollback.json`ga yoziladi, shunda asl muvaffaqiyatli/muvaffaqiyatsiz
natija doim ko'rinib turadi. Bu buyruq `/etc/sudoers.d/60-agent-svc`da YO'Q — faqat operator
serverga bevosita kirib, qo'lda ishga tushiradi. Oxirgi chora sifatida — mos zaxira topilmasa
yoki bu ham ishlamasa — yuqoridagi `.env.bak` nusxasini qo'lda joyiga nusxalang (yoki env
faylni qo'lda tahrirlang) va
`/srv/stack/scripts/deploy.sh <stack> <hozirgi-ishlab-turgan-sha>`ni ishga tushiring.

**Bilinadigan cheklov:** `env_apply.py` hozircha o'zining ichki umumiy vaqt byudjetini
kuzatmaydi (faqat `agent-ops-apply.service`ning `TimeoutStartSec=900`i tashqi chegara
sifatida bor) — juda sekin `docker compose`/tarmoq holatida nazariy jihatdan shu tashqi
limitga urilib, natija hech qachon yozilmasdan to'xtatilishi mumkin (kam ehtimol, lekin
mumkin). Server aylanishida kuzatiladigan keyingi ish sifatida qoldirilgan.

### Audit va loglar

- `/var/log/agent-ops/audit.jsonl` — har bir urinish uchun bitta JSON qator (vaqt,
  request_id, run_id, loyiha, kalit, amal, qiymatlarning FAQAT sha256 hash'lari, natija
  kodi, qayta ishga tushirilganmi, `env_restored` (env fayl niyat qilingan holatga
  qaytarilganmi), `rollback_message` (rollback urinishining o'z natijasi, alohida kichik
  lug'at), image tag, davomiylik). Qiymatning o'zi bu yerga hech qachon yozilmaydi.
- `journalctl -t agent-ops` — audit qatorining syslog orqali ko'chirmasi.
- `/var/log/agent-ops/compose-<request_id>.log` (root-only, 0700 katalog ichida) — shu
  so'rov uchun `docker compose`ning to'liq chiqishi, diagnostika uchun; bu yerda ham
  env qiymatlari ko'rinmaydi — compose faqat `PATH`, `IMAGE_TAG`, `DOCKER_CONFIG`,
  `HOME`ni oladi, butun env faylni emas.
- `/var/lib/agent-ops/results/<request_id>.json` (0640 root:agent-svc) — shu so'rovning
  yakuniy natijasi: `code`, `exit`, `message`, `rollback_message`, `rolled_back`,
  `restarted`, `env_restored`, `image_tag`, `request_hash` — hech qachon qiymat;
  `agent-svc` buni o'qib API'ga qaytaradi. Qo'lda `rollback` CLI ishga tushirilsa, natijasi
  shu faylni EMAS, `<request_id>.rollback.json`ni yozadi (yuqoriga qarang).
- **Tezlik chegarasi**: bitta loyiha uchun soatiga eng ko'pi bilan 5 ta URINISH
  (`/var/lib/agent-ops/ratelimit.json`, root-only, faqat hisoblagich — qiymat saqlanmaydi).
  Bu konteyner tekshiruvidan (`docker inspect` — hammasi ishlab turishi va bitta xil image
  SHA'siga ega bo'lishi) O'TGAN har bir urinishni hisoblaydi — FAQAT muvaffaqiyatli
  qo'llashlarni emas: konteyner tekshiruvidan o'tolmagan urinish (masalan
  `services_containers_mismatch`) hisoblanmaydi, lekin undan keyingi (env faylni
  o'zgartirishga yaqinlashgan) har bir urinish — muvaffaqiyatli yoki keyinroq
  muvaffaqiyatsiz bo'lsa ham — hisoblanadi. Chegaradan oshsa `refused`/`rate_limited`
  bilan rad etiladi — bu YAKUNIY (terminal) `refused` natija, agent-svc uni avtomatik
  qayta urinmaydi. Bu buzilgan/xato agent-svc'ning bitta loyihani qayta-qayta urinishiga
  qarshi qo'shimcha himoya, egasi tasdiqlagan alohida so'rovlarga odatda hech qachon
  tegmaydi.

**Muhim:** `/run/agent-svc/ops` katalogini `agent-svc.tmpfiles` YARATMAYDI — sababi
`agent-svc.service`ning o'zi `RuntimeDirectory=agent-svc`dan foydalanadi, ya'ni
`/run/agent-svc`ning butun umrini systemd boshqaradi (har safar xizmat qayta ishga
tushganda 0755 agent-svc:agent-svc holida yangidan yaratiladi, to'xtaganda esa butunlay
o'chiriladi). Shu sababli `/run/agent-svc/ops`ni (0750 agent-svc:agent-svc) **agent-svc
kodining o'zi** yaratishi kerak — bir marta ishga tushishda yoki `request.json`ni birinchi
yozishdan oldin (batafsili: `ops/agent-svc.tmpfiles`dagi izoh).

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
