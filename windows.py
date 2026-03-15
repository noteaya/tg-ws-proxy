from __future__ import annotations

import ctypes
import json
import logging
import os
import psutil
import sys
import threading
import time
import webbrowser
import pystray
import pyperclip
import asyncio as _asyncio
from pathlib import Path
from typing import Dict, Optional, Any
from PIL import Image, ImageDraw, ImageFont

import proxy.tg_ws_proxy as tg_ws_proxy

try:
    import webview as _webview_mod
    _USE_WEBVIEW = True
except Exception:
    _webview_mod = None
    _USE_WEBVIEW = False

APP_NAME = "TgWsProxy"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "proxy.log"
FIRST_RUN_MARKER = APP_DIR / ".first_run_done"
IPV6_WARN_MARKER = APP_DIR / ".ipv6_warned"


DEFAULT_CONFIG = {
    "port": 1080,
    "host": "127.0.0.1",
    "dc_ip": ["2:149.154.167.220", "4:149.154.167.220"],
    "verbose": False,
    "start_with_windows": False,
}

_STARTUP_LNK_NAME = "TgWsProxy.lnk"


def _get_ui_base_path() -> Path:
    """Path to ui/ folder (HTML/CSS/JS) for WebView. Works when run from source or from frozen exe."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return base / "ui"


_proxy_thread: Optional[threading.Thread] = None
_async_stop: Optional[object] = None
_tray_icon: Optional[object] = None
_main_window: Optional[object] = None
_config: dict = {}
_exiting: bool = False
_lock_file_path: Optional[Path] = None
_startup_error: Optional[str] = None
_startup_warnings: list = []

log = logging.getLogger("tg-ws-tray")


def _same_process(lock_meta: dict, proc: psutil.Process) -> bool:
    try:
        lock_ct = float(lock_meta.get("create_time", 0.0))
        proc_ct = float(proc.create_time())
        if lock_ct > 0 and abs(lock_ct - proc_ct) > 1.0:
            return False
    except Exception:
        return False

    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return os.path.basename(sys.executable) == proc.name()

    return False


def _release_lock():
    global _lock_file_path
    if not _lock_file_path:
        return
    try:
        _lock_file_path.unlink(missing_ok=True)
    except Exception:
        pass
    _lock_file_path = None


def _acquire_lock() -> bool:
    global _lock_file_path
    _ensure_dirs()
    lock_files = list(APP_DIR.glob("*.lock"))

    for f in lock_files:
        pid = None
        meta: dict = {}

        try:
            pid = int(f.stem)
        except Exception:
            f.unlink(missing_ok=True)
            continue

        try:
            raw = f.read_text(encoding="utf-8").strip()
            if raw:
                meta = json.loads(raw)
        except Exception:
            meta = {}

        try:
            proc = psutil.Process(pid)
            if _same_process(meta, proc):
                return False
        except Exception:
            pass

        f.unlink(missing_ok=True)

    lock_file = APP_DIR / f"{os.getpid()}.lock"
    try:
        proc = psutil.Process(os.getpid())
        payload = {
            "create_time": proc.create_time(),
        }
        lock_file.write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")
    except Exception:
        lock_file.touch()

    _lock_file_path = lock_file
    return True


def _ensure_dirs():
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    _ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception as exc:
            log.warning("Failed to load config: %s", exc)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    _ensure_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _get_startup_folder() -> Optional[Path]:
    """Return Windows Startup folder path, or None on failure / non-Windows."""
    if sys.platform != "win32":
        return None
    apd = os.environ.get("APPDATA", "")
    if not apd:
        return None
    p = Path(apd) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return p if p.exists() else Path(apd) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def enable_start_with_windows() -> bool:
    """Create shortcut in Startup folder. Returns True on success. Only for frozen exe."""
    if not getattr(sys, "frozen", False):
        return False
    folder = _get_startup_folder()
    if not folder:
        return False
    exe = Path(sys.executable).resolve()
    lnk = folder / _STARTUP_LNK_NAME
    try:
        import subprocess
        # Escape backslashes for PowerShell double-quoted strings
        lnk_s = str(lnk).replace("\\", "\\\\")
        exe_s = str(exe).replace("\\", "\\\\")
        wd_s = str(exe.parent).replace("\\", "\\\\")
        ps_cmd = (
            f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut("{lnk_s}");'
            f' $s.TargetPath="{exe_s}";'
            f' $s.WorkingDirectory="{wd_s}";'
            " $s.Save()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except Exception as exc:
        log.warning("Failed to create startup shortcut: %s", exc)
        return False


def disable_start_with_windows() -> bool:
    """Remove shortcut from Startup folder. Returns True if removed or not present."""
    if sys.platform != "win32":
        return True
    folder = _get_startup_folder()
    if not folder:
        return True
    lnk = folder / _STARTUP_LNK_NAME
    try:
        lnk.unlink(missing_ok=True)
        return True
    except Exception as exc:
        log.warning("Failed to remove startup shortcut: %s", exc)
        return False


def setup_logging(verbose: bool = False):
    _ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)

    if not getattr(sys, "frozen", False):
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG if verbose else logging.INFO)
        ch.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-5s  %(message)s",
            datefmt="%H:%M:%S"))
        root.addHandler(ch)


def _make_icon_image(size: int = 64):
    if Image is None:
        raise RuntimeError("Pillow is required for tray icon")
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    margin = 2
    draw.ellipse([margin, margin, size - margin, size - margin],
                 fill=(0, 136, 204, 255))
                 
    try:
        font = ImageFont.truetype("arial.ttf", size=int(size * 0.55))
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "T", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1]
    draw.text((tx, ty), "T", fill=(255, 255, 255, 255), font=font)

    return img


def _load_icon():
    icon_path = Path(__file__).parent / "icon.ico"
    if icon_path.exists() and Image:
        try:
            return Image.open(str(icon_path))
        except Exception:
            pass
    return _make_icon_image()



def _run_proxy_thread(port: int, dc_opt: Dict[int, str], verbose: bool,
                      host: str = '127.0.0.1'):
    global _async_stop
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    stop_ev = _asyncio.Event()
    _async_stop = (loop, stop_ev)

    try:
        loop.run_until_complete(
            tg_ws_proxy._run(port, dc_opt, stop_event=stop_ev, host=host))
    except Exception as exc:
        log.error("Proxy thread crashed: %s", exc)
        if "10048" in str(exc) or "Address already in use" in str(exc):
            global _startup_error
            _startup_error = (
                "Не удалось запустить прокси: порт уже используется другим приложением. "
                "Закройте приложение, использующее этот порт, или измените порт в настройках и перезапустите."
            )
    finally:
        loop.close()
        _async_stop = None


def start_proxy():
    global _proxy_thread, _config
    if _proxy_thread and _proxy_thread.is_alive():
        log.info("Proxy already running")
        return

    cfg = _config
    port = cfg.get("port", DEFAULT_CONFIG["port"])
    host = cfg.get("host", DEFAULT_CONFIG["host"])
    dc_ip_list = cfg.get("dc_ip", DEFAULT_CONFIG["dc_ip"])
    verbose = cfg.get("verbose", False)

    try:
        dc_opt = tg_ws_proxy.parse_dc_ip_list(dc_ip_list)
    except ValueError as e:
        log.error("Bad config dc_ip: %s", e)
        global _startup_error
        _startup_error = f"Ошибка конфигурации DC→IP: {e}"
        return

    log.info("Starting proxy on %s:%d ...", host, port)
    _proxy_thread = threading.Thread(
        target=_run_proxy_thread,
        args=(port, dc_opt, verbose, host),
        daemon=True, name="proxy")
    _proxy_thread.start()


def stop_proxy():
    global _proxy_thread, _async_stop
    if _async_stop:
        loop, stop_ev = _async_stop
        loop.call_soon_threadsafe(stop_ev.set)
        if _proxy_thread:
            _proxy_thread.join(timeout=2)
    _proxy_thread = None
    log.info("Proxy stopped")


def restart_proxy():
    log.info("Restarting proxy...")
    stop_proxy()
    time.sleep(0.3)
    start_proxy()


def _on_open_in_telegram(icon=None, item=None) -> dict:
    """Open tg://socks in browser or copy to clipboard. Returns {success: bool, message?: str} for WebView."""
    port = _config.get("port", DEFAULT_CONFIG["port"])
    url = f"tg://socks?server=127.0.0.1&port={port}"
    log.info("Opening %s", url)
    try:
        result = webbrowser.open(url)
        if not result:
            raise RuntimeError("webbrowser.open returned False")
        return {"success": True}
    except Exception:
        log.info("Browser open failed, copying to clipboard")
        try:
            pyperclip.copy(url)
            return {"success": False, "message": f"Не удалось открыть Telegram автоматически. Ссылка скопирована в буфер обмена: {url}"}
        except Exception as exc:
            log.error("Clipboard copy failed: %s", exc)
            return {"success": False, "message": f"Не удалось скопировать ссылку: {exc}"}


