"""Reasoning-trace summarizer — REMOVED.

The post-turn TL;DR of the reasoning trace was dropped: the full conversation is
stored and the model can read it directly, so the extra summarizer LLM call was
pure overhead. Delete this file from version control (``git rm app/agent/summary.py``).
"""
