# 3D Highway

A 3D note highway visualization for [Slopsmith](https://github.com/byrongamatos/slopsmith) — an alternative to the default 2D highway, with a sense of depth and perspective inspired by stage views in modern rhythm games.

## What you get

- A camera-perspective highway with notes flying down toward a virtual fretboard at the bottom of the screen
- Glowing strings that pulse and brighten on each hit
- Note Detection feedback, including hit/miss outlines and diagnostic
  early/late/sharp/flat labels when the note detection plugin emits enriched
  judgments
- Chord frame-boxes, named-chord labels, and a chord diagram overlay (configurable corner position) so you can read shapes at a glance
- Two complementary barre indicators fire together when a barre chord shape is detected (2+ consecutive strings fretted at the lowest fret, e.g. F `[1,1,2,3,3,1]`, or an outer-edge full-span barre with every intermediate string fretted, e.g. B major `x24442`): a translucent vertical line across the strings on the 3D highway, and a straight bracket drawn inside the first fret space of the chord diagram overlay
- A heat-colored fret number row that lights up around your active playing region
- Selectable color palettes for the strings — pick the look you want
- Audio-reactive ambient background animations (particles, silhouettes, stage lights, geometric — pick one or turn it off)
- Lyrics overlay synced to the song
- Works as the main player view *or* per-panel inside the splitscreen plugin

## Install

3D Highway ships **bundled** with Slopsmith — no separate installation needed. Pick **3D Highway** from the visualization picker in the player.

> **Note:** The bundled version is preferred over any user-installed copy with the same plugin ID. If you have an old `slopsmith-plugin-3dhighway` clone on disk (from before 3D Highway was promoted to core), it will be ignored at startup — a warning in the server log names the path of the discarded copy. You can safely delete the stale clone.
>
> **Fallback:** In the unlikely event that the bundled copy fails to load its routes (e.g., a broken bundled release), Slopsmith will automatically fall back to your user-installed copy and show a yellow "Fallback" badge in the Settings panel. Check the server startup log for the root cause in that case.

## Settings

Most of the visual controls (background style, intensity, audio reactivity, color palette) live on Slopsmith's **Settings** screen under the *3D Highway* section.

## Contributing / development

For maintainers and AI assistants working on the codebase, see [`CLAUDE.md`](CLAUDE.md) — it's a navigation guide that maps every visual element to where it lives in `screen.js`, plus the gotchas worth knowing before tweaking.
