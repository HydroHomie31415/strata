#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Sidebar transport fixture; never talks to the desktop or a phone."""
import json
from pathlib import Path
import sys
import threading
import time

state, record = map(Path, sys.argv[1:3])
lock = threading.Lock()


def emit(value):
    with lock:
        print(json.dumps(value), flush=True)


def watch():
    revision = 0
    while True:
        data = json.loads(state.read_text())
        if data["revision"] != revision:
            revision = data["revision"]
            emit({"version": 1, "event": "invalidate", "revision": revision})
        time.sleep(0.02)


threading.Thread(target=watch, daemon=True).start()
for line in sys.stdin:
    request = json.loads(line)
    data = json.loads(state.read_text())
    result = {"version": 1, "id": request["id"], "revision": data["revision"]}
    if request["method"] == "query":
        result["decorations"] = []
    if request["method"] == "menu":
        device = {"id": "mount-phone", "label": "Fixture phone", "context": "mount-token"}
        if data["mounted"]:
            device = {"id": "storages", "label": "Fixture phone", "children": [
                {"id": "open-storage", "label": "Internal storage", "context": "storage-token"},
                {"id": "open-sd", "label": "SD card", "context": "sd-token"},
            ]}
        device["label"] = data.get("name", "Fixture phone")
        result["actions"] = [{"id": "browse", "label": "Browse phone files", "children": [device]}] if data["connected"] else []
    elif request["method"] == "activate":
        with record.open("a") as output:
            output.write(json.dumps(request) + "\n")
        result["outcome"] = {"status": "accepted", "total": 1, "accepted": 1}
        result["message"] = "Opening fixture phone"
        result["feedback"] = "none"
    emit(result)