def _on_restart(icon=None, item=None):
    threading.Thread(target=restart_proxy, daemon=True).start()


def _check_connection_return_results(dc_ip_list: list) -> list:
    """Run check for given DC list and return list of dicts for WebView UI. On error returns list with one dict with 'error' key."""
    try:
        dc_opt = tg_ws_proxy.parse_dc_ip_list(dc_ip_list)
    except ValueError as e:
        return [{"error": str(e)}]
    if not dc_opt:
        return [{"error": "Не заданы DC→IP маппинги. Укажите их в настройках."}]
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            tg_ws_proxy.check_dc_connections(dc_opt, timeout=8.0))
    except Exception as exc:
        log.exception("Check connection failed")
        return [{"error": str(exc)}]
    finally:
        loop.close()


def _on_check_connection(icon=None, item=None):
    """Tray: show main window so user can run check from UI."""
    _show_main_window()


def _on_open_logs(icon=None, item=None) -> dict:
    """Open log file. Returns {opened: bool, message?: str} for WebView."""
    log.info("Opening log file: %s", LOG_FILE)
    if LOG_FILE.exists():
        os.startfile(str(LOG_FILE))
        return {"opened": True}
    return {"opened": False, "message": "Файл логов ещё не создан."}


def _on_exit(icon=None, item=None):
    global _exiting
    if _exiting:
        os._exit(0)
        return
    _exiting = True
    log.info("User requested exit")
    if _main_window is not None:
        try:
            if hasattr(_main_window, "destroy"):
                _main_window.destroy()
            else:
                _main_window.after(0, _main_window.quit)
        except Exception:
            pass
    if icon:
        icon.stop()



