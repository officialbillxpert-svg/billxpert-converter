# app/__init__.py
from __future__ import annotations
from flask import Flask
from .main import create_app

__all__ = ["create_app"]
