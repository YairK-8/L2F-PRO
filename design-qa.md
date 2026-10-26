# Design QA — Picking card

Source: `IMG_2099.png` and the selected product-first card direction from the referenced “עיצוב דף ליקוט” conversation.

## Implemented

- Large product image at the top of both picking cards.
- Soft image-to-white transition into the details area.
- SKU, color, and (for sales picking) size on one centered line.
- Warehouse location directly below the product details.
- Navigation and found/missing actions anchored at the bottom of the card.
- Responsive mobile sizing and matching dark-mode treatment.

## Verification

- Python syntax: passed.
- Patch whitespace validation: passed after cleanup.
- Browser visual comparison: blocked. The in-app browser could not reach the local Flask server (`ERR_CONNECTION_TIMED_OUT`), and Chrome was unavailable in this environment.

final result: blocked