def _show_first_run():
    """First-run is handled in WebView; no-op here."""
    pass


def _has_ipv6_enabled() -> bool:
    import socket as _sock
    try:
        addrs = _sock.getaddrinfo(_sock.gethostname(), None, _sock.AF_INET6)
        for addr in addrs:
            ip = addr[4][0]
            if ip and not ip.startswith('::1') and not ip.startswith('fe80::1'):
                return True
    except Exception:
        pass
    try:
        s = _sock.socket(_sock.AF_INET6, _sock.SOCK_STREAM)
        s.bind(('::1', 0))
        s.close()
        return True
    except Exception:
        return False


def _check_ipv6_warning():
    _ensure_dirs()
    if IPV6_WARN_MARKER.exists():
        return
    if not _has_ipv6_enabled():
        return
    IPV6_WARN_MARKER.touch()
    _startup_warnings.append(
        "На вашем компьютере включена поддержка IPv6. Telegram может пытаться подключаться через IPv6, "
        "что не поддерживается и может привести к ошибкам. Если прокси не работает — попробуйте отключить "
        "в настройках прокси Telegram попытку соединения по IPv6 или отключите IPv6 в системе."
    )


def _get_stats_menu_text():
    """Dynamic text for tray menu: current WS/TCP connection counts."""
    try:
        return tg_ws_proxy.get_stats_for_tray()["menu"]
    except Exception:
        return "Сейчас: — через WS, — через TCP"


def _show_main_window(icon=None, item=None):
    """Show main window from tray."""
    _do_show_main_window()


def _do_show_main_window():
    global _main_window
    if _main_window is None:
        return
    try:
        if hasattr(_main_window, "show"):
            _main_window.show()
        else:
            _main_window.deiconify()
            _main_window.lift()
            _main_window.focus_force()
    except Exception:
        pass


