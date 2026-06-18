from playwright.sync_api import sync_playwright
import pathlib
base = pathlib.Path("/root/solar-operator/sketches/liquid-cards")
shots = {"A-capped-tank.html":"shot-A.png","B-floating-plate.html":"shot-B.png","C-side-gauge.html":"shot-C.png"}
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width":1160,"height":640}, device_scale_factor=2)
    for html, out in shots.items():
        pg.goto((base/html).as_uri())
        pg.wait_for_timeout(1400)  # let liquid animate to fill + bubbles appear
        pg.screenshot(path=str(base/out))
        print("shot", out)
    b.close()
