#!/usr/bin/env python3
"""
Cheburcheck Multi — пакетная проверка адресов (VPN-серверов) через https://cheburcheck.ru

Запуск:  python3 cheburcheck.py   (Windows: start.bat, Linux: ./start.sh)
         или готовая программа cheburcheck-multi из релиза — Python не нужен
Опции:   --port 18765   --no-browser   --host 127.0.0.1

Нужен только Python 3.8+, сторонние библиотеки не требуются.
Локальный сервер отдаёт страницу index.html и проксирует запросы к API сайта
(браузер не может ходить на cheburcheck.ru напрямую из-за CORS).
Состояние (количество адресов, сами адреса, последние результаты) хранится в state.json.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_ID = "cheburcheck-multi"
VERSION = "1.4.0"
BASE = "https://cheburcheck.ru"
UA = "Mozilla/5.0 (cheburcheck-multi; +https://cheburcheck.ru)"
HERE = os.path.dirname(os.path.abspath(__file__))
# В готовой сборке (PyInstaller) index.html лежит во временной папке распаковки,
# а state.json хранится рядом с самой программой — как и рядом со скриптом.
FROZEN = getattr(sys, "frozen", False)
INDEX = os.path.join(getattr(sys, "_MEIPASS", HERE), "index.html")


def state_path():
    base = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else HERE
    if os.access(base, os.W_OK):
        return os.path.join(base, "state.json")
    # папка программы недоступна для записи (например, Program Files) — пишем в профиль пользователя
    if sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Cheburcheck-Multi")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/Cheburcheck-Multi")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "cheburcheck-multi")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "state.json")


STATE = state_path()
SETTINGS = os.path.join(os.path.dirname(STATE), "settings.json")   # выбранный порт
DEFAULT_PORT = 18765
MIN_PORT = 1024
# текущий сервер; меняется при смене порта со страницы
APP = {"httpd": None, "host": "127.0.0.1", "shown_host": "127.0.0.1", "preferred": DEFAULT_PORT,
       "stop": threading.Event(), "lock": threading.Lock()}


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**values):
    data = load_settings()
    data.update(values)
    try:
        tmp = SETTINGS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS)
    except OSError:
        pass


def app_url(port):
    return f"http://{APP['shown_host']}:{port}/"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # На Windows SO_REUSEADDR позволяет занять порт, который уже слушает другая программа, —
    # «порт занят» тогда не обнаружился бы. Там просим у системы монопольный доступ к порту.
    allow_reuse_address = not sys.platform.startswith("win")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def ssl_context():
    """Корневые сертификаты: из certifi, если он есть (кладём его в готовые сборки), иначе системные."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = ssl_context()
JOB_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
MAX_STATE = 20 * 1024 * 1024

CHECK_RETRIES = 5
PROBE_RETRY_DELAY = 13   # лимит сайта 5/мин → один новый запуск примерно раз в 12 с
PROBE_MAX_WAIT = 600
# коды ответа /api/v1/probe (см. website/src/api/probe.rs в github.com/LowderPlay/cheburcheck)
PROBE_ERRORS = {
    400: "сайт не запускает пробы для подсетей и ASN",
    403: "частный/зарезервированный IP — пробы не запускаются",
    404: "запрос не найден на сайте",
}

