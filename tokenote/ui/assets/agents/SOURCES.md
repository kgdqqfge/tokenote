# Agent icon sources

Tokenote bundles these icons locally so the sidebar works without network access. The normalized PNGs are 22 × 22 pixels, preserve the original artwork's proportions and colors, and are derived from the originals in `sources/`.

| Application | Original asset | Source |
| --- | --- | --- |
| Codex | `sources/codex.svg` | CC Switch `src/icons/extracted/openai.svg` |
| Trae CN | `sources/trae.svg` | Installed Trae CN application, `resources/app/out/media/trae-logo.svg` |
| WorkBuddy | `sources/workbuddy.png` | Installed WorkBuddy application, `resources/app.asar.unpacked/resources/icon.png` |
| Qoder | `sources/qoder.ico` | Installed Qoder CN application, `resources/app-icon.ico` |
| OpenClaw | `sources/openclaw.svg` | Inline `openclaw` SVG in CC Switch `src/icons/extracted/index.ts` |
| OpenCode | `sources/opencode.svg` | CC Switch `src/icons/extracted/opencode-logo-light.svg` |
| Kimi Code | `sources/kimi.svg` | CC Switch `src/icons/extracted/kimi.svg` |
| ZCode | `sources/zcode.png` | ZCode `packages/desktop/build/icons/128x128.png` |
| Claude Code | `sources/claude.svg` | CC Switch `src/icons/extracted/claude.svg` |
| Pi agents | `sources/pi.svg` | Inline `pi` SVG in CC Switch `src/icons/extracted/index.ts` |
| Hermes | `sources/hermes.png` | CC Switch `src/icons/extracted/hermes.png` |
| DeepSeek Harness | `sources/deepseek_harness.svg` | Inline `deepseek` SVG in CC Switch `src/icons/extracted/index.ts` |
| MiniMax Code | `sources/minimax.svg` | CC Switch `src/icons/extracted/minimax.svg` |
| Grok Build | `sources/grok.svg` | CC Switch `src/icons/extracted/grok.svg` |
| Cursor | `sources/cursor.png` | Installed Cursor application, `resources/app/out/media/logo.png` |

CC Switch assets and default colors were verified against revision `f92ff0b03964188f1cd011d9302ab8869d7a8219`; the application's own icon mapping is in `src/components/shell/AppGlyph.tsx`. Repository: https://github.com/farion1231/cc-switch/tree/f92ff0b03964188f1cd011d9302ab8869d7a8219 . Its MIT notice is retained in `LICENSE-CC-Switch.txt`.

The ZCode asset was retrieved from revision `0a1fc56d279fd1001f78387bd81aec1c26767411`. Repository: https://github.com/zai-org/ZCode/tree/0a1fc56d279fd1001f78387bd81aec1c26767411 . Its Apache 2.0 license is retained in `LICENSE-ZCode.txt`.

Product names and logos belong to their respective owners and identify the corresponding local data source. These icons do not indicate affiliation or endorsement.

Asset preparation uses Pillow and Playwright only at development time (`work/ui-review/build_agent_icons.py`); the application loads the final PNGs with standard-library Tkinter and has no added runtime dependency.