class _WebViewAPI:
    """API exposed to WebView JS via pywebview.api."""

    def __init__(self, window):
        self._window = window

    def get_status(self):
        try:
            st = tg_ws_proxy.get_stats_for_tray()
            host = _config.get("host", DEFAULT_CONFIG["host"])
            port = _config.get("port", DEFAULT_CONFIG["port"])
            status_text = f"Прокси: {host}:{port}  ·  {st['menu']}"
        except Exception:
            status_text = "Прокси запущен"
        return {"host": _config.get("host", DEFAULT_CONFIG["host"]), "port": _config.get("port", DEFAULT_CONFIG["port"]), "status_text": status_text}

    def get_startup_error(self):
        global _startup_error
        return _startup_error

    def clear_startup_error(self):
        global _startup_error
        _startup_error = None

    def get_startup_warnings(self):
        global _startup_warnings
        out = list(_startup_warnings)
        _startup_warnings.clear()
        return out

    def is_first_run(self):
        _ensure_dirs()
        return not FIRST_RUN_MARKER.exists()

    def complete_first_run(self, open_in_telegram: bool):
        _ensure_dirs()
        FIRST_RUN_MARKER.touch()
        if open_in_telegram:
            _on_open_in_telegram()

    def get_config(self):
        start_with_windows_available = bool(getattr(sys, "frozen", False) and sys.platform == "win32")
        return {
            "host": _config.get("host", DEFAULT_CONFIG["host"]),
            "port": _config.get("port", DEFAULT_CONFIG["port"]),
            "dc_ip": list(_config.get("dc_ip", DEFAULT_CONFIG["dc_ip"])),
            "verbose": bool(_config.get("verbose", False)),
            "start_with_windows": bool(_config.get("start_with_windows", False)),
            "start_with_windows_available": start_with_windows_available,
        }

    def save_config(self, cfg: dict) -> dict:
        import socket as _sock
        host_val = (cfg.get("host") or "").strip()
        try:
            _sock.inet_aton(host_val)
        except OSError:
            return {"ok": False, "error": "Некорректный IP-адрес."}
        try:
            port_val = int((cfg.get("port") or 1080))
            if not (1 <= port_val <= 65535):
                raise ValueError("invalid port")
        except (ValueError, TypeError):
            return {"ok": False, "error": "Порт должен быть числом 1–65535."}
        dc_ip = cfg.get("dc_ip")
        if isinstance(dc_ip, list):
            lines = [str(x).strip() for x in dc_ip if str(x).strip()]
        else:
            lines = []
        try:
            tg_ws_proxy.parse_dc_ip_list(lines)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        start_with_windows_available = bool(getattr(sys, "frozen", False) and sys.platform == "win32")
        new_cfg = {
            "host": host_val,
            "port": port_val,
            "dc_ip": lines,
            "verbose": bool(cfg.get("verbose", False)),
            "start_with_windows": bool(cfg.get("start_with_windows", False)) if start_with_windows_available else _config.get("start_with_windows", False),
        }
        if start_with_windows_available and new_cfg["start_with_windows"] != _config.get("start_with_windows", False):
            if new_cfg["start_with_windows"]:
                if not enable_start_with_windows():
                    return {"ok": False, "error": "Не удалось добавить в автозагрузку."}
            else:
                disable_start_with_windows()
        save_config(new_cfg)
        _config.update(new_cfg)
        log.info("Config saved: %s", new_cfg)
        if _tray_icon:
            _tray_icon.menu = _build_menu()
        return {"ok": True}

    def open_in_telegram(self):
        return _on_open_in_telegram()

    def check_connection(self, dc_ip_list=None):
        if dc_ip_list is not None:
            return _check_connection_return_results(dc_ip_list)
        return _check_connection_return_results(_config.get("dc_ip", DEFAULT_CONFIG["dc_ip"]))

    def open_logs(self):
        return _on_open_logs()

    def restart_proxy(self):
        _on_restart()

    def minimize_to_tray(self):
        try:
            if self._window:
                self._window.hide()
        except Exception:
            pass

    def quit_app(self):
        global _exiting
        _exiting = True
        try:
            if self._window:
                self._window.destroy()
        except Exception:
            pass


