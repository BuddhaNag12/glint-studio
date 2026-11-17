# Glint Studio logo

A calligraphic monogram: a lowercase **g** flows in one pen stroke into a **t**. The tail loops once, climbs into the t's stem and flicks off a small blue star (the glint).

## Files

| File | Use |
|---|---|
| `svg/glint-icon-dark.svg` | Main app icon, 1024, navy tile, white letters |
| `svg/glint-icon-dark-macos.svg` | The same icon on Apple's macOS icon grid (824 tile inside 1024). Use this one for the Dock and `.icns` |
| `svg/glint-icon-light.svg` | Light tile (#f8fafc), navy letters |
| `svg/glint-icon-{dark,light}-32.svg` | Small cut: thicker hairlines, bigger star. Use at 24–48 px |
| `svg/glint-icon-{dark,light}-16.svg` | Smallest cut. Use at 16–20 px |
| `svg/glint-mark-{white,navy,black}.svg` | Monogram only, transparent background |
| `svg/glint-menubar-template.svg` | Black-only menu bar / tray icon (macOS template image) |
| `svg/glint-lockup-{white,navy,black}.svg` | Monogram + "Glint Studio" wordmark. White is for dark backgrounds |
| `png/` | PNG exports of all of the above (icons at 1024/64/32/16, menu bar at 22 and 44, marks at 512, lockups at 1200×300) |
| `source/` | The generators that drew every SVG |

## Colours

| Role | Hex |
|---|---|
| Tile (dark) / letters (light) | `#0f172a` |
| Letters (dark) | `#ffffff` |
| Star | `#3b82f6` |
| Light tile | `#f8fafc` |
| Accent (only on the twist, not used in the final mark) | `#1d4ed8` |

In the one-colour black versions the star is black too.

## Use

- **Clear space:** keep at least the star's height empty on every side of the monogram or lockup.
- **Minimum sizes:** full icon down to 64 px; the `-32` cut at 24–48 px; the `-16` cut at 16–20 px. The lockup should be no less than 24 px tall on screen.
- **At 16 px** the loop fills in. The g, the t and the star still read; the twist does not. This is expected.
- Don't recolour, stretch, add shadows or gradients, or swap the wordmark font.

## Wordmark

"Glint Studio" is set in IBM Plex Sans Medium (SIL Open Font License) and converted to outlines, so no font is needed to display it. Kerning was not applied. The letter pairs in this name look fine without it, but check if you ever change the text.

## Regenerate

```bash
python3 -m venv venv && venv/bin/pip install fonttools
cd source
../venv/bin/python kit.py ../svg /path/to/IBMPlexSans-Medium.ttf
```

`gen.py` must sit next to `kit.py`. Size cuts (stroke weights, star size) are in `CUTS` at the top of `kit.py`.

## In the app

- `apps/desktop/src-tauri/icons/`: the app icons come from `npx tauri icon ../../branding/png/glint-icon-dark-1024.png`, except `icon.icns`, which comes from `glint-icon-dark-macos-1024.png` (Apple's grid). Delete the `android/`, `ios/` and `64x64.png` it adds.
- `icon.icon` there is a copy of `glint.icon`, the layered Icon Composer document `kit.py` writes. On macOS 26 and later the bundler compiles it into `Assets.car`, so the Dock icon follows Default, Dark, Clear and Tinted; older macOS uses `icon.icns`. Building it needs Xcode 26.
- `app-icon.svg`, `app-icon-32.svg` and `app-icon-16.svg` there are copies of `svg/glint-icon-dark{,-32,-16}.svg`: the in-app logo (`.mark` in `App.css`) and the favicons.
- `tray-template.png` is `png/glint-menubar-template@2x.png`; `tray-template.svg` is `svg/glint-menubar-template.svg`.
