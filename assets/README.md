# TinyReadAloud brand assets

Regenerate everything:

```powershell
.\venv\Scripts\python.exe generate_icon.py
```

Create Desktop + Start Menu shortcuts (source install):

```powershell
.\scripts\create-shortcut.ps1
```

## Icons

| File | Use |
|------|-----|
| `app.ico` | PyInstaller exe, Inno Setup installer |
| `shortcut.ico` | Windows `.lnk` shortcuts |
| `icon-16.png` … `icon-512.png` | UI, docs, favicons |
| `icon-speaking-256.png` | Speaking / active state preview |

## Banners

| File | Size | Use |
|------|------|-----|
| `banner-github.png` | 1280×640 | GitHub repository social preview |
| `banner-opengraph.png` | 1200×630 | Open Graph / Twitter card |
| `banner-readme.png` | 1280×400 | README header |

Set GitHub social preview: **Settings → General → Social preview → Upload `banner-github.png`**.
