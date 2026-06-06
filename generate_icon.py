"""Generate assets/app.ico and PNG previews from the tray icon renderer."""

import os

from app import create_tray_icon


def main():
    os.makedirs("assets", exist_ok=True)
    sizes = [16, 32, 48, 64, 128, 256]
    images = [create_tray_icon(size=s, speaking=False) for s in sizes]
    images[0].save(
        "assets/app.ico",
        format="ICO",
        append_images=images[1:],
        sizes=[(s, s) for s in sizes],
    )
    create_tray_icon(size=256, speaking=False).save("assets/icon-idle-256.png", format="PNG")
    create_tray_icon(size=256, speaking=True).save("assets/icon-speaking-256.png", format="PNG")
    print("Created assets/app.ico, assets/icon-idle-256.png, assets/icon-speaking-256.png")


if __name__ == "__main__":
    main()
