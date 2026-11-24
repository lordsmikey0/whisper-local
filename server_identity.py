"""Identify the project copy served on localhost without exposing its path."""
import hashlib
import os
from pathlib import Path

INSTANCE_ID = hashlib.sha256(os.path.normcase(str(Path(__file__).resolve().parent)).encode('utf-8')).hexdigest()[:24]
