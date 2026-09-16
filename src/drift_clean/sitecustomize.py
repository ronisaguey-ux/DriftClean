"""Python startup hook — DISABLED 2026-09-10 by owner order.

This file used to call drift_clean(autoCleanOnStartup) inside every Python
interpreter that started. Sessions are cleaned only when the owner types
/clean. The previous implementation is kept alongside as sitecustomize.py.disabled-*.
"""
