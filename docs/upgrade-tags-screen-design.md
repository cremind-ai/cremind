# Upgrading: the new Cremind Tag screen design

Applies to the first release after **v0.0.20rc7.dev1**: the release that
draws Cremind Tag screens in a new design and pins a new font pack (contract
0.3.0 of cremind-tag: 12 and 14 px text and Noto Sans Bold).

## In one paragraph

Tag screens are redrawn in a new design: the tag's name and "Updated <time>"
over a red rule, a status chip on the first card (red when it needs you or
reports an error), bold titles, compact rows with their times, "+N MORE" for
the rest ([docs/tags/layout.md](tags/layout.md) "Screen model"). Nothing is
set up again and no firmware is flashed: gateways, bridges and tags draw the
new screens with the commands they already know. Each tag redraws **by
itself** — no `cremind tags refresh` needed — once right after the upgrade,
in the new design with the fonts its gateway computer still has (16 px, no
bold). The new font pack arrives when the computer's components are prepared
again: tags on the gateway's own radio then redraw once more with it; tags
behind a bridge get it with their next card, once that bridge has the same
pack, installed over USB.

## What an administrator does

1. **Upgrade Cremind** as usual. Each gateway computer keeps drawing with the
   font pack it has and offers the new one: **Settings → Tags** shows it as
   *Ready, font update available*, and `cremind tags hosts list` says *a font
   update is available*. Its tags redraw once in the new design.
2. **Prepare each gateway computer once:**
   - the Cremind server's own computer: **Settings → Tags → Prepare
     components** (admin), or `cremind -p admin tags hosts prepare`;
   - a desktop gateway computer: `cremind tags host prepare` on that
     computer, then restart the Cremind app there (or `cremind tags host
     run`) — gateway support that is already running keeps the fonts it
     started with.

   This installs the font pack this Cremind pins and starts gateway support on
   it (the server's computer then also removes the older packs nothing uses
   any more — never one a bridge still shows). Every tag on the gateway's own
   radio redraws once more, now with the small sizes and bold.
3. **Bridges, after that:** plug each bridge into a computer with USB and
   install the same pack: `cremind tags tools bridge fonts-install <pack>
   --url <its maintenance port>` (`COM9`, `/dev/ttyACM1`), where `<pack>` is the
   `.tag-runtime/assets/fonts/<pack id>/fontpack.ctfp` that step 2 installed in
   the gateway computer's system folder; `cremind tags tools bridge info`
   shows the pack a bridge has.

A manual runtime (`cremind tags tools daemon`) draws with the pack its own
configuration names (`hardware.fontpack`): point that at the new pack and
install the same one on its bridges.

## What the tags show meanwhile

| Tag | After the upgrade | After step 2 | After step 3 |
|---|---|---|---|
| on the gateway's own radio | the new design at 16 px, no bold (one refresh) | 12/14 px and bold (one more refresh) | — |
| behind a bridge | the new design at 16 px, no bold (one refresh) | keeps its screen; a new card for it fails with `FONTPACK_MISMATCH` and the tag waits | redraws with its next card (or `cremind tags refresh <tag>`); a tag that waited is drawn again once the runtime sees the bridge's new pack |

So install the pack on the bridges soon after preparing the gateway
computer: until a bridge has it, cards sent to its tags end `failed`. The
tags themselves never need anything: no reflash, no new pairing, no new
setup code.

## Unchanged

The wire protocol and the firmware: devices keep their owners, keys, pairings
and assignments. The Tags page's screen previews are the same PNGs, now shown
at whole-number zoom so the 12 px text stays sharp
([docs/tags/layout.md](tags/layout.md) "Previews").
