# Qurbot, Kans Shop va Ketoshop agentlari

Uch public repo Task Manager’ning **private** `agent-public-task.yml` workflow’i
orqali ishlaydi. Public repolarga self-hosted runner qo‘shilmaydi. Codex
implementatsiyani yozadi, lekin GitHub write tokenini olmaydi; alohida
GitHub-hosted publisher tekshirilgan patchdan branch va PR yaratadi. Hozir bu
loyihalarda faqat `@codex` → PR bor; `!fast` faqat Task Manager pilotida qoladi.

| Task Manager loyiha kaliti | GitHub repo | Asosiy branch |
| --- | --- | --- |
| `qurbot` | `muradjanov-dev/qurbot` | `master` |
| `kans-shop` | `muradjanov-dev/kans-shop` | `main` |
| `ketoshop` | `muradjanov-dev/ketoshop` | `master` |

## Yangi loyiha ulash

Repo, asosiy branch, CI job nomlari va production image/container juftliklari
uchun yagona kod manbasi
[`backend/app/services/agent_repos.py`](../backend/app/services/agent_repos.py).
Botdagi suhbat worker’i, public PR publisher va tashqi deploy kuzatuvchisi shu
katalogdan o‘qiydi. Noma’lum repo yoki branch rad etiladi; public repo uchun
CI job va sog‘lom image tekshiruvi ko‘rsatilmasa katalog yuklanmaydi.

Yangi mijoz uchun katalogga bitta aniq yozuv qo‘shing, uning repo/branch
ma’lumotini Task Manager loyihasiga bog‘lang va GitHub tokeniga aynan shu repo
huquqini bering. `GITHUB_AGENT_ALLOWED_REPOS` ham deploy paytida yangilanishi
kerak: katalogdagi yozuv o‘zi kirish huquqini bermaydi. Yangi loyiha avval PR
rejimida sinaladi; `!fast` Task Manager’dan tashqariga avtomatik kengaymaydi.

CI va Task Manager deploy’i reviewed `main` commitida tugagach, shu commitning
toza lokal `main` checkoutidan `bash ops/install_project_catalog.sh`ni ishga
tushiring. Skript production API image SHA’sini tekshiradi, katalogning ayni
nusxasini root-owned server xizmatlariga o‘rnatadi va intake worker’ni qayta
ishga tushiradi. Keyin kichik haqiqiy bot taski bilan PR → CI → merge → exact
SHA deploy → bot xabarini sinang. Eski to‘rt loyiha ushbu yangilanishdan keyin
ham ishlashi kerak.

Botdagi draft suhbat va 3 tagacha rasm ham shu repolarni ko‘radi. Tasdiqdan
oldin task ochilmaydi. Publisher PR ochishdan oldin repo uchun tasdiqlangan
format/lint yoki sintaksis preflightini qiladi. PR havolasi task sahifasida darhol ko‘rinadi,
ammo botdagi **PR tayyor** xabari faqat PRning joriy commitidagi CI muvaffaqiyatli
tugagach yuboriladi. CI qizil bo‘lsa, task sahifasida xato va CI havolasi
ko‘rinadi; shu PR branchiga tuzatish push qilinib, yangi SHA qayta tekshiriladi.
Merge bo‘lgach bot
alohida xabar beradi; deploy faqat GitHub `Deploy` workflow’i aynan merge
commitida muvaffaqiyatli tugab, netcup’dagi tegishli Docker image’lar shu SHA’da
ishlayotgan va health tekshiruvi o‘tganidan keyin `done` bo‘ladi. Qo‘lda
ochilgan, Task Manager agent runiga bog‘lanmagan PRlar bu oqimga kirmaydi.

## Bir martalik yoqish

1. Uch repo egasi bilan AGENTS ko‘rsatma PRlarini merge qiling: [Qurbot](https://github.com/muradjanov-dev/qurbot/pull/3), [Kans Shop](https://github.com/muradjanov-dev/kans-shop/pull/1), [Ketoshop](https://github.com/muradjanov-dev/ketoshop/pull/2). Bu PRlar majburiy texnik dependency emas, lekin agent ularning qoidalarini default branch’dan o‘qishi uchun kerak.
2. Netcup’da `ops/agent_deploy_monitor.py` va
   `ops/update_public_agent_token.py`ni root-owned/read-only
   `/opt/task-manager/ops/`ga, service/timer unitlarini
   `/etc/systemd/system/`ga o‘rnating. `/etc/task-manager/external-monitor.env`
   root-owned 0600 bo‘lsin va mavjud server env’dagi `GITHUB_AGENT_TOKEN` hamda
   `AGENT_CALLBACK_TOKEN` qiymatlarini o‘z ichiga olsin. `systemd-analyze verify`
   va `systemctl enable --now task-manager-external-monitor.timer`ni bajaring.
   Server Compose’da `qurbot-worker` uchun `arq --check
   app.workers.main.WorkerSettings`, `kans-frontend` uchun esa localhost HTTP
   healthcheck qo‘shing. Monitor **har bir** container healthy bo‘lmaguncha
   deployni tasdiqlamaydi.
3. GitHub’da faqat shu uch repo uchun muddati cheklangan fine-grained PAT
   yarating. Repository permissions: **Contents: Read and write** va
   **Pull requests: Read and write**. Tokenni chatga, kodga yoki logga
   yozmang. Mac terminalida `bash scripts/set-public-agent-token.sh`ni
   bajaring. Skript tokenni yashirin qabul qilib, private
   `Asadtop4ik/task-manager` Actions secretiga `AGENT_PUBLIC_REPO_TOKEN` va
   server API env’iga `GITHUB_PUBLIC_AGENT_TOKEN` qilib saqlaydi. Ikkinchisi
   faqat bekor qilingan public PRni yopish uchun ishlatiladi; model uni
   olmaydi. Task Manager’ning mavjud `AGENT_REPO_TOKEN` secretini almashtirmang.
4. Task Manager’dagi uch loyiha uchun yuqoridagi repo va branch’ni manager
   API/UI orqali saqlang. Server env’da
   `GITHUB_AGENT_ALLOWED_REPOS=Asadtop4ik/task-manager,muradjanov-dev/qurbot,muradjanov-dev/kans-shop,muradjanov-dev/ketoshop`
   va `AGENT_PUBLIC_ENABLED=true` qo‘ying; `task-api`ni sog‘lom holda qayta
   yarating. Flagni token, monitor va repo binding tayyor bo‘lmaguncha yoqmang.
5. Har repo uchun bitta kichik `@codex` taskini botning shaxsiy chatidan
   yuboring. Xulosa → tasdiq → markaziy agent run → o‘sha repodagi PR →
   CI → qo‘lda merge → o‘sha repodagi deploy → bot xabari ketma-ketligini
   tekshiring. Mijozning haqiqiy buyurtmasi yoki to‘lovini sinov ma’lumoti
   sifatida ishlatmang.

Muammo bo‘lsa `AGENT_PUBLIC_ENABLED=false` qilib API’ni qayta yarating; bu
Task Manager’ning o‘z agent oqimiga tegmaydi. Monitor hech qachon merge’ni
deploy deb atamaydi: image SHA yoki health mos kelmasa task `review` holatida
qoladi.

[GitHub xavfsizlik yo‘riqnomasi](https://docs.github.com/en/actions/reference/security/secure-use)
self-hosted runnerni public repo PRlariga bevosita ulashdan qaytaradi.
