# src/hockey_context.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# --------- Простые справочники (эвристика) ---------
KHL_TOP_TEAMS = {
    "СКА",
    "ЦСКА",
    "АК БАРС",
    "АВАНГАРД",
    "ДИНАМО МСК",
    "ЛОКОМОТИВ",
    "ТОРПЕДО",
}

KHL_MID_TEAMS = {
    "
