"""Browser check run in CI: every text box in the main flows must accept typing.
(A key handler once returned false and silently swallowed every key press.)"""
import json, os, subprocess, sys, tempfile, time
from playwright.sync_api import sync_playwright

tmp = tempfile.mkdtemp()
os.makedirs(f"{tmp}/servers/.spawnpoint")
json.dump({"servers": {"survival": {
    "id": "survival", "name": "survival", "kind": "paper", "mc_version": "1.21.1", "loader": "paper",
    "pack": None, "hostname": "survival.mc.example.com", "java_tag": "java21",
    "memory": {"init_gb": 2, "max_gb": 6, "auto": True}, "home_groups": 1, "phase": "stopped",
    "phase_msg": "", "rcon_password": "x", "created": time.time(), "first_boot": False,
    "auto_sleep_min": 0, "extra_env": {}, "pregen": {"spawn_radius": 750, "full_radius": 5000, "stage": "done"}}}},
    open(f"{tmp}/servers/.spawnpoint/state.json", "w"))
env = dict(os.environ, ISOLATED_DOCKER="false", DATA_DIR=f"{tmp}/servers", BACKUP_DIR=f"{tmp}/backups",
           ADMIN_PASSWORD="pw", MC_PORT="25599")
app = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8099"], env=env)
time.sleep(4)
failures = []
try:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("CHROME") or None)
        pg = b.new_page()
        pg.goto("http://localhost:8099/")
        pg.fill("#user", "admin"); pg.fill("#pw", "pw"); pg.keyboard.press("Enter"); pg.wait_for_timeout(1000)

        def typed(sel, text):
            pg.click(sel); pg.keyboard.type(text)
            if pg.input_value(sel) != text:
                failures.append(f"{sel}: typed {text!r}, box shows {pg.input_value(sel)!r}")

        pg.click("#newbtn"); pg.wait_for_timeout(500)
        typed("#q", "all the mods"); typed("#nm", "atm10")
        pg.click("[data-t=plain]"); pg.wait_for_timeout(500); typed("#nm", "survival2")
        pg.click("[data-t=import]"); pg.wait_for_timeout(500); typed("#nm", "oldworld"); pg.click("#cbody details summary"); typed("#imv", "1.20.1")
        # an upload keeps going, and stays visible, after the dialog is closed
        held = []
        pg.route("**/api/import*", lambda r: held.append(r))            # answered later, so the upload stays open
        big = os.path.join(tmp, "world.zip"); open(big, "wb").write(os.urandom(4 << 20))
        pg.set_input_files("#imf", big); pg.click("#mk"); pg.wait_for_timeout(300)
        pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
        if not pg.is_visible("#uppill"):
            failures.append("upload progress vanished when the dialog closed")
        else:
            pg.click("#uppill .upopen"); pg.wait_for_timeout(300)
            if not pg.is_visible("#upw") or pg.is_visible("#uppill"):
                failures.append("clicking the upload pill didn't bring back the dialog's progress")
        pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
        for r in held:
            r.fulfill(status=400, json={"detail": "test"})
        pg.wait_for_timeout(500)
        if pg.is_visible("#uppill"):
            failures.append("upload pill stayed after the upload ended")
        pg.goto("http://localhost:8099/#/s/survival/mods"); pg.wait_for_timeout(800); typed("#mq", "jei")
        pg.goto("http://localhost:8099/#/s/survival/console"); pg.wait_for_timeout(800); typed("#cmd", "say hi")
        pg.goto("http://localhost:8099/#/s/survival/players"); pg.wait_for_timeout(800); typed("#pn", "Steve")
        b.close()
finally:
    app.terminate()
print("\n".join(failures) or "all text boxes accept typing")
sys.exit(1 if failures else 0)
