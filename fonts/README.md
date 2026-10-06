# Cremind Tag fonts

Pinned inputs of the font packs that Cremind Tag bridges draw text from
(docs/tags/fonts.md has the full story; the binary format is the firmware
repository's `docs/fontpack.md`, pinned in `app/tags/runtime/protocol/pinned/docs/`).

| Path | What | In git |
|---|---|---|
| `manifest.yaml` | Pinned sources, faces, face ids, render rules, build profiles | yes |
| `manifest.lock.json` | SHA-256 of every pinned file | yes |
| `icons.yaml` | Spec icon ids → Material Icons glyphs | yes |
| `LICENSES/` | OFL-1.1 and Apache-2.0 texts | yes |
| `cache/`, `out/` | Downloaded sources and built packs | no |

```sh
cremind tags tools fonts fetch                  # fill fonts/cache from the lock (≈ 57 MB, verified)
cremind tags tools fonts build --profile full   # fonts/out/full/fontpack.ctfp
cremind tags tools fonts build --profile dev    # fonts/out/dev/fontpack.ctfp (development only)
```

People never build fonts: the release pipeline publishes the verified font
asset bundle and Cremind installs it when a gateway computer is prepared.
