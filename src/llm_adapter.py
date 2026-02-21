# src/llm_adapter.py
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Конфиг (строго под SLA
