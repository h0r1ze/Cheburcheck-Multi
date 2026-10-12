## ⬇ Скачать

| Система | Файл | Python нужен? |
|---|---|:-:|
| 🪟 **Windows 10 / 11** (x64) | [Cheburcheck-Multi-Windows-x64.exe](https://github.com/h0r1ze/Cheburcheck-Multi/releases/download/{{TAG}}/Cheburcheck-Multi-Windows-x64.exe) | нет |
| 🐧 **Linux** x64 | [Cheburcheck-Multi-Linux-x64.tar.gz](https://github.com/h0r1ze/Cheburcheck-Multi/releases/download/{{TAG}}/Cheburcheck-Multi-Linux-x64.tar.gz) | нет |
| 🐧 **Linux** ARM64 (Raspberry Pi 4/5 и др.) | [Cheburcheck-Multi-Linux-arm64.tar.gz](https://github.com/h0r1ze/Cheburcheck-Multi/releases/download/{{TAG}}/Cheburcheck-Multi-Linux-arm64.tar.gz) | нет |
| 🍎 **macOS** Apple Silicon (M1–M4) | [Cheburcheck-Multi-macOS-arm64.tar.gz](https://github.com/h0r1ze/Cheburcheck-Multi/releases/download/{{TAG}}/Cheburcheck-Multi-macOS-arm64.tar.gz) | нет |
| 🧩 **Любая система** (в т.ч. macOS на Intel, старые Linux) | [Cheburcheck-Multi.zip](https://github.com/h0r1ze/Cheburcheck-Multi/releases/download/{{TAG}}/Cheburcheck-Multi.zip) | да, 3.8+ |

## 🚀 Запуск

- **Windows:** запустите `.exe`. Если появится «Windows защитила ваш компьютер» — нажмите **Подробнее → Выполнить в любом случае** (программа не подписана сертификатом).
- **Linux:** `tar xzf Cheburcheck-Multi-Linux-*.tar.gz && ./cheburcheck-multi`
- **macOS:** распакуйте архив, затем в Терминале: `xattr -d com.apple.quarantine cheburcheck-multi; ./cheburcheck-multi` (снимает блокировку Gatekeeper для неподписанной программы).
- **Через Python:** распакуйте `Cheburcheck-Multi.zip` и запустите `start.bat` (Windows) или `./start.sh` (Linux/macOS).

Браузер откроется сам. Список серверов сохраняется в `state.json` рядом с программой. Целостность файлов можно сверить по `SHA256SUMS.txt`.