state_lock = threading.Lock()
probe_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "CheburcheckMulti/1.0"

    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            super().log_message(fmt, *args)

    # ---------- helpers ----------
    def send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    # ---------- routes ----------
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    self.send_bytes(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self.get_state()
            elif path == "/api/ping":
                self.send_json(200, {"app": APP_ID, "version": VERSION})
            elif path == "/api/info":
                port = self.server.server_address[1]
                self.send_json(200, {"app": APP_ID, "version": VERSION, "port": port, "url": app_url(port),
                                     "preferred": APP["preferred"], "default": DEFAULT_PORT, "min": MIN_PORT,
                                     "state": STATE})
            elif path == "/api/check":
                target = urllib.parse.parse_qs(url.query).get("target", [""])[0].strip()
                self.check(target)
            elif path.startswith("/api/probe/"):
                self.probe(path[len("/api/probe/"):])
            else:
                self.send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def origin_ok(self):
        """POST принимаем только со своей страницы: чужой сайт, открытый в браузере,
        не должен уметь перезаписать список серверов или выключить программу."""
        origin = self.headers.get("Origin")
        if origin is None:          # не из браузера (curl и т.п.)
            return True
        return urllib.parse.urlsplit(origin).netloc == self.headers.get("Host", "")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.origin_ok():
            return self.send_json(403, {"error": "forbidden"})
        if path == "/api/shutdown":
            self.send_json(200, {"ok": True})
            print("Остановлено со страницы.", flush=True)
            APP["stop"].set()
            return
        if path == "/api/port":
            return self.change_port()
        if path != "/api/state":
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_STATE:
            return self.send_json(400, {"error": "bad length"})
        raw = self.rfile.read(length)
        try:
            json.loads(raw.decode("utf-8"))
        except ValueError:
            return self.send_json(400, {"error": "bad json"})
        with state_lock:
            tmp = STATE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, STATE)
        self.send_json(200, {"ok": True})

    def change_port(self):
        """Переезд на другой порт без перезапуска: поднимаем сервер на новом порту,
        страница переходит туда сама, старый порт закрываем через несколько секунд."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            port = int(json.loads(self.rfile.read(length).decode("utf-8")).get("port")) if 0 < length < 1024 else 0
        except (ValueError, TypeError, AttributeError):
            port = 0
        if not MIN_PORT <= port <= 65535:
            return self.send_json(400, {"error": f"Порт — число от {MIN_PORT} до 65535"})
        with APP["lock"]:
            old = APP["httpd"]
            if port == old.server_address[1]:
                APP["preferred"] = port
                save_settings(port=port)
                return self.send_json(200, {"url": app_url(port), "port": port, "moved": False})
            try:
                new = Server((APP["host"], port), Handler)
            except OSError:
                return self.send_json(409, {"error": f"Порт {port} занят другой программой — выберите другой"})
            APP["httpd"], APP["preferred"] = new, port
            threading.Thread(target=new.serve_forever, daemon=True).start()
        save_settings(port=port)
        print(f"Порт изменён: теперь {app_url(port)}", flush=True)
        self.send_json(200, {"url": app_url(port), "port": port, "moved": True})

        def retire():
            time.sleep(5)               # дать странице перейти на новый адрес
            old.shutdown()
            old.server_close()
        threading.Thread(target=retire, daemon=True).start()

    def get_state(self):
        with state_lock:
            try:
                with open(STATE, "rb") as f:
                    data = f.read()
                json.loads(data.decode("utf-8"))
            except (OSError, ValueError):
                data = b"{}"
        self.send_bytes(200, data, "application/json; charset=utf-8")

    def check(self, target):
        if len(target) < 3:
            return self.send_json(400, {"error": "Адрес слишком короткий (минимум 3 символа)"})
        url = f"{BASE}/api/v1/check?target={urllib.parse.quote(target, safe='')}"
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
        try:
            for attempt in range(CHECK_RETRIES + 1):
                try:
                    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
                        return self.send_bytes(200, r.read(), "application/json; charset=utf-8")
                except urllib.error.HTTPError as e:
                    # лимит сайта: 30 проверок в минуту с одного IP
                    if e.code != 429 or attempt == CHECK_RETRIES:
                        raise
                    time.sleep(3)
        except urllib.error.HTTPError as e:
            info = ""
            try:
                info = json.loads(e.read().decode("utf-8")).get("info", "")
            except Exception:
                pass
            msg = {404: "Не найдено / неверный формат адреса",
                   429: "Слишком много запросов, подождите"}.get(e.code, info or f"HTTP {e.code}")
            self.send_json(e.code, {"error": msg})
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            self.send_json(502, {"error": f"Нет связи с cheburcheck.ru: {getattr(e, 'reason', e)}"})

    def sse(self, event, data):
        self.wfile.write(f"event:{event}\ndata:{json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def open_probe_stream(self, job):
        """Открывает SSE-поток проб на сайте. Сайт пускает 5 запусков проб в минуту с одного IP,
        поэтому при 429 ждём и повторяем; запуски идут по очереди (probe_lock), чтобы строки
        не толкались. Возвращает ответ или None, если поток не открыт (событие уже отправлено)."""
        req = urllib.request.Request(f"{BASE}/api/v1/probe/{job}",
                                     headers={"Accept": "text/event-stream", "User-Agent": UA})
        waited = 0
        with probe_lock:
            while True:
                try:
                    return urllib.request.urlopen(req, timeout=120, context=SSL_CTX)
                except urllib.error.HTTPError as e:
                    if e.code == 429 and waited < PROBE_MAX_WAIT:
                        self.sse("wait", {"seconds": PROBE_RETRY_DELAY, "waited": waited})
                        time.sleep(PROBE_RETRY_DELAY)
                        waited += PROBE_RETRY_DELAY
                        continue
                    reason = PROBE_ERRORS.get(e.code, f"пробы недоступны (HTTP {e.code})")
                    self.sse("unavailable", {"code": e.code, "reason": reason})
                    return None
                except (urllib.error.URLError, socket.timeout, OSError) as e:
                    self.sse("unavailable", {"code": 502, "reason": f"нет связи: {getattr(e, 'reason', e)}"})
                    return None

    def probe(self, job):
        """Проксирует SSE-поток результатов проб (+ свои события wait/unavailable)."""
        job = urllib.parse.unquote(job)
        if not JOB_RE.match(job):
            return self.send_json(400, {"error": "bad job id"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            up = self.open_probe_stream(job)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return  # вкладку закрыли или проверку остановили, пока ждали лимит
        if up is None:
            return
        try:
            while True:
                line = up.readline()
                if not line:
                    break
                self.wfile.write(line)
                if line in (b"\n", b"\r\n"):
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout, OSError):
            pass
        finally:
            up.close()


def clean_env(extra=None):
    """Окружение для запуска других программ. Готовая сборка (PyInstaller) подменяет
    LD_LIBRARY_PATH на свою временную папку и ставит служебные переменные — системный
    терминал или браузер не должны их унаследовать, а новая копия программы должна
    стартовать как самостоятельная."""
    env = dict(os.environ)
    if FROZEN:
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.update(extra or {})
    return env


def open_browser(url):
    """Открывает url в браузере по умолчанию. Возвращает True, если удалось запустить."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], env=clean_env())
            return True
        # Linux: сначала системные «открывалки», они берут браузер по умолчанию из DE
        for cmd in (["xdg-open"], ["gio", "open"], ["sensible-browser"], ["x-www-browser"],
                    ["firefox"], ["chromium"], ["chromium-browser"], ["google-chrome"], ["yandex-browser"]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd + [url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, env=clean_env())
                return True
    except Exception:
        pass
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def in_terminal():
    return any(getattr(f, "isatty", lambda: False)() for f in (sys.stdin, sys.stdout) if f)


def relaunch_in_terminal(argv):
    """Linux: при запуске двойным кликом из файлового менеджера окна терминала нет — сервер
    работал бы невидимо и его нечем было бы остановить. Перезапускаемся в окне терминала:
    там видно адрес, а Ctrl+C или закрытие окна останавливают программу."""
    if not sys.platform.startswith("linux") or in_terminal() or os.environ.get("CHEBURCHECK_IN_TERMINAL"):
        return False
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    cmd = ([sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)]) + argv
    title = "Cheburcheck Multi"
    terminals = {
        "x-terminal-emulator": ["-e"] + cmd,           # терминал по умолчанию в Debian/Ubuntu
        "gnome-terminal": ["--title", title, "--"] + cmd,
        "konsole": ["-p", f"tabtitle={title}", "-e"] + cmd,
        "mate-terminal": ["--title", title, "-x"] + cmd,
        "xfce4-terminal": ["--title", title, "-x"] + cmd,
        "lxterminal": ["-t", title, "-e", shlex.join(cmd)],
        "qterminal": ["-e", shlex.join(cmd)],
        "tilix": ["-t", title, "-e", shlex.join(cmd)],
        "terminator": ["-T", title, "-x"] + cmd,
        "kitty": ["--title", title] + cmd,
        "alacritty": ["--title", title, "-e"] + cmd,
        "xterm": ["-T", title, "-e"] + cmd,
    }
    # сначала терминал «родного» окружения рабочего стола
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    native = [t for key, t in (("KDE", "konsole"), ("GNOME", "gnome-terminal"), ("MATE", "mate-terminal"),
                               ("XFCE", "xfce4-terminal"), ("LXQT", "qterminal"), ("LXDE", "lxterminal")) if key in desktop]
    env = clean_env({"CHEBURCHECK_IN_TERMINAL": "1"})
    for name in native + [t for t in terminals if t not in native]:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            p = subprocess.Popen([exe] + terminals[name], env=env, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.wait(timeout=1.5)         # gnome-terminal сразу возвращает 0, остальные продолжают работать
            if p.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            return True
        except OSError:
            continue
    return False                        # терминала нет — работаем без окна (выключить можно кнопкой «Выход»)


def running_instance(host, port):
    """Если приложение уже запущено на этом порту — возвращает его адрес."""
    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(url + "api/ping", timeout=1) as r:
            if json.loads(r.read().decode("utf-8")).get("app") == APP_ID:
                return url
    except Exception:
        pass
    return None


def main():
    # Русский текст не должен ронять программу, если вывод перенаправлен в файл
    # (на Windows тогда используется cp1252) или консоль в другой кодировке.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Cheburcheck Multi — пакетная проверка адресов")
    ap.add_argument("--version", action="version", version=f"Cheburcheck Multi {VERSION}")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None,
                    help=f"порт (по умолчанию — выбранный в настройках на странице или {DEFAULT_PORT})")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    ap.add_argument("--verbose", action="store_true", help="логировать запросы")
    ap.add_argument("--no-terminal", action="store_true", help="Linux: не открывать своё окно терминала")
    args = ap.parse_args()
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    preferred = args.port or load_settings().get("port") or DEFAULT_PORT
    if not isinstance(preferred, int) or not MIN_PORT <= preferred <= 65535:
        preferred = DEFAULT_PORT
    ports = range(preferred, min(preferred + 20, 65536))

    if not args.no_terminal and not in_terminal() and sys.platform.startswith("linux"):
        # если уже запущено — просто открыть вкладку, терминал не нужен
        for port in ports:
            existing = running_instance(shown_host, port)
            if existing:
                if not args.no_browser:
                    open_browser(existing)
                return
        if relaunch_in_terminal(sys.argv[1:]):
            # ждём, пока программа поднимется в новом окне; если не вышло — работаем здесь, без окна
            for _ in range(25):
                time.sleep(0.3)
                if any(running_instance(shown_host, p) for p in ports):
                    return

    httpd = None
    for port in ports:
        existing = running_instance(shown_host, port)
        if existing:
            print(f"Cheburcheck Multi уже запущен: {existing}", flush=True)
            if not args.no_browser and not open_browser(existing):
                print("Откройте этот адрес в браузере вручную.", flush=True)
            return
        try:
            httpd = Server((args.host, port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print(f"Не удалось занять ни один порт из {ports.start}–{ports.stop - 1}. Запустите с --port <другой порт>.")
        sys.exit(1)
    APP.update(httpd=httpd, host=args.host, shown_host=shown_host, preferred=preferred)

    url = app_url(port)
    if port != preferred:
        print(f"Порт {preferred} занят другой программой — работаем на {port}. Поменять: ⚙ на странице.", flush=True)
    print(f"Cheburcheck Multi v{VERSION} запущен: {url}", flush=True)
    print("Остановить: кнопка «Выход» на странице, Ctrl+C или закройте это окно", flush=True)
    print(f"Список серверов хранится в: {STATE}", flush=True)
    if not args.no_browser:
        def _open():
            if not open_browser(url):
                print("Не удалось открыть браузер — откройте адрес выше вручную.", flush=True)
        # сервер уже слушает порт, поэтому страница загрузится сразу
        threading.Thread(target=_open, daemon=True).start()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        while not APP["stop"].wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        with APP["lock"]:
            current = APP["httpd"]
        current.shutdown()
        current.server_close()
    if os.environ.get("CHEBURCHECK_IN_TERMINAL"):
        time.sleep(1.5)             # своё окно терминала закроется — дать прочитать последнюю строку


if __name__ == "__main__":
    main()
