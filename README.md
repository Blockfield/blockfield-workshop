# Blockfield Workshop

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

`Publish workshop image` запускается вручную на `main` после проверок обеих ОС.
Публикует только `ghcr.io/blockfield/blockfield-workshop:sha-<полный SHA>`,
без тега `prod`, webhook или автоматического деплоя. Dockerfile закрепляет
проверенный серверный образ по digest; ресурсы игры не копируются в Git.

Сервис мастерской сохраняет прежние mounts, healthcheck и `replicas=0`;
переключение на новый образ требует отдельного подтверждения Deploy.
Кандидаты ZIP используют Python 3.12.14 / zlib 1.3.2; неверный compressor
останавливает snapshot до закрытия входа. `just` и systemd используют pinned Python.
Контроллер остаётся host-systemd service: запускать второй controller в контейнере нельзя.
`/opt/blockfield-workshop` в образе содержит его исходники для установки на хост.

[Операции и протокол файлов](docs/OPERATIONS.md), [systemd](systemd/),
[изолированный E2E](e2e/README.md). Публикация карт и пины находятся
в [blockfield-server/scripts/maps](https://github.com/Blockfield/blockfield-server/tree/main/scripts/maps);
server получает validator и map IDs по полному SHA и checksum.
Исторические архивные релизы не изменяются.