def _create_main_window():
    """Create and return the main application window (WebView only)."""
    global _main_window
    if not _USE_WEBVIEW or not _webview_mod:
        log.error("pywebview not available")
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(0, "Для работы приложения требуется pywebview.", "TG WS Proxy", 0x10)
        return None
    ui_base = _get_ui_base_path()
    index_path = ui_base / "index.html"
    if not index_path.exists():
        log.error("WebView UI not found at %s", index_path)
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(0, f"Не найдена папка интерфейса: {index_path}", "TG WS Proxy", 0x10)
        return None
    try:
        url = index_path.as_uri()
        window = _webview_mod.create_window(
            "TG WS Proxy",
            url,
            width=520,
            height=680,
            resizable=True,
        )
        api = _WebViewAPI(window)
        # pywebview 5: expose() accepts callables only, not an object
        window.expose(
            api.get_status,
            api.get_startup_error,
            api.clear_startup_error,
            api.get_startup_warnings,
            api.is_first_run,
            api.complete_first_run,
            api.get_config,
            api.save_config,
            api.open_in_telegram,
            api.check_connection,
            api.open_logs,
            api.restart_proxy,
            api.minimize_to_tray,
            api.quit_app,
        )

        def _on_closing():
            def _hide():
                try:
                    window.hide()
                except Exception:
                    pass
            threading.Thread(target=_hide, daemon=True).start()
            return False

        window.events.closing += _on_closing
        _main_window = window
        return window
    except Exception as e:
        log.exception("WebView window failed: %s", e)
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(0, f"Ошибка создания окна: {e}", "TG WS Proxy", 0x10)
        return None


def _build_menu():
    if pystray is None:
        return None
    host = _config.get("host", DEFAULT_CONFIG["host"])
    port = _config.get("port", DEFAULT_CONFIG["port"])
    return pystray.Menu(
        pystray.MenuItem("Открыть", _show_main_window, default=True),
        pystray.MenuItem(
            f"Открыть в Telegram ({host}:{port})",
            _on_open_in_telegram),
        pystray.MenuItem(lambda item: _get_stats_menu_text(), lambda icon, item: None),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Перезапустить прокси", _on_restart),
        pystray.MenuItem("Проверить подключение", _on_check_connection),
        pystray.MenuItem("Настройки", _show_main_window),
        pystray.MenuItem("Открыть логи", lambda i, it: _on_open_logs()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", _on_exit),
    )


def run_tray():
    global _tray_icon, _main_window, _config

    _config = load_config()
    save_config(_config)

    if LOG_FILE.exists():
        try:
            LOG_FILE.unlink()
        except Exception:
            pass

    setup_logging(_config.get("verbose", False))
    log.info("TG WS Proxy starting")
    log.info("Config: %s", _config)
    log.info("Log file: %s", LOG_FILE)

    if pystray is None or Image is None:
        log.error("pystray or Pillow not installed; running in console mode")
        start_proxy()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_proxy()
        return

    start_proxy()
    _show_first_run()
    _check_ipv6_warning()

    root = _create_main_window()
    if root is None:
        log.error("Could not create main window; exiting")
        return
    use_webview = hasattr(root, "show")

    if not use_webview:
        root.deiconify()

    def _tray_setup(icon):
        icon.visible = True
        def update_stats():
            while getattr(icon, "_stats_thread_running", True):
                try:
                    st = tg_ws_proxy.get_stats_for_tray()
                    icon.title = st["tooltip"]
                    icon.update_menu()
                except Exception:
                    pass
                for _ in range(20):
                    if not getattr(icon, "_stats_thread_running", True):
                        return
                    time.sleep(0.1)
        icon._stats_thread_running = True
        t = threading.Thread(target=update_stats, daemon=True, name="tray-stats")
        t.start()

    try:
        initial_title = tg_ws_proxy.get_stats_for_tray()["tooltip"]
    except Exception:
        initial_title = "TG WS Proxy"

    icon_image = _load_icon()
    _tray_icon = pystray.Icon(
        APP_NAME,
        icon_image,
        initial_title,
        menu=_build_menu())

    def _run_tray_loop():
        _tray_icon.run(setup=_tray_setup)

    tray_thread = threading.Thread(target=_run_tray_loop, daemon=True, name="tray")
    tray_thread.start()

    if use_webview:
        _webview_mod.start()
    else:
        root.mainloop()

    _tray_icon._stats_thread_running = False
    try:
        _tray_icon.stop()
    except Exception:
        pass
    stop_proxy()
    _main_window = None
    log.info("Tray app exited")


def main():
    if not _acquire_lock():
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(0, "Приложение уже запущено.", os.path.basename(sys.argv[0]), 0x40)
        return

    try:
        run_tray()
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
