# Design QA — Sales replenishment page

- Source visual truth: `C:\Users\yairk\AppData\Local\Temp\codex-clipboard-648c6766-f32f-49a7-8481-dfd0eab78b19.png`
- Source pixels: 271 × 603
- Intended CSS viewport: mobile, up to 640 px wide
- Density normalization: not required for the source review
- State: authenticated branch, `השלמות מכר`, populated last-scan card and pending list
- Implementation URL: `http://100.116.82.84:5000/`
- Implementation screenshot: unavailable; the isolated QA browser reached the login screen and has no branch session

## Full-view comparison evidence

Blocked at authentication. The live server responds successfully and serves version `20261026-sales-page-90`, but a same-state rendered capture could not be made without a branch login.

## Focused region comparison evidence

Blocked for the same reason. Source regions reviewed: page heading and scan field, last-scanned product card, pending-item rows, action row, primary picking button.

## Implemented fidelity changes

- Green scan field and sales-page accent.
- Large last-scanned product card with catalog image, success state, SKU, variant, time, and undo action.
- Compact pending-item list with thumbnail, SKU, color, size, time, and cancel action.
- Three compact secondary actions above a full-width green picking action.
- Responsive single-screen layout with the pending list taking the remaining available height.
- Fixed 132 px pending-list viewport with internal vertical scrolling so the footer remains stationary.
- White image-fade treatment on the last-scanned product card, matching the supplied reference direction.

## Findings

- P2: Browser-rendered fidelity and overflow remain unverified behind authentication.
  - Fix: capture the authenticated sales page at a mobile viewport and compare it directly with the source image.

## Comparison history

- Pass 1: source visual opened and measured; live implementation could not be captured in the same authenticated state.
- Pass 2: user-supplied live screenshot revealed an expanding queue and a hard image edge. The queue was fixed to a 132 px scroll viewport and a white image fade was added; authenticated post-fix capture remains unavailable.

## Final result

final result: blocked
