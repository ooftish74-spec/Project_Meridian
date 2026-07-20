#!/usr/bin/env python3
"""
conftest.py — pytest 공통 설정
"""

import os
import sys

# 프로젝트 루트를 sys.path에 추가
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
