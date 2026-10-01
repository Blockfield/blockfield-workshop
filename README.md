# Мастерская Blockfield

Контроллер переключения game/workshop/test, проверка кандидатов карт и отдельный
образ мастерской. Исходники выделены из `blockfield-server/tools/workshop`
с историей; исходная история сервера сохранена.

## Разработка

Нужны Python 3.12+, Node.js 22 и just 1.57.0. Без mise и глобальных установок.

```sh
just setup
just check
just format
just python workshopctl.py --help
```

Linux проверяет весь контроллер. Windows проверяет переносимые ZIP-validator
и journal; production-controller требует Linux, Docker Swarm и `flock`.
Запуск стенда — только через workspace и с проверкой владельцев процессов.

## Образ и интеграция

`Deploy workshop` запускается вручную на `main` после подтверждения владельца.
После проверок обеих ОС публикует `ghcr.io/blockfield/blockfield-workshop:latest`
и `:sha-<полный SHA>`, затем вызывает секрет `DOKPLOY_WORKSHOP_WEBHOOK`.
Dokploy использует `:latest`, существующий GHCR registry и `replicas=0`;
workflow сообщает о постановке Deploy в очередь, проверка rollout — отдельно.
Push в main выполняет только проверки. Для чтения приватного
parent-образа в GHCR `blockfield-server` → Package settings → Manage Actions access
добавьте `Blockfield/blockfield-workshop` с ролью Read. Используется `GITHUB_TOKEN`,
локальные credentials в Actions не копируются. Dockerfile закрепляет
проверенный серверный образ по digest; ресурсы игры не копируются в Git.

Текущая база — сервер v2.60.4 с протоколом 2. Workshop наследует его общие
JAR, `build.json` и PackVersionGate. После парного выпуска с новым серверным
образом обновите immutable digest базы, пересоберите и отдельно
разверните Workshop с `replicas=0`: Deploy игрового сервера его не обновляет.
Порядок — [server DEPLOY](https://github.com/Blockfield/blockfield-server/blob/main/docs/DEPLOY.md).
Перед активацией нужна отдельная проверка подключения клиента к Workshop.

При отдельном клиентском выпуске без нового образа сервера отдельно проверьте
и обновите принятый клиент в Workshop. Game Deploy и его live gate не переносят
это обновление в мастерскую. Автоматизация и live-приёмка этого пути ещё
не подтверждены; новый серверный выпуск только ради него не требуется.

Сервис мастерской сохраняет прежние mounts, healthcheck и `replicas=0`;
каждый ручной запуск Deploy требует отдельного подтверждения владельца.
Кандидаты ZIP используют Python 3.12.14 / zlib 1.3.2; неверный compressor
останавливает snapshot до закрытия входа. `just` и systemd используют pinned Python.
Контроллер остаётся host-systemd service: запускать второй controller в контейнере нельзя.
`/opt/blockfield-workshop` в образе содержит его исходники для установки на хост.

[Операции и протокол файлов](docs/OPERATIONS.md), [systemd](systemd/),
[изолированный E2E](e2e/README.md). Публикация карт и пины находятся
в [blockfield-server/scripts/maps](https://github.com/Blockfield/blockfield-server/tree/main/scripts/maps);
server получает validator и map IDs по полному SHA и checksum.
Исторические архивные релизы не изменяются.

Условия использования: [LICENSE](LICENSE).
