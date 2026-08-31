import contextlib
import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(path, env):
    """Fresh module instance of a script under a patched environment (module
    config is read from os.environ at import; importing is side-effect-free)."""
    old = dict(os.environ)
    os.environ.update(env)
    try:
        loader = importlib.machinery.SourceFileLoader(pathlib.Path(path).stem, str(path))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(old)


@contextlib.contextmanager
def serve(module):
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run_notify(tmp, *args, notify_env=None, events="session-exit bell agent-done unit-failure doctor"):
    """Drive bin/grave notify under tmp with a temp conf and a fake curl on PATH
    that records its argv to tmp/curl.log."""
    tmp = pathlib.Path(tmp)
    (tmp / "logs").mkdir(exist_ok=True)
    (tmp / "config/secrets").mkdir(parents=True, exist_ok=True)
    conf = tmp / "grave.conf"
    conf.write_text(f'GRAVE_ROOT="{tmp}"\nNOTIFY_EVENTS="{events}"\n')
    if notify_env is not None:
        (tmp / "config/secrets/notify.env").write_text(notify_env)
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    fake_curl = bindir / "curl"
    fake_curl.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CURL_LOG"\nexit 0\n')
    fake_curl.chmod(0o755)
    env = dict(os.environ, GRAVE_CONF=str(conf), PATH=f"{bindir}:{os.environ['PATH']}",
               CURL_LOG=str(tmp / "curl.log"))
    return subprocess.run([str(ROOT / "bin/grave"), "notify", *args],
                          env=env, capture_output=True, text=True, timeout=30)
