# Entry timing extension — documentation check

Checked 2026-10-26 against `DESIGN.md`, `.impeccable/surfaces/admin-entry-timing.md`, `src/signal_desk/web/design-tokens.css`, and the finished `index.html` / `entry-timing.css` / `entry-timing.js` extension. This is an incumbent extension verification, not a global design-system refresh. The existing context exception for absent `PRODUCT.md` applies; no new visual identity was introduced.

## Evidence

- The section reuses the incumbent `.card` and native `.btn` components. Panel background, border, radius, button states, and focus treatment come from existing shell rules. The extension adds no color literals, font family, shadows, or radius vocabulary.
- New text uses the existing `--fs-sm`, `--fs-lg`, and `--fs-xl` scale and inherited Pretendard body styling. Numeric tables use tabular numerals. Secondary text and borders use `--dim` and `--line`; gains/losses use existing `--buy` / `--sell` aliases. Chart lines use the existing accent and secondary-text colors, with solid/dashed differentiation.
- The implemented order matches the surface brief: archived-research scope, conclusion and two-method table, cumulative result, then native disclosure controls for events and assumptions. Refresh uses native `disabled` during requests; loading, missing calculation, unavailable result, and request failure have distinct copy in source.
- Inspected `desktop.png`, `mobile.png`, `missing.png`, and `error.png` in this directory. The isolated captures retain the incumbent neutral panel, indigo chart, two-method comparison, wrapped text, visible loss values, research scope, and retry action. No shipping raster asset was added; these are review evidence only.
- The parent reports the fresh finish review disposition as `ship`. This check does not repeat browser, interaction, or full-app verification and does not certify investment performance.

## Existing documentation drift

The sidecar was already stale before this extension. Its `generatedAt` remains `2026-10-18T13:10:00Z`, and its component snippets omit the current global focus-visible treatment. Its breakpoint list is a partial summary of the wider app; it does not describe this component's 600px adjustment. This check does not promote that component breakpoint into a global rule.

`DESIGN.md` remains adequate authority for this extension. No root `DESIGN.md`, `.impeccable/design.json`, product context, or source file was changed by this check. Sidecar refresh remains separate, unrequested work.
