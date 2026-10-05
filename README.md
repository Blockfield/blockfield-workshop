# Мастерская Blockfield

Контроллер переключения game/workshop/test и проверка кандидатов карт. Исходники выделены из `blockfield-server/tools/workshop`
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

Отдельного образа у мастерской нет. Сервис `blockfield-workshop` в Dokploy
использует тег `ghcr.io/blockfield/blockfield-server:latest` (без digest) с
`BLOCKFIELD_PROFILE=workshop`; WorldEdit сервер добавляет сам в этом профиле.
Swarm скачивает тег заново при каждом масштабировании 0→1 (при недоступном
registry берёт локальный образ, обновлённый Deploy игры). Мастерская поэтому
запускается на текущем образе игры с теми же модами, `build.json` и
PackVersionGate: после выпуска сервера или клиента её не нужно пересобирать
и деплоить. Не закрепляйте этот сервис по digest — он перестанет следовать за игрой.

Сервис мастерской сохраняет прежние mounts, env, healthcheck и `replicas=0`.
Кандидаты ZIP используют Python 3.12.14 / zlib 1.3.2; неверный compressor
останавливает snapshot до закрытия входа. `just` и systemd используют pinned Python.
Контроллер остаётся host-systemd service: запускать второй controller в контейнере нельзя.
На хост он ставится из checkout этого репозитория (см. OPERATIONS).

[Операции и протокол файлов](docs/OPERATIONS.md), [systemd](systemd/),
[изолированный E2E](e2e/README.md). Публикация карт и пины находятся
в [blockfield-server/scripts/maps](https://github.com/Blockfield/blockfield-server/tree/main/scripts/maps);
server получает validator и map IDs по полному SHA и checksum.
Исторические архивные релизы не изменяются.

Условия использования: [LICENSE](LICENSE).
