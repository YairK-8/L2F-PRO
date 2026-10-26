# Design QA — Picking card

Source: `codex-clipboard-55431967-3f33-4787-8e0c-70d86545f0f0.png` — the compact, full-height mobile picking card selected by the user.

## Implemented

- Full-height, edge-to-edge picking card with no segmented inner card.
- Compact title and a single row for aisle filters plus remaining count.
- Product image fills the available card body.
- Product details and location sit over the image-to-white transition.
- Navigation and found/missing actions stay in a compact bottom row.
- The same responsive structure is used for morning and sales picking.

## Verification

- Python syntax: passed.
- Patch whitespace validation: passed after cleanup.
- Browser visual comparison: blocked. The in-app browser could not reach the local Flask server (`ERR_CONNECTION_TIMED_OUT`), and Chrome was unavailable in this environment.

final result: blocked
