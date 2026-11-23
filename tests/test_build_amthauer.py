"""`scripts/build_amthauer.py` — its GENERIC helpers on synthetic input only.

The script itself holds no licensed content (it reads the gitignored private
folder); these tests feed it invented tables, text blocks and drawings: the key
table reader (cells wrapping across lines, dash fillers), the five-option item
parser, the number-series parser, the box finder (frame + number label removed)
and the picture composer.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# --------------------------------------------------------------------------- #
# The private build script's generic helpers on synthetic input (no content).
# --------------------------------------------------------------------------- #
SYNTH_KEY = """Субтесты:

|№1   |№2   |№3  |№4        |№5   |№6  |№7  |№8  |№9  |
|1. а |3. б |5. в|7. слово  |9. 12|11. |13. г|15. д|17. а|
|     |     |    |длинное   |     |42  |     |     |     |
|2. б |4. в |6. г|8. ре     |10. 7|12. 5|14. а|16. б|18. в|
|     |     |    |--------- |     |    |     |     |     |


tail"""

SYNTH_PART = """СУБТЕСТ №1
1. Синтетический вопрос ……...
      а) раз, б)два, в) 3,5,  г) четыре,
      д) пять.
2. Ещё вопрос
      а) x, б) y, в) z, г) w, д) v
Конец
"""


def test_build_helpers_parse_a_synthetic_key_and_items() -> None:
    from scripts.build_amthauer import key_from_table, parse_choice_items, parse_series

    assert key_from_table(SYNTH_KEY) == {
        1: "а", 2: "б", 3: "б", 4: "в", 5: "в", 6: "г", 7: "слово длинное", 8: "ре",
        9: "12", 10: "7", 11: "42", 12: "5", 13: "г", 14: "а", 15: "д", 16: "б",
        17: "а", 18: "в",
    }  # fmt: skip
    items = parse_choice_items(SYNTH_PART, 1, 2)
    assert items[0] == {
        "n": 1,
        "stem": "Синтетический вопрос …",
        "options": ["раз", "два", "3,5", "четыре", "пять"],
    }
    assert items[1]["options"] == ["x", "y", "z", "w", "v"]
    series = parse_series("СУБТЕСТ\n 7.   1  2   3 ?\nКонец", 7, 7)
    assert series == [{"n": 7, "series": [1, 2, 3]}]


def test_build_helpers_find_boxes_and_compose(tmp_path: Path) -> None:
    from scripts.build_amthauer import components, compose, frame_figure, frames, ink_mask

    img = Image.new("L", (900, 500), 230)
    d = ImageDraw.Draw(img)
    d.rectangle((50, 50, 700, 460), outline=40, width=7)  # a box
    d.text((70, 65), "17", fill=0)  # a tiny number label
    d.ellipse((200, 150, 400, 350), fill=0)  # the piece
    a = np.asarray(img)
    boxes = frames(components(ink_mask(a)))
    assert len(boxes) == 1
    fig = frame_figure(a, boxes[0])
    assert 200 <= fig.width <= 240 and 200 <= fig.height <= 240  # the disc only
    cells = [Image.new("L", (100, 120), 0) for _ in range(5)]
    out = compose(cells, fig, per_row=3, width=600)
    assert out.width == 600 and out.height > fig.height
